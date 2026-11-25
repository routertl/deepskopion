# rr unknown-llm (AI-generated)
"""
test_stimulus_core_seam_i2c — DS-P2.100 landing-review (fourth pass)
Finding 2: the core-seam suite only wrote UART/EDGE regbank slots
(_core_seam_common.py before this revision), so killing the I2C leg's own
unpack — `stim_i2c_data_vld_w` at deepskopion_core.vhd:1427, or a broken
`C_INDEX_STIM_I2C_*` index — left every test GREEN (reproduced by the
lander: mutating stim_i2c_data_vld_w => '0' and re-running all core-seam +
shard tests, all GREEN).

This test actually writes STIM_I2C_CONTROL/CLK_DIV/DATA through the REAL
regbank on the REAL `tich.deepskopion_core` (tb_stimulus_core_seam.vhd) and
observes the START condition (SDA falls while SCL is high) on
PROBE_CAPTURE_I2C_SCL/SDA — core's capture_pin(12)/(13), fed from
ds_emio_mux.SRC_I2C_SCL/SDA, themselves gated by EMIO_MUX_SEL(0). That is
downstream of the C_INDEX_STIM_I2C_* unpack, so mutating it (or the
DATA_VLD tap specifically) turns this test RED instead of leaving the I2C
leg silently untested.

Mutation-proof (performed during this revision, then reverted):
    sed -i "s/stim_i2c_data_vld_w <= regs_out_local(C_INDEX_STIM_I2C_DATA \/ 4).valid;/stim_i2c_data_vld_w <= '0';/"
        hw/rtl/deepskopion_core.vhd
    rr sim run test_stimulus_core_seam_i2c -> FAILED (TimeoutError, bounded)
    git checkout -- hw/rtl/deepskopion_core.vhd

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_i2c
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, with_timeout

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    SEAM_RED_TIMEOUT_NS, SEL_BIT_I2C, SLOT_EMIO_MUX_SEL, SLOT_STIM_I2C_CLK_DIV,
    SLOT_STIM_I2C_CONTROL, SLOT_STIM_I2C_DATA, requires, run_core_seam, start,
)


@cocotb.test()
@requires("STIM-SEAM-REQ-3")
async def test_core_seam_i2c_write_starts_on_capture_pins_12_13(dut):
    """A real AXI write to EMIO_MUX_SEL (bit[0]=1, select the EMIO I2C
    source) + STIM_I2C_CONTROL/CLK_DIV/DATA must produce a START condition
    (SDA falls while SCL is high) on core's own internal capture_pin(12)/(13)
    — proving the C_INDEX_STIM_I2C_* unpack inside deepskopion_core.vhd
    actually reaches ds_stimulus_gen's I2C master, not just
    ds_stimulus_gen in isolation (tb_stimulus_gen_loop.vhd's job)."""
    axi = await start(dut)

    await axi.write(SLOT_EMIO_MUX_SEL, SEL_BIT_I2C.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)

    addr = 0x50
    await axi.write(SLOT_STIM_I2C_CONTROL, ((addr & 0x7F) << 1 | 1).to_bytes(4, "little"))
    await axi.write(SLOT_STIM_I2C_CLK_DIV, (8).to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)

    # Idle bus: both lines released high (pulled up) before the transaction.
    assert int(dut.PROBE_CAPTURE_I2C_SCL.value) == 1, "I2C SCL not idle-high before the write"
    assert int(dut.PROBE_CAPTURE_I2C_SDA.value) == 1, "I2C SDA not idle-high before the write"

    async def wait_for_start():
        while True:
            # DS-P2.100 landing-review Finding 6: bounded — a killed
            # DATA_VLD unpack means this START never arrives.
            await with_timeout(
                FallingEdge(dut.PROBE_CAPTURE_I2C_SDA), SEAM_RED_TIMEOUT_NS, "ns"
            )
            if int(dut.PROBE_CAPTURE_I2C_SCL.value) == 1:
                return

    wait_task = cocotb.start_soon(wait_for_start())
    await axi.write(SLOT_STIM_I2C_DATA, (0xC3).to_bytes(4, "little"))
    await wait_task.join()


if __name__ == "__main__":
    run_core_seam("test_stimulus_core_seam_i2c")
