#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""On-silicon UART baud sweep (DS-T2.30).

Drives the EMIO UART0 line at several baud rates and reports whether the
uart_datalogger (auto-baud) decodes a known payload at each. Discriminates two
hypotheses for the REQ-5 garble:

  * relativeDistCnt wrap bug (cap compares prescaled ticks against a RAW-cycle
    constant, so the 15-bit counter wraps instead of saturating): BAUD-DEPENDENT
    — fails at LOW baud (long tick-distances wrap), works at HIGH baud.
  * stale calibration FIFO between enables: BAUD-INDEPENDENT.

For each baud: re-enable the UART datalogger fresh, snapshot the ring, drive a
150-byte random cal + the payload + flush, then search the captured id=12 bytes
for the payload. Also reports the EDGE#1 (raw-line) min inter-edge interval as a
ground-truth sanity check that the line itself is clean at that baud.
"""
from __future__ import annotations

import random
import sys
import time

import serial  # pyserial

sys.path.insert(0, "/usr/local/share/deepskopion")

from sw.stream.testbench.emio_inject import SelfTestBench, emio_uart_dev
from sw.stream.testbench.ring_oracle import RingOracle, open_ring
from sw.stream.regbank import Regs

UART_ID = 12
EDGE1_ID = 1
PAYLOAD = b"DEEPSKOPION-DS-T9-UART-READBACK!"
BAUDS = [9600, 19200, 38400, 57600, 115200]
# The EMIO UART0 tty, found by its base address (DS-P1.105): the ttyPS
# number of the test wire depends on the device-tree generator.
DEV = None


def run_one(bench, oracle, baud: int) -> dict:
    rb = bench.regbank
    # Fresh enable of UART + EDGE#1; mux bit1 (uart inject) + bit5 (edge<-uart).
    rb.enable_datalogger(UART_ID, enable=False)
    time.sleep(0.005)
    rb.enable_datalogger(UART_ID, enable=True)
    rb.enable_datalogger(EDGE1_ID, enable=True, extra=0)
    rb._rb.write_u32(Regs.EMIO_MUX_SEL, (1 << 1) | (1 << 5))
    time.sleep(0.01)

    ser = serial.Serial(DEV or emio_uart_dev(), baudrate=baud, bytesize=8, parity="N", stopbits=1, timeout=0.2)
    cal = bytes(random.Random(0xC0FFEE).randint(0, 0xFF) for _ in range(150))
    oracle.snapshot()
    ser.write(cal)
    ser.flush()
    time.sleep(0.010 * len(cal) / baud + 0.10)
    ser.write(PAYLOAD)
    ser.write(b"\x00\xFF\x00\xFF")
    ser.flush()
    time.sleep(0.15 + 12.0 * len(PAYLOAD) / baud)
    ser.close()

    try:
        frames = oracle.collect_new_frames(min_count=10, timeout_s=3.0)
    except TimeoutError:
        frames = []
    uart_frames = RingOracle.filter_by_datalogger(frames, UART_ID)
    edge_frames = RingOracle.filter_by_datalogger(frames, EDGE1_ID)
    ubytes = b"".join(f.payload for f in uart_frames)

    # EDGE ground-truth: min inter-edge interval (clk ticks) — should be ~1 bit.
    ts = [f.header.timestamp & ((1 << 48) - 1) for f in edge_frames]
    deltas = sorted(d for d in (ts[i + 1] - ts[i] for i in range(len(ts) - 1)) if d > 0)
    one_bit_clk = round(1e9 / baud / 10)   # 1 bit in 10ns clk ticks
    edge_min = deltas[0] if deltas else None
    return {
        "baud": baud,
        "uart_frames": len(uart_frames),
        "edge_frames": len(edge_frames),
        "payload_found": PAYLOAD in ubytes,
        "edge_min_ticks": edge_min,
        "expected_bit_ticks": one_bit_clk,
        "sample": ubytes[:24].hex(),
    }


def main() -> int:
    bench = SelfTestBench()
    oracle = open_ring(bench.regbank)
    print(f"{'baud':>7} | {'decode':>7} | {'uart_fr':>7} | {'edge_fr':>7} | "
          f"{'edge_min':>9} | {'1bit_clk':>8} | sample")
    print("-" * 78)
    results = []
    for baud in BAUDS:
        r = run_one(bench, oracle, baud)
        results.append(r)
        print(f"{r['baud']:>7} | {'PASS' if r['payload_found'] else 'GARBLE':>7} | "
              f"{r['uart_frames']:>7} | {r['edge_frames']:>7} | "
              f"{str(r['edge_min_ticks']):>9} | {r['expected_bit_ticks']:>8} | {r['sample']}")
    print("\n=== VERDICT ===")
    passed = [r['baud'] for r in results if r['payload_found']]
    failed = [r['baud'] for r in results if not r['payload_found']]
    print(f"  decode OK at: {passed}")
    print(f"  garble at:    {failed}")
    if failed and passed and max(failed) < min(passed):
        print("  → BAUD-DEPENDENT (low fails, high works) ⇒ relativeDistCnt wrap bug "
              "(tick-counter vs raw-cycle cap). EDGE min ≈ 1 bit everywhere = line clean.")
    elif failed and not passed:
        print("  → ALL bauds garble ⇒ not baud-dependent; suspect stale FIFO / "
              "calibration state (or a deeper decode bug).")
    elif not failed:
        print("  → all bauds decode — could not reproduce on this run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
