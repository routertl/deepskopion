# rr unknown-llm (AI-generated)
"""
test_stimulus_uart_drop_write — DS-P2.100 / DS-D.38 option a fabric-side
stimulus generator, write-strobe idiom invariant (STIM-REQ-8), proven
against the REAL fabric RTL. See test_stimulus_uart_frame.py's docstring
for why this suite is one @cocotb.test() per file (nvc multi-test-session
hang, reproduced independent of file/protocol grouping).

Run (from the deepskopion dir):  rr sim run test_stimulus_uart_drop_write
All tests together:               rr sim --tag stimulus_gen_loop
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, SimTimeoutError, with_timeout, FallingEdge

from _stim_common import (  # noqa: E402 (sets up sys.path for sdk.* below)
    CLK_PERIOD_NS, UartCapture, pulse_write, reset_dut, run_stimulus_gen_loop, start_clock,
)

from sdk.cocotb_helpers import requires  # noqa: E402


@cocotb.test()
@requires("STIM-REQ-8")
async def test_uart_mid_frame_write_is_dropped_not_glitched(dut):
    await start_clock(dut)
    await reset_dut(dut)

    baud_div = 20
    bit_time_ns = CLK_PERIOD_NS * baud_div
    dut.STIM_UART_CONTROL.value = 1
    dut.STIM_UART_BAUD_DIV.value = baud_div

    cap = UartCapture(dut.SRC_UART_TX, bit_time_ns)
    cocotb.start_soon(cap.run())

    await pulse_write(dut, dut.STIM_UART_DATA, dut.STIM_UART_DATA_VLD, 0x11)
    # Land a second write mid-frame (well before the first frame's ~10 bit
    # periods complete) — it must be dropped, not glitch the in-flight frame.
    await ClockCycles(dut.CLK, baud_div * 3)
    await pulse_write(dut, dut.STIM_UART_DATA, dut.STIM_UART_DATA_VLD, 0x22)

    await cap.done.wait()
    assert cap.data == 0x11, f"in-flight frame corrupted by the dropped write: got {cap.data:#x}"
    assert cap.stop_bit == 1, "dropped mid-frame write produced a bad stop bit"

    # No second frame should follow — the dropped write must not queue. Race
    # a further START (falling edge) against a generous no-second-frame window.
    watch_ns = bit_time_ns * 15
    try:
        await with_timeout(FallingEdge(dut.SRC_UART_TX), watch_ns, "ns")
        assert False, "a dropped mid-frame write still produced a second frame (START observed)"
    except SimTimeoutError:
        pass


if __name__ == "__main__":
    run_stimulus_gen_loop("test_stimulus_uart_drop_write")
