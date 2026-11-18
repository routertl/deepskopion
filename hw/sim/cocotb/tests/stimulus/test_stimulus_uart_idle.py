"""
test_stimulus_uart_idle — DS-P2.100 / DS-D.38 option a fabric-side
stimulus generator, UART TX generator coverage (STIM-REQ-2), proven against
the REAL fabric RTL. See test_stimulus_uart_frame.py's docstring for why
this suite is one @cocotb.test() per file (nvc multi-test-session hang,
reproduced independent of file/protocol grouping).

Run (from the deepskopion dir):  rr sim run test_stimulus_uart_idle
All tests together:               rr sim --tag stimulus_gen_loop
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles

from _stim_common import (  # noqa: E402 (sets up sys.path for sdk.* below)
    pulse_write, reset_dut, run_stimulus_gen_loop, start_clock,
)

from sdk.cocotb_helpers import requires  # noqa: E402


@cocotb.test()
@requires("STIM-REQ-2")
async def test_uart_disabled_stays_idle(dut):
    await start_clock(dut)
    await reset_dut(dut)

    # CONTROL[0]=0 (disabled) — a pending DATA write must NOT produce a frame.
    dut.STIM_UART_CONTROL.value = 0
    dut.STIM_UART_BAUD_DIV.value = 20
    await pulse_write(dut, dut.STIM_UART_DATA, dut.STIM_UART_DATA_VLD, 0xFF)

    for _ in range(500):
        assert dut.SRC_UART_TX.value == 1, "disabled UART generator drove TX low"
        await ClockCycles(dut.CLK, 1)


if __name__ == "__main__":
    run_stimulus_gen_loop("test_stimulus_uart_idle")
