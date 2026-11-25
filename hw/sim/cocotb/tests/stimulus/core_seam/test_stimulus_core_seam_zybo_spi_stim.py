# rr unknown-llm (AI-generated)
"""
test_stimulus_core_seam_zybo_spi_stim — DS-P1.90, STIM-SEAM-REQ-5 (SPI).

Zynq wiring (tb_stimulus_core_seam_zybo.vhd). The PS holds its chip-select
(native SS and the GPIO-CS the SPI datalogger frames on) ASSERTED low for the
whole test. With STIM_SPI_CONTROL bit[0] set the core takes the SPI legs and
the GPIO-CS from the generator: capture_pin(20) reads deasserted, then
asserts for the generator's transfer and deasserts again. With
G_STIM_DRIVES_EMIO false the PS keeps it low and the idle check fails.

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_zybo_spi_stim
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, with_timeout

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    SEAM_RED_TIMEOUT_NS, SEL_BIT_SPI, SLOT_EMIO_MUX_SEL, SLOT_STIM_SPI_CLK_DIV,
    SLOT_STIM_SPI_CONTROL, SLOT_STIM_SPI_DATA, requires, start,
)
from _zybo_seam import run


@cocotb.test()
@requires("STIM-SEAM-REQ-5")
async def test_zybo_seam_spi_generator_wins_over_ps_cs(dut):
    dut.PS_SPI_SS.value = 0
    axi = await start(dut)
    await axi.write(SLOT_EMIO_MUX_SEL, SEL_BIT_SPI.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 16)
    assert int(dut.PROBE_CAPTURE_SPI_CS.value) == 0, (
        "precondition: the PS chip-select must read asserted before the generator is enabled")

    await axi.write(SLOT_STIM_SPI_CONTROL, (1).to_bytes(4, "little"))
    await axi.write(SLOT_STIM_SPI_CLK_DIV, (4).to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 16)
    assert int(dut.PROBE_CAPTURE_SPI_CS.value) == 1, (
        "GPIO-CS not deasserted with the generator enabled: the PS leg still drives it")

    await axi.write(SLOT_STIM_SPI_DATA, (0x3C).to_bytes(4, "little"))
    await with_timeout(FallingEdge(dut.PROBE_CAPTURE_SPI_CS), SEAM_RED_TIMEOUT_NS, "ns")
    await with_timeout(RisingEdge(dut.PROBE_CAPTURE_SPI_CS), SEAM_RED_TIMEOUT_NS, "ns")


if __name__ == "__main__":
    run("test_stimulus_core_seam_zybo_spi_stim")
