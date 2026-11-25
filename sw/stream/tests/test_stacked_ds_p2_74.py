# rr unknown-llm (AI-generated)
"""DS-P2.74 stacked decoders, driven from real zs_msg frames in the DDR ring
through DdrRingDataSource, and out through the wave_stream_v1 server and CLI."""
from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sw.stream import __main__ as cli
from sw.stream import channels as ch
from sw.stream import protocol as proto
from sw.stream import protocol_codes, zs_msg
from sw.stream.data_source import Annotation, Batch, DdrRingDataSource
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.regbank import Regs
from sw.stream.server import serve_one
from sw.stream.stacked import (
    Nmea0183Decoder, Pdu, StackedChain, StackedDataSource, StackedDecoder, build_chain,
)
from sw.stream.tests.test_ddr_ring_data_source import _build_frame, _clock_seq, _MirrorRegbank

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "contracts" / "wave_stream_v1"))
import wave_stream_contract as C  # noqa: E402

SENTENCE = b"$GPAAM,A,A,0.10,N,WPTNME*32\r\n"   # published NMEA 0183 example
UART_LANE = 100
NMEA_LANE = 400
TICK_NS = 10    # 100 MHz


class Rig:
    def __init__(self, **source_kwargs):
        self.regbank = _MirrorRegbank()
        self.ddr = FakeDdrMmap(1 << 18)
        times = _clock_seq()
        self.inner = DdrRingDataSource(
            regbank=self.regbank, ddr=self.ddr, sample_clk_hz=100_000_000,
            poll_period_s=0.05, write_back_read_ptr=False,
            clock=lambda: next(times), **source_kwargs)
        self.chain = build_chain(["uart_byte:nmea0183"], ch.CHANNELS)
        self.source = StackedDataSource(self.inner, self.chain)
        self.offset = 0
        self.ts = 1000
        self.source.next_batch()     # seed pointers and the loss baseline

    def put(self, frame: bytes) -> None:
        self.ddr.write_bytes(self.offset, frame)
        self.offset += len(frame)
        self.regbank.set(Regs.RING_WRITE_PTR, self.offset // 4)

    def uart(self, data: bytes, ticks_apart: int = 100) -> None:
        for byte in data:
            self.put(_build_frame(
                datalogger_id=12, domain=zs_msg.DOMAIN_UART,
                event_id=protocol_codes.UART_TYPES["payload"],
                payload=struct.pack("<I", byte), timestamp=self.ts))
            self.ts += ticks_apart

    def parity_error(self) -> None:
        code = next(k for k, v in protocol_codes.UART_EVENTS.items() if v == "parity_error")
        self.put(_build_frame(
            datalogger_id=12, domain=zs_msg.DOMAIN_UART,
            event_id=protocol_codes.UART_TYPES["status"],
            payload=struct.pack("<I", code), timestamp=self.ts))
        self.ts += 100

    def drop(self, reg=Regs.DROP_CNT_UART, count=3) -> None:
        self.regbank.set(reg, self.regbank.read_u32(reg) + count)

    def in_band_loss(self, event_id: int = zs_msg.ERR_EVT_INTERNAL_FIFO_OVERFLOW,
                     count: int = 3, domain: int = zs_msg.DOMAIN_UART,
                     datalogger_id: int = 12) -> None:
        self.put(_build_frame(
            datalogger_id=datalogger_id, domain=domain, event_id=event_id,
            severity=zs_msg.SEV_ERROR,
            payload=struct.pack("<I", count), timestamp=self.ts))
        self.ts += 100

    def tick(self) -> list:
        return self.source.next_batch().annotations


def lane(annotations, channel=NMEA_LANE):
    return [a for a in annotations if a.channel_id == channel]


def kinds(annotations):
    return [a.kind for a in lane(annotations)]


def test_pdu_keeps_first_byte_stamp():
    """Bytes span two ticks, with the last tick later than the bytes' PL stamps."""
    rig = Rig()
    rig.uart(SENTENCE[:10])
    first = lane(rig.tick(), UART_LANE)[0]
    rig.uart(SENTENCE[10:])
    tick2 = rig.tick()
    rig.tick()
    pdu = lane(tick2)[0]
    assert pdu.kind == "nmea0183_sentence"
    assert pdu.ts_start_ns == first.ts_start_ns, "PDU re-stamped at reassembly"
    last = lane(tick2, UART_LANE)[-1]
    assert pdu.ts_end_ns == last.ts_end_ns
    # 29 bytes, 100 PL ticks apart: the last byte is 2800 ticks after the first.
    assert last.ts_start_ns - pdu.ts_start_ns == 2800 * TICK_NS


def test_pdu_carries_source_channel_and_layer_depth():
    rig = Rig()
    rig.uart(SENTENCE)
    pdu = lane(rig.tick())[0]
    assert pdu.fields.get("source_channel") == 100, "PDU does not name its source"
    assert pdu.fields.get("layer_depth") == 1, "PDU does not name its layer"
    assert pdu.fields.get("decoder") == "nmea0183"
    assert pdu.fields["sentence"] == "GPAAM"
    assert pdu.fields["checksum_ok"] is True


def test_nmea_checksum_failure_is_not_a_sentence():
    rig = Rig()
    rig.uart(b"$GPAAM,A,A,0.10,N,WPTNME*33\r\n")
    assert kinds(rig.tick()) == ["nmea0183_checksum_error"]


class _SentenceCount(StackedDecoder):
    """A depth-2 decoder: one PDU per sentence from the lane below."""
    name = "count"

    def feed(self, event):
        return [Pdu("count_pdu", "n", event.ts_start_ns, event.ts_end_ns, {})]

    def partial_events(self):
        return 0

    def reset(self):
        pass


def _two_layer_rig():
    rig = Rig()
    rig.chain.register(_SentenceCount(), "nmea0183@uart_byte")
    return rig


def test_second_layer_chains_and_keeps_the_timeline():
    rig = _two_layer_rig()
    rig.uart(SENTENCE)
    annotations = rig.tick()
    upper = lane(annotations, 401)[0]
    assert upper.fields["source_channel"] == NMEA_LANE
    assert upper.fields["layer_depth"] == 2
    assert upper.ts_start_ns == lane(annotations)[0].ts_start_ns


def test_source_error_mid_pdu_gaps_instead_of_joining():
    rig = Rig()
    rig.uart(SENTENCE[:10])
    rig.parity_error()
    rig.uart(SENTENCE[10:])
    annotations = rig.tick()
    assert kinds(annotations) == ["stacked_gap"]
    gap = lane(annotations)[0]
    assert gap.is_error is True
    assert gap.fields["reason"] == "source_error:uart_parity_error"
    assert gap.fields["partial_events"] == 10
    assert (gap.fields["source_channel"], gap.fields["layer_depth"]) == (100, 1)


def test_gap_below_reaches_the_layer_above():
    rig = _two_layer_rig()
    rig.uart(SENTENCE[:10])
    rig.parity_error()
    upper = lane(rig.tick(), 401)
    assert [a.kind for a in upper] == ["stacked_gap"]
    assert upper[0].fields["layer_depth"] == 2


def test_polled_drop_mid_pdu_emits_gap_and_no_sentence():
    rig = Rig()
    rig.uart(SENTENCE[:10])
    rig.tick()
    rig.drop()
    rig.uart(SENTENCE[10:])
    annotations = rig.tick()
    assert kinds(annotations) == ["stacked_gap"]
    assert lane(annotations)[0].fields["reason"] == "arbiter_drop"
    assert lane(annotations)[0].fields["partial_events"] == 10


def test_backlogged_ring_keeps_discarding_until_past_the_drop(monkeypatch):
    """The drop is in the middle of a sentence still unread in the ring. The
    marker arrives first, so resetting at the marker alone would join the two
    halves into one clean-looking sentence."""
    monkeypatch.setattr(DdrRingDataSource, "_MAX_FRAMES_PER_TICK", 8)
    rig = Rig()
    rig.uart(SENTENCE)       # read over several ticks; the drop is somewhere in here
    rig.drop()
    seen = []
    for _ in range(3):       # 29 frames at 8 per tick: still a backlog after 3
        seen += rig.tick()
    assert kinds(seen) == ["stacked_gap"], "backlog bytes were decoded"
    while not any(a.kind == "stacked_resync" for a in lane(seen)):
        seen += rig.tick()
    resync = [a for a in lane(seen) if a.kind == "stacked_resync"][0]
    assert resync.fields["discarded_events"] == len(SENTENCE)
    rig.uart(SENTENCE)
    after = []
    for _ in range(5):
        after += rig.tick()
    assert kinds(after) == ["nmea0183_sentence"]


def test_pdu_completed_between_drop_and_coalesced_loss_frame_is_not_emitted(monkeypatch):
    """DS-P3.178: an in-band loss frame is coalesced in the PL, so it sits in
    the ring AFTER data written behind the drop. When the backlog drains across
    multiple batches, both the tail that would join a pre-drop partial sentence
    and a full sentence completed before the coalesced loss frame must be held
    and discarded rather than emitted."""
    monkeypatch.setattr(DdrRingDataSource, "_MAX_FRAMES_PER_TICK", 8)
    rig = Rig()
    rig.uart(SENTENCE[:10])
    for _ in range(2):
        rig.tick()
    # Drop occurs here; the PL writes post-drop bytes into the ring before the
    # coalesced SEV_ERROR loss frame is flushed.
    rig.uart(SENTENCE[10:])
    rig.uart(SENTENCE)
    rig.in_band_loss()
    seen = []
    for _ in range(8):
        seen += rig.tick()
    assert kinds(seen) == ["stacked_gap", "stacked_resync"], (
        f"PDU completed before coalesced loss frame leaked through: {kinds(seen)}"
    )
    gap = [a for a in lane(seen) if a.kind == "stacked_gap"][0]
    assert gap.fields["reason"] == "internal_fifo_overflow"
    assert gap.fields["partial_events"] == 10
    resync = [a for a in lane(seen) if a.kind == "stacked_resync"][0]
    assert resync.fields["discarded_events"] == len(SENTENCE[10:]) + len(SENTENCE)
    rig.uart(SENTENCE)
    after = []
    for _ in range(5):
        after += rig.tick()
    assert kinds(after) == ["nmea0183_sentence"]


def test_quarantine_outlives_the_marker_batch_when_drained():
    """The marker batch's commit pointer predates the counter read, so the batch
    after it is still suspect even with an empty backlog."""
    rig = Rig()
    rig.drop()
    assert kinds(rig.tick()) == ["stacked_gap"]
    rig.uart(SENTENCE)
    annotations = rig.tick()
    assert kinds(annotations) == ["stacked_resync"]
    assert lane(annotations)[0].fields["discarded_events"] == len(SENTENCE)


def test_ring_full_gaps_a_lane_it_is_not_reported_on():
    rig = Rig()
    rig.uart(SENTENCE[:10])
    rig.tick()
    rig.drop(Regs.DDR_BACKPRESSURE_CNT)
    annotations = rig.tick()
    assert kinds(annotations) == ["stacked_gap"]
    assert lane(annotations)[0].fields["reason"] == "ring_full"


def test_drop_on_another_lane_leaves_the_pdu_alone():
    rig = Rig()
    rig.uart(SENTENCE[:10])
    rig.tick()
    rig.drop(Regs.DROP_CNT_I2C)
    rig.uart(SENTENCE[10:])
    assert kinds(rig.tick()) == ["nmea0183_sentence"]


def test_storm_suppressed_error_inside_a_sentence_is_not_silent():
    """Past the storm threshold the error annotation is suppressed, and only an
    error_run at the batch end tells of it. It arrives after the bytes around the
    suppressed error, so it has to be seen before they are decoded."""
    rig = Rig()
    for _ in range(8):
        rig.parity_error()
    rig.tick()
    rig.uart(SENTENCE[:10])
    rig.parity_error()       # ninth: suppressed
    rig.uart(SENTENCE[10:])
    annotations = rig.tick()
    assert "error_run" in [a.kind for a in lane(annotations, UART_LANE)]
    assert kinds(annotations) == ["stacked_gap"]
    assert lane(annotations)[0].fields["reason"] == "error_run"


def test_ring_resync_inside_a_sentence_is_not_silent():
    rig = Rig()
    rig.uart(SENTENCE[:10])
    rig.put(b"\xde\xad\xbe\xef" * 2)
    rig.uart(SENTENCE[10:])
    annotations = rig.tick()
    assert kinds(annotations) == ["stacked_gap"]
    assert lane(annotations)[0].fields["reason"] == "zs_resync"


class _Scripted:
    def __init__(self, batches):
        self._batches = iter(batches)

    def next_batch(self):
        return next(self._batches, None)


def test_end_of_stream_with_a_pdu_in_flight_is_reported():
    byte = Annotation(UART_LANE, 5, 6, "uart_byte", "$", fields={"byte": ord("$")})
    source = StackedDataSource(_Scripted([Batch(annotations=[byte])]),
                               build_chain(["uart_byte:nmea0183"], ch.CHANNELS))
    source.next_batch()
    tail = source.next_batch()
    assert kinds(tail.annotations) == ["stacked_gap"]
    assert tail.annotations[0].fields["reason"] == "end_of_stream"
    assert tail.annotations[0].fields["partial_events"] == 1
    assert source.next_batch() is None


def _sentence_bytes(t0=5):
    return [Annotation(UART_LANE, t0 + i, t0 + 1 + i, "uart_byte", chr(b),
                       fields={"byte": b}) for i, b in enumerate(SENTENCE)]


def test_undrained_eof_gaps_a_held_sentence_instead_of_emitting_it():
    """DS-P3.178 MC ruling: a capture that ends with the ring undrained leaves
    committed data unread, and a coalesced loss frame may be in it. The held
    sentence must not come out as a PDU; one gap spans the held interval."""
    source = StackedDataSource(
        _Scripted([Batch(annotations=_sentence_bytes(), ring_drained=False)]),
        build_chain(["uart_byte:nmea0183"], ch.CHANNELS))
    first = source.next_batch()
    assert kinds(first.annotations) == []      # held back, not yet emitted
    tail = source.next_batch()
    assert tail is not None, "undrained EOF dropped the held interval silently"
    assert kinds(tail.annotations) == ["stacked_gap"], (
        f"held PDU emitted at undrained EOF: {kinds(tail.annotations)}")
    gap = tail.annotations[0]
    assert gap.is_error is True
    assert gap.fields["reason"] == "ring_not_drained_at_eof"
    assert gap.fields["withheld_pdus"] == 1
    assert gap.fields["withheld_events"] == 29
    assert gap.fields["partial_events"] == 0
    assert (gap.ts_start_ns, gap.ts_end_ns) == (5, 34)
    assert (gap.fields["source_channel"], gap.fields["layer_depth"]) == (100, 1)
    assert source.next_batch() is None


def test_drained_eof_emits_the_same_held_sentence():
    """The same held sentence, but the capture's last batch drained the ring:
    no loss frame can follow, so the sentence is emitted and nothing is gapped."""
    source = StackedDataSource(
        _Scripted([Batch(annotations=_sentence_bytes(), ring_drained=False),
                   Batch(annotations=[], ring_drained=True)]),
        build_chain(["uart_byte:nmea0183"], ch.CHANNELS))
    first = source.next_batch()
    assert kinds(first.annotations) == []      # held back, not yet emitted
    seen = []
    while (batch := source.next_batch()) is not None:
        seen += batch.annotations
    assert kinds(seen) == ["nmea0183_sentence"], (
        f"held sentence not emitted at drained EOF: {kinds(seen)}")
    assert (seen[0].ts_start_ns, seen[0].ts_end_ns) == (5, 34)
    assert seen[0].text == "$GPAAM,A,A,0.10,N,WPTNME*32"


def test_undrained_eof_gap_reaches_the_layer_above():
    """The layer above never sees the withheld sentence; it sees the gap."""
    chain = build_chain(["uart_byte:nmea0183"], ch.CHANNELS)
    chain.register(_SentenceCount(), "nmea0183@uart_byte")
    source = StackedDataSource(
        _Scripted([Batch(annotations=_sentence_bytes(), ring_drained=False)]), chain)
    source.next_batch()
    tail = source.next_batch().annotations
    assert kinds(tail) == ["stacked_gap"]
    upper = lane(tail, 401)
    assert [a.kind for a in upper] == ["stacked_gap"], "layer above saw the PDU"
    assert upper[0].fields["reason"] == "source_error:stacked_gap"
    assert upper[0].fields["layer_depth"] == 2


def test_registration_refuses_a_source_it_cannot_consume():
    chain = StackedChain(ch.CHANNELS)
    with pytest.raises(ValueError, match="no annotation lane 'no_such_lane'"):
        chain.register(Nmea0183Decoder(), "no_such_lane")
    with pytest.raises(ValueError, match="no annotation lane 'uart_rx'"):
        chain.register(Nmea0183Decoder(), "uart_rx")     # a SAMPLES bit
    chain.register(Nmea0183Decoder(), "uart_byte")
    with pytest.raises(ValueError, match="already on"):
        chain.register(Nmea0183Decoder(), UART_LANE)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve_and_read(port, run_server):
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    conn = socket.socket()
    for _ in range(200):
        try:
            conn.connect(("127.0.0.1", port))
            break
        except OSError:
            conn.close()
            conn = socket.socket()
            time.sleep(0.02)
    conn.settimeout(5.0)
    conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"consumer": "test"}))
    data = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    conn.close()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    return data


