#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-P1.71 — one LIVE four-channel EDGE session: board stimulus -> daemon ->
RouteWave, recorded at the wire and judged as RouteWave drew it.

    host:  RouteWave --stream attach--> TEE (127.0.0.1) --TCP--> board daemon :7475
    board: python3 -m sw.stream.testbench.edge4_live_stimulus <phase>   (via UART)

The tee forwards both directions untouched and records every board->host frame,
so ``wire.json`` is exactly what RouteWave ingested — a live stream, not a
replay. After the stimulus finishes the session asks RouteWave (WCP) for its
own view: ``export`` (VCD of every lane as drawn), ``stream status``, and
screenshots at full, mid and edge-level zoom under Xvfb.

The console must already be logged by ONE reader (DE25: DTR+RTS asserted,
Haz.107) into ``--console-log``; this script only writes commands to the tty
and reads that log.

    python3 tools/capture/edge4_live_session.py --phase dense \\
        --board-ip <ip> --console <by-id tty> --console-log <log> \\
        --routewave <binary> --out <dir>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import msgpack

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from sw.stream.testbench import edge4_live_verdict as V  # noqa: E402
from sw.stream.testbench.edge4_live_stimulus import BEGIN_MARK, END_MARK  # noqa: E402

DISPLAY = ":71"


class Tee:
    """Forward RouteWave <-> daemon and record the daemon's frames."""

    def __init__(self, board: str, port: int, listen_port: int) -> None:
        self.board, self.port = board, port
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", listen_port))
        self.srv.listen(1)
        self.frames: List[list] = []
        self.rx_times: List[float] = []
        self.raw = bytearray()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def _pump(self, src, dst, record: bool) -> None:
        unp = msgpack.Unpacker(raw=False, strict_map_key=False)
        src.settimeout(0.5)
        while not self.stop.is_set():
            try:
                b = src.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not b:
                break
            try:
                dst.sendall(b)
            except OSError:
                break
            if record:
                with self.lock:
                    self.raw += b
                    unp.feed(b)
                    for fr in unp:
                        self.frames.append(fr)
                        self.rx_times.append(time.time())
        self.stop.set()

    def _run(self) -> None:
        cli, _ = self.srv.accept()
        up = socket.create_connection((self.board, self.port), timeout=5)
        threading.Thread(target=self._pump, args=(up, cli, True), daemon=True).start()
        threading.Thread(target=self._pump, args=(cli, up, False), daemon=True).start()

    def tags(self) -> List[int]:
        with self.lock:
            return [int(f[0]) for f in self.frames]


def wcp(port: int, cmd: str, arg: str = "", timeout: float = 30.0) -> str:
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall((json.dumps({"cmd": cmd, "arg": arg}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(1 << 20)
        if not chunk:
            break
        buf += chunk
    s.close()
    try:
        return json.loads(buf.decode()).get("output", "")
    except json.JSONDecodeError:
        return buf.decode()


def shot(path: Path) -> None:
    subprocess.run(["xdotool", "mousemove", "1910", "1070"],
                   env=dict(os.environ, DISPLAY=DISPLAY), check=False)
    time.sleep(1.2)
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "x11grab",
                    "-video_size", "1920x1080", "-i", f"{DISPLAY}.0", "-vframes", "1", str(path)],
                   check=True)


def console_send(tty: str, line: str) -> None:
    fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY)
    try:
        for ch in (line + "\r").encode():
            os.write(fd, bytes([ch]))
            time.sleep(0.002)
    finally:
        os.close(fd)


