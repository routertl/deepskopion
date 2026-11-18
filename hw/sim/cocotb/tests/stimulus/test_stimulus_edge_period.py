"""
test_stimulus_edge_period — DS-P2.100 / DS-D.38 option a fabric-side
stimulus generator, edge/pulse source coverage (STIM-REQ-7), proven against
the REAL fabric RTL. See test_stimulus_uart_frame.py's docstring for why
this suite is one @cocotb.test() per file.

Run (from the deepskopion dir):  rr sim run test_stimulus_edge_period
All tests together:               rr sim --tag stimulus_gen_loop
"""
from __future__ import annotations

import cocotb
import cocotb.utils
from cocotb.triggers import RisingEdge

from _stim_common import (  # noqa: E402 (sets up sys.path for sdk.* below)
    CLK_PERIOD_NS, reset_dut, run_stimulus_gen_loop, start_clock,
)

from sdk.cocotb_helpers import requires  # noqa: E402


@cocotb.test()
@requires("STIM-REQ-7")
async def test_edge_source_free_runs_at_configured_period(dut):
    await start_clock(dut)
    await reset_dut(dut)

    half_period_cycles = 5
    dut.STIM_EDGE_CONTROL.value = (half_period_cycles << 4) | 1  # enable
    dut.STIM_EDGE_SEL.value = 1  # route into the EDGE0 self-test tap

    await RisingEdge(dut.CAPTURE_EDGE0)
    t0 = cocotb.utils.get_sim_time(unit="ns")
    await RisingEdge(dut.CAPTURE_EDGE0)
    t1 = cocotb.utils.get_sim_time(unit="ns")

    period_ns = t1 - t0
    expected_ns = 2 * half_period_cycles * CLK_PERIOD_NS
    assert period_ns == expected_ns, f"edge period={period_ns}ns, expected {expected_ns}ns"


if __name__ == "__main__":
    run_stimulus_gen_loop("test_stimulus_edge_period")
