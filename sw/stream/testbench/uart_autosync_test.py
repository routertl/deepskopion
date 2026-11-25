#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-T2.30 root-cause confirmation: UART decodes when autoSync=1.

Hypothesis: REQ-5 garbles because enable_datalogger(12) sets config payload 0x01 =
{enable=1, autoSync=0, dataBits=0} → the datalogger decodes the 8N1 stream as
explicit 5N1 → garbage at any baud. The auto-baud RTL is fine; the config is
wrong. This drives the SAME cal+payload but enables the UART datalogger with
autoSync=1 (payload 0x03) and checks the decode.
"""
from __future__ import annotations
import random, sys, time
sys.path.insert(0, "/usr/local/share/deepskopion")
from sw.stream.testbench.emio_inject import SelfTestBench, encode_datalogger_config
from sw.stream.testbench.ring_oracle import RingOracle, open_ring
from sw.stream.regbank import Regs

UART_ID = 12
PAYLOAD = b"DEEPSKOPION-DS-T9-UART-READBACK!"


def drive_and_check(bench, oracle, autosync_extra: int, label: str) -> bool:
    rb = bench.regbank
    # Force a FRESH calibration: disable (configEnable=0 → FSM→DISABLED, resets
    # baudRateMin) then enable with the target config. Config changes alone don't
    # reset the calibration FSM (configEnable stays 1), so without this each run
    # inherits the previous run's stale calibration state.
    rb._rb.write_u32(Regs.DATALOGGER_CONFIG, encode_datalogger_config(UART_ID, enable=False, extra=0))
    time.sleep(0.02)
    # extra=0 → 0x01 (autoSync=0,5N1); extra=1 → 0x03 (autoSync=1); extra=6 → 0x0D (autoSync=0,8N1).
    word = encode_datalogger_config(UART_ID, enable=True, extra=autosync_extra)
    rb._rb.write_u32(Regs.DATALOGGER_CONFIG, word)
    rb.set_emio_mux(uart=True)
    time.sleep(0.02)
    cal = bytes(random.Random(0xC0FFEE).randint(0, 0xFF) for _ in range(150))
    oracle.snapshot()
    bench.uart.write(cal); time.sleep(0.010 * len(cal) / 9600 + 0.10)
    bench.uart.write(PAYLOAD); bench.uart.write(b"\x00\xFF\x00\xFF")
    time.sleep(0.15)
    try:
        frames = oracle.collect_new_frames(min_count=10, timeout_s=3.0)
    except TimeoutError:
        frames = []
    uf = RingOracle.filter_by_datalogger(frames, UART_ID)
    ubytes = b"".join(f.payload for f in uf)
    found = PAYLOAD in ubytes
    print(f"  {label}: config=0x{word:04x} → {len(uf)} frames, "
          f"payload {'FOUND ✓' if found else 'GARBLE ✗'}; sample={ubytes[:24].hex()}")
    return found


def main() -> int:
    bench = SelfTestBench()
    oracle = open_ring(bench.regbank)
    print("UART decode vs config word (cal+payload, 9600 8N1 on the wire):")
    a0 = drive_and_check(bench, oracle, 0, "0x6001 autoSync=0 dataBits=0 (5N1) = REQ-5 BUG")
    a1 = drive_and_check(bench, oracle, 1, "0x6003 autoSync=1 (full auto-format)")
    a8 = drive_and_check(bench, oracle, 6, "0x600D autoSync=0 dataBits=3 (explicit 8N1)")
    print("\n=== VERDICT ===")
    if a8 and not a0:
        print("  CONFIRMED: 0x6001 (5N1) garbles, 0x600D (explicit 8N1) DECODES → REQ-5 "
              "root cause is the CONFIG: enable_datalogger(12) writes 0x6001 (dataBits=0 → "
              "5N1) for an 8N1 stream. Fix = use explicit 8N1 (0x600D). Auto-baud RTL is "
              f"not the culprit. [autoSync=1/0x6003 → {'decodes' if a1 else 'no frames'}]")
    else:
        print(f"  a0(5N1)={a0} a1(auto)={a1} a8(8N1)={a8} — re-examine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
