# rr unknown-llm (AI-generated)
"""DS-P2.30: server smoke test — spin up serve_one() against a real Unix socket
and verify a fake consumer can complete the HELO handshake and receive a few
SAMPLES + ANNOTATIONS frames before clean EOF.

Catches wiring regressions in the daemon-to-host path without needing an
actual RouteWave instance running.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import msgpack
import pytest

from sw.stream import protocol as proto
from sw.stream.data_source import Batch, StubDataSource
from sw.stream.server import serve_one


def _connect_with_retry(socket_path: Path, attempts: int = 20,
                        backoff_s: float = 0.05) -> socket.socket:
    """Race-tolerant connect — server's accept loop might not be ready yet."""
    last_err = None
    for _ in range(attempts):
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(socket_path))
            return c
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last_err = exc
            time.sleep(backoff_s)
    raise RuntimeError(f"Could not connect to {socket_path} after "
                       f"{attempts} attempts: {last_err}")


def _read_one_frame(unp: msgpack.Unpacker, conn: socket.socket,
                    deadline_s: float = 2.0) -> tuple[int, dict]:
    """Pull one [tag, payload] frame from the unpacker, refilling as needed."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        for obj in unp:
            assert isinstance(obj, list) and len(obj) == 2
            return obj[0], obj[1]
        try:
            conn.settimeout(max(0.01, end - time.monotonic()))
            data = conn.recv(8192)
        except socket.timeout:
            continue
        if not data:
            raise RuntimeError("connection closed before frame received")
        unp.feed(data)
    raise TimeoutError("timed out waiting for next frame")


def test_smoke_helo_samples_eof(sock_dir):
    """End-to-end smoke: HELO → HELO_OK → SAMPLES (and ANNOTATIONS) → EOF.

    Drives a 0.3-second stub source so the test completes quickly.
    """
    sock_path = sock_dir / "ds_test.sock"
    source = StubDataSource(
        sample_clk_hz=10_000,           # slower so 0.3s = 3000 samples = 3 batches
        samples_per_batch=1000,
        emit_annotations=True,
        duration_s=0.3,
    )

    server_rc: list[int] = []

    def run_server():
        server_rc.append(serve_one(sock_path, source, connect_timeout_s=2.0))

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    conn = _connect_with_retry(sock_path)
    try:
        unp = msgpack.Unpacker(raw=False)

        # 1. HELO must arrive first.
        tag, payload = _read_one_frame(unp, conn)
        assert tag == proto.TAG_HELO
        assert payload["ver"] == "wave_stream_v1"
        assert payload["sample_clk_hz"] == 10_000
        assert payload["channel_total_bits"] >= 1
        assert any(c["name"] == "uart_rx" for c in payload["channels"])

        # 2. Send HELO_OK so the server proceeds to streaming.
        conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"consumer": "test"}))

        # 3. Drain frames until we see EOF (or the loop gives up).
        sample_count = 0
        annotation_count = 0
        saw_eof = False
        for _ in range(200):  # generous frame budget
            try:
                tag, payload = _read_one_frame(unp, conn, deadline_s=2.0)
            except TimeoutError:
                break
            if tag == proto.TAG_SAMPLES:
                sample_count += len(payload["samples"])
            elif tag == proto.TAG_ANNOTATIONS:
                annotation_count += len(payload["annotations"])
            elif tag == proto.TAG_EOF:
                saw_eof = True
                break

        assert saw_eof, "stub source should hit duration and emit EOF"
        assert sample_count >= 1000, \
            f"expected at least 1k samples, got {sample_count}"
        assert annotation_count >= 1, \
            f"expected at least one annotation, got {annotation_count}"
    finally:
        try:
            conn.close()
        except OSError:
            pass

    server_thread.join(timeout=2.0)
    assert not server_thread.is_alive(), "server did not shut down cleanly"
    assert server_rc == [0], f"server returned non-zero rc: {server_rc}"


def test_connect_timeout_when_no_consumer(sock_dir):
    """Server returns rc=1 if nobody attaches within the timeout window."""
    sock_path = sock_dir / "ds_timeout.sock"
    source = StubDataSource(sample_clk_hz=1000, duration_s=0.0)

    rc = serve_one(sock_path, source, connect_timeout_s=0.2)
    assert rc == 1
    # Socket file should be cleaned up.
    assert not sock_path.exists()


# ─── TCP transport (DS-P1.11) ──────────────────────────────────────────────

def _pick_ephemeral_port(host: str = "127.0.0.1") -> int:
    """Ask the kernel for a free port via bind-to-0 then release."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _connect_tcp_with_retry(host: str, port: int, attempts: int = 20,
                            backoff_s: float = 0.05) -> socket.socket:
    last_err = None
    for _ in range(attempts):
        try:
            c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            c.connect((host, port))
            return c
        except (ConnectionRefusedError, OSError) as exc:
            last_err = exc
            time.sleep(backoff_s)
    raise RuntimeError(f"Could not connect to {host}:{port} after "
                       f"{attempts} attempts: {last_err}")


