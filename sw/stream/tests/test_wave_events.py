"""DS-P2.50: wave_events_v1, the agent-facing NDJSON lane of the daemon.

Consumers here are what the ticket's acceptance names: a plain TCP client reading
lines, real `nc`, and a real `curl telnet://`. Expected values are hard-coded
(ROUTERTL-002). Sockets are TCP on loopback because AF_UNIX paths under this bench's
long TMPDIR exceed the kernel limit.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import msgpack
import pytest

from sw.stream import __main__ as daemon
from sw.stream import channels as ch
from sw.stream import protocol as proto
from sw.stream import protocol_codes, server, zs_msg
from sw.stream.data_source import (
    Annotation, Batch, DdrRingDataSource, RegbankDataSource,
)
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.regbank import FakeRegbank, Regs

REPO_ROOT = Path(__file__).resolve().parents[3]
DEADLINE_S = 5.0
SETTLE_S = 0.6      # past the server's 500 ms subscribe window


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Lines:
    """Line reader with a hard deadline, so a silent server fails, not hangs."""

    def __init__(self, conn: socket.socket):
        self.conn = conn
        self.buf = b""

    def next(self, deadline_s: float = DEADLINE_S) -> dict:
        end = time.monotonic() + deadline_s
        while b"\n" not in self.buf:
            self.conn.settimeout(max(0.01, end - time.monotonic()))
            data = self.conn.recv(65536)
            if not data:
                raise EOFError(f"closed with {self.buf!r} pending")
            self.buf += data
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def until_closed(self, deadline_s: float = DEADLINE_S) -> list:
        """Every line up to close. A lane that goes silent or never came up
        returns what arrived, so the caller's assertion is what reports it."""
        out = []
        if self.conn is None:
            return out
        try:
            while True:
                out.append(self.next(deadline_s))
        except (EOFError, OSError):
            return out


def _open_lane(port: int) -> _Lines:
    """Connect and read past HELO. No lane → a reader with nothing to read."""
    try:
        lines = _Lines(_connect(port))
        lines.next()
    except (OSError, EOFError):
        return _Lines(None)
    return lines


def _connect(port: int) -> socket.socket:
    end = time.monotonic() + DEADLINE_S
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=DEADLINE_S)
        except ConnectionRefusedError:
            if time.monotonic() > end:
                raise
            time.sleep(0.02)


class _ScriptedSource:
    """Serves `batches` once `gate` opens, then idles or exhausts."""

    def __init__(self, batches, gate=None, then_idle=False):
        self.batches = list(batches)
        self.gate = gate
        self.then_idle = then_idle
        self.drained = threading.Event()
        self.drainer_threads = set()

    def sample_clk_hz(self):
        return 100_000_000

    def read_stats(self):
        return None

    def next_batch(self):
        self.drainer_threads.add(threading.get_ident())
        if self.gate is not None and not self.gate.wait(DEADLINE_S):
            return None
        if self.batches:
            return self.batches.pop(0)
        self.drained.set()
        if self.then_idle:
            time.sleep(0.02)
            return Batch()
        return None


def _uart(ts: int, byte: int) -> Annotation:
    return Annotation(100, ts, ts + 80, "uart_byte", f"0x{byte:02X}",
                      fields={"byte": byte})


def _fanout(source, wave_port=None, events_port=None, **kwargs):
    wave_port = wave_port or _free_port()
    events_port = events_port or _free_port()
    thread = threading.Thread(
        target=server.serve_fanout,
        args=(("127.0.0.1", wave_port), source),
        kwargs={"events_endpoint": ("127.0.0.1", events_port),
                "stats_period_s": 0, **kwargs},
        daemon=True)
    thread.start()
    return thread, wave_port, events_port


def _subscribe(port: int, subscription=None) -> _Lines:
    """Connect, read HELO, send the subscription, and return past the wait."""
    lines = _open_lane(port)
    if subscription is not None and lines.conn is not None:
        lines.conn.sendall(json.dumps(subscription).encode() + b"\n")
    time.sleep(SETTLE_S)
    return lines