def test_wire_session_declares_the_lane_and_conforms():
    """The live guard refuses a whole ANNOTATIONS frame on an undeclared lane, so
    the sentence reaches the wire only if HELO declared it."""
    rig = Rig(duration_s=0.3)
    rig.uart(SENTENCE)
    port = _free_port()
    data = _serve_and_read(
        port, lambda: serve_one(("127.0.0.1", port), rig.source, connect_timeout_s=5.0))
    assert C.check_stream(data, "p2c").accepted
    frames, _ = C.decode_stream(data)
    helo = next(p for t, p in frames if t == proto.TAG_HELO)
    declared = {c["id"]: c for c in helo["channels"]}
    assert declared[NMEA_LANE]["name"] == "nmea0183@uart_byte"
    assert declared[NMEA_LANE]["source_channel"] == 100
    assert declared[NMEA_LANE]["layer_depth"] == 1
    sent = [a for t, p in frames if t == proto.TAG_ANNOTATIONS for a in p["annotations"]
            if a["channel_id"] == NMEA_LANE]
    assert [a["kind"] for a in sent] == ["nmea0183_sentence"]
    assert sent[0]["payload"]["sentence"] == "GPAAM"


def test_wave_events_exposes_the_layer():
    chain = build_chain(["uart_byte:nmea0183"], ch.CHANNELS)
    encoder = proto.WaveEventsEncoder(100_000_000, ch.CHANNELS + chain.channels())
    import json
    entry = next(c for c in json.loads(encoder.helo())["channels"] if c["id"] == NMEA_LANE)
    assert entry["kind"] == "frame"
    assert (entry["source_channel"], entry["layer_depth"]) == (100, 1)
    event = encoder.annotation(Annotation(NMEA_LANE, 1, 2, "nmea0183_sentence", "x",
                                          fields={"source_channel": 100, "layer_depth": 1}))
    assert event.kind == "frame"
    assert event.name == "nmea0183@uart_byte"


