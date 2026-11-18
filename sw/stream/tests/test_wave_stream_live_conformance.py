"""DS-WS1 (DS-P2.89) — producer conformance of a LIVE capture session.

WS-C0 (DS-P1.29) bound the producer's STATIC surface (constants, the channel
table, the frame builders) to ``contracts/wave_stream_v1/`` — see
``test_wave_stream_contract_conformance.py``. It explicitly stopped short of a
LIVE session: nothing stood between a bad batch (a decoder bug, a stale
channel reference, a corrupted regbank read) and the wire. This file is that
other half — does the RUNNING daemon (real threads, a real socket, a real
DataSource) actually put contract-conformant bytes on the wire, and does it
refuse to rather than silently emit a batch that would not?

Three things proved here, matching the ticket's Done-when:

  1. **Byte-equivalence for the same stimuli.** Driving the real server
     (``serve_one``, over a real AF_UNIX socket) with the SAME sample/
     annotation/edge-word content ``contracts/wave_stream_v1/fixtures/
     session_golden.json``'s SAMPLES/ANNOTATIONS/EDGE_EVENTS frames were built
     from produces wire bytes identical to what the producer's own pinned
     builders (``_samples_payload`` / ``_annotations_payload`` /
     ``proto.make_edge_events`` — the exact functions WS-C0 already bound to
     the contract) author for that stimulus. This proves the LIVE pipeline —
     threads, a real socket, ``sendall`` chunking — does not reformat, reorder
     or corrupt anything relative to the deterministic construction WS-C0
     already pinned. It is not a claim that this repo's ``.mp`` fixture bytes
     are reproduced verbatim: ``session_golden.json``'s key ORDER is
     hand-authored for readability and differs from the builders' insertion
     order, and msgpack maps are unordered on the wire by the contract's own
     compatibility policy (``contracts/wave_stream_v1/README.md``), so raw
     fixture-byte identity is not the invariant that exists to hold.
  2. **Invalid internal state is refused, not emitted.** An annotation
     referencing a channel HELO never declared is dropped by
     ``LiveConformanceGuard`` (wired into ``sw.stream.server``) before it
     reaches the wire — logged naming the field, session continues.
  3. **The check is not decorative.** A deliberate one-field drift (an
     ``i2c_byte`` annotation with its contracted ``ack`` role key renamed) is
     DETECTED — the same class of drift WS-C0's README lists as a shipped
     defect (D.14, the ``detail``/``message`` mislabel, the unregistered enum
     members).

Plus the revision round (landing refusal on the first delivery):

  4. **Inbound HELO_OK never blacks out the connection.** A wrongly-typed
     OPTIONAL HELO_OK field must not roll back the guard's handshake state —
     that was the landing BLOCKER: the guard restoring ``_Session`` on an
     INBOUND violation left ``helo_ok`` cleared, so every later SAMPLES/
     ANNOTATIONS/EDGE_EVENTS was refused as pre-handshake and the daemon went
     dark on a leniency the contract explicitly documents.
  5. **A PRODUCER-side HELO violation stays fail-closed, on purpose.** Our
     own malformed channel table rolling back the whole HELO (so every later
     frame is refused as pre-HELO) is the deliberate, DIFFERENT-from-(4)
     behaviour for a defect we introduced ourselves, pinned here so it does
     not regress silently into leniency either (DS-WS1 FINDING 1).
"""
from __future__ import annotations

import copy
import json
import logging
import socket
import sys
import threading
import time
from pathlib import Path

import msgpack

from sw.stream import channels as ch
from sw.stream import protocol as proto
from sw.stream.data_source import Annotation, Batch, Sample
from sw.stream.server import _annotations_payload, _samples_payload, serve_one
from sw.stream.wave_stream_guard import LiveConformanceGuard

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_DIR = _REPO_ROOT / "contracts" / "wave_stream_v1"
_GOLDEN_FIXTURE = _CONTRACT_DIR / "fixtures" / "session_golden.json"
if str(_CONTRACT_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTRACT_DIR))

import wave_stream_contract as C  # noqa: E402  (path set above)