@contextlib.contextmanager
def _running_stub_daemon(started=None):
    """The real daemon process over TCP: `python -m sw.stream --source stub`.

    The test owns its lifetime: the daemon runs in its own session and the whole
    process group is killed and reaped in `finally`, so neither a failed connect nor
    a failed assertion leaves an orphan spinning at ~20% CPU. `started` collects Popen
    handles for the leak regression below.
    """
    wave_port, events_port = _free_port(), _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "sw.stream", "--source", "stub",
         "--listen", f"127.0.0.1:{wave_port}",
         "--events-listen", f"127.0.0.1:{events_port}"],
        cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    if started is not None:
        started.append(proc)
    try:
        _connect(events_port).close()
        yield events_port
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


@pytest.fixture
def stub_daemon():
    with _running_stub_daemon() as events_port:
        yield events_port


def test_stub_daemon_helper_leaves_no_daemon_behind():
    """DS-P2.50 / DS-P2.203: every daemon the helper starts is dead afterwards, on a
    clean exit and on a failing body alike."""
    started = []
    for i in range(4):
        try:
            with _running_stub_daemon(started) as events_port:
                _connect(events_port).close()
                if i % 2:
                    raise AssertionError("body failed")
        except AssertionError:
            pass
    assert len(started) == 4
    survivors = [p.pid for p in started if p.poll() is None]
    assert survivors == []
    for p in started:
        with pytest.raises(ProcessLookupError):
            os.killpg(p.pid, 0)


def test_daemon_process_serves_helo_then_decoded_uart_frame(stub_daemon):
    """Acceptance surface: a client on the real daemon port gets HELO, then JSON
    event lines. The stub has been running since before the connect, so which
    byte arrives first isn't known, only that its fields agree."""
    lines = _Lines(_connect(stub_daemon))
    helo = lines.next()
    assert helo["v"] == "wave_events_v1"
    assert helo["producer"].startswith("deepskopion-daemon/")
    assert {"id": 100, "name": "uart_byte", "kind": "frame",
            "schema": "uart_byte_v1"} in helo["channels"]
    ev = lines.next()
    assert (ev["ch"], ev["kind"], ev["name"]) == (100, "frame", "uart_byte")
    assert isinstance(ev["ts_ns"], int)
    assert isinstance(ev["body"]["value"], int)
    assert ev["body"]["text"] == f"0x{ev['body']['value']:02X}"


