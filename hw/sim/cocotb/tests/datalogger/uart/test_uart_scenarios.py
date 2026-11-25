# rr unknown-llm (AI-generated)
"""DS-T2.31 — comprehensive UART scenario campaign.

Demonstrates, in ONE sim, that the UART datalogger:
  * has NO REGRESSION on the explicit-format path across the full matrix
    (data bits 5-8 x parity none/even/odd x stop 1/2), and
  * AUTO-CALIBRATES (autoSync) on the common formats,
with the per-datalogger reset (DS-T2.31) flushing state BETWEEN every scenario —
so the matrix runs back-to-back in a single elaboration with no cross-scenario
contamination. Each scenario follows the sanctioned arm sequence:
    RESET (ENABLE=0) -> ENABLE=1 (+format) -> THEN drive traffic
(reset-before-enable + enable-before-traffic; the datalogger is never 'late to the
party' — UART-REQ-15/16).

Run:  rm -rf hw/sim/work && rr sim run test_uart_scenarios
"""
import sys
import random
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles

current_dir = Path(__file__).resolve().parent
import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation
from sdk.cocotb_helpers import requires
from uart_datalogger_test_utils import (
    GENERICS, CUSTOM_LIBRARIES, setup_dut,
    FastSignalCollector, UartDataloggerMsgReassembler,
)
from rr_cocotb_tb.drivers.uart_master import UartSource

BAUD = 230_400               # ~54 prescaled ticks/bit: good sampling precision
# (1 Mbaud gives only ~12 ticks/bit -> ±1-tick quantization accumulates to
#  ~0.8 bit by the stop position -> mis-sample. The prescaler bounds the usable
#  baud; high-baud decode degradation is characterised separately, not here.)
DEV_ID = GENERICS["G_UART_DEV_ID"]

BITS = [5, 6, 7, 8]
PARITY = ["none", "even", "odd"]
STOP = [1, 2]
# autoSync RECOVER is hard-asserted only for these (the common, realistic
# formats); other cells are recorded for visibility (format-detect coverage).
AUTOSYNC_MUST = {(8, "none", 1), (8, "even", 1), (8, "odd", 1),
                 (7, "none", 1), (7, "even", 1)}


def cfg_word(payload):
    return ((DEV_ID & 0x1F) << 11) | (payload & 0x7FF)


def explicit_payload(bits, parity, stop):
    pen = 0 if parity == "none" else 1
    ptype = 1 if parity == "odd" else 0       # 0=even, 1=odd
    stop2 = 1 if stop == 2 else 0
    return (1 | (0 << 1) | ((bits - 5) << 2) | (pen << 5) | (ptype << 6) | (stop2 << 7))


async def write_cfg(dut, payload):
    dut.CONFIG.value = (DEV_ID & 0x1F) << 11      # ID-only prime
    await ClockCycles(dut.CLK, 3)
    dut.CONFIG.value = cfg_word(payload)
    await ClockCycles(dut.CLK, 6)


async def arm(dut, payload):
    """Sanctioned sequence: RESET (ENABLE=0) -> ENABLE=1 (+format)."""
    await write_cfg(dut, 0x00)                     # per-datalogger reset
    await ClockCycles(dut.CLK, 40)
    await write_cfg(dut, payload)                  # enable + format
    await ClockCycles(dut.CLK, 40)


async def drive_scenario(dut, bits, parity, stop, payload_cfg):
    await arm(dut, payload_cfg)                    # arm BEFORE traffic
    reasm = UartDataloggerMsgReassembler(bits, dut._log)
    coro = cocotb.start_soon(FastSignalCollector(
        dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT,
        value_fn=lambda v: reasm.push(v)).start())
    src = UartSource(dut.UART_IN, baud=BAUD, bits=bits, stop_bits=stop, parity=parity)

    mask = (1 << bits) - 1
    rng = random.Random(0x5EED + bits * 7 + len(parity) + stop)
    await src.write_bytes([rng.randint(0, mask) for _ in range(40)])   # cal
    done = False
    for _ in range(300):
        if int(dut.calibrationDone_r.value) == 1:
            done = True
            break
        await ClockCycles(dut.CLK, 40)
    # payload that exercises the top bit so width is unambiguous
    payload = bytes([mask, 0x01, mask >> 1, 1 << (bits - 1), 0x02, mask ^ 0x01,
                     0x03, mask])
    await src.write_bytes(list(payload))
    # Drain/flush window. The playback decode latency scales INVERSELY with the
    # tick prescaler (DS-T2.40: prescaler 8 -> 4 doubled the playback cycles per
    # byte), so this must comfortably cover the slowest-prescaler decode of the
    # trailing bytes — 20000 covers prescaler 4 @230400 with margin (was 3000,
    # tuned for prescaler 8, which truncated recovery and showed false FAILs).
    await ClockCycles(dut.CLK, 20000)
    coro.kill()
    got = bytes(reasm.get_results())
    # trapped-final-byte: check payload minus last byte
    recovered = payload[:-1] in got
    return done, recovered, len(got)


# DS-P3.222: one cocotb test per format. Each UartSource arms a strict
# protocol monitor on UART_IN for its own format, and that monitor lives until
# the test ends (rr_cocotb_tb has no way to retire it, RTL-P2.2215). In one
# test that walked the whole matrix, the earlier formats' monitors failed the
# later formats' frames ("stop bit low"). Per-format tests keep strict
# checking on, with the same hard gates per cell; each logs its matrix row.
@requires("UART-REQ-15")
@cocotb.test()
@cocotb.parametrize(bits=BITS, parity=PARITY, stop=STOP)
async def uart_scenarios(dut, bits, parity, stop):
    await setup_dut(dut)
    fmt = f"{bits}{parity[0].upper()}{stop}"
    # EXPLICIT (no-regression): tell the datalogger the format.
    ed, er, en = await drive_scenario(dut, bits, parity, stop,
                                       explicit_payload(bits, parity, stop))
    # AUTOSYNC (autocalibration): let it detect the format.
    ad, ar, an = await drive_scenario(dut, bits, parity, stop, 0x03)
    dut._log.info(f"UART SCENARIO MATRIX [{fmt}] explicit={'REC' if er else 'FAIL'} "
                  f"autoSync={'REC' if ar else ('CAL' if ad else 'SILENT')} "
                  f"calDone={ed}/{ad}")
    # Hard gates: NO REGRESSION on explicit across the whole matrix, and
    # autoSync MUST work for the common formats.
    assert er, f"explicit-format REGRESSION on: {fmt}"
    if (bits, parity, stop) in AUTOSYNC_MUST:
        assert ar, f"autoSync failed on must-pass format: {fmt}"


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_scenarios",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
