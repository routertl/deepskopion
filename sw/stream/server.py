# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MPL-2.0
"""wave_stream_v1 server — socket egress for RouteWave consumers, plus the
wave_events_v1 NDJSON lane for agents (DS-P2.50).

Mirrors the routertl mock_wave_stream.py producer's loop shape so the wire-
level behaviour is uniform: bind a SOCK_STREAM, listen, accept one consumer,
HELO/HELO_OK handshake, then drain the data source into SAMPLES + ANNOTATIONS
frames until EOF or the consumer disconnects.

This module owns ONLY the wire side. The data source is injected so the
daemon can swap stub data for real PL drains (DS-P2.31) without touching
the protocol code.

Transport: either AF_UNIX (local; `serve_one(Path)`) or AF_INET (network;
`serve_one((host, port))`) — RouteWave host-side ingest (DS-P1.11) uses
the TCP form. Protocol on the wire is identical for both.
"""

from __future__ import annotations

import collections
import logging
import os
import queue
import select
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Optional, Tuple, Union

import msgpack

from . import channels as ch
from . import protocol as proto
from . import stim_emio
from .data_source import ERROR_RUN_KIND, Annotation, DataSource, Sample
from .wave_stream_guard import LiveConformanceGuard

logger = logging.getLogger(__name__)

#: wave_stream_v1 sets no HELO_OK deadline. After sending HELO the server waits this long for the consumer's
#: HELO_OK, then drops the connection. 2.0 s dropped a RouteWave Debug build that answers in 1.1-2.5 s under load.
#: DS_HELO_OK_TIMEOUT_S (seconds, > 0) overrides it; an unusable value is reported and the default is used.
DEFAULT_HELO_OK_TIMEOUT_S = 10.0
HELO_OK_TIMEOUT_ENV = "DS_HELO_OK_TIMEOUT_S"