def test_nc_and_a_curl_loop_read_helo_and_events(stub_daemon):
    """The ticket's literal consumers, in sequence and with curl twice: a loop
    needs the lane to survive each consumer leaving."""
    for tool in ("nc", "curl"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not installed")
    nc = subprocess.run(
        f"nc 127.0.0.1 {stub_daemon} </dev/null | head -5",
        shell=True, capture_output=True, text=True, timeout=30)
    nc_lines = [json.loads(l) for l in nc.stdout.splitlines()]
    assert len(nc_lines) == 5
    assert nc_lines[0]["v"] == "wave_events_v1"
    assert all(l["kind"] == "frame" for l in nc_lines[1:])
    for _ in range(2):
        curl = subprocess.run(
            ["curl", "-sN", "--max-time", "2", f"telnet://127.0.0.1:{stub_daemon}"],
            stdin=subprocess.PIPE, capture_output=True, text=True, timeout=30)
        curl_lines = [json.loads(l) for l in curl.stdout.splitlines()[:-1]]
        assert curl_lines[0]["v"] == "wave_events_v1"
        assert curl_lines[1]["name"] == "uart_byte"


def test_ring_uart_0x55_reaches_the_lane_as_a_uart_byte_frame():
    """The hardware criterion, minus the hardware: a real zs_msg UART frame for
    0x55 in the DDR ring, through DdrRingDataSource and its real decoder, out on
    the socket as the ticket's frame. Only the PL is faked."""
    regbank, ddr = FakeRegbank(), FakeDdrMmap(256)
    source = DdrRingDataSource(regbank=regbank, ddr=ddr, write_back_read_ptr=False)
    source.next_batch()             # first tick seeds the read pointer at 0
    hdr = zs_msg.make_header(
        datalogger_id=12, domain=zs_msg.DOMAIN_UART,
        event_id=protocol_codes.UART_TYPES["payload"], severity=zs_msg.SEV_INFO,
        timestamp=0x123456, payload_len=4)
    frame = zs_msg.pack_header(hdr) + struct.pack("<I", 0x55)
    ddr.write_bytes(0, frame)
    regbank.set(Regs.RING_COMMIT_PTR, len(frame))
    port = _free_port()
    rc = []
    thread = threading.Thread(
        target=lambda: rc.append(server.serve_one_events(("127.0.0.1", port), source)),
        daemon=True)
    thread.start()
    lines = _Lines(_connect(port))
    assert lines.next()["v"] == "wave_events_v1"
    ev = lines.next()
    assert (ev["ch"], ev["kind"], ev["name"]) == (100, "frame", "uart_byte")
    assert ev["body"]["value"] == 0x55
    assert ev["body"]["char"] == "U"
    assert ev["body"]["errors"] == []
    lines.conn.close()


def test_control_characters_stay_on_one_line():
    """A CR on the UART must be the two-char JSON escape, not a raw byte that
    splits the line, and a non-text byte must not masquerade as a character."""
    enc = proto.WaveEventsEncoder(100_000_000, ch.CHANNELS)
    events = [enc.annotation(_uart(5, byte)) for byte in (0x0D, 0xF0)]
    lines = [e.line for e in events if e is not None]
    assert [json.loads(l)["body"]["char"] for l in lines] == ["\r", "\\xf0"]
    assert all(l.count(b"\n") == 1 and l.endswith(b"\n") and b"\r" not in l
               for l in lines)


def test_both_lanes_carry_the_same_annotations_from_one_drainer():
    """next_batch() is destructive, so a second reader would split the data
    between the lanes. Every annotation must appear on BOTH."""
    gate = threading.Event()
    source = _ScriptedSource([Batch(annotations=[_uart(t, 0x40)])
                              for t in (1000, 2000, 3000)], gate=gate)
    _, wave_port, events_port = _fanout(source)
    wave = _connect(wave_port)
    unpacker = msgpack.Unpacker(raw=False)
    wave.settimeout(DEADLINE_S)
    unpacker.feed(wave.recv(65536))
    wave.sendall(proto.pack_frame(proto.TAG_HELO_OK, {}))
    events = _subscribe(events_port)
    time.sleep(0.1)
    gate.set()
    event_ts = [l["ts_ns"] for l in events.until_closed() if l["kind"] == "frame"]
    wave_ts = []
    try:
        while True:
            for tag, payload in unpacker:
                if tag == proto.TAG_ANNOTATIONS:
                    wave_ts += [a["ts_start"] for a in payload["annotations"]]
                if tag == proto.TAG_EOF:
                    raise EOFError
            data = wave.recv(65536)
            if not data:
                break
            unpacker.feed(data)
    except EOFError:
        pass
    assert wave_ts == [1000, 2000, 3000]
    assert event_ts == [1000, 2000, 3000]
    assert len(source.drainer_threads) == 1


def test_batch_the_contract_guard_refused_is_on_neither_lane():
    """Channel 999 is undeclared, so the guard keeps it off wave_stream_v1. The
    lanes are two views of one truth: it must not appear here either."""
    gate = threading.Event()
    source = _ScriptedSource(
        [Batch(annotations=[Annotation(999, 500, 600, "bogus", "x")]),
         Batch(annotations=[_uart(1000, 0x41)])], gate=gate)
    _, _, events_port = _fanout(source)
    events = _subscribe(events_port)
    gate.set()
    lines = events.until_closed()
    assert [l["ch"] for l in lines if "ch" in l] == [100]


def test_subscription_filters_server_side_by_name_and_by_id():
    """Both the ticket's name form and the spec's id form. A heartbeat and a QSPI
    frame are also published and must be absent."""
    gate = threading.Event()
    batch = Batch(annotations=[
        _uart(1000, 0x41),
        Annotation(102, 1100, 1200, "spi_mosi", "MOSI", fields={"mosi": 1}),
        Annotation(200, 1200, 1300, "pl_heartbeat", "hb", fields={"counter": 7}),
    ])
    source = _ScriptedSource([batch], gate=gate)
    _, _, events_port = _fanout(source)
    by_name = _subscribe(events_port, {"subscribe": {"channels": ["uart_byte"],
                                                     "kinds": ["frame"]}})
    by_id = _subscribe(events_port, {"subscribe": {"channels": [200]}})
    gate.set()
    assert [l["name"] for l in by_name.until_closed() if "name" in l] == ["uart_byte"]
    assert [l["name"] for l in by_id.until_closed() if "name" in l] == ["pl_heartbeat"]


def test_end_line_closes_a_finite_session():
    gate = threading.Event()
    source = _ScriptedSource([], gate=gate)
    _, _, events_port = _fanout(source)
    events = _subscribe(events_port)
    gate.set()
    assert events.until_closed()[-1]["body"] == {"reason": "source_exhausted"}


def test_fatal_drainer_error_is_reported_on_the_lane():
    gate = threading.Event()

    class _Dies(_ScriptedSource):
        def next_batch(self):
            gate.wait(DEADLINE_S)
            raise RuntimeError("lost the PL")

    _, _, events_port = _fanout(_Dies([]))
    events = _subscribe(events_port)
    gate.set()
    tail = [(l["kind"], l["body"]) for l in events.until_closed()]
    assert tail == [("error", {"message": "lost the PL", "fatal": True})]


# An EDGE_EVENT word for instance 2: rising edge, ts_us=100, ext=5 (50 ns at 100 MHz).
_EDGE_WORD = (2 << 27) | (100 << 8) | 5
# Anchor well past one 65.536 ms window, with the low 16 µs bits at 90.
_EDGE_ANCHOR_NS = (3 * 65536 + 90) * 1000


def test_edges_are_omitted_by_default():
    gate = threading.Event()
    source = _ScriptedSource([Batch(edge_words=[_EDGE_WORD],
                                    edge_anchor_ns=_EDGE_ANCHOR_NS)], gate=gate)
    _, _, events_port = _fanout(source)
    events = _subscribe(events_port)
    gate.set()
    assert [l["kind"] for l in events.until_closed()] == ["end"]


def test_subscribed_edge_is_placed_in_absolute_time():
    """ts_us alone wraps every 65.536 ms. On the anchor's timebase this edge is
    10 µs after it, the same timebase as annotations (DS-P2.78)."""
    gate = threading.Event()
    source = _ScriptedSource([Batch(edge_words=[_EDGE_WORD],
                                    edge_anchor_ns=_EDGE_ANCHOR_NS)], gate=gate)
    _, _, events_port = _fanout(source)
    events = _subscribe(events_port, {"subscribe": {"kinds": ["edge"]}})
    gate.set()
    edges = [l for l in events.until_closed() if l["kind"] == "edge"]
    assert [l["ts_ns"] for l in edges] == [(3 * 65536 + 100) * 1000 + 50]
    assert (edges[0]["ch"], edges[0]["name"]) == (302, "edge[2]")
    assert edges[0]["body"] == {"value": 1, "prev_value": 0}


def test_malformed_subscription_is_refused_in_band():
    """Silently falling back would read as a quiet bus."""
    source = _ScriptedSource([], then_idle=True)
    _, _, events_port = _fanout(source)
    lines = _subscribe(events_port, {"subscribe": {"channels": ["no_such_lane"]}})
    refusal = lines.until_closed(deadline_s=1.0)
    assert [l["kind"] for l in refusal] == ["error"]
    assert "no_such_lane" in refusal[0]["body"]["message"]
    assert refusal[0]["body"]["fatal"] is True


def test_first_line_without_a_newline_is_not_a_subscription():
    """Only a complete line subscribes. Bytes still waiting for their newline when
    the window closes mean defaults, not a malformed-subscription refusal."""
    gate = threading.Event()
    source = _ScriptedSource([Batch(annotations=[_uart(1000, 0x41)])], gate=gate)
    _, _, events_port = _fanout(source)
    lines = _open_lane(events_port)
    if lines.conn is not None:
        lines.conn.sendall(b'{"subscribe"')
    time.sleep(SETTLE_S)
    gate.set()
    received = [l["ts_ns"] for l in lines.until_closed() if "ch" in l]
    assert received == [1000]


def test_half_closed_consumer_keeps_receiving():
    """`nc </dev/null` shuts down its write side at once while still reading."""
    gate = threading.Event()
    source = _ScriptedSource([Batch(annotations=[_uart(1000, 0x41)])], gate=gate)
    _, _, events_port = _fanout(source)
    lines = _open_lane(events_port)
    if lines.conn is not None:
        lines.conn.shutdown(socket.SHUT_WR)
    time.sleep(SETTLE_S)
    gate.set()
    # Pings may precede it: the liveness probe a half-closed peer gets.
    received = [l["ts_ns"] for l in lines.until_closed() if "ch" in l]
    assert received == [1000]


def _direct_session(max_queue=8):
    """One subscriber thread on a socketpair, for what the fan-out hides."""
    hub = server._EventHub(proto.WaveEventsEncoder(100_000_000, ch.CHANNELS))
    ours, theirs = socket.socketpair()
    sub = server._EventSubscriber(theirs, max_queue, "test")
    thread = threading.Thread(target=server._serve_events_subscriber, args=(sub, hub),
                              daemon=True)
    thread.start()
    return hub, ours, thread


def test_consumer_gone_on_an_idle_bus_is_reaped(monkeypatch):
    """No traffic means no send to fail. Only the ping can find it, otherwise a
    curl loop on a quiet bus leaks a thread and an fd per iteration."""
    monkeypatch.setattr(server, "EVENTS_PING_PERIOD_S", 0.05)
    hub, ours, thread = _direct_session()
    _Lines(ours).next()
    ours.close()
    thread.join(timeout=DEADLINE_S)
    assert not thread.is_alive()
    assert hub.count() == 0


def test_slow_consumer_is_told_how_many_events_it_lost():
    """Drop-oldest protects the drain; the in-band count is the only way the
    consumer can know its stream has a hole. Every event is accounted for."""
    hub, ours, _ = _direct_session()
    lines = _Lines(ours)
    lines.next()
    ours.sendall(b"{}\n")
    end = time.monotonic() + DEADLINE_S
    total = 20_000
    while hub.count() == 0 and time.monotonic() < end:
        time.sleep(0.01)
    time.sleep(0.1)
    for i in range(total):
        hub.publish_annotations([_uart(i, 0x41)])
    hub.publish_session(proto.KIND_END, reason="done")
    got = lines.until_closed()
    dropped = sum(l["body"]["dropped"] for l in got if l["kind"] == "warning")
    frames = sum(1 for l in got if l["kind"] == "frame")
    assert dropped > 0
    assert frames + dropped == total


def test_drop_during_a_blocked_warning_send_is_still_reported():
    """The count must be taken before the send, which is where a slow consumer
    blocks. Drops landing inside it belong to the next warning."""
    hub = server._EventHub(proto.WaveEventsEncoder(100_000_000, ch.CHANNELS))
    ours, theirs = socket.socketpair()
    subscriber = []

    class _DropsDuringWarning(socket.socket):
        def sendall(self, data, *args):
            super().sendall(data, *args)
            if b'"warning"' in data and subscriber[0].dropped == 3:
                subscriber[0].dropped += 5

    conn = _DropsDuringWarning(fileno=theirs.detach())
    sub = server._EventSubscriber(conn, 8, "test")
    subscriber.append(sub)
    threading.Thread(target=server._serve_events_subscriber, args=(sub, hub),
                     daemon=True).start()
    lines = _Lines(ours)
    lines.next()
    ours.sendall(b"{}\n")
    sub.dropped = 3
    hub.publish_annotations([_uart(1000, 0x41)])
    assert lines.next()["body"]["dropped"] == 3
    assert lines.next()["ts_ns"] == 1000
    hub.publish_annotations([_uart(2000, 0x42)])
    assert lines.next()["body"]["dropped"] == 5
    ours.close()


def test_offer_counts_only_events_really_lost():
    """The send thread can drain the queue between the Full and the eviction. That
    lost nothing, and it must not be reported to the consumer as a loss."""
    class _DrainedMeanwhile:
        def put_nowait(self, item):
            self.items = getattr(self, "items", 0) + 1
            if self.items == 1:
                raise server.queue.Full

        def get_nowait(self):
            raise server.queue.Empty

    a, b = socket.socketpair()
    sub = server._Subscriber(a, 1, "test")
    sub.q = _DrainedMeanwhile()
    sub.offer(b"frame")
    assert sub.dropped == 0
    a.close()
    b.close()


def test_port_conflict_on_the_events_lane_leaves_wave_stream_up():
    """7476 taken must cost the agent lane, not RouteWave's stream."""
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    taken = blocker.getsockname()[1]
    source = _ScriptedSource([], then_idle=True)
    errors = []
    wave_port = _free_port()

    def run():
        try:
            server.serve_fanout(("127.0.0.1", wave_port), source, stats_period_s=0,
                                events_endpoint=("127.0.0.1", taken))
        except OSError as exc:
            errors.append(exc)

    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.3)
    assert errors == []
    wave = _connect(wave_port)
    wave.settimeout(DEADLINE_S)
    tag, helo = next(msgpack.Unpacker(raw=False, file_like=wave.makefile("rb")))
    assert tag == proto.TAG_HELO
    assert helo["ver"] == "wave_stream_v1"
    blocker.close()