# ---------------------------------------------------------------------------
# Socket plumbing (mirrors test_server.py's helpers)
# ---------------------------------------------------------------------------

def _connect_with_retry(socket_path: Path, attempts: int = 20,
                        backoff_s: float = 0.05) -> socket.socket:
    last_err = None
    for _ in range(attempts):
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(socket_path))
            return c
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last_err = exc
            time.sleep(backoff_s)
    raise RuntimeError(f"could not connect to {socket_path}: {last_err}")


def _read_one_frame(unp: "msgpack.Unpacker", conn: socket.socket,
                    deadline_s: float = 2.0):
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        for obj in unp:
            assert isinstance(obj, list) and len(obj) == 2
            return obj[0], obj[1]
        conn.settimeout(max(0.01, end - time.monotonic()))
        try:
            data = conn.recv(8192)
        except socket.timeout:
            continue
        if not data:
            raise RuntimeError("connection closed before frame received")
        unp.feed(data)
    raise TimeoutError("timed out waiting for the next frame")


# ---------------------------------------------------------------------------
# Stimuli reconstructed from the pinned golden fixture
# ---------------------------------------------------------------------------

# The golden fixture's HELO declares a bespoke demo channel table (including
# `edge_lane`=920) that is NOT the production `sw.stream.channels.CHANNELS`
# table this daemon actually opens a session with. The loss-provenance
# annotation kind/payload IS the stimulus under test; which numeric channel_id
# an edge-domain loss lands on is the fixture's own demo wiring, not part of
# that stimulus — remapped here to the real production edge/system lane
# (`zs_frame`=203, see data_source._ZS_FRAME_CHANNEL) so replay is judged
# against the daemon's REAL declared channel set rather than the fixture's.
_CHANNEL_ID_REMAP = {920: 203}


def _annotation_from_fixture(entry: dict) -> Annotation:
    return Annotation(
        channel_id=_CHANNEL_ID_REMAP.get(entry["channel_id"], entry["channel_id"]),
        ts_start_ns=entry["ts_start"],
        ts_end_ns=entry["ts_end"],
        kind=entry["kind"],
        text=entry["text"],
        color=entry.get("color", "#4a8c2a"),
        is_error=entry.get("is_error", False),
        fields=entry.get("payload"),
    )


def _golden_stimulus_batches() -> list:
    """One ``Batch`` per producible ``session_golden`` p2c frame, in wire
    order. STATS/DIGEST (heartbeat), CONTROL_ACK/SYNC (handshake/subscriber
    join), and the deliberately-synthetic unknown tag 200 are not a
    DataSource's stimulus — they are not reconstructed here."""
    frames = json.loads(_GOLDEN_FIXTURE.read_text())["frames"]
    batches: list = []
    for f in frames:
        if f["dir"] != "p2c":
            continue
        tag, payload = f["tag"], f["payload"]
        if tag == proto.TAG_SAMPLES:
            batches.append(Batch(samples=[
                Sample(ts_ns=s["ts"], packed_value=s["value"])
                for s in payload["samples"]
            ]))
        elif tag == proto.TAG_ANNOTATIONS:
            batches.append(Batch(annotations=[
                _annotation_from_fixture(a) for a in payload["annotations"]
            ]))
        elif tag == proto.TAG_EDGE_EVENTS:
            batches.append(Batch(edge_words=list(payload["words"]),
                                 edge_anchor_ns=payload["anchor_ns"]))
    return batches


