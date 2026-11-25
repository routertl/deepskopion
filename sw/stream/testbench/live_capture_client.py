#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-P2.101 — host-side TCP client for a LIVE DE25 capture-correctness
witness against a running ``python3 -m sw.stream`` daemon (DS-P2.99/.102).

``sw/stream/testbench/capture_correctness.py`` is deliberately board-free
(a ``FakeDdrMmap``/AF_UNIX socket drives ``serve_one`` in-process). This
module is its live-silicon counterpart: it opens a real TCP connection to
the board's ``--listen 0.0.0.0:7475`` daemon and records the exact same
wire shape (HELO, then ANNOTATIONS/EDGE_EVENTS frames) so the SAME
``_extract_uart``/``_extract_i2c``/``_extract_spi``/``_extract_edge``
readers from ``capture_correctness.py`` can judge it — only the transport
changed (AF_UNIX + in-process server -> AF_INET + real daemon on real
silicon), never the judgement.

The actual bus stimulus is generated ON THE BOARD by DS-P2.100's fabric
stimulus generator, armed over the UART console with plain regbank writes
(see the live-witness execution documentation for
the exact commands) — this module only captures + decodes what the daemon
already streams once that stimulus fires; it never drives the bus itself.

Run (capture for 5s, print every frame + HELO's sample_clk_hz):

    python3 -m sw.stream.testbench.live_capture_client --host <board-ds-ip> \\
        --port 7475 --duration 5 --out /tmp/uart_capture.json
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import msgpack

from sw.stream import protocol as proto
from sw.stream.stimulus_armer import ROUTED_ARMERS

WireFrame = Tuple[int, dict]


def split_live(captured: List[WireFrame]) -> Tuple[List[WireFrame], int]:
    """Return ``(live, backfill_dropped)`` — the frames AFTER the daemon's
    catch-up→live boundary (``TAG_SYNC``), and the count discarded before it.

    On connect the daemon replays a bounded BACKFILL snapshot (recent frames
    from an earlier era) and then emits ONE ``TAG_SYNC`` frame marking the
    live boundary (deepskopion-bringup skill, rung 2; ``server.py`` ``_Hub``).
    A daemon-live witness that ARMS its stimulus AFTER connecting (so the burst
    lands in the live stream) must judge only the post-SYNC frames — otherwise
    replayed history from an earlier arm pollutes the exact-payload check. This
    is the live-daemon analogue of the stop-daemon driver's ``next_batch()``
    seed that discards the backlog; it does NOT loosen the judge (an empty live
    stream still fails closed against the expected payload).

    Split at the FIRST SYNC (the backfill boundary). If the daemon sent no
    SYNC, everything is treated as live (nothing to discard)."""
    for i, (tag, _payload) in enumerate(captured):
        if tag == proto.TAG_SYNC:
            return captured[i + 1:], i + 1
    return list(captured), 0


def capture(host: str, port: int, duration_s: float,
            stim: Optional[str] = None,
            edge_route: Optional[str] = None) -> Tuple[dict, List[WireFrame]]:
    """Connect, complete the HELO handshake, then record every frame the
    daemon sends for ``duration_s`` wall-clock seconds. Returns
    ``(helo_payload, captured)`` where ``captured`` is ``[(tag, payload), ...]``
    in wire order — the exact shape ``capture_correctness.py``'s
    ``_extract_*`` readers expect.

    ``stim`` (DS-P2.136): once the daemon's ``TAG_SYNC`` live boundary arrives,
    send ``make_stim_control(stim)`` on this same connection so the DAEMON arms
    the on-board stimulus generator — no console-side armer — and the burst
    lands in the live stream. The daemon's CONTROL_ACK is captured in-line.

    ``edge_route`` (DS-P3.123): at the same boundary, after any ``stim``, send
    ``make_edge_route_control(edge_route)`` so the daemon routes that signal set
    onto the Edge Detector inputs and enables their data loggers."""
    sock = socket.create_connection((host, port), timeout=5.0)
    sock.settimeout(0.5)
    unp = msgpack.Unpacker(raw=False)

    def read_one(deadline_s: float):
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            for obj in unp:
                assert isinstance(obj, list) and len(obj) == 2
                return obj[0], obj[1]
            remaining = max(0.01, end - time.monotonic())
            sock.settimeout(remaining)
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                raise RuntimeError("connection closed before HELO")
            unp.feed(data)
        raise TimeoutError("timed out waiting for a frame")

    tag, helo = read_one(5.0)
    assert tag == proto.TAG_HELO, f"expected HELO, got tag {tag}: {helo}"
    sock.sendall(proto.pack_frame(proto.TAG_HELO_OK,
                                  {"consumer": "de-c0-live-witness"}))

    captured: List[WireFrame] = []
    end = time.monotonic() + duration_s
    while time.monotonic() < end:
        try:
            tag, payload = read_one(max(0.05, end - time.monotonic()))
        except TimeoutError:
            break
        if tag == proto.TAG_EOF:
            break
        captured.append((tag, payload))
        if stim and tag == proto.TAG_SYNC:
            # DS-P2.343: a routed arm (spi) takes the edge route in the same frame, so the route is in place
            # before the burst; no separate edge_route frame follows it.
            routed = edge_route if edge_route and stim in ROUTED_ARMERS else None
            sock.sendall(proto.pack_frame(proto.TAG_CONTROL,
                                          proto.make_stim_control(stim, route=routed)))
            stim = None
            if routed:
                edge_route = None   # routed by the arm itself; a later edge_route would only re-reset its loggers
        if edge_route and tag == proto.TAG_SYNC:
            # After any stim: a stim arm rewrites the whole EMIO_MUX_SEL, the
            # edge route rewrites only bits [7:3] (DS-P3.123).
            sock.sendall(proto.pack_frame(proto.TAG_CONTROL,
                                          proto.make_edge_route_control(edge_route)))
            edge_route = None
    sock.close()
    return helo, captured


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=7475)
    p.add_argument("--duration", type=float, default=5.0,
                   help="seconds to capture after HELO (default 5.0)")
    p.add_argument("--out", type=Path, default=None,
                   help="write {helo, captured} JSON here")
    p.add_argument("--live-only", action="store_true",
                   help="write ONLY the post-SYNC live frames (discard the "
                        "replayed backfill). Use when the stimulus is armed "
                        "AFTER connecting, so the burst is in the live stream "
                        "(the DS-P1.59 daemon-live witness choreography).")
    p.add_argument("--stim", choices=["uart", "i2c", "spi", "edge"], default=None,
                   help="after SYNC, ask the DAEMON to arm the on-board stimulus "
                        "generator for this protocol (CONTROL op=stim, DS-P2.136)")
    p.add_argument("--edge-route", choices=["i2c", "spi", "uart", "can", "stim", "pmod"],
                   default=None,
                   help="after SYNC (and after --stim), ask the DAEMON to route this "
                        "signal set onto the Edge Detector inputs and enable their "
                        "data loggers (CONTROL op=edge_route, DS-P3.123)")
    args = p.parse_args(argv)

    helo, captured = capture(args.host, args.port, args.duration, stim=args.stim,
                             edge_route=args.edge_route)
    backfill_dropped = 0
    if args.live_only:
        captured, backfill_dropped = split_live(captured)
    doc = {"helo": helo, "captured": captured}
    if args.live_only:
        doc["live_only"] = True
        doc["backfill_dropped"] = backfill_dropped
    text = json.dumps(doc, indent=2, default=str)
    if args.out:
        args.out.write_text(text)
    print(f"HELO: {helo}")
    if args.live_only:
        print(f"live-only: kept {len(captured)} live frame(s), "
              f"dropped {backfill_dropped} backfill frame(s) before SYNC")
    print(f"captured {len(captured)} frame(s) over {args.duration}s")
    for tag, payload in captured:
        print(f"  tag={tag} {payload}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
