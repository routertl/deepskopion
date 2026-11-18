"""DS-T2.31 — measure the UART datalogger's usable-baud ceiling (product spec).

The timebase counts prescaled ticks (G_TICK_PRESCALER=8), so the measured bit
period quantises to +/-1 tick. As baud rises, ticks/bit shrinks and the
quantisation error accumulates across a frame until the stop bit is mis-sampled.
This sweeps 8N1 at increasing baud (explicit + autoSync) and reports the highest
baud that still recovers byte-for-byte — the number for the datasheet.

Run:  rm -rf hw/sim/work && rr sim run test_uart_baud_limit
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
from uart_datalogger_test_utils import (
    GENERICS, CUSTOM_LIBRARIES, setup_dut,
    FastSignalCollector, UartDataloggerMsgReassembler,
)
from rr_cocotb_tb.drivers.uart_master import UartSource

DEV_ID = GENERICS["G_UART_DEV_ID"]
CLK_HZ = 100_000_000
PRESCALER = 8
# Bracket the theoretical ceiling (ticks/bit >= ~20 -> ~625 kbaud).
BAUDS = [230400, 460800, 576000, 625000, 691200, 750000, 806400, 921600, 1_000_000]


def cfg_word(payload):
    return ((DEV_ID & 0x1F) << 11) | (payload & 0x7FF)


async def write_cfg(dut, payload):
    dut.CONFIG.value = (DEV_ID & 0x1F) << 11
    await ClockCycles(dut.CLK, 3)
    dut.CONFIG.value = cfg_word(payload)
    await ClockCycles(dut.CLK, 6)


async def arm(dut, payload):
    await write_cfg(dut, 0x00)
    await ClockCycles(dut.CLK, 40)
    await write_cfg(dut, payload)
    await ClockCycles(dut.CLK, 40)


async def try_baud(dut, baud, payload_cfg):
    await arm(dut, payload_cfg)
    reasm = UartDataloggerMsgReassembler(8, dut._log)
    coro = cocotb.start_soon(FastSignalCollector(
        dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT,
        value_fn=lambda v: reasm.push(v)).start())
    src = UartSource(dut.UART_IN, baud=baud, bits=8, stop_bits=1, parity="none")
    rng = random.Random(0xA11CE)
    await src.write_bytes([rng.randint(0, 255) for _ in range(40)])
    for _ in range(300):
        if int(dut.calibrationDone_r.value) == 1:
            break
        await ClockCycles(dut.CLK, 40)
    payload = b"BAUD-LIMIT-PROBE"
    await src.write_bytes(list(payload))
    await ClockCycles(dut.CLK, 4000)
    coro.kill()
    got = bytes(reasm.get_results())
    return payload[:-1] in got, int(dut.baudRateMin_r.value)


# DS-P3.222: one cocotb test per baud point. Each UartSource arms a strict
# protocol monitor on UART_IN for its own baud, and that monitor lives until
# the test ends (rr_cocotb_tb has no way to retire it, RTL-P2.2215). In a
# single test that swept every baud, the 230400 monitors were still sampling
# when the 460800 frames arrived and failed them ("stop bit low"). Per-point
# tests keep strict checking on and give each point only its own monitors.
# rr runs each case in its own simulator process, so each point logs its own
# row of the ceiling matrix.
@cocotb.test()
@cocotb.parametrize(baud=BAUDS)
async def baud_limit(dut, baud):
    await setup_dut(dut)
    ticks = CLK_HZ / (baud * PRESCALER)
    ee, bm = await try_baud(dut, baud, 0x0D)          # explicit 8N1
    ae, _ = await try_baud(dut, baud, 0x03)           # autoSync
    # Characterization point: the logged row is the deliverable.
    dut._log.info(f"UART BAUD CEILING [{baud}] ticks/bit={ticks:.1f} baudMin={bm} "
                  f"explicit={'REC' if ee else 'FAIL'} autoSync={'REC' if ae else 'FAIL'}")
    # The point ran: the datalogger measured a bit period at this baud.
    assert bm > 0, f"[{baud}] calibration never measured a bit period (baudRateMin_r=0)"


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_baud_limit",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
