"""
test_stimulus_core_seam_spi — DS-P2.100 landing-review (fourth pass)
Finding 2 + Finding 3 (STIM-REQ-6 / DS-T2.16 GPIO-CS decode), proven
together against the REAL tich.deepskopion_core (tb_stimulus_core_seam.vhd).

Two gaps the third pass left open:
  (a) the core-seam suite never wrote the SPI stimulus slots, so a broken
      `C_INDEX_STIM_SPI_*` unpack left every test GREEN (reproduced by the
      lander);
  (b) the real silicon SPI chip-select is `spi0_cs_gpio_w`
      (deepskopion_core.vhd:981), decoded from `emio_gpio_o_i`/
      `emio_gpio_t_i` — NOT `stim_spi_ss_w` directly. `tb_stimulus_gen_loop.
      vhd:255` wires the injector's CS_N straight to `stim_spi_ss_w`,
      bypassing that decode entirely, so leaving `emio_gpio_t_i` tied high
      (GPIO tristated, CS stuck high per the DS-P2.95 shape) was invisible
      to every test.

This test observes PROBE_CAPTURE_SPI_CS — core's capture_pin(20), fed from
ds_emio_mux.SRC_SPI_SS, itself fed from `SPI0_CS_GPIO => spi0_cs_gpio_r`
(deepskopion_core.vhd:798), which is the REGISTERED version of
`spi0_cs_gpio_w <= gpio_o_w(0) when gpio_t_w(0) = '0' else '1'`
(deepskopion_core.vhd:981) — i.e. the actual GPIO-CS decode chain, driven
here through `tb_stimulus_core_seam.vhd`'s `emio_gpio_o_i => stim_spi_ss_w,
emio_gpio_t_i => '0'` loopback (the exact mirror of
deepskopion_de25_top.vhd:751). Both (a) and (b) turn this test RED:
  * breaking C_INDEX_STIM_SPI_CONTROL/CLK_DIV/DATA -> the generator never
    starts a transfer -> PROBE_CAPTURE_SPI_CS never falls;
  * tying `emio_gpio_t_i => '1'` in THIS FILE (mirroring a tristated real
    GPIO) -> spi0_cs_gpio_w sticks at '1' regardless of the transfer ->
    PROBE_CAPTURE_SPI_CS never falls even though the generator DID run.

Mutation-proof (performed during this revision, then reverted):
    1) sed -i "s/C_INDEX_STIM_SPI_DATA/C_INDEX_STIM_SPI_CONTROL/" (one unpack
       line only) hw/rtl/deepskopion_core.vhd
       rr sim run test_stimulus_core_seam_spi -> FAILED
       git checkout -- hw/rtl/deepskopion_core.vhd
    2) sed -i "s/emio_gpio_t_i => '0'/emio_gpio_t_i => '1'/" (this file only,
       the u_core instantiation)
       rr sim run test_stimulus_core_seam_spi -> FAILED
       git checkout -- hw/sim/tb_stimulus_core_seam.vhd

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_spi
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, with_timeout

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    SEAM_RED_TIMEOUT_NS, SEL_BIT_SPI, SLOT_EMIO_MUX_SEL, SLOT_STIM_SPI_CLK_DIV,
    SLOT_STIM_SPI_CONTROL, SLOT_STIM_SPI_DATA, requires, run_core_seam, start,
)


@cocotb.test()
@requires("STIM-SEAM-REQ-4")
async def test_core_seam_spi_write_asserts_gpio_cs_on_capture_pin_20(dut):
    """A real AXI write to EMIO_MUX_SEL (bit[2]=1, select the EMIO SPI
    source) + STIM_SPI_CONTROL/CLK_DIV/DATA must assert core's own
    internal capture_pin(20) (SRC_SPI_SS, fed from the GPIO-CS decode chain,
    NOT the dead native SPI0_SS_O) for the duration of the transfer —
    proving BOTH the C_INDEX_STIM_SPI_* unpack AND the DS-T2.16 GPIO-CS
    decode (STIM-REQ-6) are load-bearing, not bypassed."""
    axi = await start(dut)

    await axi.write(SLOT_EMIO_MUX_SEL, SEL_BIT_SPI.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)

    await axi.write(SLOT_STIM_SPI_CONTROL, (1).to_bytes(4, "little"))
    await axi.write(SLOT_STIM_SPI_CLK_DIV, (4).to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)

    # Idle: CS (active-low) deasserted before the transfer.
    assert int(dut.PROBE_CAPTURE_SPI_CS.value) == 1, "SPI GPIO-CS not idle-high before the write"

    await axi.write(SLOT_STIM_SPI_DATA, (0x3C).to_bytes(4, "little"))

    # DS-P2.100 landing-review Finding 6: bounded — a killed SPI unpack, or
    # a tristated emio_gpio_t_i, means CS never asserts.
    await with_timeout(FallingEdge(dut.PROBE_CAPTURE_SPI_CS), SEAM_RED_TIMEOUT_NS, "ns")
    await with_timeout(RisingEdge(dut.PROBE_CAPTURE_SPI_CS), SEAM_RED_TIMEOUT_NS, "ns")


if __name__ == "__main__":
    run_core_seam("test_stimulus_core_seam_spi")