def test_smoke_helo_samples_eof_tcp():
    """End-to-end smoke over TCP — same flow as the unix variant, network mode.

    Mirrors test_smoke_helo_samples_eof but binds an AF_INET listener on a
    loopback ephemeral port; verifies the wave_stream_v1 protocol is
    transport-agnostic (DS-P1.11).
    """
    host = "127.0.0.1"
    port = _pick_ephemeral_port(host)
    source = StubDataSource(
        sample_clk_hz=10_000,
        samples_per_batch=1000,
        emit_annotations=True,
        duration_s=0.3,
    )

    server_rc: list[int] = []

    def run_server():
        server_rc.append(serve_one((host, port), source, connect_timeout_s=2.0))

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    conn = _connect_tcp_with_retry(host, port)
    try:
        unp = msgpack.Unpacker(raw=False)

        tag, payload = _read_one_frame(unp, conn)
        assert tag == proto.TAG_HELO
        assert payload["ver"] == "wave_stream_v1"
        assert payload["sample_clk_hz"] == 10_000

        conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"consumer": "test_tcp"}))

        sample_count = 0
        saw_eof = False
        for _ in range(200):
            try:
                tag, payload = _read_one_frame(unp, conn, deadline_s=2.0)
            except TimeoutError:
                break
            if tag == proto.TAG_SAMPLES:
                sample_count += len(payload["samples"])
            elif tag == proto.TAG_EOF:
                saw_eof = True
                break

        assert saw_eof, "stub source should hit duration and emit EOF over TCP"
        assert sample_count >= 1000, \
            f"expected at least 1k samples over TCP, got {sample_count}"
    finally:
        try:
            conn.close()
        except OSError:
            pass

    server_thread.join(timeout=2.0)
    assert not server_thread.is_alive(), "tcp server did not shut down cleanly"
    assert server_rc == [0], f"tcp server returned non-zero rc: {server_rc}"


def test_connect_timeout_when_no_consumer_tcp():
    """TCP listener returns rc=1 if nobody attaches within the timeout."""
    host = "127.0.0.1"
    port = _pick_ephemeral_port(host)
    source = StubDataSource(sample_clk_hz=1000, duration_s=0.0)

    rc = serve_one((host, port), source, connect_timeout_s=0.2)
    assert rc == 1


# ─── --listen HOST:PORT parser ─────────────────────────────────────────────

@pytest.mark.parametrize("spec, expected", [
    ("0.0.0.0:7475", ("0.0.0.0", 7475)),
    ("127.0.0.1:7475", ("127.0.0.1", 7475)),
    (":7475", ("0.0.0.0", 7475)),
    ("example.local:80", ("example.local", 80)),
    ("[::]:7475", ("::", 7475)),
    ("[::1]:7475", ("::1", 7475)),
])
def test_parse_listen_accepts(spec, expected):
    from sw.stream.__main__ import _parse_listen
    assert _parse_listen(spec) == expected