def test_encoder_defect_does_not_stop_wave_stream(monkeypatch):
    """The encoder runs on the drainer thread. If it raises, the wave_stream_v1
    consumer must still get every later frame."""
    def boom(self, ann):
        raise ValueError("encoder defect")

    monkeypatch.setattr(proto.WaveEventsEncoder, "annotation", boom)
    gate = threading.Event()
    source = _ScriptedSource([Batch(annotations=[_uart(1000, 0x41)]),
                              Batch(annotations=[_uart(2000, 0x42)])], gate=gate)
    _, wave_port, events_port = _fanout(source, backfill_frames=16)
    _subscribe(events_port)
    gate.set()
    assert source.drained.wait(DEADLINE_S), "the drainer died on the encoder defect"


@pytest.mark.parametrize("argv,expected", [
    (["--listen", "0.0.0.0:7475"], ("0.0.0.0", 7476)),
    (["--listen", "127.0.0.1:7475"], ("127.0.0.1", 7476)),
    (["--listen", "0.0.0.0:7475", "--events-listen", ""], None),
    (["--listen", "0.0.0.0:7475", "--events-listen", "127.0.0.1:9000"],
     ("127.0.0.1", 9000)),
    (["--listen", "0.0.0.0:7475", "--events-listen", "unix:/tmp/e.sock"],
     Path("/tmp/e.sock")),
    ([], None),
])
def test_events_endpoint_resolution(monkeypatch, argv, expected):
    """The first row is the exact board launch (S99deepskopion-stream passes only
    --listen), so the lane is up with no init-script change. The local Unix-socket
    daemon opens no TCP port, and a loopback --listen isn't widened."""
    seen = {}

    def fake_fanout(endpoint, source, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(daemon, "serve_fanout", fake_fanout)
    assert daemon.main(["--source", "stub", "--duration", "0.01", *argv]) == 0
    assert seen.get("events_endpoint") == expected


def test_serve_once_with_events_listen_serves_that_consumer_on_ndjson():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "sw.stream", "--source", "stub", "--serve-once",
         "--events-listen", f"127.0.0.1:{port}"],
        cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        lines = _open_lane(port)
        names = [lines.next()["name"]] if lines.conn is not None else []
        assert names == ["uart_byte"]
    finally:
        proc.kill()
        proc.wait()