def helo_ok_timeout_s() -> float:
    """The HELO_OK wait in seconds: DS_HELO_OK_TIMEOUT_S when it is a number > 0, else the default."""
    raw = os.environ.get(HELO_OK_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_HELO_OK_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value > 0:
        return value
    logger.warning("%s=%r is not a number of seconds > 0; using the default %.1f s",
                   HELO_OK_TIMEOUT_ENV, raw, DEFAULT_HELO_OK_TIMEOUT_S)
    return DEFAULT_HELO_OK_TIMEOUT_S


def _peer_label(conn: socket.socket) -> str:
    """host:port of a TCP consumer, or the local-socket form, for log messages."""
    try:
        peer = conn.getpeername()
    except OSError:
        return "an unknown consumer"
    if isinstance(peer, tuple) and len(peer) >= 2:
        return f"{peer[0]}:{peer[1]}"
    return f"local-socket consumer {peer!r}" if peer else "local-socket consumer"

# Either a filesystem path (AF_UNIX) or a (host, port) tuple (AF_INET).
Endpoint = Union[Path, Tuple[str, int]]


def _send_frame(conn: socket.socket, tag: int, payload: dict) -> None:
    conn.sendall(proto.pack_frame(tag, payload))


def _edge_frame_bytes(guard: LiveConformanceGuard, frame, seq: int,
                      clk_mhz: int) -> Tuple[Optional[str], Optional[bytes]]:
    """Judge one EDGE_EVENTS frame of a batch and return (violation, wire bytes).

    A native frame (DS-P1.74) arrives packed by C and goes to the guard's
    native-frame check; a Python frame is built here, as before. The bytes are
    None when the guard refuses the frame."""
    if frame.native:
        data = frame.pack(seq, clk_mhz)
        violation = guard.check_native_edge_frame(data, seq, clk_mhz, frame.anchor_ns,
                                                  frame.n_words)
        return violation, (data if violation is None else None)
    payload = proto.make_edge_events(seq, frame.words(), clk_mhz=clk_mhz,
                                     anchor_ns=frame.anchor_ns)
    violation = guard.check(proto.TAG_EDGE_EVENTS, payload)
    return violation, (proto.pack_frame(proto.TAG_EDGE_EVENTS, payload)
                       if violation is None else None)


def _apply_control(payload: dict, source: DataSource, conn: socket.socket) -> None:
    """DS-P3.72 apply + DS-P3.75 CONTROL_ACK: decode + apply an inbound CONTROL
    frame, then reply with a (non-fatal) CONTROL_ACK reporting the ground-truth
    applied state, so the host panel reflects reality and rejects surface.

    The ACK is a normal producer→consumer frame — NOT a wave_stream ERR (which
    is fatal and would kill the consumer's DATA stream over an out-of-band
    control; see D.15). Every outcome is also logged."""
    if isinstance(payload, dict) and payload.get("op") == proto.OP_STIM:
        _apply_stim_control(payload, source, conn)
        return
    if isinstance(payload, dict) and payload.get("op") == proto.OP_EDGE_ROUTE:
        _apply_edge_route_control(payload, source, conn)
        return
    try:
        op, datalogger_id, cfg_word = proto.decode_control(payload)
    except ValueError as exc:
        logger.warning("CONTROL malformed: %s", exc)
        _send_frame(conn, proto.TAG_CONTROL_ACK,
                    proto.make_control_ack(0xFFFFFFFF, 0, ok=False, reason=str(exc)))
        return
    reject = source.apply_config(datalogger_id, cfg_word, op=op)
    ok = reject is None
    _send_frame(conn, proto.TAG_CONTROL_ACK,
                proto.make_control_ack(datalogger_id, cfg_word if ok else 0, ok=ok, reason=reject))
    if ok:
        logger.info("CONTROL applied (id=%s cfg=0x%04x) → ACK ok", datalogger_id, cfg_word)
    else:
        logger.warning("CONTROL rejected (id=%s cfg=0x%04x): %s → ACK",
                       datalogger_id, cfg_word, reject)


def _apply_edge_route_control(payload: dict, source: DataSource,
                              conn: socket.socket) -> None:
    """DS-P3.123: ``op="edge_route"`` — the daemon routes ``payload["route"]``
    onto the Edge Detector inputs (EMIO_MUX_SEL bits [7:3]) and enables the edge
    data loggers it feeds. Sources without a regbank have no
    ``apply_edge_route`` and reject. The ACK echoes ``op``/``route`` and, on
    success, carries ``emio_mux_sel`` and the ``edge_channels`` the HELO would
    now declare, so a connected consumer can rename its edge lanes."""
    route = str(payload.get("route", ""))
    datalogger_id = payload.get("datalogger_id")
    if not isinstance(datalogger_id, int) or isinstance(datalogger_id, bool) \
            or not 0 <= datalogger_id <= 0xFFFFFFFF:
        datalogger_id = 0xFFFFFFFF
    apply = getattr(source, "apply_edge_route", None)
    applied = None
    if apply is None:
        reject = f"edge routing unsupported by the {type(source).__name__} source (no regbank)"
    else:
        try:
            reject, applied = apply(route)
        except Exception as exc:  # noqa: BLE001 - a routing failure must ACK, not kill the sub
            reject = f"edge route failed: {exc}"
    ack = proto.make_control_ack(datalogger_id, 0, ok=reject is None, reason=reject)
    ack.update({"op": proto.OP_EDGE_ROUTE, "route": route})
    if reject is None and applied is not None:
        from .edge_route import edge_lanes
        ack.update({"emio_mux_sel": applied, "edge_channels": edge_lanes(applied)})
    _send_frame(conn, proto.TAG_CONTROL_ACK, ack)
    if reject is None:
        logger.info("CONTROL edge_route %s → EMIO_MUX_SEL 0x%08x, ACK ok", route, applied)
    else:
        logger.warning("CONTROL edge_route %s rejected: %s → ACK", route, reject)


def _apply_stim_control(payload: dict, source: DataSource, conn: socket.socket) -> None:
    """DS-P2.136: ``op="stim"`` — the daemon arms the on-board stimulus
    generator (STIM_* registers) for ``payload["protocol"]``. Sources without a
    regbank (stub, eye) have no ``apply_stimulus`` and reject. The ACK reuses
    the CONTROL_ACK shape and echoes ``op``/``protocol`` so a consumer can tell
    it apart from a datalogger config ACK. ``datalogger_id`` is echoed from the
    frame (the observing logger, ``make_stim_control``), or the 0xFFFFFFFF
    sentinel when the frame has none."""
    protocol = str(payload.get("protocol", ""))
    # DS-P2.343: an optional edge route, written by the arm before its burst. Absent: the arm as before.
    route = payload.get("route")
    route = None if route is None else str(route)
    datalogger_id = payload.get("datalogger_id")
    if not isinstance(datalogger_id, int) or isinstance(datalogger_id, bool) \
            or not 0 <= datalogger_id <= 0xFFFFFFFF:
        datalogger_id = 0xFFFFFFFF
    apply = getattr(source, "apply_stimulus", None)
    if apply is None:
        reject = f"stimulus unsupported by the {type(source).__name__} source (no regbank)"
    else:
        try:
            reject = apply(protocol) if route is None else apply(protocol, route=route)
        except Exception as exc:  # noqa: BLE001 - an arm failure must ACK, not kill the sub
            reject = f"stimulus arm failed: {exc}"
    ack = proto.make_control_ack(datalogger_id, 0, ok=reject is None, reason=reject)
    ack.update({"op": proto.OP_STIM, "protocol": protocol})
    if route is not None:
        ack["route"] = route
    # DS-P2.253: on a Zynq-7000 image the armed generator drives the PS EMIO
    # legs of uart/i2c/spi; the ACK says so, so a consumer that goes on to
    # drive the PS controller is told before its transfers fail.
    taken = stim_emio.stim_takes_ps_emio(protocol) if reject is None else None
    if protocol in stim_emio.EMIO_LEG_PROTOCOLS and reject is None:
        ack["ps_emio_taken"] = taken is not None
    if taken is not None:
        ack["warning"] = taken
    _send_frame(conn, proto.TAG_CONTROL_ACK, ack)
    if taken is not None:
        logger.warning("CONTROL stim armed (protocol=%s): %s", protocol, taken)
    if reject is None:
        logger.info("CONTROL stim armed (protocol=%s) → ACK ok", protocol)
    else:
        logger.warning("CONTROL stim rejected (protocol=%s): %s → ACK", protocol, reject)


def _samples_payload(seq: int, samples: Iterable[Sample]) -> dict:
    return {
        "seq": int(seq),
        "samples": [{"ts": int(s.ts_ns), "value": int(s.packed_value)}
                    for s in samples],
    }


def _channels(source) -> list:
    """The HELO channel table: the fixed lanes plus any a source adds (DS-P2.74).
    An annotation on an undeclared lane is refused by the live guard."""
    extra = getattr(source, "extra_channels", None)
    return ch.CHANNELS + list(extra()) if extra is not None else ch.CHANNELS


def _helo_channels(source) -> list:
    """The wave_stream_v1 HELO table: `_channels` plus the edge lanes (DS-P3.124).

    wave_stream_v1 carries edges as EDGE_EVENTS words keyed by device_id, and
    RouteWave names each Edge Scope lane after the HELO channel whose id equals
    that device_id. A source that streams edges declares those entries through
    `edge_channels()`. They are kept out of `_channels` because the
    wave_events_v1 manifest declares its own edge lanes (EDGE_CHANNEL_BASE)."""
    edges = getattr(source, "edge_channels", None)
    return _channels(source) + list(edges()) if edges is not None else _channels(source)


#: The inline-label bound the contract holds ``text_res.short`` to.
TEXT_SHORT_MAX = 32


def _text_res(kind: str, text: str) -> dict:
    """DS-P3.86: the annotation label at two resolutions (HWTX T1 ``text``).

    ``short`` is the inline waveform label, clipped to TEXT_SHORT_MAX with a
    trailing ellipsis when the decoder's label is longer. ``long`` is the full
    label qualified by its kind, so it still reads when it is lifted out of the
    timeline: ``"PARITY ERROR"`` on a UART lane is ``"uart_parity_error: PARITY
    ERROR"`` in a transcript line that has no lane around it. ``text`` itself
    is left exactly as the decoder wrote it."""
    short = text if len(text) <= TEXT_SHORT_MAX else text[:TEXT_SHORT_MAX - 1] + "…"
    return {"long": f"{kind}: {text}" if text else kind, "short": short or kind}


def _annotations_payload(seq: int, annotations: Iterable[Annotation]) -> dict:
    def _one(i: int, a: Annotation) -> dict:
        # DS-P3.86: stamp the event_id ONCE, the first time the annotation goes
        # out, and keep it on the object. The keyframe a late joiner receives
        # re-serialises old annotations under seq 0; deriving the id from THAT
        # seq would give the same event a second id, and could hand a0_<i> to a
        # different event than the session's first annotation already holds.
        if a.event_id is None:
            a.event_id = f"a{int(seq)}_{i}"
        d = {
            "event_id": a.event_id,
            "ts_start": int(a.ts_start_ns),
            "ts_end":   int(a.ts_end_ns),
            "channel_id": int(a.channel_id),
            "kind": str(a.kind),
            "text": str(a.text),
            "text_res": _text_res(str(a.kind), str(a.text)),
            "color": str(a.color),
            "is_error": bool(a.is_error),
        }
        # DS-P3.88: structured role-tagged decoded fields under the "payload" key
        # the consumer already reserves (RouteWave Annotation.payload_msgpack →
        # per-protocol columns, LT-P3.168). Omitted when the decoder produced
        # none, so older/plain annotations stay byte-identical on the wire.
        if a.fields:
            d["payload"] = a.fields
        return d

    return {
        "seq": int(seq),
        "annotations": [_one(i, a) for i, a in enumerate(annotations)],
    }


#: sun_path holds 108 bytes on Linux, one of them the terminating NUL.
AF_UNIX_PATH_MAX = 107


def _bind_unix(path: Path) -> socket.socket:
    # DS-P3.199: bind() would refuse with "AF_UNIX path too long"; say which
    # limit and which path, so a deep socket directory is not mistaken for a
    # daemon fault.
    if len(os.fsencode(str(path))) > AF_UNIX_PATH_MAX:
        raise OSError(f"AF_UNIX socket path is {len(os.fsencode(str(path)))} bytes, over the "
                      f"{AF_UNIX_PATH_MAX}-byte sun_path limit: {path}; use a shorter "
                      "directory or a TCP endpoint")
    if path.exists():
        path.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    os.chmod(str(path), 0o600)
    return srv


def _bind_tcp(host: str, port: int) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    return srv


def _unlink_quiet(path: Path) -> None:
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def _make_listener(endpoint: Endpoint):
    """Bind `endpoint` and return (srv, label, cleanup). Shared by serve_one
    and serve_forever so the AF_UNIX/AF_INET split lives in one place."""
    if isinstance(endpoint, Path):
        srv = _bind_unix(endpoint)
        return srv, str(endpoint), lambda: _unlink_quiet(endpoint)
    host, port = endpoint
    srv = _bind_tcp(host, port)
    return srv, f"{host}:{port}", (lambda: None)


def serve_one(endpoint: Endpoint, source: DataSource,
              connect_timeout_s: float = 60.0) -> int:
    """Bind `endpoint`, accept one consumer, drain `source` until EOF.

    `endpoint` is either a Path (AF_UNIX local socket) or a (host, port)
    tuple (AF_INET TCP listener). The wave_stream_v1 protocol on the wire
    is identical for both transports; only the bind/accept differ.

    Returns process exit code: 0 on clean stream end, 1 on connect timeout
    or socket error.
    """
    srv, label, cleanup_listener = _make_listener(endpoint)

    srv.listen(1)
    logger.info("listening on %s; waiting for consumer (timeout %.1fs)",
                label, connect_timeout_s)

    srv.settimeout(connect_timeout_s)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        logger.error("no consumer connected within %.1fs; exiting", connect_timeout_s)
        return 1
    finally:
        srv.close()
        cleanup_listener()

    logger.info("consumer connected")
    try:
        return _drain(conn, source)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve_forever(endpoint: Endpoint, source: DataSource,
                  connect_timeout_s: float = 60.0,
                  max_connections: int = 0) -> int:
    """Bind `endpoint` once, then accept + drain consumers SEQUENTIALLY, forever
    (DS-P3.61). Unlike serve_one this survives:
      * a consumer disconnect — the listener stays bound and loops back to
        accept the next consumer (a fresh HELO is sent per connection);
      * an idle period — a connect timeout just re-arms accept() instead of
        exiting, so the daemon keeps listening after an idle boot.

    A per-connection drain error is logged and swallowed so one bad consumer
    can't kill the daemon. `max_connections` > 0 caps the total served (returns
    after that many, for tests); 0 = truly forever.

    NOTE: this serves ONE consumer at a time (the ring drain is single-owner and
    destructive — it advances RING_READ_PTR). Between consumers the source is
    NOT drained, so under load the ring can fill during the gap (the DS-P3.60
    watermark IRQ / DS-T2.22 backpressure counter make that visible). Concurrent
    consumers via a single-drainer broadcast fan-out are DS-P3.62.
    """
    srv, label, cleanup_listener = _make_listener(endpoint)
    srv.listen(1)
    logger.info("listening on %s (serve-forever); waiting for consumers", label)

    served = 0
    try:
        while True:
            srv.settimeout(connect_timeout_s)
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                logger.info("no consumer within %.1fs — still listening",
                            connect_timeout_s)
                continue
            logger.info("consumer connected")
            try:
                _drain(conn, source)
            except Exception as exc:  # noqa: BLE001 - one bad consumer must not kill the daemon
                logger.error("consumer session error: %s", exc)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
            served += 1
            logger.info("consumer disconnected (served %d total); listening again",
                        served)
            if max_connections and served >= max_connections:
                return 0
    finally:
        srv.close()
        cleanup_listener()


def _handshake(conn: socket.socket, source: DataSource,
               timeout_s: Optional[float] = None,
               guard: Optional[LiveConformanceGuard] = None,
               ) -> Optional["msgpack.Unpacker"]:
    """Send HELO (channel map + sample clk) and await the consumer's HELO_OK.

    Returns the per-connection msgpack Unpacker on success (None on timeout /
    early close). DS-P3.78: the consumer may pipeline frames (e.g. a CONTROL)
    in the SAME recv as HELO_OK; those stay buffered in this Unpacker, so we
    hand it back to the inbound loop rather than discarding it — otherwise the
    pipelined frame is silently lost. Shared by _drain + the fan-out subscriber.

    `guard` (DS-WS1), when given, judges HELO against the vendored contract and
    PRIMES the guard's session state with the channel table this connection
    actually opened with — required so later ANNOTATIONS can be checked against
    the real declared channel_id set. HELO itself is still SENT even on a
    violation (there is no session without one) — but the guard's session
    state IS rolled back (fail-closed, deliberate: see FINDING 1 of the DS-WS1
    revision log), so a violation here (e.g. one unregistered `radix`) leaves
    `channel_ids` empty and `helo=None`, and every later SAMPLES/ANNOTATIONS/
    EDGE_EVENTS on THIS connection is then refused as arriving before HELO. A
    loud, correct response to a defect in OUR OWN channel table — logged below
    naming the violating channel/field, not merely inferred from the resulting
    "arrives before HELO" cascade.

    The inbound HELO_OK check is the opposite: OBSERVE-only (`direction=
    "c2p"`), never rolled back — see `LiveConformanceGuard`'s module
    docstring. `consumer` and `max_rate_hint_hz` are OPTIONAL keys and the
    contract's own comment says a bare `[1, {}]` is a valid acknowledgement,
    so a wrongly-typed optional field must not black out the connection; it
    is logged and the handshake completes anyway."""
    helo = proto.make_helo(
        sample_clk_hz=source.sample_clk_hz(),
        channels=_helo_channels(source),
        channel_total_bits=ch.CHANNEL_TOTAL_BITS,
    )
    if guard is not None:
        violation = guard.check(proto.TAG_HELO, helo)
        if violation is not None:
            logger.error("HELO fails the wave_stream_v1 contract (sent anyway "
                        "— no session without it; every later frame on this "
                        "connection is now refused as fail-closed, DS-WS1 "
                        "FINDING 1): %s", violation)
    _send_frame(conn, proto.TAG_HELO, helo)

    # Best-effort HELO_OK read — consumers may omit fields, so we accept
    # anything that parses as the HELO_OK tag.
    if timeout_s is None:
        timeout_s = helo_ok_timeout_s()
    conn.settimeout(timeout_s)
    try:
        unp = msgpack.Unpacker(raw=False)
        while True:
            data = conn.recv(4096)
            if not data:
                logger.error("consumer closed before HELO_OK")
                return None
            unp.feed(data)
            for obj in unp:
                if isinstance(obj, list) and len(obj) == 2 and obj[0] == proto.TAG_HELO_OK:
                    logger.info("handshaked with consumer (payload=%r)", obj[1])
                    if guard is not None:
                        payload = obj[1] if isinstance(obj[1], dict) else {}
                        violation = guard.check(proto.TAG_HELO_OK, payload,
                                                direction="c2p")
                        if violation is not None:
                            logger.warning(
                                "HELO_OK fails the wave_stream_v1 contract "
                                "(accepted anyway — inbound frames are never "
                                "rolled back, DS-WS1 revision blocker): %s",
                                violation)
                    return unp   # keep buffered post-HELO_OK frames (DS-P3.78)
    except socket.timeout:
        logger.error("HELO_OK timeout: no HELO_OK from %s within %.1f s (%s; default %.1f s); dropping the connection",
                     _peer_label(conn), timeout_s, HELO_OK_TIMEOUT_ENV, DEFAULT_HELO_OK_TIMEOUT_S)
        return None


def _drain(conn: socket.socket, source: DataSource) -> int:
    """Run the per-connection HELO + frame-drain loop. Returns exit code."""
    guard = LiveConformanceGuard()  # DS-WS1: judged before anything is sent
    unp = _handshake(conn, source, guard=guard)
    if unp is None:
        return 1

    # Stream loop.
    seq = 0
    sample_count = 0
    annotation_count = 0
    # DS-T (RouteWave bring-up 2026-06-02): a GENEROUS send budget. The old
    # 0.05s timeout was borrowed by sendall() too, so the moment a consumer
    # (e.g. a busy GUI renderer) backpressured the TCP egress mid-batch, sendall
    # raised TimeoutError and the whole stream was dropped — RouteWave saw the
    # HELO + a few thousand frames, then the connection died. Inbound control
    # frames are polled non-blocking via select() below, so this timeout only
    # bounds a genuinely-stuck consumer (10s), never a transient render hiccup.
    conn.settimeout(10.0)
    t0 = time.monotonic()
    try:
        while True:
            batch = source.next_batch()
            if batch is None:
                _send_frame(conn, proto.TAG_EOF, proto.make_eof("source_exhausted"))
                logger.info("EOF sent (source exhausted)")
                break

            if batch.samples:
                payload = _samples_payload(seq, batch.samples)
                violation = guard.check(proto.TAG_SAMPLES, payload)
                if violation is None:
                    _send_frame(conn, proto.TAG_SAMPLES, payload)
                    sample_count += len(batch.samples)
                    seq += 1
                else:
                    logger.error("refusing SAMPLES batch — not put on the wire "
                                "(contract violation): %s", violation)
            if batch.annotations:
                payload = _annotations_payload(seq, batch.annotations)
                violation = guard.check(proto.TAG_ANNOTATIONS, payload)
                if violation is None:
                    _send_frame(conn, proto.TAG_ANNOTATIONS, payload)
                    annotation_count += len(batch.annotations)
                    seq += 1
                else:
                    logger.error("refusing ANNOTATIONS batch — not put on the "
                                "wire (contract violation): %s", violation)
            # DS-P3.69: raw Edge Detector edges → RouteWave EdgeStreams. One
            # frame per anchor: DS-P3.138 keeps each word within reach of it.
            clk_mhz = max(1, source.sample_clk_hz() // 1_000_000)
            for frame in batch.edge_wire_frames():
                violation, data = _edge_frame_bytes(guard, frame, seq, clk_mhz)
                if violation is None:
                    conn.sendall(data)
                    seq += 1
                else:
                    logger.error("refusing EDGE_EVENTS batch — not put on the "
                                "wire (contract violation): %s", violation)

            # Best-effort drain of inbound control frames (RATE_HINT, EOF),
            # polled NON-BLOCKING via select so it never borrows the (now
            # generous) send timeout and never stalls the egress loop. A clean
            # consumer disconnect surfaces as the next sendall raising
            # BrokenPipeError (caught below) or recv() returning b"".
            inbound = b""
            try:
                if select.select([conn], [], [], 0)[0]:
                    inbound = conn.recv(4096)
                    if not inbound:
                        # select readable + recv b"" = peer closed. With an idle
                        # ring we never sendall, so this is the ONLY way to see a
                        # disconnect (no BrokenPipe). serve_forever needs it to
                        # release the connection and accept the next consumer.
                        logger.info("consumer disconnected (idle)")
                        return 0
            except (socket.timeout, BlockingIOError, InterruptedError, OSError):
                inbound = b""
            if inbound:
                unp.feed(inbound)
            # Drain the persistent Unpacker: new bytes AND any frame the consumer
            # pipelined right after HELO_OK that the handshake left buffered
            # (DS-P3.78). Non-blocking — yields only complete objects.
            for obj in unp:
                if isinstance(obj, list) and len(obj) == 2:
                    t, payload = obj
                    if t == proto.TAG_EOF:
                        logger.info("consumer requested EOF: %r", payload)
                        return 0
                    if t == proto.TAG_RATE_HINT and isinstance(payload, dict):
                        logger.info("rate hint: %r", payload)
                    if t == proto.TAG_CONTROL and isinstance(payload, dict):
                        _apply_control(payload, source, conn)  # DS-P3.72

    except (BrokenPipeError, ConnectionResetError) as exc:
        logger.warning("consumer dropped: %s", exc)
    finally:
        elapsed = time.monotonic() - t0
        logger.info("sent %d samples / %d annotations in %.2fs",
                    sample_count, annotation_count, elapsed)
        if guard.native_full_checked or guard.native_header_checked:
            logger.info("native EDGE_EVENTS frames judged: %d word by word, "
                        "%d by header (one in %d whole)", guard.native_full_checked,
                        guard.native_header_checked, guard.native_full_every)

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# DS-P3.62: multiple concurrent consumers via single-drainer broadcast fan-out.
#
# The ring drain is single-owner + destructive (next_batch advances RING_READ_PTR
# and owns the DS-P3.60 watermark IRQ), so there is exactly ONE drainer thread.
# It serializes each Batch ONCE (global seq) and broadcasts the bytes to every
# subscriber's BOUNDED queue. Each subscriber has its own send thread, so a
# slow/stuck consumer only fills its own queue (drop-oldest + send-budget
# disconnect) and can never stall the drain or the other consumers. The drainer
# runs even with zero subscribers, so the ring stays drained between clients.
# ─────────────────────────────────────────────────────────────────────────────

def _poll_inbound(conn: socket.socket, source: DataSource,
                  unp: "msgpack.Unpacker") -> bool:
    """Non-blocking inbound poll on the persistent per-connection Unpacker.
    Returns True if the consumer disconnected or requested EOF (caller should
    stop); logs RATE_HINT; applies CONTROL frames (DS-P3.72). Drains any frame
    pipelined right after HELO_OK (DS-P3.78) even with no new socket data.
    Safe on an idle socket."""
    try:
        if select.select([conn], [], [], 0)[0]:
            data = conn.recv(4096)
            if not data:
                return True   # peer closed
            unp.feed(data)
    except (BlockingIOError, InterruptedError):
        pass
    except OSError:
        return True
    try:
        for obj in unp:
            if isinstance(obj, list) and len(obj) == 2:
                tag, payload = obj
                if tag == proto.TAG_EOF:
                    logger.info("subscriber requested EOF: %r", payload)
                    return True
                if tag == proto.TAG_RATE_HINT and isinstance(payload, dict):
                    logger.info("rate hint: %r", payload)
                if tag == proto.TAG_CONTROL and isinstance(payload, dict):
                    _apply_control(payload, source, conn)  # DS-P3.72
    except Exception:  # noqa: BLE001 - malformed control input must not kill the sub
        pass
    return False


class _Subscriber:
    """One connected consumer: a socket + a BOUNDED egress queue. The drainer
    offer()s pre-serialized frame bytes; this subscriber's own send thread
    dequeues and sends. The bounded queue is the firewall — a slow consumer
    fills ITS queue (drop-oldest) and is disconnected on the send budget,
    never stalling the drainer or the other subscribers."""

    def __init__(self, conn: socket.socket, max_queue: int, label: str):
        self.conn = conn
        self.q: "queue.Queue" = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self.label = label
        self.closing = False
        # DS-P3.66: channel-STATE keyframe captured atomically at register().
        self.keyframe_anns: list = []      # last annotation per channel_id
        self.keyframe_sample = None        # last sample (current channel levels)

    def offer(self, frame: bytes) -> None:
        """Non-blocking enqueue; drop the OLDEST frame on overflow (keep live
        semantics for a logic-analyzer viewer that cares about 'now')."""
        try:
            self.q.put_nowait(frame)
        except queue.Full:
            # Count only what was really lost: the send thread can empty the queue
            # between the Full and the eviction. DS-P2.50 reports this count to
            # the consumer, so it must be exact.
            try:
                self.q.get_nowait()      # evict oldest
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(frame)
            except queue.Full:
                self.dropped += 1

    def close(self) -> None:
        self.closing = True
        try:
            self.q.put_nowait(None)      # wake the send thread
        except queue.Full:
            pass


class _Hub:
    """Thread-safe registry of subscribers + a bounded backfill snapshot ring
    (DS-P3.63 / DS-P3.64). broadcast() appends each frame to the ring (so recent
    history accrues even with zero subscribers) and offers it to every current
    subscriber (copy-on-iterate). register() atomically captures the current
    snapshot AND joins the live set, so a frame is never both replayed and
    live-queued — no gap/dup at the join boundary.

    The ring is bounded by BYTES (len of the serialized frames = exact memory
    cost, the predictable bound) and/or FRAME COUNT (a safety cap against a flood
    of tiny frames), whichever binds first. The newest frame is always kept (≥1),
    so an oversize frame is retained alone rather than dropped. The ring is
    active iff either bound is > 0."""

    def __init__(self, backfill_bytes: int = 0, backfill_frames: int = 0,
                 backfill_seconds: float = 0.0):
        self._lock = threading.Lock()
        self._subs: set = set()
        self._max_bytes = max(backfill_bytes, 0)
        self._max_frames = max(backfill_frames, 0)
        self._max_seconds = max(backfill_seconds, 0.0)
        self._ring = (collections.deque()  # entries: (monotonic_ts, frame_bytes)
                      if (self._max_bytes or self._max_frames or self._max_seconds)
                      else None)
        self._ring_bytes = 0
        # DS-P3.66 keyframe state: last annotation per channel + last sample.
        self._kf_anns: dict = {}
        self._kf_sample = None
        # DS-P2.302: the latest error_run record per run, (channel_id, run_of, ts_start) -> (noted_at, annotation).
        # A storm emits one record per batch while it grows; the ring stores frames without them and a joiner gets
        # each run once, in its latest state, through the keyframe.
        self._kf_runs: "collections.OrderedDict" = collections.OrderedDict()

    def note_state(self, samples, annotations) -> None:
        """DS-P3.66: fold a drained batch into the keyframe state (last sample =
        current levels; last annotation per channel = per-lane baseline)."""
        with self._lock:
            if samples:
                self._kf_sample = samples[-1]
            now = time.monotonic()
            for a in annotations:
                if a.kind == ERROR_RUN_KIND:
                    key = (a.channel_id, (a.fields or {}).get("run_of"), a.ts_start_ns)
                    self._kf_runs.pop(key, None)
                    self._kf_runs[key] = (now, a)
                else:
                    self._kf_anns[a.channel_id] = a
            self._bound_runs()

    MAX_KEYFRAME_RUNS = 256

    def _bound_runs(self) -> None:
        """DS-P2.302: an ended run is dropped once it is older than the ring's oldest frame (it would have scrolled out
        of the backfill), and the oldest ended runs are dropped while more than MAX_KEYFRAME_RUNS remain. A run that is
        still ongoing is never dropped. Caller holds the lock."""
        def ended(entry) -> bool:
            return (entry[1].fields or {}).get("ongoing") is False
        if self._ring:
            oldest = self._ring[0][0]
            for key in [k for k, e in self._kf_runs.items() if ended(e) and e[0] < oldest]:
                del self._kf_runs[key]
        while len(self._kf_runs) > self.MAX_KEYFRAME_RUNS:
            key = next((k for k, e in self._kf_runs.items() if ended(e)), None)
            if key is None:
                break
            del self._kf_runs[key]

    def register(self, sub: _Subscriber) -> list:
        """Add the subscriber and return the backfill snapshot (recent frames to
        replay before the live stream); ALSO stamp the subscriber's keyframe
        state. Atomic vs any SINGLE broadcast()/note_state() call under the
        lock — no torn read of either. NOT atomic across the TWO calls
        `_drainer_loop` makes per batch (broadcast() the frame, THEN
        note_state() folds it into the keyframe, server.py around the
        `hub.note_state(sent_samples, sent_annotations)` line): a register()
        landing between them joins too late to receive that batch live (it
        was already broadcast to the prior subscriber set) AND stamps a
        keyframe that does not yet reflect it either — a gap, not a dup
        (DS-WS1 FINDING 2). Self-heals on the next batch; only matters with
        the backfill ring disabled, since an active ring's backfill still
        carries the frame."""
        with self._lock:
            sub.keyframe_anns = list(self._kf_anns.values()) + [a for _, a in self._kf_runs.values()]
            sub.keyframe_sample = self._kf_sample
            backfill = ([f for _, f in self._ring]
                        if self._ring is not None else [])
            self._subs.add(sub)
        return backfill

    def unregister(self, sub: _Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    def broadcast(self, frame: bytes, ring_frame: Optional[bytes] = None) -> None:
        """Offer `frame` to every subscriber and keep `ring_frame` (default: the same bytes) in the backfill ring.
        DS-P2.302: an ANNOTATIONS frame that carries error_run records is kept in the ring without them."""
        with self._lock:
            if self._ring is not None:
                now = time.monotonic()
                kept = frame if ring_frame is None else ring_frame
                self._ring.append((now, kept))
                self._ring_bytes += len(kept)
                # Evict oldest while over ANY bound (bytes / frames / seconds);
                # keep ≥1 so the newest frame survives (oversize or idle-stale).
                while len(self._ring) > 1 and (
                        (self._max_bytes and self._ring_bytes > self._max_bytes)
                        or (self._max_frames and len(self._ring) > self._max_frames)
                        or (self._max_seconds
                            and self._ring[0][0] < now - self._max_seconds)):
                    self._ring_bytes -= len(self._ring.popleft()[1])
            subs = tuple(self._subs)
        for sub in subs:
            sub.offer(frame)

    def count(self) -> int:
        with self._lock:
            return len(self._subs)

    def backfill_bytes(self) -> int:
        with self._lock:
            return self._ring_bytes


def _drainer_loop(source: DataSource, hub: _Hub, stop: threading.Event,
                  events_hub: Optional["_EventHub"] = None) -> None:
    """The SINGLE ring drainer: sole caller of next_batch() (→ RING_READ_PTR +
    the watermark IRQ). Runs continuously regardless of subscriber count, so the
    ring stays drained even with zero clients. Serializes each Batch ONCE under a
    global seq and broadcasts the bytes. next_batch() paces the loop (IRQ wait /
    poll-period). On source exhaustion it broadcasts EOF; on error it bails (it's
    the single owner — a dead drainer is fatal to the daemon).

    DS-WS1: every batch is judged by a LiveConformanceGuard before it is
    broadcast. The drainer never sends a HELO itself (each subscriber gets its
    own via `_handshake`), so the guard is PRIMED here with the same channel
    table `_handshake` sends, purely to establish the declared channel_id set
    that ANNOTATIONS are checked against — no frame goes out for this priming
    check. A batch that fails is refused (not broadcast) and only the parts of
    it that DID go out are folded into the keyframe state, so a late joiner's
    backfill/keyframe never carries a frame that was never actually sent.

    The priming HELO check is fail-closed like `_handshake`'s (DS-WS1 FINDING
    1): a violation (e.g. a malformed channel table) rolls back the guard's
    session state, so every subsequent SAMPLES/ANNOTATIONS/EDGE_EVENTS this
    drainer would broadcast is then refused as arriving before HELO — logged
    below naming the violating channel/field.

    DS-P2.50: `events_hub` receives exactly what went out on wave_stream_v1, so the
    two lanes stay views of the same truth: a refused batch is on neither."""
    seq = 0
    guard = LiveConformanceGuard()
    violation = guard.check(proto.TAG_HELO, proto.make_helo(
        sample_clk_hz=source.sample_clk_hz(),
        channels=_helo_channels(source), channel_total_bits=ch.CHANNEL_TOTAL_BITS))
    if violation is not None:
        logger.error("drainer: priming HELO fails the wave_stream_v1 contract "
                    "(every broadcast batch is now refused as fail-closed, "
                    "DS-WS1 FINDING 1): %s", violation)
    # The drainer broadcasts independent of any one subscriber's handshake
    # (DS-P3.62 — it runs even at zero subscribers), so there is no real c2p
    # HELO_OK to observe here; mark the guard past that gate synthetically so
    # it judges CONTENT (channel refs, monotonic ts, seq), which is what a
    # broadcast batch actually risks getting wrong, not per-connection ordering.
    # Direction is c2p (inbound/observe), same as a real HELO_OK, so this can
    # never roll back the HELO priming above.
    guard.check(proto.TAG_HELO_OK, {}, direction="c2p")
    try:
        while not stop.is_set():
            batch = source.next_batch()
            if batch is None:
                hub.broadcast(proto.pack_frame(
                    proto.TAG_EOF, proto.make_eof("source_exhausted")))
                logger.info("drainer: source exhausted; EOF broadcast")
                if events_hub is not None:
                    events_hub.publish_session(proto.KIND_END, reason="source_exhausted")
                return
            sent_samples: list = []
            sent_annotations: list = []
            if batch.samples:
                payload = _samples_payload(seq, batch.samples)
                violation = guard.check(proto.TAG_SAMPLES, payload)
                if violation is None:
                    hub.broadcast(proto.pack_frame(proto.TAG_SAMPLES, payload))
                    sent_samples = batch.samples
                    seq += 1
                else:
                    logger.error("drainer: refusing SAMPLES batch — not "
                                "broadcast (contract violation): %s", violation)
            if batch.annotations:
                payload = _annotations_payload(seq, batch.annotations)
                violation = guard.check(proto.TAG_ANNOTATIONS, payload)
                if violation is None:
                    # DS-P2.302: live subscribers get every record; the ring keeps the same seq without the error_run
                    # snapshots (the keyframe carries each run once), an empty list when the frame held only those.
                    ring_frame = None
                    if any(a.kind == ERROR_RUN_KIND for a in batch.annotations):
                        ring_frame = proto.pack_frame(proto.TAG_ANNOTATIONS, _annotations_payload(
                            seq, [a for a in batch.annotations if a.kind != ERROR_RUN_KIND]))
                    hub.broadcast(proto.pack_frame(proto.TAG_ANNOTATIONS, payload), ring_frame=ring_frame)
                    sent_annotations = batch.annotations
                    seq += 1
                else:
                    logger.error("drainer: refusing ANNOTATIONS batch — not "
                                "broadcast (contract violation): %s", violation)
            # Fold into the keyframe state ONLY what was actually sent (DS-P3.66).
            hub.note_state(sent_samples, sent_annotations)
            if events_hub is not None:
                events_hub.publish_annotations(sent_annotations)
            # DS-P3.69: raw Edge Detector edges → RouteWave EdgeStreams. One
            # frame per anchor: DS-P3.138 keeps each word within reach of it.
            clk_mhz = max(1, source.sample_clk_hz() // 1_000_000)
            for frame in batch.edge_wire_frames():
                violation, data = _edge_frame_bytes(guard, frame, seq, clk_mhz)
                if violation is None:
                    hub.broadcast(data)
                    seq += 1
                    if events_hub is not None:
                        events_hub.publish_edge_frame(frame)
                else:
                    logger.error("drainer: refusing EDGE_EVENTS batch — not "
                                "broadcast (contract violation): %s", violation)
    except Exception as exc:  # noqa: BLE001 - single owner; log and let the daemon exit
        logger.error("drainer loop error: %s", exc)
        if events_hub is not None:
            events_hub.publish_session(proto.KIND_ERROR, message=str(exc), fatal=True)
    finally:
        if guard.native_full_checked or guard.native_header_checked:
            logger.info("drainer: native EDGE_EVENTS frames judged: %d word by word, "
                        "%d by header (one in %d whole)", guard.native_full_checked,
                        guard.native_header_checked, guard.native_full_every)
        stop.set()


def _stats_heartbeat_loop(source: DataSource, hub: _Hub, stop: threading.Event,
                          period_s: float) -> None:
    """DS-P3.82: periodic STATS telemetry heartbeat, independent of the data
    rate (fires even on an idle bus). Each tick reads the source's regbank
    snapshot and broadcasts ONE STATS frame to every subscriber + the backfill
    ring, so a late joiner gets the latest telemetry immediately on connect.
    Sources without a regbank (read_stats()→None, e.g. the stub) emit nothing.
    Best-effort: a read error is logged and the loop continues — telemetry must
    never kill the daemon. Emits the first tick immediately, then every
    period_s; stop.wait() makes shutdown prompt."""
    ver = proto.daemon_version()
    seq = 0
    digest_seq = 0
    # DS-P3.89: the DIGEST (rolling-window summary + anomaly verdict) rides the
    # same heartbeat cadence as STATS. Optional on the source — getattr so a
    # source predating read_digest() is unaffected (returns None → no DIGEST).
    read_digest = getattr(source, "read_digest", None)
    while not stop.is_set():
        try:
            snap = source.read_stats()
            if snap is not None:
                hub.broadcast(proto.pack_frame(
                    proto.TAG_STATS, proto.make_stats(seq, daemon_ver=ver, stats=snap)))
                seq += 1
        except Exception as exc:  # noqa: BLE001 - telemetry must not kill the daemon
            logger.warning("stats heartbeat read failed: %s", exc)
        if read_digest is not None:
            try:
                dig = read_digest()
                if dig is not None:
                    hub.broadcast(proto.pack_frame(
                        proto.TAG_DIGEST, proto.make_digest(digest_seq, **dig)))
                    digest_seq += 1
            except Exception as exc:  # noqa: BLE001 - telemetry must not kill the daemon
                logger.warning("digest heartbeat read failed: %s", exc)
        stop.wait(period_s)


def _serve_subscriber(sub: _Subscriber, source: DataSource, hub: _Hub,
                      send_budget_s: float = 10.0) -> None:
    """Per-consumer thread: HELO handshake, then register with the hub and
    dequeue→sendall until the consumer drops or requests EOF. The drainer is
    never blocked by this socket; a stuck consumer trips the send budget and is
    dropped alone."""
    conn = sub.conn
    try:
        unp = _handshake(conn, source)
        if unp is None:
            return
        conn.settimeout(send_budget_s)
        # register = atomically join the live broadcast AND get the recent
        # backfill snapshot. Send the backfill DIRECTLY (bypassing the bounded
        # live queue) before the live loop, so a late joiner catches up on
        # recent history first (DS-P3.63). Frames keep their global seq, so
        # backfill seqs precede the live seqs contiguously.
        backfill = hub.register(sub)
        # 1. Channel-STATE keyframe (DS-P3.66): the last sample (current levels)
        #    + last annotation per channel, as ordinary frames with their
        #    original timestamps + a keyframe flag — so a very-late joiner sees
        #    each lane's last-known state even if it scrolled out of the ring.
        if sub.keyframe_sample is not None:
            kf = _samples_payload(0, [sub.keyframe_sample]); kf["keyframe"] = True
            conn.sendall(proto.pack_frame(proto.TAG_SAMPLES, kf))
        if sub.keyframe_anns:
            kf = _annotations_payload(0, sub.keyframe_anns); kf["keyframe"] = True
            conn.sendall(proto.pack_frame(proto.TAG_ANNOTATIONS, kf))
        # 2. Backfill (recent frames).
        backfill_bytes = 0
        for fb in backfill:
            conn.sendall(fb)
            backfill_bytes += len(fb)
        # 3. Catch-up→live boundary marker (DS-P3.67).
        conn.sendall(proto.pack_frame(proto.TAG_SYNC, proto.make_sync()))
        logger.info("subscriber %s joined (subs=%d, keyframe=%d ch, "
                    "backfill=%d frames / %d B)", sub.label, hub.count(),
                    len(sub.keyframe_anns), len(backfill), backfill_bytes)
        while True:
            try:
                frame = sub.q.get(timeout=0.5)
            except queue.Empty:
                # Queue drained: honour a server-side close, else poll the peer.
                # (closing is checked ONLY when empty so already-queued frames —
                # including a final EOF — are always flushed first.)
                if sub.closing or _poll_inbound(conn, source, unp):
                    break
                continue
            if frame is None:             # close sentinel
                break
            conn.sendall(frame)
            if _poll_inbound(conn, source, unp):       # EOF request / disconnect after a send
                break
    except (BrokenPipeError, ConnectionResetError, socket.timeout) as exc:
        logger.warning("subscriber %s dropped: %s", sub.label, exc)
    except OSError as exc:
        logger.warning("subscriber %s socket error: %s", sub.label, exc)
    finally:
        hub.unregister(sub)
        try:
            conn.close()
        except OSError:
            pass
        logger.info("subscriber %s left (subs=%d, dropped=%d)",
                    sub.label, hub.count(), sub.dropped)


# ─────────────────────────────────────────────────────────────────────────────
# DS-P2.50: wave_events_v1 NDJSON lane. Never a second drainer: next_batch() is
# destructive, so a peer reader would steal frames from RouteWave. In fan-out the
# ONE drainer feeds this hub as well. A consumer that wants no input may half-close
# (`nc </dev/null`), which TCP cannot tell from a full close, so EOF on the read
# side never ends a session; only a failed send does.
# ─────────────────────────────────────────────────────────────────────────────

EVENTS_MAX_QUEUE = 4096         # lines, not frames: one frame is many events
EVENTS_SUBSCRIBE_WAIT_S = 0.5
EVENTS_PING_PERIOD_S = 2.0
EVENTS_SEND_BUDGET_S = 10.0


class _EventSubscriber(_Subscriber):
    """A consumer on the events lane. ``matcher`` None = subscription still open:
    everything queues, and the send loop applies the filter once it is known, so
    nothing published during the subscribe window is lost."""

    def __init__(self, conn: socket.socket, max_queue: int, label: str):
        super().__init__(conn, max_queue, label)
        self.matcher: Optional[proto.EventFilter] = None


class _EventHub:
    """The events lane's registry. Runs on the drainer thread, so it filters
    before queueing (a narrow subscription isn't evicted by traffic it never asked
    for) and contains its own failures: a defect here must not take the
    wave_stream_v1 drain with it."""

    def __init__(self, encoder: proto.WaveEventsEncoder):
        self.encoder = encoder
        self._lock = threading.Lock()
        self._subs: set = set()
        self._last_ts_ns = 0

    def register(self, sub: _EventSubscriber) -> None:
        with self._lock:
            self._subs.add(sub)

    def unregister(self, sub: _EventSubscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    def count(self) -> int:
        with self._lock:
            return len(self._subs)

    def _publish(self, subs: tuple, events: Iterable[proto.WaveEvent]) -> None:
        for event in events:
            for sub in subs:
                if sub.matcher is None or sub.matcher.matches(event):
                    sub.offer(event)

    def publish_annotations(self, annotations: list) -> None:
        with self._lock:
            subs = tuple(self._subs)
        if annotations:
            self._last_ts_ns = int(annotations[-1].ts_start_ns)
        if not subs:
            return
        try:
            self._publish(subs, (self.encoder.annotation(a) for a in annotations))
        except Exception as exc:  # noqa: BLE001 - see class docstring
            logger.error("events lane: annotation batch not published: %s", exc)

    def publish_edge_frame(self, frame) -> None:
        """publish_edges for one data_source.EdgeWireFrame. A native frame's
        words become a Python list only when a subscriber wants edges."""
        with self._lock:
            wanted = any(s.matcher is not None and s.matcher.wants_kind(proto.KIND_EDGE)
                         for s in self._subs)
        if wanted:
            self.publish_edges(frame.words(), frame.anchor_ns)

    def publish_edges(self, words: list, anchor_ns: int) -> None:
        with self._lock:
            subs = tuple(s for s in self._subs
                         if s.matcher is not None
                         and s.matcher.wants_kind(proto.KIND_EDGE))
        if not subs:      # opt-in, and the highest-volume decode on the path
            return
        try:
            self._publish(subs, self.encoder.edges(words, anchor_ns))
        except Exception as exc:  # noqa: BLE001 - see class docstring
            logger.error("events lane: edge batch not published: %s", exc)

    def publish_session(self, kind: str, **body) -> None:
        with self._lock:
            subs = tuple(self._subs)
        self._publish(subs, [self.encoder.session(kind, self._last_ts_ns, **body)])


class _PeerWatch:
    """Reads and discards whatever the consumer sends after its subscription.
    Once it has seen EOF the peer may be half-closed or gone. Only a send can tell,
    so an idle session then gets a ping every EVENTS_PING_PERIOD_S: a gone peer
    answers RST, the next send raises, and an idle bus can't pile up dead sessions."""

    def __init__(self, encoder: proto.WaveEventsEncoder):
        self._encoder = encoder
        self.read_closed = False
        self._last_ping = 0.0

    def poll(self, conn: socket.socket) -> None:
        if not self.read_closed:
            try:
                if select.select([conn], [], [], 0)[0] and not conn.recv(4096):
                    self.read_closed = True
            except (BlockingIOError, InterruptedError):
                pass
        now = time.monotonic()
        if self.read_closed and now - self._last_ping >= EVENTS_PING_PERIOD_S:
            self._last_ping = now
            conn.sendall(self._encoder.session(proto.KIND_PING).line)


def _events_handshake(conn: socket.socket, encoder: proto.WaveEventsEncoder,
                      watch: _PeerWatch) -> Optional[proto.EventFilter]:
    """HELO, then give the consumer EVENTS_SUBSCRIBE_WAIT_S to send one complete
    line. No line = defaults, which is what `nc | jq` and `curl telnet://` get, and
    bytes with no newline yet are not a line. Returns None after telling the
    consumer its subscription was refused."""
    conn.sendall(encoder.helo())
    deadline = time.monotonic() + EVENTS_SUBSCRIBE_WAIT_S
    pending = b""
    while b"\n" not in pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([conn], [], [], remaining)[0]:
            return proto.EventFilter()
        data = conn.recv(4096)
        if not data:
            watch.read_closed = True
            return proto.EventFilter()
        pending += data
    try:
        return encoder.parse_subscribe(pending.split(b"\n", 1)[0])
    except ValueError as exc:
        logger.warning("events consumer refused: %s", exc)
        conn.sendall(encoder.session(proto.KIND_ERROR, message=str(exc), fatal=True).line)
        return None


def _serve_events_subscriber(sub: _EventSubscriber, hub: _EventHub) -> None:
    """Per-consumer thread for the events lane. Registers BEFORE the handshake so
    the subscribe window can't swallow an event; drop-oldest overflow is reported
    in-band as a warning line carrying the count."""
    conn = sub.conn
    encoder = hub.encoder
    watch = _PeerWatch(encoder)
    try:
        conn.settimeout(EVENTS_SEND_BUDGET_S)
        hub.register(sub)
        matcher = _events_handshake(conn, encoder, watch)
        if matcher is None:
            return
        sub.matcher = matcher
        reported_drops = 0
        while True:
            try:
                event = sub.q.get(timeout=0.25)
            except queue.Empty:
                if sub.closing:
                    break
                watch.poll(conn)
                continue
            if event is None:
                break
            dropped = sub.dropped   # before the send: more can drop while it blocks
            if dropped > reported_drops:
                conn.sendall(encoder.session(
                    proto.KIND_WARNING, message="consumer too slow; oldest events dropped",
                    dropped=dropped - reported_drops).line)
                reported_drops = dropped
            if matcher.matches(event):
                conn.sendall(event.line)
            if event.kind in (proto.KIND_END, proto.KIND_ERROR):
                break
    except OSError as exc:  # includes BrokenPipe/ConnectionReset/socket.timeout
        logger.warning("events subscriber %s dropped: %s", sub.label, exc)
    finally:
        hub.unregister(sub)
        try:
            conn.close()
        except OSError:
            pass
        logger.info("events subscriber %s left (subs=%d, dropped=%d)",
                    sub.label, hub.count(), sub.dropped)


def _events_accept_loop(srv: socket.socket, hub: _EventHub, stop: threading.Event,
                        max_queue: int, subs: list, threads: list) -> None:
    srv.settimeout(0.25)
    served = 0
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        served += 1
        sub = _EventSubscriber(conn, max_queue, label=f"events#{served}")
        subs.append(sub)
        thread = threading.Thread(target=_serve_events_subscriber, args=(sub, hub),
                                  name=f"ds-events-{served}", daemon=True)
        threads.append(thread)
        thread.start()


def serve_one_events(endpoint: Endpoint, source: DataSource,
                     connect_timeout_s: float = 60.0) -> int:
    """wave_events_v1 peer of serve_one: bind `endpoint`, accept one consumer and
    drain `source` straight to it until EOF or disconnect. It owns the source, so
    it never runs beside a wave_stream_v1 consumer; serve_fanout is how the two
    lanes coexist. Returns 0 on a clean end, 1 on connect timeout or a refused
    subscription."""
    srv, label, cleanup_listener = _make_listener(endpoint)
    srv.listen(1)
    logger.info("events: listening on %s; waiting for consumer", label)
    srv.settimeout(connect_timeout_s)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        logger.error("no events consumer connected within %.1fs; exiting",
                     connect_timeout_s)
        return 1
    finally:
        srv.close()
        cleanup_listener()

    encoder = proto.WaveEventsEncoder(source.sample_clk_hz(), _channels(source))
    watch = _PeerWatch(encoder)
    last_ts_ns = 0
    try:
        conn.settimeout(EVENTS_SEND_BUDGET_S)
        matcher = _events_handshake(conn, encoder, watch)
        if matcher is None:
            return 1
        while True:
            batch = source.next_batch()
            if batch is None:
                conn.sendall(encoder.session(proto.KIND_END, last_ts_ns,
                                             reason="source_exhausted").line)
                return 0
            events = [encoder.annotation(a) for a in batch.annotations]
            if batch.annotations:
                last_ts_ns = int(batch.annotations[-1].ts_start_ns)
            if batch.has_edges() and matcher.wants_kind(proto.KIND_EDGE):
                for anchor_ns, words in batch.wire_edge_frames():  # DS-P3.138
                    events += encoder.edges(words, anchor_ns)
            for event in events:
                if matcher.matches(event):
                    conn.sendall(event.line)
            watch.poll(conn)
    except OSError as exc:
        logger.warning("events consumer dropped: %s", exc)
        return 0
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve_fanout(endpoint: Endpoint, source: DataSource,
                 connect_timeout_s: float = 60.0,
                 max_queue: int = 256,
                 backfill_bytes: int = 0,
                 backfill_frames: int = 0,
                 backfill_seconds: float = 0.0,
                 max_connections: int = 0,
                 stats_period_s: float = 1.0,
                 events_endpoint: Optional[Endpoint] = None,
                 events_max_queue: int = EVENTS_MAX_QUEUE) -> int:
    """Serve MANY concurrent consumers via single-drainer broadcast fan-out
    (DS-P3.62). One drainer thread owns the ring (next_batch / RING_READ_PTR /
    the watermark IRQ) and broadcasts each serialized Batch to every connected
    subscriber's bounded queue; each subscriber runs its own send thread.

    The daemon listens forever (the connect timeout is just the accept stop-poll
    interval — it never exits on idle). It returns only when the drainer stops
    (source exhausted or error). max_connections>0 stops ACCEPTING after that
    many total (existing subscribers + the drainer keep running) — for tests.

    `events_endpoint` (DS-P2.50) adds the wave_events_v1 NDJSON lane, fed by the
    same drainer. If it can't bind, that is logged and the daemon runs on: losing
    the agent lane must not cost RouteWave its stream.
    """
    srv, label, cleanup_listener = _make_listener(endpoint)
    srv.listen(8)
    events_hub = None
    events_srv = None
    cleanup_events = lambda: None
    if events_endpoint is not None:
        try:
            events_srv, events_label, cleanup_events = _make_listener(events_endpoint)
            events_srv.listen(8)
            events_hub = _EventHub(proto.WaveEventsEncoder(source.sample_clk_hz(),
                                                           _channels(source)))
            logger.info("wave_events_v1 listening on %s", events_label)
        except OSError as exc:
            logger.error("wave_events_v1 lane disabled, cannot bind %s: %s",
                         events_endpoint, exc)
    hub = _Hub(backfill_bytes=backfill_bytes, backfill_frames=backfill_frames,
               backfill_seconds=backfill_seconds)
    stop = threading.Event()
    drainer = threading.Thread(target=_drainer_loop,
                               args=(source, hub, stop, events_hub),
                               name="ds-drainer", daemon=True)
    drainer.start()
    events_subs: list = []
    events_threads: list = []
    events_acceptor = None
    if events_hub is not None:
        events_acceptor = threading.Thread(
            target=_events_accept_loop,
            args=(events_srv, events_hub, stop, events_max_queue,
                  events_subs, events_threads),
            name="ds-events-accept", daemon=True)
        events_acceptor.start()
    # DS-P3.82: telemetry heartbeat — broadcasts a STATS frame ~1Hz (independent
    # of the data rate). stats_period_s<=0 disables it. Sources with no regbank
    # (read_stats()→None) emit nothing, so the stub daemon is unaffected.
    if stats_period_s > 0:
        threading.Thread(
            target=_stats_heartbeat_loop, args=(source, hub, stop, stats_period_s),
            name="ds-stats", daemon=True).start()
    logger.info("listening on %s (fan-out); drainer running, max_queue=%d, "
                "backfill=%dB/%d frames/%.0fs", label, max_queue,
                backfill_bytes, backfill_frames, backfill_seconds)

    subs: list = []
    served = 0
    accept_poll_s = min(connect_timeout_s, 1.0)
    try:
        while not stop.is_set():
            srv.settimeout(accept_poll_s)
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            served += 1
            sub = _Subscriber(conn, max_queue, label=f"#{served}")
            subs.append(sub)
            threading.Thread(target=_serve_subscriber, args=(sub, source, hub),
                             name=f"ds-sub-{served}", daemon=True).start()
            if max_connections and served >= max_connections:
                break
    finally:
        srv.close()
        cleanup_listener()

    if max_connections:
        # Test mode: let the (finite) source drain to EOF + subscribers flush.
        drainer.join(timeout=10.0)
    stop.set()
    for sub in subs:
        sub.close()
    if events_acceptor is not None:
        events_acceptor.join(timeout=2.0)
        events_srv.close()
        cleanup_events()
        for sub in events_subs:
            sub.close()
        # The process exits right after this: let the final end/error line out.
        for thread in events_threads:
            thread.join(timeout=2.0)
    return 0