@pytest.mark.parametrize("spec", [
    "7475",          # no colon
    "host:abc",      # non-integer port
    "host:0",        # port out of range
    "host:65536",    # port out of range
    "[::1]7475",     # missing colon after IPv6 bracket
    "[::1:7475",     # missing closing bracket
])
def test_parse_listen_rejects(spec):
    import argparse as _ap
    from sw.stream.__main__ import _parse_listen
    with pytest.raises(_ap.ArgumentTypeError):
        _parse_listen(spec)


# ── DS-P3.61: serve_forever (survive disconnect + idle) ─────────────────────
def _client_handshake_until_eof(conn: socket.socket, deadline_s: float = 3.0):
    """HELO → HELO_OK → drain until EOF. Returns (samples, annotations, saw_eof)."""
    unp = msgpack.Unpacker(raw=False)
    tag, _ = _read_one_frame(unp, conn, deadline_s=deadline_s)
    assert tag == proto.TAG_HELO
    conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"consumer": "test"}))
    samples = annotations = 0
    saw_eof = False
    for _ in range(500):
        try:
            tag, payload = _read_one_frame(unp, conn, deadline_s=deadline_s)
        except (TimeoutError, RuntimeError):
            break
        if tag == proto.TAG_SAMPLES:
            samples += len(payload["samples"])
        elif tag == proto.TAG_ANNOTATIONS:
            annotations += len(payload["annotations"])
        elif tag == proto.TAG_EOF:
            saw_eof = True
            break
    return samples, annotations, saw_eof


def test_serve_forever_survives_idle_then_serves(sock_dir):
    """A connect timeout must NOT exit serve_forever — it keeps listening, so a
    consumer attaching after an idle period is still served (vs serve_one which
    would have exited rc=1 on the timeout)."""
    from sw.stream.server import serve_forever
    sock = sock_dir / "fwd_idle.sock"
    source = StubDataSource(sample_clk_hz=10_000, samples_per_batch=1000,
                            emit_annotations=True, duration_s=0.6)
    rc: list[int] = []
    th = threading.Thread(target=lambda: rc.append(
        serve_forever(sock, source, connect_timeout_s=0.2, max_connections=1)),
        daemon=True)
    th.start()
    time.sleep(0.45)  # ≥2 connect-timeout cycles with NO consumer
    conn = _connect_with_retry(sock)
    try:
        _, _, saw_eof = _client_handshake_until_eof(conn)
    finally:
        conn.close()
    th.join(timeout=3.0)
    assert saw_eof, "consumer attaching after idle timeouts should still be served"
    assert rc == [0]
    assert not th.is_alive()


def test_serve_forever_serves_sequential_consumers(sock_dir):
    """After one consumer disconnects, serve_forever accepts the next (the ring
    drainer/source is the single owner, reused across connections)."""
    from sw.stream.server import serve_forever
    sock = sock_dir / "fwd_seq.sock"
    source = StubDataSource(sample_clk_hz=10_000, samples_per_batch=500,
                            emit_annotations=True, duration_s=0.0)  # stream forever
    rc: list[int] = []
    th = threading.Thread(target=lambda: rc.append(
        serve_forever(sock, source, connect_timeout_s=2.0, max_connections=2)),
        daemon=True)
    th.start()
    for i in range(2):
        conn = _connect_with_retry(sock)
        unp = msgpack.Unpacker(raw=False)
        tag, _ = _read_one_frame(unp, conn)
        assert tag == proto.TAG_HELO, f"consumer {i} must get a fresh HELO"
        conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"consumer": f"c{i}"}))
        # pull at least one streamed frame, then disconnect
        _read_one_frame(unp, conn)
        conn.close()
    th.join(timeout=3.0)
    assert rc == [0], "serve_forever should return after max_connections served"
    assert not th.is_alive()


