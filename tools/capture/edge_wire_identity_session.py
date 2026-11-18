#!/usr/bin/env python3
"""DS-T2.84 — one LIVE EDGE wire-identity session: board stimulus -> daemon ->
this consumer, recorded at the wire and judged by edge_wire_identity_verdict.

    host:  this script --TCP--> board daemon :7475 (HELO, HELO_OK, then record)
    board: python3 -m sw.stream.testbench.edge4_live_stimulus <phase>  (via UART)

The stimulus is DS-P1.71's (all four EDGE loggers on the fabric SPI bus, loggers
off while the mux moves, then CONFIG "10" per logger). This session connects as
an ordinary consumer, waits for the daemon's TAG_SYNC live boundary, types the
stimulus command on the board console, and records every frame until the board
prints its log. No RouteWave: the question is what a consumer receives, not how
one viewer draws it.

The console must already be logged by ONE reader into ``--console-log``; this
script only writes the command to the tty and reads that log.

    python3 tools/capture/edge_wire_identity_session.py --phase sparse \\
        --board-ip <ip> --console <by-id tty> --console-log <log> --out <dir> \\
        [--prior-mosi-byte 0x5A] [--expect-disabled]
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import msgpack

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from sw.stream import protocol as proto  # noqa: E402
from sw.stream.testbench import edge_wire_identity_verdict as V  # noqa: E402
from sw.stream.testbench.edge4_live_stimulus import BEGIN_MARK, END_MARK  # noqa: E402
from tools.capture.edge4_live_session import console_send, wait_console  # noqa: E402


class Recorder:
    """A plain consumer: HELO -> HELO_OK, then record every frame and byte."""

    def __init__(self, host: str, port: int) -> None:
        self.sock = socket.create_connection((host, port), timeout=5.0)
        self.frames: List[list] = []
        self.raw = bytearray()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        unp = msgpack.Unpacker(raw=False, strict_map_key=False)
        self.sock.settimeout(0.5)
        acked = False
        while not self.stop.is_set():
            try:
                b = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not b:
                break
            with self.lock:
                self.raw += b
                unp.feed(b)
                for fr in unp:
                    self.frames.append(fr)
                    if not acked and int(fr[0]) == proto.TAG_HELO:
                        self.sock.sendall(proto.pack_frame(
                            proto.TAG_HELO_OK, {"consumer": "ds-t2-84-wire-identity"}))
                        acked = True
        self.stop.set()

    def tags(self) -> List[int]:
        with self.lock:
            return [int(f[0]) for f in self.frames]

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2.0)
        self.sock.close()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phase", required=True, choices=["sparse", "dense"])
    ap.add_argument("--board-ip", required=True)
    ap.add_argument("--daemon-port", type=int, default=7475)
    ap.add_argument("--console", required=True)
    ap.add_argument("--console-log", required=True, type=Path)
    ap.add_argument("--board-cwd", default="/tmp/dsnew")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--prior-mosi-byte", type=lambda s: int(s, 0), default=None,
                    help="last MOSI byte p_spi shifted since power-up (MOSI idles at its bit 0)")
    ap.add_argument("--expect-disabled", action="store_true",
                    help="the loggers were running before arm(), so each must emit a DISABLED marker")
    args = ap.parse_args(argv)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    phase = {"phase": args.phase, "prior_mosi_byte": args.prior_mosi_byte,
             "expect_disabled": bool(args.expect_disabled)}
    (out / "phase.json").write_text(json.dumps(phase, indent=1) + "\n")

    rec = Recorder(args.board_ip, args.daemon_port)
    log: dict = dict(phase)
    try:
        end = time.time() + 30
        while time.time() < end and proto.TAG_SYNC not in rec.tags():
            time.sleep(0.2)
        if proto.TAG_SYNC not in rec.tags():
            raise RuntimeError(f"no SYNC from the daemon; tags={rec.tags()[:20]}")
        log["sync_at_frame"] = rec.tags().index(proto.TAG_SYNC)
        start = args.console_log.stat().st_size
        log["stimulus_sent_unix"] = time.time()
        console_send(args.console, f"cd {args.board_cwd} && python3 -u -m "
                                   f"sw.stream.testbench.edge4_live_stimulus {args.phase}")
        txt = wait_console(args.console_log, start, END_MARK.encode(), 180)
        body = txt.split(BEGIN_MARK.encode(), 1)[1].split(END_MARK.encode(), 1)[0]
        stim = json.loads(re.sub(rb"\s+", b"", body).decode())
        (out / "stimulus.json").write_text(json.dumps(stim, indent=1) + "\n")
        time.sleep(4.0)                   # let the daemon drain the last edges
        log["stimulus_done_unix"] = time.time()
    finally:
        rec.close()
        with rec.lock:
            frames = list(rec.frames)
            raw = bytes(rec.raw)
        helo = next((p for t, p in frames if int(t) == proto.TAG_HELO), {})
        (out / "wire.json").write_text(json.dumps({"helo": helo, "captured": frames}) + "\n")
        (out / "wire.msgpack").write_bytes(raw)
        log["frames"] = len(frames)
        (out / "session.json").write_text(json.dumps(log, indent=1, default=str) + "\n")
    return V.main([str(out)])


if __name__ == "__main__":
    raise SystemExit(main())