def test_cli_stack_flag_declares_the_lane_on_the_wire():
    port = _free_port()
    data = _serve_and_read(port, lambda: cli.main([
        "--source", "stub", "--duration", "0.1", "--serve-once", "--listen", f"127.0.0.1:{port}",
        "--connect-timeout", "5", "--stack", "uart_byte:nmea0183"]))
    helo = next(p for t, p in C.decode_stream(data)[0] if t == proto.TAG_HELO)
    assert NMEA_LANE in {c["id"] for c in helo["channels"]}


def test_cli_refuses_an_unknown_decoder(capsys):
    assert cli.main(["--source", "stub", "--stack", "uart_byte:nope"]) == 2
    assert "decoder one of nmea0183" in capsys.readouterr().err


def test_no_stack_flag_leaves_the_channel_table_alone():
    port = _free_port()
    data = _serve_and_read(port, lambda: cli.main([
        "--source", "stub", "--duration", "0.1", "--serve-once", "--listen", f"127.0.0.1:{port}",
        "--connect-timeout", "5"]))
    helo = next(p for t, p in C.decode_stream(data)[0] if t == proto.TAG_HELO)
    assert [c["id"] for c in helo["channels"]] == [c["id"] for c in ch.CHANNELS]


def _routewave_bin() -> Path:
    env = os.environ.get("ROUTEWAVE_BIN")
    candidates = [Path(env)] if env else []
    which = shutil.which("routewave")
    if which:
        candidates.append(Path(which))
    try:
        from sdk.cli.viewer_manager import get_viewer_path

        vp = get_viewer_path()
        if vp:
            candidates.append(Path(vp))
    except Exception:
        pass
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    pytest.skip("RouteWave binary not found")