def test_serve_forever_detects_idle_disconnect(sock_dir):
    """Regression: when the ring is idle (empty batches → no sendall), a consumer
    disconnect must still be detected via recv()==b'' so serve_forever releases
    the connection and serves the next consumer. Without the fix _drain loops
    forever on the dead client and the second consumer is never served."""
    from sw.stream.server import serve_forever
    from sw.stream.data_source import Batch

    class _IdleSource:
        def sample_clk_hz(self):
            return 10_000

        def next_batch(self):
            time.sleep(0.02)   # pace the idle loop
            return Batch()     # empty (no samples/annotations), never None

    sock = sock_dir / "idle_disc.sock"
    rc: list[int] = []
    th = threading.Thread(target=lambda: rc.append(
        serve_forever(sock, _IdleSource(), connect_timeout_s=2.0, max_connections=2)),
        daemon=True)
    th.start()
    for i in range(2):
        conn = _connect_with_retry(sock)
        unp = msgpack.Unpacker(raw=False)
        tag, _ = _read_one_frame(unp, conn)
        assert tag == proto.TAG_HELO
        conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"c": i}))
        time.sleep(0.15)   # daemon now idle-streaming (no data frames)
        conn.close()       # disconnect — only recv()==b'' can detect this
    th.join(timeout=4.0)
    assert rc == [0], "idle disconnect not detected → 2nd consumer never served"
    assert not th.is_alive()


# ── DS-P3.62: single-drainer broadcast fan-out ──────────────────────────────
def test_subscriber_offer_drops_oldest_when_full():
    """The bounded queue is the firewall: past max_queue a slow consumer drops
    its OLDEST frame (live semantics) and counts it — never blocks the drainer."""
    from sw.stream.server import _Subscriber
    sub = _Subscriber(conn=None, max_queue=2, label="t")
    sub.offer(b"a")
    sub.offer(b"b")          # queue full
    sub.offer(b"c")          # overflow → drop oldest "a"
    assert sub.dropped == 1
    drained = []
    while not sub.q.empty():
        drained.append(sub.q.get_nowait())
    assert drained == [b"b", b"c"], "oldest dropped, newest kept"


def test_hub_broadcast_reaches_all_then_respects_unregister():
    """broadcast() fans the SAME bytes to every registered subscriber; an
    unregistered subscriber receives nothing further."""
    from sw.stream.server import _Hub, _Subscriber
    hub = _Hub()
    s1 = _Subscriber(None, 10, "1")
    s2 = _Subscriber(None, 10, "2")
    hub.register(s1)
    hub.register(s2)
    hub.broadcast(b"x")
    assert s1.q.get_nowait() == b"x"
    assert s2.q.get_nowait() == b"x"
    hub.unregister(s1)
    hub.broadcast(b"y")
    assert s2.q.get_nowait() == b"y"
    assert s1.q.empty(), "unregistered subscriber must get nothing"
    assert hub.count() == 1


def test_fanout_two_concurrent_consumers_get_the_same_stream(sock_dir):
    """Two clients connected at once both receive the broadcast (one drainer,
    fanned out) — both handshake, both get samples+annotations, both see EOF."""
    from sw.stream.server import serve_fanout
    sock = sock_dir / "fan.sock"
    source = StubDataSource(sample_clk_hz=10_000, samples_per_batch=500,
                            emit_annotations=True, duration_s=0.6)
    rc: list[int] = []
    th = threading.Thread(target=lambda: rc.append(
        serve_fanout(sock, source, connect_timeout_s=0.3, max_queue=256,
                     max_connections=2)), daemon=True)
    th.start()
    results: dict[str, tuple] = {}

    def client(name: str):
        conn = _connect_with_retry(sock)
        try:
            results[name] = _client_handshake_until_eof(conn, deadline_s=4.0)
        finally:
            conn.close()

    ca = threading.Thread(target=client, args=("A",))
    cb = threading.Thread(target=client, args=("B",))
    ca.start(); cb.start(); ca.join(6); cb.join(6)
    th.join(6)
    for name in ("A", "B"):
        s, a, eof = results[name]
        assert eof, f"consumer {name} should see EOF"
        assert s > 0 and a > 0, f"consumer {name} should receive samples+annotations"