def test_regbank_heartbeat_and_counter_deltas_are_annots():
    """The ticket's annot line. A pkt delta rides the uart_byte lane but is not a
    decoded byte, so an agent selecting kind=frame must not get it."""
    regbank = FakeRegbank()
    source = RegbankDataSource(regbank=regbank, sample_clk_hz=100_000_000,
                               poll_period_s=0.0, snapshot_trigger=False)
    source.next_batch()
    regbank.set(Regs.HEARTBEAT_CNT, 42)
    regbank.set(Regs.PKT_CNT_UART, 3)
    enc = proto.WaveEventsEncoder(100_000_000, ch.CHANNELS)
    lines = [json.loads(enc.annotation(a).line) for a in source.next_batch().annotations]
    hb = next(l for l in lines if l["name"] == "pl_heartbeat")
    assert hb["kind"] == "annot"
    assert hb["body"]["counter"] == 42
    delta = next(l for l in lines if l["body"]["decoded_as"] == "uart_pkt_count")
    assert (delta["ch"], delta["kind"]) == (100, "annot")


def test_helo_manifest_ids_cross_reference_wave_stream():
    """Annotation channels keep their wave_stream_v1 ids; sample lanes, which this
    stream omits, aren't advertised."""
    helo = json.loads(proto.WaveEventsEncoder(100_000_000, ch.CHANNELS).helo())
    by_id = {c["id"]: c for c in helo["channels"]}
    wave = {c["id"]: c["name"] for c in ch.CHANNELS if c["width"] == 0}
    for channel_id, name in wave.items():
        assert by_id[channel_id]["name"] == name
    assert 0 not in by_id
    assert by_id[200]["kind"] == "annot" and by_id[203]["kind"] == "frame"
