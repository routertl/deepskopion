"""
test_stimulus_core_seam_edge_gate — DS-P2.100 landing-review Finding B
adversarial control: EMIO_MUX_SEL bit[7]=0 must gate the edge/pulse source
OUT of core's own capture_pin(42) — proven against the REAL
tich.deepskopion_core (tb_stimulus_core_seam.vhd).

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_edge_gate
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, Timer

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    SLOT_EMIO_MUX_SEL, SLOT_STIM_EDGE_CONTROL, requires, run_core_seam, start,
)


@cocotb.test()
@requires("STIM-SEAM-REQ-1")
async def test_core_seam_bit7_clear_gates_stim_source(dut):
    """Adversarial control: with EMIO_MUX_SEL bit[7]=0, an enabled edge
    source must NOT reach capture_pin(42) — proves
    test_stimulus_core_seam_edge.py's toggling is really gated by the mux
    select, not a stuck-open tap."""
    axi = await start(dut)

    await axi.write(SLOT_EMIO_MUX_SEL, (0).to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)

    edge_ctrl = (2 << 4) | 1  # enabled, fast toggle
    await axi.write(SLOT_STIM_EDGE_CONTROL, edge_ctrl.to_bytes(4, "little"))
    await Timer(1, unit="ns")

    for _ in range(200):
        assert dut.PROBE_CAPTURE_EDGE0.value == 0, (
            "core.capture_pin(42) leaked the stim source with EMIO_MUX_SEL bit[7]=0"
        )
        await ClockCycles(dut.CLK, 1)


if __name__ == "__main__":
    run_core_seam("test_stimulus_core_seam_edge_gate")