def _golden_fixture_samples_and_edge_payloads() -> list:
    """The RAW ``session_golden`` p2c SAMPLES/EDGE_EVENTS payloads, in wire
    order, straight off disk — no server builder involved. DS-WS1 FINDING 3:
    the byte-equivalence test below also compares the daemon's captured
    output against THESE (not only against ``_samples_payload``/
    ``make_edge_events``'s own output), so the assertion is not entirely
    self-referential — it is not just proving the socket path doesn't mangle
    the server's own construction, but that the content-bearing fields (`ts`/
    `value` per sample; `words`/`anchor_ns` for edges) match the PINNED
    fixture directly. `seq` is excluded: the fixture's seq numbering spans
    frame kinds (STATS/DIGEST/ANNOTATIONS) this replay does not reconstruct,
    so it is not part of "the stimulus" — see the seq comment on the caller."""
    frames = json.loads(_GOLDEN_FIXTURE.read_text())["frames"]
    out: list = []
    for f in frames:
        if f["dir"] != "p2c":
            continue
        if f["tag"] == proto.TAG_SAMPLES:
            out.append((proto.TAG_SAMPLES,
                        [(s["ts"], s["value"]) for s in f["payload"]["samples"]]))
        elif f["tag"] == proto.TAG_EDGE_EVENTS:
            out.append((proto.TAG_EDGE_EVENTS,
                        (list(f["payload"]["words"]), f["payload"]["anchor_ns"])))
    return out


class _ReplayDataSource:
    """Replays a fixed, pre-built list of Batches — one per ``next_batch()``
    call, then clean EOF. No wall-clock pacing, so the test is fast and the
    stimulus is exact rather than approximated by a timing window."""

    def __init__(self, batches, sample_clk_hz: int = 100_000_000):
        self._batches = list(batches)
        self._i = 0
        self._sample_clk_hz = sample_clk_hz

    def sample_clk_hz(self) -> int:
        return self._sample_clk_hz

    def apply_config(self, datalogger_id, cfg_word, op="set_config"):
        return None

    def read_stats(self):
        return None

    def next_batch(self):
        if self._i >= len(self._batches):
            return None
        batch = self._batches[self._i]
        self._i += 1
        return batch


_HELO_OK_PAYLOAD = {"consumer": "ds-ws1-test"}


def _run_server_and_capture(sock_path: Path, source, deadline_s: float = 2.0,
                            helo_ok_payload=None):
    """Spin up serve_one(), handshake, drain until EOF. Returns
    (helo_payload, [(tag, payload), ...] excluding EOF, server_rc).

    `helo_ok_payload` overrides the acknowledgement this test sends back —
    used by the DS-WS1 revision blocker test to send a deliberately
    malformed HELO_OK and prove it does not black out the connection."""
    if helo_ok_payload is None:
        helo_ok_payload = _HELO_OK_PAYLOAD
    rc: list = []
    th = threading.Thread(
        target=lambda: rc.append(serve_one(sock_path, source, connect_timeout_s=2.0)),
        daemon=True)
    th.start()
    conn = _connect_with_retry(sock_path)
    try:
        unp = msgpack.Unpacker(raw=False)
        tag, helo = _read_one_frame(unp, conn, deadline_s)
        assert tag == proto.TAG_HELO
        conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, helo_ok_payload))
        captured = []
        for _ in range(200):
            tag, payload = _read_one_frame(unp, conn, deadline_s)
            if tag == proto.TAG_EOF:
                break
            captured.append((tag, payload))
    finally:
        try:
            conn.close()
        except OSError:
            pass
    th.join(timeout=3.0)
    assert not th.is_alive(), "server did not shut down cleanly"
    return helo, captured, rc


def _judge_session(helo: dict, captured: list):
    """Reassemble the BIDIRECTIONAL session (including the c2p HELO_OK the
    test sent) and judge it with the contract's own checker."""
    session = [("p2c", proto.TAG_HELO, helo),
              ("c2p", proto.TAG_HELO_OK, _HELO_OK_PAYLOAD)]
    session += [("p2c", t, p) for t, p in captured]
    return C.check_session(session)


# ---------------------------------------------------------------------------
# 1. Byte-equivalence for the same stimuli
# ---------------------------------------------------------------------------

