# rr unknown-llm (AI-generated)
"""
test_stimulus_core_seam_zybo_i2c_stim — DS-P1.90, STIM-SEAM-REQ-5 (I2C).

Zynq wiring (tb_stimulus_core_seam_zybo.vhd). The PS I2C legs DRIVE both SCL
and SDA low for the whole test (T=0, O=0). With STIM_I2C_CONTROL bit[0] set
the core takes the I2C output and tristate legs from the generator, so the
bus reads idle-high on capture_pin(12)/(13) and the generator's START (SDA
falls while SCL is high) arrives. With G_STIM_DRIVES_EMIO false the PS keeps
the bus low and the idle-high check fails.

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_zybo_i2c_stim
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, with_timeout

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    SEAM_RED_TIMEOUT_NS, SEL_BIT_I2C, SLOT_EMIO_MUX_SEL, SLOT_STIM_I2C_CLK_DIV,
    SLOT_STIM_I2C_CONTROL, SLOT_STIM_I2C_DATA, requires, start,
)
from _zybo_seam import run


@cocotb.test()
@requires("STIM-SEAM-REQ-5")
async def test_zybo_seam_i2c_generator_wins_over_ps_legs(dut):
    dut.PS_I2C_SCL_O.value = 0
    dut.PS_I2C_SCL_T.value = 0
    dut.PS_I2C_SDA_O.value = 0
    dut.PS_I2C_SDA_T.value = 0
    axi = await start(dut)
    await axi.write(SLOT_EMIO_MUX_SEL, SEL_BIT_I2C.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 8)
    assert (int(dut.PROBE_CAPTURE_I2C_SCL.value),
            int(dut.PROBE_CAPTURE_I2C_SDA.value)) == (0, 0), (
        "precondition: the PS legs must hold the bus low before the generator is enabled")

    await axi.write(SLOT_STIM_I2C_CONTROL, ((0x50 << 1) | 1).to_bytes(4, "little"))
    await axi.write(SLOT_STIM_I2C_CLK_DIV, (8).to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 8)
    assert int(dut.PROBE_CAPTURE_I2C_SCL.value) == 1, (
        "SCL not idle-high with the generator enabled: the PS leg still drives it")
    assert int(dut.PROBE_CAPTURE_I2C_SDA.value) == 1, (
        "SDA not idle-high with the generator enabled: the PS leg still drives it")

    async def wait_for_start():
        while True:
            await with_timeout(FallingEdge(dut.PROBE_CAPTURE_I2C_SDA),
                               SEAM_RED_TIMEOUT_NS, "ns")
            if int(dut.PROBE_CAPTURE_I2C_SCL.value) == 1:
                return

    task = cocotb.start_soon(wait_for_start())
    await axi.write(SLOT_STIM_I2C_DATA, (0xC3).to_bytes(4, "little"))
    await task.join()


if __name__ == "__main__":
    run("test_stimulus_core_seam_zybo_i2c_stim")