def test_fanout_drainer_runs_with_zero_subscribers(sock_dir):
    """The drainer must consume the source even with NO client connected (keeps
    the ring drained between clients)."""
    from sw.stream.server import serve_fanout
    from sw.stream.data_source import Batch
    sock = sock_dir / "fanzero.sock"
    calls = {"n": 0}

    class _CountingSource:
        def sample_clk_hz(self):
            return 10_000

        def next_batch(self):
            calls["n"] += 1
            time.sleep(0.01)
            return None if calls["n"] >= 10 else Batch()  # exhaust → drainer stops

    rc: list[int] = []
    th = threading.Thread(target=lambda: rc.append(
        serve_fanout(sock, _CountingSource(), connect_timeout_s=0.2,
                     max_queue=8)), daemon=True)
    th.start()
    time.sleep(0.4)   # NObody connects
    th.join(3)
    assert calls["n"] >= 10, "drainer must keep draining with zero subscribers"
    assert not th.is_alive()


# ── DS-P3.63: late-joiner backfill (snapshot ring) ──────────────────────────
def test_hub_backfill_snapshot_is_atomic_with_register():
    """register() returns the recent broadcast as backfill (not queued); frames
    broadcast AFTER register go to the live queue — no overlap at the boundary."""
    from sw.stream.server import _Hub, _Subscriber
    hub = _Hub(backfill_frames=8)
    hub.broadcast(b"f1")
    hub.broadcast(b"f2")
    sub = _Subscriber(None, 10, "late")
    backfill = hub.register(sub)
    assert backfill == [b"f1", b"f2"], "late joiner gets the recent frames as backfill"
    assert sub.q.empty(), "backfill is returned for direct send, not live-queued"
    hub.broadcast(b"f3")
    assert sub.q.get_nowait() == b"f3", "post-register frames go to the live queue"


def test_hub_backfill_ring_is_bounded():
    """The snapshot ring keeps only the last N frames (drop-oldest)."""
    from sw.stream.server import _Hub, _Subscriber
    hub = _Hub(backfill_frames=2)
    for f in (b"a", b"b", b"c"):
        hub.broadcast(f)
    assert hub.register(_Subscriber(None, 10, "late")) == [b"b", b"c"]


def test_hub_backfill_disabled_returns_empty():
    """backfill_frames=0 (default) → no snapshot, late joiners get no history."""
    from sw.stream.server import _Hub, _Subscriber
    hub = _Hub()  # disabled
    hub.broadcast(b"a")
    assert hub.register(_Subscriber(None, 10, "x")) == []


def test_fanout_late_joiner_receives_backfill(sock_dir):
    """A client connecting AFTER frames were broadcast (source now idle, no other
    clients) immediately receives the recent backfill, then the live stream."""
    from sw.stream.server import serve_fanout
    from sw.stream.data_source import Batch, Annotation
    sock = sock_dir / "backfill.sock"

    class _BurstThenIdle:
        """N annotated batches, then idle, then exhaust (so the test terminates)."""
        def __init__(self, n: int, total: int):
            self.i = 0
            self.n = n
            self.total = total

        def sample_clk_hz(self):
            return 10_000

        def next_batch(self):
            time.sleep(0.01)
            self.i += 1
            if self.i > self.total:
                return None  # exhaust → drainer stops → serve_fanout returns
            if self.i <= self.n:
                return Batch(annotations=[Annotation(   # 33 = level_pin[1] (DS-P3.124)
                    channel_id=33, ts_start_ns=self.i * 1000,
                    ts_end_ns=self.i * 1000 + 500, kind="t", text=f"a{self.i}")])
            return Batch()  # idle tail

    source = _BurstThenIdle(n=20, total=120)   # burst ~0.2s, idle ~1s, exhaust
    rc: list[int] = []
    th = threading.Thread(target=lambda: rc.append(
        serve_fanout(sock, source, connect_timeout_s=0.2, max_queue=256,
                     backfill_frames=64, max_connections=1)), daemon=True)
    th.start()
    time.sleep(0.5)   # let the 20-frame burst broadcast into the ring (NO client yet)
    conn = _connect_with_retry(sock)   # the LATE joiner
    try:
        unp = msgpack.Unpacker(raw=False)
        tag, _ = _read_one_frame(unp, conn)
        assert tag == proto.TAG_HELO
        conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"c": "late"}))
        anns = 0
        for _ in range(200):
            try:
                tag, payload = _read_one_frame(unp, conn, deadline_s=1.5)
            except (TimeoutError, RuntimeError):
                break
            if tag == proto.TAG_ANNOTATIONS:
                anns += len(payload["annotations"])
            elif tag == proto.TAG_EOF:
                break
    finally:
        conn.close()
    th.join(4)
    assert anns >= 20, f"late joiner should receive the 20-frame backfill, got {anns}"


