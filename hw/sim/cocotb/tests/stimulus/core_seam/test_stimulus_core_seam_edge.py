# rr unknown-llm (AI-generated)
"""
test_stimulus_core_seam_edge — DS-P2.100 landing-review Finding B: the
core-side bit[7] tap (capture_pin(42) chain, core.vhd:874-880), proven
against the REAL tich.deepskopion_core (tb_stimulus_core_seam.vhd — see
its header for the full rationale and the two-mutation proof this suite is
built to catch).

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_edge
"""
from __future__ import annotations

import cocotb
import cocotb.utils
from cocotb.triggers import ClockCycles, RisingEdge, with_timeout

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    CLK_PERIOD_NS, SEAM_RED_TIMEOUT_NS, SEL_BIT_STIM_EDGE, SLOT_EMIO_MUX_SEL,
    SLOT_STIM_EDGE_CONTROL, requires, run_core_seam, start,
)


@cocotb.test()
@requires("STIM-SEAM-REQ-1")
async def test_core_seam_edge_source_reaches_capture_pin_bit7(dut):
    """A real AXI write to EMIO_MUX_SEL (bit[7]=1) + STIM_EDGE_CONTROL
    (enable) must make the CORE's own internal capture_pin(42) toggle at the
    configured period — proving the regbank -> C_INDEX_STIM_EDGE_CONTROL
    unpack -> ds_stimulus_gen -> DE25-loopback -> ds_emio_mux bit[7]
    priority chain all the way down to the datalogger's own private wire,
    not just a TB-local replica of it (that's test_stimulus_edge_muxsel.
    py's job, in the sibling stimulus/ dir)."""
    axi = await start(dut)

    await axi.write(SLOT_EMIO_MUX_SEL, SEL_BIT_STIM_EDGE.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)

    half_period_cycles = 5
    edge_ctrl = (half_period_cycles << 4) | 1
    await axi.write(SLOT_STIM_EDGE_CONTROL, edge_ctrl.to_bytes(4, "little"))

    # DS-P2.100 landing-review Finding 6: a broken C_INDEX_STIM_EDGE_CONTROL
    # unpack means PROBE_CAPTURE_EDGE0 never toggles — bound both waits so
    # that failure mode is a clean, fast TestFailure, not an unbounded hang
    # (reproduced elsewhere in this suite: >30 min before manual cancel).
    await with_timeout(RisingEdge(dut.PROBE_CAPTURE_EDGE0), SEAM_RED_TIMEOUT_NS, "ns")
    t0 = cocotb.utils.get_sim_time(unit="ns")
    await with_timeout(RisingEdge(dut.PROBE_CAPTURE_EDGE0), SEAM_RED_TIMEOUT_NS, "ns")
    t1 = cocotb.utils.get_sim_time(unit="ns")

    period_ns = t1 - t0
    expected_ns = 2 * half_period_cycles * CLK_PERIOD_NS
    assert period_ns == expected_ns, (
        f"core.capture_pin(42) period={period_ns}ns via real AXI writes, "
        f"expected {expected_ns}ns — regbank->core->stim_gen->mux chain broken"
    )


if __name__ == "__main__":
    run_core_seam("test_stimulus_core_seam_edge")