def wait_console(log: Path, start: int, pattern: bytes, timeout: float) -> bytes:
    end = time.time() + timeout
    while time.time() < end:
        data = log.read_bytes()[start:].replace(b"\0", b"")
        if pattern in data:
            return data
        time.sleep(0.25)
    raise TimeoutError(f"{pattern!r} not seen on the console within {timeout}s")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phase", required=True, choices=["sparse", "dense"])
    ap.add_argument("--board-ip", required=True)
    ap.add_argument("--daemon-port", type=int, default=7475)
    ap.add_argument("--console", required=True)
    ap.add_argument("--console-log", required=True, type=Path)
    ap.add_argument("--board-cwd", default="/tmp/dsnew")
    ap.add_argument("--routewave", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--tee-port", type=int, default=19475)
    ap.add_argument("--wcp-port", type=int, default=19777)
    args = ap.parse_args(argv)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    rwhome = out / ".rwhome"            # throwaway HOME: no stale RouteWave session
    rwhome.mkdir(exist_ok=True)

    tee = Tee(args.board_ip, args.daemon_port, args.tee_port)
    xvfb = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1920x1080x24"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    env = dict(os.environ, DISPLAY=DISPLAY, LIBGL_ALWAYS_SOFTWARE="1", HOME=str(rwhome))
    rw = subprocess.Popen([str(args.routewave), "--port", str(args.wcp_port),
                           "-c", f"stream attach 127.0.0.1:{args.tee_port}"],
                          env=env, stdout=open(out / "routewave.log", "w"), stderr=subprocess.STDOUT)
    log: dict = {"phase": args.phase, "routewave": str(args.routewave)}
    try:
        end = time.time() + 30
        while time.time() < end and V.TAG_SYNC not in tee.tags():
            time.sleep(0.2)
        if V.TAG_SYNC not in tee.tags():
            raise RuntimeError(f"no SYNC from the daemon via the tee; tags={tee.tags()[:20]}")
        xenv = dict(os.environ, DISPLAY=DISPLAY)
        wins = subprocess.run(["xdotool", "search", "--onlyvisible", "--pid", str(rw.pid)],
                              env=xenv, capture_output=True, text=True).stdout.split()
        for w in wins:
            subprocess.run(["xdotool", "windowmove", w, "0", "0"], env=xenv, check=False)
            subprocess.run(["xdotool", "windowsize", w, "1920", "1080"], env=xenv, check=False)
        log["sync_at_frame"] = tee.tags().index(V.TAG_SYNC)
        start = args.console_log.stat().st_size
        log["stimulus_sent_unix"] = time.time()
        console_send(args.console, f"cd {args.board_cwd} && python3 -u -m "
                                   f"sw.stream.testbench.edge4_live_stimulus {args.phase}")
        txt = wait_console(args.console_log, start, END_MARK.encode(), 180)
        body = txt.split(BEGIN_MARK.encode(), 1)[1].split(END_MARK.encode(), 1)[0]
        stim = json.loads(re.sub(rb"\s+", b"", body).decode())
        (out / "stimulus.json").write_text(json.dumps(stim, indent=1) + "\n")
        time.sleep(4.0)                   # let the daemon drain the last edges
        log["stream_status"] = json.loads(wcp(args.wcp_port, "stream", "status --json") or "{}")
        wcp(args.wcp_port, "stream", "auto-follow off")
        bounds = wcp(args.wcp_port, "bounds").split()
        log["bounds"] = bounds
        lo, hi = 0, int(bounds[1])
        log["export"] = wcp(args.wcp_port, "export", f"{(out / 'routewave.vcd').resolve()} {lo} {hi}")
        lanes = V.parse_vcd((out / "routewave.vcd").read_text())
        log["lanes"] = [{"name": ln.name, "code": ln.code, "transitions": len(ln.transitions)}
                        for ln in lanes]
        log["edges_cmd"] = {ln.name: wcp(args.wcp_port, "edges", ln.name)
                            for ln in lanes if ln.transitions}
        with tee.lock:
            frames = list(tee.frames)
            raw = bytes(tee.raw)
        helo = next((p for t, p in frames if int(t) == V.TAG_HELO), {})
        (out / "wire.json").write_text(json.dumps({"helo": helo, "captured": frames}) + "\n")
        (out / "wire.msgpack").write_bytes(raw)

        # Zoom targets from what RouteWave drew: CS lane falls anchor them.
        allt = sorted(t for ln in lanes for t, _ in ln.transitions)
        cs_dev = next(int(d) for d, line in stim["edge_lines"].items() if line == "cs")
        cs_lane, _ = V.lane_for(cs_dev, lanes, V.helo_names(helo))
        falls = [t for t, v in (cs_lane.transitions if cs_lane else []) if v == 0] or allt[:1]
        targets = [("01_full", None)]
        if args.phase == "dense":
            mid = falls[len(falls) // 2]
            targets += [("02_mid_3windows", (mid - 98_304_000, mid + 98_304_000)),
                        ("03_edge_level_one_transfer", (mid - 2_000, mid + 40_000))]
        else:
            f0 = falls[0]
            d = int(stim["clk_div"]) * 10
            targets += [("02_mid_one_transfer", (f0 - d, f0 + 19 * d)),
                        ("03_edge_level_sclk_fall", (f0 + 3 * d - 150, f0 + 3 * d + 250))]
        log["zoom"] = {}
        for name, rng in targets:
            if rng is None:
                wcp(args.wcp_port, "zoom", "fit")
            else:
                wcp(args.wcp_port, "zoom", f"{rng[0]} {rng[1]}")
            log["zoom"][name] = wcp(args.wcp_port, "view", "get")
            shot(out / f"{args.phase}_{name}.png")
        log["first_last_drawn_ns"] = [allt[0], allt[-1]] if allt else []
    finally:
        tee.stop.set()
        rw.terminate()
        try:
            rw.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rw.kill()
        xvfb.terminate()
        (out / "session.json").write_text(json.dumps(log, indent=1, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