# ── DS-P3.64: byte-bounded backfill snapshot ring ───────────────────────────
def test_hub_backfill_byte_bounded_evicts_oldest():
    """The ring caps at ~backfill_bytes (len of serialized frames), evicting the
    oldest — predictable memory regardless of frame count."""
    from sw.stream.server import _Hub, _Subscriber
    hub = _Hub(backfill_bytes=10)
    hub.broadcast(b"aaaa")   # 4B  → [aaaa]
    hub.broadcast(b"bbbb")   # 8B  → [aaaa,bbbb]
    hub.broadcast(b"cccc")   # 12B > 10 → evict aaaa → [bbbb,cccc] = 8B
    assert hub.register(_Subscriber(None, 10, "x")) == [b"bbbb", b"cccc"]
    assert hub.backfill_bytes() == 8


def test_hub_backfill_keeps_oversized_newest_frame():
    """A single frame larger than the byte budget is kept alone (keep-newest),
    not dropped — a viewer still gets the latest frame."""
    from sw.stream.server import _Hub, _Subscriber
    hub = _Hub(backfill_bytes=4)
    hub.broadcast(b"aa")          # 2B
    hub.broadcast(b"bbbbbbbb")    # 8B > 4 → evict aa, keep the oversize newest alone
    assert hub.register(_Subscriber(None, 10, "x")) == [b"bbbbbbbb"]
    assert hub.backfill_bytes() == 8


def test_hub_backfill_dual_bound_whichever_binds():
    """Byte and frame caps apply together; whichever binds first wins."""
    from sw.stream.server import _Hub, _Subscriber
    # generous bytes, tight frame cap → frame cap binds
    h1 = _Hub(backfill_bytes=1 << 20, backfill_frames=2)
    for f in (b"a", b"b", b"c"):
        h1.broadcast(f)
    assert h1.register(_Subscriber(None, 10, "x")) == [b"b", b"c"]
    # tight bytes, generous frame cap → byte cap binds
    h2 = _Hub(backfill_bytes=2, backfill_frames=1000)
    for f in (b"x", b"y", b"z"):   # 1B each, budget 2 → keep last 2
        h2.broadcast(f)
    assert h2.register(_Subscriber(None, 10, "y")) == [b"y", b"z"]


# ── DS-P3.66/67/68: keyframe + sync marker + time-bounded ring ───────────────
def test_hub_keyframe_tracks_last_annotation_per_channel():
    from sw.stream.server import _Hub, _Subscriber
    from sw.stream.data_source import Annotation

    def ann(ch, t):
        return Annotation(channel_id=ch, ts_start_ns=t, ts_end_ns=t + 1,
                          kind="k", text=f"{ch}@{t}")
    hub = _Hub(backfill_frames=8)
    hub.note_state([], [ann(1, 10), ann(2, 11)])
    hub.note_state([], [ann(1, 20)])   # ch1 updated to the newer event
    sub = _Subscriber(None, 10, "x")
    hub.register(sub)
    kf = {a.channel_id: a.text for a in sub.keyframe_anns}
    assert kf == {1: "1@20", 2: "2@11"}, "keyframe holds the LAST annotation per channel"


