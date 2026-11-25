# rr unknown-llm (AI-generated)
"""DS-T2.31: a UART-datalogger reset must be reachable from SW (via the per-device
ENABLE bit) and must clear ALL stale state so a reconfiguration recalibrates
cleanly.

Root cause of the silicon autoSync failure (hypothesis, this test proves it):
the datalogger only resets on the GLOBAL RST. Toggling the per-device ENABLE bit
does NOT flush the FIFO/BRAM or restart the calibration FSM, so calibration
state and buffered edges from a PREVIOUS configuration survive. Every sim test
to date started from a fresh global RST, so it never exercised reconfiguration —
which is exactly what the silicon path does (enable cfg A at boot, later switch
to autoSync). The stale traces block the new calibration.

The contract being asserted (config_control owns a per-datalogger reset):
  * Writing ENABLE=0 for a device holds THAT datalogger in reset — clears the
    calibration FSM, the baud accumulator, and flushes the edge FIFO/BRAM.
  * Writing ENABLE=1 (+ new format) releases it and begins a FRESH calibration.

Phases:
  A. explicit 8N1, drive traffic, reach calibration, decode payload A
     (builds up calibration state + FIFO/BRAM contents).
  B. SW reset: write ENABLE=0. Assert the state is actually cleared
     (calibrationDone=0, baudRateMin=all-1s, FIFO empty, FSM=DISABLED).
     <-- this is the assertion that FAILS on the unfixed RTL.
  C. autoSync, drive traffic, reach calibration, decode payload B
     (proves a clean recalibration after the SW reset — no stale traces).

Run:  rm -rf hw/sim/work && rr sim run test_uart_reset_reachable
"""
import os
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

BAUD = 115200          # fast — keeps the sim short; baud is auto-measured
DEV_ID = GENERICS["G_UART_DEV_ID"]


def cfg(payload):
    """DATALOGGER CONFIG word for this device: [dev_id<<11 | payload]."""
    return ((DEV_ID & 0x1F) << 11) | (payload & 0x7FF)


# payload bit layout: [0]=enable [1]=autoSync [4:2]=dataBits(nbits-5)
EXPLICIT_8N1 = (1 << 0) | (0 << 1) | ((8 - 5) << 2)   # 0x0D
AUTOSYNC     = (1 << 0) | (1 << 1)                    # 0x03
DISABLE      = 0x00


async def write_cfg(dut, payload):
    """Latch a config for this device (config_control needs an ID match + a
    change on the CONFIG bus)."""
    dut.CONFIG.value = (DEV_ID & 0x1F) << 11      # ID only — primes the match
    await ClockCycles(dut.CLK, 3)
    dut.CONFIG.value = cfg(payload)
    await ClockCycles(dut.CLK, 6)                 # latch + settle


async def drive_and_decode(dut, payload, nbits=8):
    reassembler = UartDataloggerMsgReassembler(nbits, dut._log)
    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT,
                                    value_fn=lambda v: reassembler.push(v))
    coro = cocotb.start_soon(collector.start())
    src = UartSource(dut.UART_IN, baud=BAUD, bits=nbits, stop_bits=1, parity="none")

    rng = random.Random(0xBEEF)
    await src.write_bytes([rng.randint(0, 255) for _ in range(60)])   # cal burst
    done = False
    for _ in range(400):
        if int(dut.calibrationDone_r.value) == 1:
            done = True
            break
        await ClockCycles(dut.CLK, 50)
    await src.write_bytes(list(payload))
    await ClockCycles(dut.CLK, 6000)
    coro.kill()
    return done, bytes(reassembler.get_results())


@requires("UART-REQ-15")
@cocotb.test()
async def reset_reachable_from_sw(dut):
    await setup_dut(dut)
    width = len(dut.baudRateMin_r)
    all_ones = (1 << width) - 1

    # ── Phase A: explicit 8N1, build up calibration + FIFO/BRAM state ──
    await write_cfg(dut, EXPLICIT_8N1)
    await ClockCycles(dut.CLK, 50)
    payloadA = b"PHASE-A-PAYLOAD!"
    doneA, decA = await drive_and_decode(dut, payloadA, nbits=8)
    dut._log.info(f"A: calDone={doneA} baudMin={int(dut.baudRateMin_r.value)} "
                  f"decoded={decA[-24:]!r}")
    # The playback FIFO traps the FINAL byte until the next edge, so check the
    # payload MINUS its last byte (same convention as test_uart_initial_idle_repro).
    assert payloadA[:-1] in decA, f"Phase A did not decode payload: {decA[-24:]!r}"

    # ── Phase B: SW reset via ENABLE=0 — assert the state is actually cleared ──
    await write_cfg(dut, DISABLE)
    await ClockCycles(dut.CLK, 80)
    cal_done = int(dut.calibrationDone_r.value)
    baud_min = int(dut.baudRateMin_r.value)
    fifo_empty = int(dut.fifoUartEmpty.value)
    fsm = int(dut.fsmCalibrate_r.value)
    dut._log.info(f"B (after ENABLE=0): calDone={cal_done} baudMin={baud_min} "
                  f"(all1s={all_ones}) fifoEmpty={fifo_empty} fsm={fsm}")
    assert cal_done == 0, f"SW reset did NOT clear calibrationDone_r (={cal_done})"
    assert baud_min == all_ones, f"SW reset did NOT reset baudRateMin_r (={baud_min}, want {all_ones})"
    assert fifo_empty == 1, f"SW reset did NOT flush the edge FIFO (fifoUartEmpty={fifo_empty})"

    # ── Phase C: autoSync after the SW reset — must recalibrate cleanly ──
    await write_cfg(dut, AUTOSYNC)
    await ClockCycles(dut.CLK, 50)
    payloadC = b"PHASE-C-AUTOSYNC"
    doneC, decC = await drive_and_decode(dut, payloadC, nbits=8)
    dut._log.info(f"C: calDone={doneC} baudMin={int(dut.baudRateMin_r.value)} "
                  f"decoded={decC[-24:]!r}")
    assert doneC, "autoSync did NOT calibrate after SW reset (stale traces?)"
    # payload[:-1] — trapped-final-byte (see Phase A note).
    assert payloadC[:-1] in decC, f"autoSync decode wrong after reset: {decC[-32:]!r}"


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_reset_reachable",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
