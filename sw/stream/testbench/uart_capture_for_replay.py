#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Capture the REAL silicon UART waveform to an edge-capture file for sim replay
(DS-T2.30).

Drives EMIO UART0 (cal + payload) while EDGE#1 (id=1) streams the raw line
(EMIO_MUX_SEL bit[5] uart_edge_sel). For each EDGE frame we reconstruct an
EDGE_EVENT word from the frame's global timestamp (clk_sys ticks, 10 ns) and the
payload level bit, then write the word stream as a deepskopion-edge-capture/v1
JSONL file. That file is replayed into the bare uart_datalogger in cocotb
(test_uart_replay_silicon) to reproduce the HW auto-baud garble deterministically.

Also prints the concurrent UART#12 decode so we can confirm THIS capture is one
that garbles on silicon.

Out: /tmp/uart_silicon_capture.jsonl   (pull to host, replay in sim)
"""
from __future__ import annotations

import random
import sys
import time

sys.path.insert(0, "/usr/local/share/deepskopion")

from sw.stream.testbench.emio_inject import SelfTestBench
from sw.stream.testbench.ring_oracle import RingOracle, open_ring
from sw.stream.regbank import Regs
from sw.stream.edge_capture import EdgeCaptureWriter, encode_edge_word

UART_ID = 12
EDGE1_ID = 1
PAYLOAD = b"DEEPSKOPION-DS-T9-UART-READBACK!"
OUT = "/tmp/uart_silicon_capture.jsonl"


def main() -> int:
    bench = SelfTestBench()
    rb = bench.regbank
    oracle = open_ring(rb)

    rb.enable_datalogger(UART_ID, enable=True)
    rb.enable_datalogger(EDGE1_ID, enable=True, extra=0)
    rb._rb.write_u32(Regs.EMIO_MUX_SEL, (1 << 1) | (1 << 5))
    time.sleep(0.01)

    cal = bytes(random.Random(0xC0FFEE).randint(0, 0xFF) for _ in range(150))
    oracle.snapshot()
    bench.uart.write(cal)
    time.sleep(0.010 * len(cal) / 9600 + 0.10)
    bench.uart.write(PAYLOAD)
    bench.uart.write(b"\x00\xFF\x00\xFF")
    time.sleep(0.15)

    frames = oracle.collect_new_frames(min_count=10, timeout_s=3.0)
    uart_frames = RingOracle.filter_by_datalogger(frames, UART_ID)
    edge_frames = RingOracle.filter_by_datalogger(frames, EDGE1_ID)

    ubytes = b"".join(f.payload for f in uart_frames)
    print(f"UART#12 decode: {len(uart_frames)} frames; payload_found={PAYLOAD in ubytes}; "
          f"sample={ubytes[:32].hex()}")
    print(f"EDGE#1: {len(edge_frames)} frames → writing EDGE_EVENT words to {OUT}")

    # Reconstruct EDGE_EVENT words from (header.timestamp in 10ns ticks, level).
    # The framer timestamp is the authoritative edge time; the EDGE payload only
    # carries the post-edge level (last byte LSB).
    words = []
    for f in edge_frames:
        if not f.payload:
            continue
        ts_ticks = f.header.timestamp & ((1 << 48) - 1)
        ts_ns = ts_ticks * 10           # clk_sys = 100 MHz → 10 ns/tick
        # The EDGE frame payload's LSB is the INVERSE of the true post-edge line
        # level (verified by host-decoding the capture: it only recovers the
        # payload with inverted levels). Store the corrected level so the replay
        # drives the real (idle-high) UART waveform, not its inverse.
        level = 1 - (f.payload[-1] & 1)
        words.append(encode_edge_word(EDGE1_ID, ts_ns, level, clk_mhz=100))

    with EdgeCaptureWriter(OUT, clk_mhz=100,
                           meta={"src": "silicon", "git": "0x95647D67",
                                 "baud": 9600, "fixture": "uart-8n1-cal+payload",
                                 "payload": PAYLOAD.decode()}) as w:
        # chunk so JSONL stays readable
        for i in range(0, len(words), 64):
            w.append(words[i:i + 64])
        total = w.total_words
    print(f"✅ wrote {total} edge words to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
