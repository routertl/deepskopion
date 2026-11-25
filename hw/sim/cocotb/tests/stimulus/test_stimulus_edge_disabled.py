# rr unknown-llm (AI-generated)
"""
test_stimulus_edge_disabled — DS-P2.100 / DS-D.38 option a fabric-side
stimulus generator, edge/pulse source coverage (STIM-REQ-7), proven against
the REAL fabric RTL. See test_stimulus_uart_frame.py's docstring for why
this suite is one @cocotb.test() per file.

Run (from the deepskopion dir):  rr sim run test_stimulus_edge_disabled
All tests together:               rr sim --tag stimulus_gen_loop
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles

from _stim_common import (  # noqa: E402 (sets up sys.path for sdk.* below)
    reset_dut, run_stimulus_gen_loop, start_clock,
)

from sdk.cocotb_helpers import requires  # noqa: E402


@cocotb.test()
@requires("STIM-REQ-7")
async def test_edge_source_disabled_holds_low(dut):
    await start_clock(dut)
    await reset_dut(dut)

    dut.STIM_EDGE_CONTROL.value = 0  # disabled
    dut.STIM_EDGE_SEL.value = 1
    for _ in range(200):
        assert dut.CAPTURE_EDGE0.value == 0, "disabled edge/pulse source toggled anyway"
        await ClockCycles(dut.CLK, 1)


if __name__ == "__main__":
    run_stimulus_gen_loop("test_stimulus_edge_disabled")
