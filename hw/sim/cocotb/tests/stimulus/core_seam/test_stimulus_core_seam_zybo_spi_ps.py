"""
test_stimulus_core_seam_zybo_spi_ps — DS-P1.90, STIM-SEAM-REQ-6 (SPI).

Zynq wiring (tb_stimulus_core_seam_zybo.vhd). With STIM_SPI_CONTROL bit[0]
clear, capture_pin(20) follows the PS chip-select through the GPIO-CS decode
chain; the same walk is repeated after the generator has been enabled and
disabled again.

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_zybo_spi_ps
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    SEL_BIT_SPI, SLOT_EMIO_MUX_SEL, SLOT_STIM_SPI_CLK_DIV, SLOT_STIM_SPI_CONTROL,
    requires, start,
)
from _zybo_seam import run

SETTLE = 16


async def _walk(dut, label: str) -> None:
    for ss in (1, 0, 1, 0, 1):
        dut.PS_SPI_SS.value = ss
        await ClockCycles(dut.CLK, SETTLE)
        got = int(dut.PROBE_CAPTURE_SPI_CS.value)
        assert got == ss, f"{label}: PS chip-select {ss} read as {got} on capture_pin(20)"


@cocotb.test()
@requires("STIM-SEAM-REQ-6")
async def test_zybo_seam_spi_ps_cs_reaches_pin_before_and_after_generator(dut):
    dut.PS_SPI_SS.value = 1
    axi = await start(dut)
    await axi.write(SLOT_EMIO_MUX_SEL, SEL_BIT_SPI.to_bytes(4, "little"))
    await axi.write(SLOT_STIM_SPI_CLK_DIV, (4).to_bytes(4, "little"))
    await _walk(dut, "power-up")

    await axi.write(SLOT_STIM_SPI_CONTROL, (1).to_bytes(4, "little"))
    await ClockCycles(dut.CLK, SETTLE)
    await axi.write(SLOT_STIM_SPI_CONTROL, (0).to_bytes(4, "little"))
    await _walk(dut, "after the generator was enabled and disabled")


if __name__ == "__main__":
    run("test_stimulus_core_seam_zybo_spi_ps")