def test_live_session_is_byte_equivalent_for_the_golden_stimuli(sock_dir):
    batches = _golden_stimulus_batches()
    assert batches, "the golden fixture yielded no replayable batches"
    source = _ReplayDataSource(batches)

    helo, captured, rc = _run_server_and_capture(sock_dir / "ds_ws1.sock", source)
    assert rc == [0]

    # Reconstruct what the daemon SHOULD have produced by driving the SAME
    # stimuli through the SAME builder functions WS-C0 already bound to the
    # contract. seq is the server's own bookkeeping (monotonic, but not part
    # of "the stimulus"); replaying the identical one-frame-per-batch
    # assignment order reproduces it exactly.
    expected = []
    seq = 0
    for batch in batches:
        if batch.samples:
            expected.append((proto.TAG_SAMPLES, _samples_payload(seq, batch.samples)))
            seq += 1
        if batch.annotations:
            expected.append((proto.TAG_ANNOTATIONS,
                             _annotations_payload(seq, batch.annotations)))
            seq += 1
        if batch.edge_words:
            expected.append((proto.TAG_EDGE_EVENTS,
                             proto.make_edge_events(seq, batch.edge_words,
                                                    anchor_ns=batch.edge_anchor_ns)))
            seq += 1

    assert len(captured) == len(expected), (
        f"stimulus should have produced {len(expected)} data frames; the wire "
        f"carried {len(captured)} — a batch was dropped or an extra one appeared")
    for i, ((etag, epayload), (atag, apayload)) in enumerate(zip(expected, captured)):
        assert atag == etag, f"frame {i}: tag {atag} != expected {etag}"
        assert apayload == epayload, f"frame {i} (tag {etag}) payload mismatch"
        # Byte-level: identical stimuli -> identical wire bytes. Structural
        # equality above and a re-packed byte compare are the same claim here
        # (msgpack decode preserves map insertion order into the python dict),
        # so this also catches a future msgpack version that stopped doing so.
        assert (msgpack.packb([atag, apayload], use_bin_type=True) ==
               msgpack.packb([etag, epayload], use_bin_type=True)), (
            f"frame {i} (tag {etag}) is not byte-identical to the pinned "
            f"builder output for the same stimulus")

    # And judged by the contract's OWN checker — not just this test's
    # reconstruction — the live-captured session fully conforms.
    result = _judge_session(helo, captured)
    assert result.accepted, str(result)

    # DS-WS1 FINDING 3: also compare against the PINNED fixture directly, not
    # only against the same builders the server calls — closes the
    # self-referential gap the landing review flagged.
    fixture_content = _golden_fixture_samples_and_edge_payloads()
    captured_content = []
    for tag, payload in captured:
        if tag == proto.TAG_SAMPLES:
            captured_content.append(
                (tag, [(s["ts"], s["value"]) for s in payload["samples"]]))
        elif tag == proto.TAG_EDGE_EVENTS:
            captured_content.append(
                (tag, (list(payload["words"]), payload["anchor_ns"])))
    assert captured_content == fixture_content, (
        "the daemon's captured SAMPLES/EDGE_EVENTS content diverges from the "
        "pinned session_golden fixture payloads")


# ---------------------------------------------------------------------------
# 2. Invalid internal state is refused, not emitted as a plausible frame
# ---------------------------------------------------------------------------

def test_invalid_channel_reference_is_refused_not_emitted(sock_dir, caplog):
    good1 = Batch(annotations=[Annotation(
        channel_id=100, ts_start_ns=1000, ts_end_ns=1080, kind="uart_byte",
        text="0x55 'U'", is_error=False, fields={"byte": 0x55, "ascii": "U"})])
    # DELIBERATELY invalid internal state: channel_id 9999 was never declared
    # in HELO (the class of defect contracts/wave_stream_v1/fixtures/
    # reject_annotation_channel_ref.json pins).
    bad = Batch(annotations=[Annotation(
        channel_id=9999, ts_start_ns=2000, ts_end_ns=2080, kind="uart_byte",
        text="0xFF", is_error=False, fields={"byte": 0xFF})])
    good2 = Batch(annotations=[Annotation(
        channel_id=101, ts_start_ns=3000, ts_end_ns=3000, kind="i2c_start",
        text="START", is_error=False, fields={"event": "start"})])
    source = _ReplayDataSource([good1, bad, good2])

    with caplog.at_level(logging.ERROR, logger="sw.stream.server"):
        helo, captured, rc = _run_server_and_capture(
            sock_dir / "ds_ws1_invalid.sock", source)
    assert rc == [0], "one bad batch must not crash or hang the daemon"

    ann_frames = [p for t, p in captured if t == proto.TAG_ANNOTATIONS]
    assert len(ann_frames) == 2, (
        f"expected exactly the 2 valid ANNOTATIONS frames on the wire, got "
        f"{len(ann_frames)} — the invalid batch must be refused, not sent")
    seen_channel_ids = {a["channel_id"] for f in ann_frames for a in f["annotations"]}
    assert 9999 not in seen_channel_ids, (
        "an annotation referencing an undeclared channel reached the wire — "
        "it was emitted as a plausible frame instead of refused")

    # Flagged, not silently dropped: an error naming the offending field and
    # value is on record.
    assert any("channel_id" in r.getMessage() and "9999" in r.getMessage()
               for r in caplog.records), (
        "the refusal must be LOGGED, naming the field and the bad value — "
        "silent refusal is indistinguishable from a lost batch")

    # The session that DID reach the wire still fully conforms — the bad
    # frame never touched it.
    result = _judge_session(helo, captured)
    assert result.accepted, str(result)


