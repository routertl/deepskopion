# rr unknown-llm (AI-generated)
"""DS-T2.30 repro: does a long initial idle before the cal burst corrupt the
uart_datalogger auto-baud (baudRateMin)?

HW REQ-5 garbles at EVERY baud on a provably-clean line; the key sim-vs-HW
difference is that the HW burst is preceded by a long idle (EMIO mux engage →
first byte ≈ ms), whereas the sim UartSource drives tight back-to-back bytes.

baudRateMin inits to all-1s, so the FIRST captured edge-distance always becomes
the running minimum. relativeDistCnt_r is only C_DIST_WIDTH (15) bits and counts
PRESCALED ticks (every G_TICK_PRESCALER=8 clks), so it WRAPS at 32768 ticks
(= 262144 clks = 2.62 ms) instead of saturating — the cap compares against a
RAW-cycle constant it can never reach. A long initial idle can therefore wrap to
a value SMALLER than a real bit, locking baudRateMin too small (real bits are
larger, so it never recovers) → baud too fast → garbage decode.

This test is FULL decode-verify (not just calibrationDone) at 1 Mbaud (fast).
INITIAL_IDLE_US (env) sets the pre-cal idle. Sweep a few values:
  rr sim run test_uart_initial_idle_repro            # idle=0 (control, expect PASS)
  INITIAL_IDLE_US=2621 rr sim run test_uart_initial_idle_repro   # wraps to ~0
  INITIAL_IDLE_US=5000 rr sim run test_uart_initial_idle_repro   # 5 ms (HW-like)
"""

import os
import sys
import random
from pathlib import Path

import cocotb
from cocotb.triggers import Timer, ClockCycles

current_dir = Path(__file__).resolve().parent
import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    GENERICS, CUSTOM_LIBRARIES, setup_dut, apply_config,
    FastSignalCollector, UartDataloggerMsgReassembler,
)
from rr_cocotb_tb.drivers.uart_master import UartSource

# Defaults make a bare run a DS-T2.30 regression: 9600 (prescaler-exact, no
# rounding) with a 3 ms initial idle that WRAPS the 15-bit relativeDistCnt
# (wraps at ~2.62 ms regardless of baud) — the exact condition that garbled
# before the prescaled-tick saturation fix. Env-overridable for sweeps.
BAUD_RATE = int(os.environ.get("BAUD", "9600"))
DATA_BITS = 8
PARITY = "none"
STOP_BITS = 1
CAL_BYTES = int(os.environ.get("CAL", "30"))
PAYLOAD_BYTES = 32
INITIAL_IDLE_US = float(os.environ.get("INITIAL_IDLE_US", "3000"))


AUTOSYNC = int(os.environ.get("AUTOSYNC", "1"))


def _cfg_word(dev_id, *, auto_sync, nbits=8):
    payload = (1 | ((auto_sync & 1) << 1) | (((nbits - 5) & 0x7) << 2))
    return ((dev_id & 0x1F) << 11) | (payload & 0x7FF)


@cocotb.test()
async def test_initial_idle(dut):
    await setup_dut(dut)
    dev_id = GENERICS["G_UART_DEV_ID"]
    if AUTOSYNC:
        await apply_config(dut, dev_id)          # autoSync=1 (full auto)
    else:
        word = _cfg_word(dev_id, auto_sync=0, nbits=8)   # explicit 8N1
        dut.CONFIG.value = (dev_id & 0x1F) << 11
        await ClockCycles(dut.CLK, 3)
        dut.CONFIG.value = word
        await ClockCycles(dut.CLK, 5)
    dut._log.info(f"config: autoSync={AUTOSYNC}")

    reassembler = UartDataloggerMsgReassembler(DATA_BITS, dut._log)
    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT,
                                    value_fn=lambda v: reassembler.push(v))
    collector_coro = cocotb.start_soon(collector.start())

    uart_source = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=DATA_BITS,
                             stop_bits=STOP_BITS, parity=PARITY)

    # ── The HW-faithful bit: a long IDLE (line high) before any traffic ──
    dut.UART_IN.value = 1
    if INITIAL_IDLE_US > 0:
        dut._log.info(f"Initial idle: {INITIAL_IDLE_US} us (line high) before cal burst")
        await Timer(INITIAL_IDLE_US, units="us")
    await ClockCycles(dut.CLK, 100)

    cal_data = [random.randint(0, 255) for _ in range(CAL_BYTES)]
    await uart_source.write_bytes(cal_data)

    cal_done = False
    for _ in range(400):
        if int(dut.calibrationDone_r.value) == 1:
            cal_done = True
            break
        await ClockCycles(dut.CLK, 100)
    baud_min = int(dut.baudRateMin_r.value)
    def _rd(name):
        try:
            return int(getattr(dut, name).value)
        except Exception:
            return "?"
    # baudRateMin_r = 2604 ticks = one bit at 9600 baud (10417 clk / prescaler 4),
    # measured in the DS-P2.241 trace.
    dut._log.info(f"calibrationDone={cal_done} baudRateMin_r={baud_min} ticks "
                  f"calibrationFail_r={_rd('calibrationFail_r')} "
                  f"nbitsConf_r={_rd('nbitsConf_r')} stopIndex_r={_rd('stopIndex_r')} "
                  f"stopBitFound_r={_rd('stopBitFound_r')} "
                  f"(expect at 9600: baudRateMin_r=2604, nbits=8, fail=0)")

    # Payload sent AFTER calibrationDone; the trailing bytes of the decoded
    # stream must equal it (mirrors the proven test_integration_1M_8N1 — early
    # cal bytes are expected garbage until lock, so we check only the tail).
    # Optional MID-STREAM gap (line idle high) between cal and payload — models
    # the real silicon waveform's ~254-bit cal→payload gap (the PS sleep). The
    # initial-idle fix saturates the counter; this probes whether a long
    # POST-calibration gap is also handled.
    mid_gap = float(os.environ.get("MID_GAP_US", "3000"))   # default on: guards both gap facets
    if mid_gap > 0:
        dut._log.info(f"Mid-stream gap: {mid_gap} us (line high) before payload")
        dut.UART_IN.value = 1
        await Timer(mid_gap, units="us")

    payload = [random.randint(0, 255) for _ in range(PAYLOAD_BYTES)]
    await uart_source.write_bytes(payload)
    await ClockCycles(dut.CLK, 4000)
    collector_coro.kill()

    results = reassembler.get_results()
    decoded = bytes(results)
    # The datalogger's playback FIFO traps the FINAL byte of a stream until the next
    # edge, so the last payload byte is legitimately unrecovered — check that the
    # payload MINUS its last byte appears contiguously (robust to that + to the
    # leading trapped cal byte). A real garble (wrong baud/playback) fails this.
    needle = bytes(payload[:-1])
    match = needle in decoded
    dut._log.info(f"reassembled {len(results)} bytes; tail={decoded[-40:].hex()}")
    dut._log.info(f"idle={INITIAL_IDLE_US}us baudRateMin={baud_min} → payload "
                  f"{'MATCHES ✓' if match else 'MISMATCH ✗ (garble)'}")
    assert match, (
        f"DS-T2.30: payload not found with initial idle={INITIAL_IDLE_US}us "
        f"(baudRateMin={baud_min} ticks). needle={needle.hex()} "
        f"decoded_tail={decoded[-48:].hex()}"
    )


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_initial_idle_repro",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