def test_hub_keyframe_tracks_last_sample():
    from sw.stream.server import _Hub, _Subscriber
    from sw.stream.data_source import Sample
    hub = _Hub(backfill_frames=4)
    hub.note_state([Sample(ts_ns=1, packed_value=0xA),
                    Sample(ts_ns=2, packed_value=0xB)], [])
    sub = _Subscriber(None, 10, "x")
    hub.register(sub)
    assert sub.keyframe_sample is not None
    assert sub.keyframe_sample.packed_value == 0xB, "keyframe holds the last sample"


def test_hub_backfill_time_bounded_evicts_stale():
    from sw.stream.server import _Hub, _Subscriber
    hub = _Hub(backfill_seconds=0.1)
    hub.broadcast(b"old")
    time.sleep(0.15)
    hub.broadcast(b"new")   # "old" aged past 0.1s → evicted
    assert hub.register(_Subscriber(None, 10, "x")) == [b"new"]


def test_fanout_emits_keyframe_then_sync_marker(sock_dir):
    """A joiner receives a keyframe (keyframe=true) THEN a TAG_SYNC catch-up
    marker before the live stream."""
    from sw.stream.server import serve_fanout
    from sw.stream.data_source import Batch, Annotation
    sock = sock_dir / "kf.sock"

    class _AnnSource:
        def __init__(self):
            self.i = 0

        def sample_clk_hz(self):
            return 10_000

        def next_batch(self):
            time.sleep(0.01)
            self.i += 1
            if self.i > 80:
                return None
            if self.i <= 5:
                ch = 32 + self.i % 3   # level_pin[0..2] (DS-P3.124)
                return Batch(annotations=[Annotation(
                    channel_id=ch, ts_start_ns=self.i * 1000,
                    ts_end_ns=self.i * 1000 + 1, kind="t", text=f"a{self.i}")])
            return Batch()

    rc: list[int] = []
    th = threading.Thread(target=lambda: rc.append(
        serve_fanout(sock, _AnnSource(), connect_timeout_s=0.2,
                     backfill_frames=64, max_connections=1)), daemon=True)
    th.start()
    time.sleep(0.4)   # let the 5 annotated batches broadcast (ring + keyframe state)
    conn = _connect_with_retry(sock)
    saw_keyframe = saw_sync = sync_after_keyframe = False
    try:
        unp = msgpack.Unpacker(raw=False)
        tag, _ = _read_one_frame(unp, conn)
        assert tag == proto.TAG_HELO
        conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"c": "x"}))
        for _ in range(200):
            try:
                tag, payload = _read_one_frame(unp, conn, deadline_s=1.5)
            except (TimeoutError, RuntimeError):
                break
            if (tag == proto.TAG_ANNOTATIONS and isinstance(payload, dict)
                    and payload.get("keyframe")):
                saw_keyframe = True
            if tag == proto.TAG_SYNC:
                saw_sync = True
                sync_after_keyframe = saw_keyframe
                break
    finally:
        conn.close()
    th.join(3)
    assert saw_keyframe, "joiner should receive a keyframe (keyframe=true) frame"
    assert saw_sync, "joiner should receive a TAG_SYNC catch-up marker"
    assert sync_after_keyframe, "TAG_SYNC must come after the keyframe"


# ── DS-P3.75: _apply_control emits a CONTROL_ACK (fire-and-forget, non-fatal) ──
from sw.stream.server import _apply_control  # noqa: E402
from sw.stream import protocol as _proto         # noqa: E402


class _FakeConn:
    def __init__(self): self.sent = []
    def sendall(self, b): self.sent.append(b)


class _OkSource:
    def apply_config(self, sid, cfg, op="set_config"): return None


