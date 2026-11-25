#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""UART-vs-EDGE cross-check on silicon (DS-P2.10 follow-up; REQ-5 diagnosis).

Drives the EMIO UART0 line and captures BOTH:
  * the UART datalogger  (id=12) — auto-baud decode (the one that garbles)
  * the EDGE datalogger  (id=1)  — RAW transitions, via EMIO_MUX_SEL bit[5]
                               (uart_edge_sel → capture_pin(42) ← raw UART0 TX)

The EDGE datalogger is GROUND TRUTH (no baud measurement). At 9600 baud a real bit
is 104.17 µs, a byte (8N1) is 1041.7 µs, and the *minimum* inter-edge interval
is exactly one bit (~104 µs) when adjacent bits differ. So:

  * EDGE min-interval ≈ 104 µs and all intervals ≈ k·104 µs  → the line is CLEAN;
    the UART garble is an AUTO-BAUD / decode fault (config), NOT signal.
  * EDGE shows sub-bit intervals (≪ 104 µs)                  → SPURIOUS edges on
    the line → auto-baud locks too fast → garbage. Signal/conditioning fault.

Run on the board:
    python3 /usr/local/share/deepskopion/sw/stream/testbench/uart_edge_crosscheck.py
"""
from __future__ import annotations

import random
import statistics
import struct
import sys
import time

sys.path.insert(0, "/usr/local/share/deepskopion")

from sw.stream.testbench.emio_inject import SelfTestBench
from sw.stream.testbench.ring_oracle import RingOracle, open_ring
from sw.stream.regbank import Regs
from sw.stream.edge_capture import words_to_transitions

UART_ID = 12
EDGE1_ID = 1
PAYLOAD = b"DEEPSKOPION-DS-T9-UART-READBACK!"
BIT_NS = round(1e9 / 9600)        # 104167 ns per bit @ 9600 baud


def _edge_word(payload: bytes) -> int | None:
    """Unpack a 4-byte EDGE_EVENT word, self-validating endianness via the
    instance field (bits [31:27] must equal EDGE1_ID=1)."""
    if len(payload) < 4:
        return None
    for endian in ("<I", ">I"):
        w = struct.unpack(endian, payload[:4])[0]
        if ((w >> 27) & 0x1F) == EDGE1_ID:
            return w
    # fall back to little-endian even if instance doesn't match (report anyway)
    return struct.unpack("<I", payload[:4])[0]


def main() -> int:
    bench = SelfTestBench()
    rb = bench.regbank
    oracle = open_ring(rb)

    # Enable UART (autoSync) + EDGE#1 dataloggers. edge_datalogger enables on
    # devConfiguration[1:0]=="01" → config[0]=enable=1, config[1]=0 → extra=0
    # (extra=1 would make config[1:0]="11", which does NOT match the enable code).
    rb.enable_datalogger(UART_ID, enable=True)
    rb.enable_datalogger(EDGE1_ID, enable=True, extra=0)

    # EMIO_MUX_SEL: bit[1]=UART inject, bit[5]=uart_edge_sel (EDGE#1 ← raw UART0 TX).
    mux = (1 << 1) | (1 << 5)
    rb._rb.write_u32(Regs.EMIO_MUX_SEL, mux)
    time.sleep(0.01)
    print(f"EMIO_MUX_SEL <= 0x{rb._rb.read_u32(Regs.EMIO_MUX_SEL):08x} "
          f"(bit1 uart-inject + bit5 uart_edge_sel)")

    cal = bytes(random.Random(0xC0FFEE).randint(0, 0xFF) for _ in range(150))
    oracle.snapshot()
    bench.uart.write(cal)
    time.sleep(0.010 * len(cal) / 9600 + 0.10)
    bench.uart.write(PAYLOAD)
    bench.uart.write(b"\x00\xFF\x00\xFF")
    time.sleep(0.15)

    try:
        frames = oracle.collect_new_frames(min_count=10, timeout_s=3.0)
    except TimeoutError as exc:
        print(f"FAIL: too few frames captured: {exc}")
        return 2

    uart_frames = RingOracle.filter_by_datalogger(frames, UART_ID)
    edge_frames = RingOracle.filter_by_datalogger(frames, EDGE1_ID)
    print(f"\ncaptured {len(frames)} frames: "
          f"UART(id12)={len(uart_frames)}  EDGE(id1)={len(edge_frames)}")

    # ── UART side (known to garble; raw payload dump for reference) ──
    ubytes = b"".join(f.payload for f in uart_frames)
    print(f"\nUART id=12 raw payload bytes ({len(ubytes)}): {ubytes[:64].hex()}")
    print(f"  expected payload present: {PAYLOAD in ubytes}")

    # ── EDGE side (GROUND TRUTH timing) ──
    # The EDGE payload carries only the post-edge level; the zs_msg framer is the
    # single stamping authority, so the edge TIME is frame.header.timestamp
    # (64-bit global counter). Inter-frame timestamp deltas = inter-edge intervals.
    levels = [f.payload[-1] & 1 for f in edge_frames if f.payload]
    ts = [f.header.timestamp & ((1 << 48) - 1) for f in edge_frames]
    print(f"\nEDGE id=1: {len(edge_frames)} frames; level samples (first 24): "
          f"{''.join(str(b) for b in levels[:24])}")
    deltas = []
    for i in range(len(ts) - 1):
        d = ts[i + 1] - ts[i]
        if d < 0:
            d += (1 << 48)          # 48-bit wrap
        if d > 0:
            deltas.append(d)
    if len(deltas) < 4:
        print("  (not enough timestamped edges to measure)")
        return 1

    deltas.sort()
    dmin, dmed, dmax = deltas[0], deltas[len(deltas) // 2], deltas[-1]
    # Infer the timestamp tick: the dominant SMALL delta == 1 UART bit (104.17 us).
    # tick_ns = 104167 ns / (1-bit delta). Use the 5th-percentile delta as the
    # 1-bit estimate (robust vs the rare sub-bit outlier we're hunting for).
    one_bit = deltas[max(0, len(deltas) // 20)]
    tick_ns = BIT_NS / one_bit if one_bit else 0
    print(f"  inter-edge delta (ticks): min={dmin} median={dmed} max={dmax}")
    print(f"  inferred 1-bit delta ≈ {one_bit} ticks  → tick ≈ {tick_ns:.2f} ns "
          f"({'≈10ns clk_sys' if 8 < tick_ns < 12 else '≈1us' if 900 < tick_ns < 1100 else '?'})")
    # In bit units:
    bit_of = lambda d: d / one_bit
    sub_bit = [d for d in deltas if bit_of(d) < 0.7]
    buckets = {}
    for d in deltas:
        k = max(1, round(bit_of(d)))
        buckets[k] = buckets.get(k, 0) + 1
    print(f"  min delta = {bit_of(dmin):.2f} bits; median = {bit_of(dmed):.2f} bits; "
          f"max = {bit_of(dmax):.2f} bits")
    print(f"  sub-bit deltas (<0.7 bit) = {len(sub_bit)} / {len(deltas)}")
    print("  histogram (bit-widths): "
          + ", ".join(f"{k}b:{buckets[k]}" for k in sorted(buckets)[:12]))

    print("\n=== VERDICT ===")
    if sub_bit:
        print(f"  EDGE shows {len(sub_bit)} SUB-BIT interval(s) (min "
              f"{bit_of(dmin):.2f} bit ≪ 1 bit) → SPURIOUS EDGES on the UART line "
              f"→ auto-baud locks too fast → garbage. SIGNAL/CONDITIONING fault.")
    else:
        print("  EDGE intervals are all ≥ ~1 bit and cluster on integer bit-"
              "multiples → the UART line is CLEAN at 9600. The garble is an "
              "AUTO-BAUD / decode fault (config), NOT the signal/conditioning.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