def _wcp(port: int, cmd: str, arg: str = "") -> str:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall((json.dumps({"cmd": cmd, "arg": arg}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            assert chunk, "RouteWave WCP closed before a reply"
            buf += chunk
    payload = json.loads(buf.decode())
    assert payload.get("status") == "ok", payload
    return str(payload.get("output", ""))


def test_routewave_renders_stacked_lane_and_helo_descriptor_keys(tmp_path):
    """DS-P3.178: attach RouteWave to a live stacked stream, verify that HELO's
    extra descriptor keys (source_channel, layer_depth) are accepted with zero
    parse errors, and that RouteWave renders nmea0183@uart_byte as its own
    signal row and datalogger packet row."""
    rw = _routewave_bin()
    assert shutil.which("xvfb-run"), "xvfb-run missing; RouteWave GUI cannot render"

    # 1. Verify wire HELO descriptor keys + routewave_cli stream-conformance.
    wire_port = _free_port()
    wire_rig = Rig(duration_s=0.3)
    wire_rig.uart(SENTENCE)
    wire_data = _serve_and_read(
        wire_port,
        lambda: serve_one(("127.0.0.1", wire_port), wire_rig.source, connect_timeout_s=5.0),
    )
    frames, _ = C.decode_stream(wire_data)
    helo = next(p for t, p in frames if t == proto.TAG_HELO)
    nmea_desc = next(c for c in helo["channels"] if c["id"] == NMEA_LANE)
    assert nmea_desc["name"] == "nmea0183@uart_byte", (
        f"RouteWave did not render stacked lane nmea0183@uart_byte: {nmea_desc}"
    )
    assert (nmea_desc["source_channel"], nmea_desc["layer_depth"]) == (UART_LANE, 1), (
        f"RouteWave HELO missing stacked descriptor keys: {nmea_desc}"
    )

    cli_bin = rw.parent / "routewave_cli"
    if cli_bin.is_file() and os.access(cli_bin, os.X_OK):
        mp_path = tmp_path / "stacked.p2c.mp"
        mp_path.write_bytes(wire_data)
        proc_cli = subprocess.run(
            [str(cli_bin), "stream-conformance", str(mp_path),
             "--name", "DS-P3.178", "--expect", "accept"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert proc_cli.returncode == 0, proc_cli.stdout + proc_cli.stderr
        verdict = json.loads(proc_cli.stdout)
        assert verdict["verdict"] == "accept"
        assert verdict["observed"]["parse_errors"] == 0
        cli_channels = {c["id"]: c["name"] for c in verdict["channels"]}
        assert cli_channels.get(NMEA_LANE) == "nmea0183@uart_byte", (
            f"RouteWave did not render stacked lane nmea0183@uart_byte: {cli_channels}"
        )
        assert verdict["annotation_kinds"].get("nmea0183_sentence") == 1

    # 2. Verify live RouteWave GUI attach, lane row rendering, and packet export.
    live_rig = Rig(duration_s=0.3)
    live_rig.uart(SENTENCE)
    stream_port = _free_port()
    wcp_port = _free_port()
    srv = threading.Thread(
        target=lambda: serve_one(("127.0.0.1", stream_port), live_rig.source,
                                 connect_timeout_s=10.0),
        daemon=True,
    )
    srv.start()
    env = dict(os.environ, LIBGL_ALWAYS_SOFTWARE="1",
               HOME=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    proc = subprocess.Popen(
        ["xvfb-run", "-a", str(rw), "--port", str(wcp_port),
         "-c", f"stream attach 127.0.0.1:{stream_port}"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        srv.join(timeout=10.0)
        assert not srv.is_alive(), "serve_one did not finish after RouteWave attached"
        deadline = time.time() + 10.0
        status = {}
        while time.time() < deadline:
            try:
                status = json.loads(_wcp(wcp_port, "stream", "status --json") or "{}")
                if status.get("handshaked") and status.get("annotations", 0) >= 30:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        assert status.get("handshaked") is True, status
        assert status.get("parse_errors") == 0, status
        assert status.get("dropped_annotations") == 0, status
        assert status.get("annotations") == 30, status

        _wcp(wcp_port, "show", "uart_byte")
        _wcp(wcp_port, "show", "nmea0183@uart_byte")
        signals = _wcp(wcp_port, "signals")
        assert "uart_byte" in signals and "nmea0183@uart_byte" in signals, (
            f"RouteWave did not render stacked lane nmea0183@uart_byte: {signals}"
        )

        export_csv = tmp_path / "datalogger.csv"
        _wcp(wcp_port, "datalogger", f"export {export_csv}")
        rows = export_csv.read_text(encoding="utf-8").splitlines()
        stacked_rows = [r for r in rows if ",nmea0183@uart_byte," in r]
        assert (
            len(stacked_rows) == 1
            and 'NMEA0183_SENTENCE,27,"$GPAAM,A,A,0.10,N,WPTNME*32"' in stacked_rows[0]
        ), f"RouteWave did not render stacked lane nmea0183@uart_byte: {stacked_rows}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
