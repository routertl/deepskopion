"""DS-P3.86: the daemon puts the two HWTX fields on every annotation it sends.

The HWTX transcript (routertl RTL-P3.707/708) reads a decoded annotation as a
T1 event. It needs two things the ANNOTATIONS frame did not carry:

* ``event_id`` — the handle a T1v verdict points at (``event_refs``). It must
  name ONE event for the whole session. The trap is the keyframe a late joiner
  receives: it re-serialises old annotations under seq 0, so an id derived
  from the frame's seq at send time gives the same event a second id, and
  ``a0_<i>`` can then name a different event than the session's first frame
  already gave it.
* ``text_res`` ``{long, short}`` — the label at two resolutions.

Every assertion here is made on what a consumer reads off a real socket, or on
what the live guard lets out, not on a helper's return value. TCP loopback, not
AF_UNIX: a pytest tmp path can exceed the 108-byte AF_UNIX limit.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import msgpack

from sw.stream import channels as ch
from sw.stream import protocol as proto
from sw.stream.data_source import Annotation, Batch
from sw.stream.server import serve_fanout, serve_one
from sw.stream.wave_stream_guard import LiveConformanceGuard

from .test_server import (_connect_tcp_with_retry, _pick_ephemeral_port,
                          _read_one_frame)

_CONTRACT_DIR = Path(__file__).resolve().parents[3] / "contracts" / "wave_stream_v1"
if str(_CONTRACT_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTRACT_DIR))
import wave_stream_contract as C  # noqa: E402  (path set above)

_HOST = "127.0.0.1"
_UART_BYTE_LANE = 100     # declared in ch.CHANNELS ("uart_byte")
_ZS_FRAME_LANE = 203      # declared in ch.CHANNELS ("zs_frame")


class _Scripted:
    """Yields the given batches (with a short pause), an idle tail, then exhausts."""

    def __init__(self, batches, idle=0, pause_s=0.0):
        self._batches = list(batches)
        self._idle = idle
        self._pause_s = pause_s

    def sample_clk_hz(self):
        return 10_000

    def next_batch(self):
        if self._pause_s:
            time.sleep(self._pause_s)
        if self._batches:
            return self._batches.pop(0)
        if self._idle > 0:
            self._idle -= 1
            return Batch()
        return None


def _uart(ts, byte):
    return Annotation(channel_id=_UART_BYTE_LANE, ts_start_ns=ts, ts_end_ns=ts + 10,
                      kind="uart_byte", text=f"0x{byte:02X}", fields={"byte": byte})


def _consume(conn, *, until_eof=True, deadline_s=1.5):
    """HELO -> HELO_OK, then every frame up to EOF (or silence)."""
    unp = msgpack.Unpacker(raw=False)
    tag, _ = _read_one_frame(unp, conn)
    assert tag == proto.TAG_HELO
    conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"consumer": "ds-p3-86"}))
    frames = []
    for _ in range(500):
        try:
            tag, payload = _read_one_frame(unp, conn, deadline_s=deadline_s)
        except (TimeoutError, RuntimeError):
            break
        frames.append((tag, payload))
        if tag == proto.TAG_EOF and until_eof:
            break
    return frames


def _serve_one_and_read(batches):
    port = _pick_ephemeral_port(_HOST)
    rc: list = []
    th = threading.Thread(target=lambda: rc.append(serve_one(
        (_HOST, port), _Scripted(batches), connect_timeout_s=2.0)), daemon=True)
    th.start()
    conn = _connect_tcp_with_retry(_HOST, port)
    try:
        frames = _consume(conn)
    finally:
        conn.close()
    th.join(3)
    assert rc == [0], rc
    return [a for tag, p in frames if tag == proto.TAG_ANNOTATIONS
            for a in p["annotations"]]


def test_every_annotation_on_the_wire_carries_an_event_id():
    anns = _serve_one_and_read([
        Batch(annotations=[_uart(1000, 0x55), _uart(2000, 0x41)]),
        Batch(annotations=[_uart(3000, 0x42), _uart(4000, 0x43)]),
        Batch(annotations=[_uart(5000, 0x44)]),
    ])
    assert [a["ts_start"] for a in anns] == [1000, 2000, 3000, 4000, 5000]
    assert [a.get("event_id") for a in anns] == \
        ["a0_0", "a0_1", "a1_0", "a1_1", "a2_0"]


def test_every_annotation_on_the_wire_carries_its_label_at_two_resolutions():
    lost = "! 37 edge event(s) lost (internal FIFO overflow) on ch 3"   # 56 chars
    anns = _serve_one_and_read([
        Batch(annotations=[_uart(1000, 0x55)]),
        Batch(annotations=[Annotation(
            channel_id=_ZS_FRAME_LANE, ts_start_ns=2000, ts_end_ns=2000,
            kind="zs_note", text=lost, is_error=True)]),
    ])
    assert [a.get("text_res") for a in anns] == [
        {"long": "uart_byte: 0x55", "short": "0x55"},
        {"long": "zs_note: ! 37 edge event(s) lost (internal FIFO overflow) on ch 3",
         "short": "! 37 edge event(s) lost (intern…"},   # 31 chars + "…" = 32
    ]
    # `text` is untouched: a consumer that does not know text_res sees no change.
    assert [a["text"] for a in anns] == ["0x55", lost]


def test_a_late_joiners_keyframe_resends_an_event_under_its_own_event_id():
    """Five events on one lane go out with no consumer attached. A late joiner
    gets a keyframe (the lane's LAST event, re-serialised under seq 0), then
    the backfill of all five. The keyframe copy must carry the id the event
    went out with ("a4_0"), not one minted from the keyframe's seq 0 — "a0_0",
    which the backfill already gave to the FIRST event."""
    port = _pick_ephemeral_port(_HOST)
    source = _Scripted([Batch(annotations=[_uart(1000 * (i + 1), 0x30 + i)])
                        for i in range(5)], idle=150, pause_s=0.01)
    rc: list = []
    th = threading.Thread(target=lambda: rc.append(serve_fanout(
        (_HOST, port), source, connect_timeout_s=0.2, backfill_frames=64,
        max_connections=1)), daemon=True)
    th.start()
    time.sleep(0.5)      # all five events broadcast into the ring, nobody attached
    conn = _connect_tcp_with_retry(_HOST, port)
    try:
        frames = _consume(conn)
    finally:
        conn.close()
    th.join(4)

    keyframes = [p for tag, p in frames
                 if tag == proto.TAG_ANNOTATIONS and p.get("keyframe")]
    rest = [p for tag, p in frames
            if tag == proto.TAG_ANNOTATIONS and not p.get("keyframe")]
    assert len(keyframes) == 1, frames
    assert [(a["ts_start"], a["event_id"]) for a in keyframes[0]["annotations"]] \
        == [(5000, "a4_0")]
    assert [(a["ts_start"], a["event_id"]) for p in rest for a in p["annotations"]] \
        == [(1000, "a0_0"), (2000, "a1_0"), (3000, "a2_0"), (4000, "a3_0"),
            (5000, "a4_0")]

    # And the contract's own rule over everything the joiner received.
    st, out = C._Session(), []
    for p in keyframes + rest:
        for i, a in enumerate(p["annotations"]):
            C._check_event_id(f"annotations[{i}]", a, st, out)
    assert out == [], out


# ── the live guard: rollback and a bounded id table ─────────────────────────

def _primed_guard():
    guard = LiveConformanceGuard()
    assert guard.check(proto.TAG_HELO, proto.make_helo(
        sample_clk_hz=10_000, channels=ch.CHANNELS,
        channel_total_bits=ch.CHANNEL_TOTAL_BITS)) is None
    assert guard.check(proto.TAG_HELO_OK, {}, direction="c2p") is None
    return guard


def _ann(eid, ts, channel_id=_UART_BYTE_LANE):
    return {"event_id": eid, "ts_start": ts, "ts_end": ts + 10,
            "channel_id": channel_id, "kind": "uart_byte", "text": "0x55",
            "payload": {"byte": 0x55}}


def test_a_refused_batch_does_not_keep_its_event_ids():
    """A refused batch never reached the wire, and its seq is not consumed, so
    the next batch is stamped with the same ids. The guard must forget the
    refused ids, or it refuses that next, good batch as an id reuse, and every
    batch after it, for the same reason."""
    guard = _primed_guard()
    refused = guard.check(proto.TAG_ANNOTATIONS, {
        "seq": 0, "annotations": [_ann("a0_0", 1000), _ann("a0_1", 2000, 9999)]})
    assert refused is not None and "channel_id" in refused, refused
    assert guard.check(proto.TAG_ANNOTATIONS, {
        "seq": 0, "annotations": [_ann("a0_0", 5000)]}) is None


def test_the_guard_remembers_at_most_its_window_of_event_ids():
    """The daemon's guard lives as long as the daemon. Remembering every id
    would grow without bound (UART line rate is ~10^9 events a day)."""
    from sw.stream.wave_stream_guard import EVENT_ID_WINDOW
    guard = _primed_guard()
    per_frame = 64
    frames = EVENT_ID_WINDOW // per_frame + 3
    for seq in range(frames):
        anns = [_ann(f"a{seq}_{i}", (seq * per_frame + i) * 100)
                for i in range(per_frame)]
        assert guard.check(proto.TAG_ANNOTATIONS,
                           {"seq": seq, "annotations": anns}) is None
    assert frames * per_frame > EVENT_ID_WINDOW
    assert len(guard._st.event_ids) == EVENT_ID_WINDOW
    # The newest are the ones kept, the oldest are the ones forgotten.
    kept = list(guard._st.event_ids)
    assert kept[0] == f"a{frames - EVENT_ID_WINDOW // per_frame}_0"
    assert kept[-1] == f"a{frames - 1}_{per_frame - 1}"
