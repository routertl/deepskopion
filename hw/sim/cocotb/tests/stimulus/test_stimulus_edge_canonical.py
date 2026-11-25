# rr unknown-llm (AI-generated)
"""Finite canonical EDGE stimulus contract (DS-P1.58)."""
from __future__ import annotations

import cocotb
import cocotb.utils
from cocotb.triggers import Timer

from _stim_common import reset_dut, run_stimulus_gen_loop, start_clock
from sdk.cocotb_helpers import requires


@cocotb.test()
@requires("STIM-REQ-11")
@requires("STIM-REQ-12")
async def test_edge_source_emits_finite_canonical_vector(dut):
    await start_clock(dut)
    await reset_dut(dut)
    dut.STIM_EDGE_SEL.value = 1
    enable_ns = int(cocotb.utils.get_sim_time(unit="ns"))
    dut.STIM_EDGE_CONTROL.value = 0x3

    observed = []
    for _ in range(4):
        await dut.CAPTURE_EDGE0.value_change
        observed.append((int(cocotb.utils.get_sim_time(unit="ns")) - enable_ns,
                         int(dut.CAPTURE_EDGE0.value)))
    assert observed == [(10_000, 1), (10_200, 0), (10_520, 1), (10_800, 0)], (
        f"finite EDGE vector drifted: {observed!r}")

    await Timer(1, unit="us")
    assert int(dut.CAPTURE_EDGE0.value) == 0, "finite EDGE mode emitted a periodic tail"


if __name__ == "__main__":
    run_stimulus_gen_loop("test_stimulus_edge_canonical")
