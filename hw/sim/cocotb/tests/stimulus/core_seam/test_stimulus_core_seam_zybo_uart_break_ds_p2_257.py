"""
test_stimulus_core_seam_zybo_uart_break_ds_p2_257 — DS-P2.257.

The UART datalogger reports CALIBRATION_TIMEOUT (STATUS 0x01) when an autoSync
calibration finds no UART format (DS-P2.239). On silicon nothing could force
that: the PS UART and the 8N1 stimulus generator only send valid frames. The
generator's break pattern (STIM_UART_CONTROL[1]=1, [15:8] = low bit-times) holds
the line low for N bit-times with one high bit-time between, DATA[15:0] times.

This drives it through the REAL deepskopion_core on the Zynq wiring
(tb_stimulus_core_seam_zybo.vhd, G_STIM_DRIVES_EMIO true) over core's own
AXI-Lite port, into UART datalogger 12 armed with autoSync, at 1 Mbaud
(STIM_UART_BAUD_DIV 100 at 100 MHz). 80 breaks exceed the 70-byte calibration
buffer, as in test_uart_calibration_fail_status_ds_p2_239.

1. break_13_forces_calibration_timeout: N = 13. The datalogger's UART input
   carries exactly 80 lows of 13 us, each followed by a 1 us high; the
   calibration fails (calibrationFail_r = 1, checked before the STATUS so it
   cannot pass vacuously) and the datalogger emits exactly enabled then
   calibration_timeout.
2. break_6_does_not_force_a_timeout: N = 6 (start + 5 zero data bits, then a
   high stop): a valid 5N1 frame, below the threshold. The calibration completes
   (calibrationDone_r = 1, calibrationFail_r = 0) and no calibration_timeout.

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_zybo_uart_break_ds_p2_257
"""
from __future__ import annotations

import cocotb
import cocotb.utils
from cocotb.triggers import RisingEdge, Timer

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    SEL_BIT_UART, SLOT_EMIO_MUX_SEL, SLOT_STIM_I2C_CONTROL, SLOT_STIM_SPI_CONTROL,
    SLOT_STIM_UART_BAUD_DIV, SLOT_STIM_UART_CONTROL, SLOT_STIM_UART_DATA, requires, start,
)
from _zybo_seam import run

SLOT_DATALOGGER_CONFIG = 0x20
UART_ID = 12
CFG_DISABLE = UART_ID << 11               # 0x6000
CFG_ENABLE_AUTOSYNC = (UART_ID << 11) | 0x03   # enable | autoSync
BAUD_DIV = 100                            # 1 Mbaud at 100 MHz
BREAKS = 80                               # > the 70-byte calibration buffer
MSG_TYPE_STATUS = 0b100
STATUS_NAMES = {0x00: "calibration_done", 0x01: "calibration_timeout",
                0x02: "parity_error", 0x03: "start_error", 0x04: "stop_error",
                0x05: "enabled", 0x06: "disabled"}


def _w(v: int) -> bytes:
    return (v & 0xFFFFFFFF).to_bytes(4, "little")


async def _run_breaks(dut, low_bits: int):
    axi = await start(dut)
    logger = (dut.u_core.u_datalogger_gather.GEN_UART_BLOCK
              .GEN_UART_DATALOGGERS[0].i_uart_datalogger)
    msgs: list = []
    edges: list = []

    async def pin():
        while True:
            await logger.UART_IN.value_change
            edges.append((int(cocotb.utils.get_sim_time(unit="ns")), str(logger.UART_IN.value)))
    cocotb.start_soon(pin())

    async def collect():
        while True:
            await RisingEdge(logger.CLK)
            if str(logger.UART_MSG_VLD.value) == "1" and str(logger.UART_MSG_RDY.value) == "1":
                msgs.append(int(logger.UART_MSG_OUT.value))
    cocotb.start_soon(collect())

    for slot in (SLOT_STIM_UART_CONTROL, SLOT_STIM_I2C_CONTROL, SLOT_STIM_SPI_CONTROL):
        await axi.write(slot, _w(0))
    await axi.write(SLOT_EMIO_MUX_SEL, _w(SEL_BIT_UART))
    await axi.write(SLOT_STIM_UART_BAUD_DIV, _w(BAUD_DIV))
    await axi.write(SLOT_STIM_UART_CONTROL, _w(0x1 | 0x2 | (low_bits << 8)))
    await Timer(20, unit="us")
    await axi.write(SLOT_DATALOGGER_CONFIG, _w(CFG_DISABLE))
    await Timer(20, unit="us")
    await axi.write(SLOT_DATALOGGER_CONFIG, _w(CFG_ENABLE_AUTOSYNC))
    await Timer(20, unit="us")
    await axi.write(SLOT_STIM_UART_DATA, _w(BREAKS))
    # 80 breaks x (N + 1) bit-times at 1 us, plus calibration and drain margin.
    await Timer(BREAKS * (low_bits + 1) + 1500, unit="us")
    statuses = [STATUS_NAMES.get(w & 0xFF, f"0x{w & 0xFF:02X}") for w in msgs
                if (w >> 24) & 0x7 == MSG_TYPE_STATUS]
    dut._log.info(f"N={low_bits}: calibrationFail_r={logger.calibrationFail_r.value} "
                  f"calibrationDone_r={logger.calibrationDone_r.value} STATUS {statuses}")
    return logger, statuses, edges


def _runs(edges):
    """(level, duration ns) of every complete run from the first falling edge on
    (the idle high before the first break is not a gap between breaks)."""
    first = next(i for i, (_, lvl) in enumerate(edges) if lvl == "0")
    tail = edges[first:]
    return [(lvl, t1 - t0) for (t0, lvl), (t1, _) in zip(tail, tail[1:])]


@cocotb.test()
@requires("STIM-REQ-13")
async def break_13_forces_calibration_timeout(dut):
    logger, statuses, edges = await _run_breaks(dut, 13)
    runs = _runs(edges)
    lows = [d for lvl, d in runs if lvl == "0"]
    highs = [d for lvl, d in runs if lvl == "1"]
    assert lows == [13_000] * BREAKS, f"low runs {lows[:5]}... ({len(lows)}); expected {BREAKS} of 13000 ns"
    assert highs == [1_000] * (BREAKS - 1), f"high runs between breaks {highs[:5]}... ({len(highs)}); expected {BREAKS - 1} of 1000 ns"
    assert edges[-1][1] == "1", "the line did not return high after the last break"
    assert str(logger.calibrationFail_r.value) == "1", (
        "13-bit-time breaks did not make the autoSync calibration fail (calibrationFail_r != 1)")
    assert statuses == ["enabled", "calibration_timeout"], (
        f"STATUS {statuses}; expected exactly ['enabled', 'calibration_timeout']")


@cocotb.test()
@requires("STIM-REQ-13")
async def break_6_does_not_force_a_timeout(dut):
    logger, statuses, _edges = await _run_breaks(dut, 6)
    assert (str(logger.calibrationDone_r.value), str(logger.calibrationFail_r.value)) == ("1", "0"), (
        f"6-bit-time breaks: calibrationDone_r={logger.calibrationDone_r.value} "
        f"calibrationFail_r={logger.calibrationFail_r.value}; expected a completed calibration")
    assert "calibration_timeout" not in statuses, (
        f"6-bit-time breaks (a valid 5N1 frame) gave a calibration_timeout: STATUS {statuses}")


if __name__ == "__main__":
    run("test_stimulus_core_seam_zybo_uart_break_ds_p2_257")