class _RejectSource:
    def apply_config(self, sid, cfg, op="set_config"): return "bad id"


def _last_ack(conn):
    tag, pl = msgpack.unpackb(conn.sent[-1], raw=False)
    assert tag == _proto.TAG_CONTROL_ACK
    return pl


def test_apply_control_acks_success():
    conn = _FakeConn()
    _apply_control(_proto.make_control(8, 0x1), _OkSource(), conn)
    pl = _last_ack(conn)
    assert pl["datalogger_id"] == 8 and pl["applied_cfg_word"] == 1 and pl["ok"] is True
    assert "reason" not in pl


def test_apply_control_acks_reject_with_reason():
    conn = _FakeConn()
    _apply_control(_proto.make_control(99, 0x1), _RejectSource(), conn)
    pl = _last_ack(conn)
    assert pl["datalogger_id"] == 99 and pl["ok"] is False and pl["applied_cfg_word"] == 0
    assert pl["reason"] == "bad id"


def test_apply_control_acks_malformed():
    conn = _FakeConn()
    _apply_control({"garbage": True}, _OkSource(), conn)
    pl = _last_ack(conn)
    assert pl["ok"] is False and pl["datalogger_id"] == 0xFFFFFFFF and "reason" in pl


# ── DS-P3.78: a frame pipelined into the HELO_OK recv must survive _handshake ──

def test_handshake_preserves_pipelined_frame():
    """A CONTROL the consumer sends back-to-back with HELO_OK (same recv) must
    remain buffered in the returned Unpacker, not be dropped by the handshake."""
    import socket as _socket
    from sw.stream.server import _handshake

    class _Src:
        def sample_clk_hz(self):
            return 100_000_000

    a, b = _socket.socketpair()
    try:
        a.sendall(_proto.pack_frame(_proto.TAG_HELO_OK, {}) +
                  _proto.pack_frame(_proto.TAG_CONTROL, _proto.make_control(8, 0x1)))
        unp = _handshake(b, _Src(), timeout_s=2.0)
        assert unp is not None, "handshake failed"
        frames = list(unp)   # what survived past HELO_OK
        assert any(isinstance(o, list) and o[0] == _proto.TAG_CONTROL for o in frames), \
            f"pipelined CONTROL was dropped; survivors={frames}"
    finally:
        a.close(); b.close()


def test_handshake_returns_none_on_timeout():
    """Regression: the handshake still signals failure (None, not the old False)
    when the consumer connects but never sends HELO_OK."""
    import socket as _socket
    from sw.stream.server import _handshake

    class _Src:
        def sample_clk_hz(self):
            return 100_000_000

    a, b = _socket.socketpair()   # `a` stays open (HELO send succeeds) but never replies
    try:
        assert _handshake(b, _Src(), timeout_s=0.3) is None
    finally:
        a.close(); b.close()


# ── DS-P3.88: structured fields serialize under the ANNOTATIONS "payload" key ──


def test_annotations_payload_includes_fields_when_present():
    """A decoded annotation with structured fields emits them under "payload";
    the consumer (RouteWave) reads that key into per-protocol columns. An
    annotation without fields stays byte-identical to the pre-DS-P3.88 shape."""
    from sw.stream.server import _annotations_payload
    from sw.stream.data_source import Annotation

    with_fields = Annotation(channel_id=2, ts_start_ns=10, ts_end_ns=20,
                             kind="i2c_byte", text="0x48 ACK",
                             fields={"data": 0x48, "ack": True})
    plain = Annotation(channel_id=2, ts_start_ns=30, ts_end_ns=40,
                       kind="i2c_start", text="START")

    payload = _annotations_payload(7, [with_fields, plain])
    # Survives the real wire encoding.
    payload = msgpack.unpackb(msgpack.packb(payload, use_bin_type=True), raw=False)

    anns = payload["annotations"]
    assert anns[0]["payload"] == {"data": 0x48, "ack": True}
    assert "payload" not in anns[1]            # None fields → key omitted
