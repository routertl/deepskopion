"""
test_stimulus_core_seam_zybo_i2c_ps — DS-P1.90, STIM-SEAM-REQ-6 (I2C).

Zynq wiring (tb_stimulus_core_seam_zybo.vhd). With STIM_I2C_CONTROL bit[0]
clear, capture_pin(12)/(13) follow the PS I2C legs: released reads high, a
driven-low SDA or SCL reads low. The same walk is repeated after the
generator has been enabled and disabled again.

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_zybo_i2c_ps
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    SEL_BIT_I2C, SLOT_EMIO_MUX_SEL, SLOT_STIM_I2C_CLK_DIV, SLOT_STIM_I2C_CONTROL,
    requires, start,
)
from _zybo_seam import run

SETTLE = 16  # clk_sys cycles for the glitch filter / synchronisers


async def _walk(dut, label: str) -> None:
    # (SCL_T, SDA_T) — T=0 drives the leg's O (held 0), T=1 releases it.
    for scl_t, sda_t in ((1, 1), (1, 0), (0, 0), (1, 1)):
        dut.PS_I2C_SCL_T.value = scl_t
        dut.PS_I2C_SDA_T.value = sda_t
        await ClockCycles(dut.CLK, SETTLE)
        got = (int(dut.PROBE_CAPTURE_I2C_SCL.value),
               int(dut.PROBE_CAPTURE_I2C_SDA.value))
        assert got == (scl_t, sda_t), (
            f"{label}: PS legs SCL_T={scl_t} SDA_T={sda_t} read as (SCL, SDA)={got}")


@cocotb.test()
@requires("STIM-SEAM-REQ-6")
async def test_zybo_seam_i2c_ps_legs_reach_pins_before_and_after_generator(dut):
    dut.PS_I2C_SCL_O.value = 0
    dut.PS_I2C_SDA_O.value = 0
    dut.PS_I2C_SCL_T.value = 1
    dut.PS_I2C_SDA_T.value = 1
    axi = await start(dut)
    await axi.write(SLOT_EMIO_MUX_SEL, SEL_BIT_I2C.to_bytes(4, "little"))
    await axi.write(SLOT_STIM_I2C_CLK_DIV, (8).to_bytes(4, "little"))
    await _walk(dut, "power-up")

    await axi.write(SLOT_STIM_I2C_CONTROL, ((0x50 << 1) | 1).to_bytes(4, "little"))
    await ClockCycles(dut.CLK, SETTLE)
    await axi.write(SLOT_STIM_I2C_CONTROL, (0).to_bytes(4, "little"))
    await _walk(dut, "after the generator was enabled and disabled")


if __name__ == "__main__":
    run("test_stimulus_core_seam_zybo_i2c_ps")