# ---------------------------------------------------------------------------
# 3. The check is falsifiable: a deliberate one-field drift is DETECTED
# ---------------------------------------------------------------------------

def test_deliberate_role_key_drift_is_detected_by_the_live_guard():
    """A guard that always accepts proves nothing. This shows the SAME
    construction passes with the contracted role key and FAILS the instant it
    is renamed — the class of drift WS-C0's README warns a seam pinned in two
    places ships silently (D.14; the `detail`/`message` mislabel; the
    unregistered `radix`/`type` enum members) — caught here on a LIVE frame,
    before it is ever sent."""
    guard = LiveConformanceGuard()
    helo = proto.make_helo(sample_clk_hz=100_000_000, channels=ch.CHANNELS,
                           channel_total_bits=ch.CHANNEL_TOTAL_BITS)
    assert guard.check(proto.TAG_HELO, helo) is None
    assert guard.check(proto.TAG_HELO_OK, {}, direction="c2p") is None

    good = _annotations_payload(0, [Annotation(
        channel_id=101, ts_start_ns=4010, ts_end_ns=4100, kind="i2c_byte",
        text="0xA0 ACK", is_error=False, fields={"data": 0xA0, "ack": True})])
    assert guard.check(proto.TAG_ANNOTATIONS, good) is None, (
        "the CORRECT construction must pass — a guard that always fails "
        "proves nothing either")

    # DRIFT: the contracted role key `ack` (contracts/wave_stream_v1's
    # ANNOTATION_PAYLOAD["i2c_byte"]) renamed to `acked`.
    drifted = _annotations_payload(1, [Annotation(
        channel_id=101, ts_start_ns=4200, ts_end_ns=4290, kind="i2c_byte",
        text="0x55 NACK", is_error=True, fields={"data": 0x55, "acked": False})])
    violation = guard.check(proto.TAG_ANNOTATIONS, drifted)
    assert violation is not None, (
        "a renamed role key must be DETECTED — the whole point of pinning "
        "role keys is that a consumer builds a packet-table column from "
        "them, so a rename silently empties that column instead of raising")
    assert "ack" in violation, f"the violation must NAME the field: {violation!r}"

    # State rollback on refusal: a THIRD, correct frame right after the drift
    # must still be judged cleanly — a refused frame must not poison the
    # session for the frames that follow it.
    recovered = _annotations_payload(1, [Annotation(
        channel_id=101, ts_start_ns=4200, ts_end_ns=4290, kind="i2c_stop",
        text="STOP", is_error=False, fields={"event": "stop"})])
    assert guard.check(proto.TAG_ANNOTATIONS, recovered) is None, (
        "a refused frame corrupted the guard's session state for the frame "
        "after it")


# ---------------------------------------------------------------------------
# 4. Revision blocker: a malformed INBOUND HELO_OK must never black out the
#    connection — rollback semantics depend on WHO produced the frame.
# ---------------------------------------------------------------------------

def test_malformed_helo_ok_optional_field_still_streams_data(sock_dir, caplog):
    """The landing-refused behaviour: ``LiveConformanceGuard`` restoring
    ``_Session`` on ANY violation is correct for an outgoing (p2c) frame —
    wrong for `_handshake`'s inbound (c2p) HELO_OK, because the producer
    cannot un-receive a frame the consumer actually sent. `consumer` is an
    OPTIONAL HELO_OK key; the contract's own comment says a bare `[1, {}]` is
    a valid acknowledgement. Sending it with the wrong TYPE must be logged,
    not treated as if the handshake never happened."""
    batch = Batch(annotations=[Annotation(
        channel_id=100, ts_start_ns=1000, ts_end_ns=1080, kind="uart_byte",
        text="0x55 'U'", is_error=False, fields={"byte": 0x55, "ascii": "U"})])
    source = _ReplayDataSource([batch])

    with caplog.at_level(logging.WARNING, logger="sw.stream.server"):
        helo, captured, rc = _run_server_and_capture(
            sock_dir / "ds_ws1_bad_helo_ok.sock", source,
            # `consumer` is documented as optional/best-effort; a consumer
            # that is JSON-ish (or simply wrong) may send it as a non-str.
            helo_ok_payload={"consumer": 123})
    assert rc == [0], "a malformed but present HELO_OK must not crash/hang"

    ann_frames = [p for t, p in captured if t == proto.TAG_ANNOTATIONS]
    assert len(ann_frames) == 1, (
        "the ANNOTATIONS batch queued right after a malformed-but-present "
        "HELO_OK must still reach the wire — a wrongly-typed OPTIONAL field "
        "must never black out the connection (DS-WS1 revision BLOCKER)")

    # Flagged, not silently accepted OR silently causing a blackout: the
    # actual violating field is on record instead of an opaque cascade of
    # "arrives before HELO_OK" refusals.
    assert any("consumer" in r.getMessage() for r in caplog.records), (
        "the HELO_OK violation must be LOGGED naming the field — the "
        "landing review found the return value discarded entirely")
    assert not any("before HELO_OK" in r.getMessage() for r in caplog.records), (
        "no data frame should ever be refused as pre-handshake because of "
        "an inbound HELO_OK's own (already-received) malformation")


# ---------------------------------------------------------------------------
# 5. FINDING 1: a PRODUCER-side HELO violation stays fail-closed, on purpose
#    — the opposite of (4), because THIS defect is ours to refuse.
# ---------------------------------------------------------------------------

def test_producer_side_helo_violation_cascades_fail_closed():
    """Unlike an inbound HELO_OK, a HELO WE are about to emit is judged
    p2c (outgoing) — a violation (e.g. one unregistered `radix` in our own
    channel table) rolls the guard's session state back to no-HELO, so every
    later SAMPLES/ANNOTATIONS/EDGE_EVENTS on this connection is then refused
    as arriving before HELO. Deliberate fail-closed behaviour for OUR OWN
    defect, pinned so it cannot regress into silent leniency either."""
    guard = LiveConformanceGuard()
    # Don't mutate the shared production channel table (proto.make_helo does
    # not copy `channels`) — corrupt a private deep copy instead.
    channels = copy.deepcopy(ch.CHANNELS)
    channels[0] = dict(channels[0], radix="not_a_registered_radix")
    bad_helo = proto.make_helo(sample_clk_hz=100_000_000, channels=channels,
                               channel_total_bits=ch.CHANNEL_TOTAL_BITS)

    violation = guard.check(proto.TAG_HELO, bad_helo)
    assert violation is not None and "radix" in violation, (
        f"the unregistered radix must be DETECTED and named: {violation!r}")

    # Fail-closed: the guard's session state was rolled back to no-HELO, so
    # a later, otherwise-valid frame is refused as pre-HELO — not silently
    # accepted despite our own broken channel table.
    good = _annotations_payload(0, [Annotation(
        channel_id=101, ts_start_ns=4010, ts_end_ns=4100, kind="i2c_start",
        text="START", is_error=False, fields={"event": "start"})])
    cascade = guard.check(proto.TAG_ANNOTATIONS, good)
    assert cascade is not None and "before HELO" in cascade, (
        f"a producer-side HELO violation must cascade fail-closed onto "
        f"every later frame, not just the HELO itself: {cascade!r}")
