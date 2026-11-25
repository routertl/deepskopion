# rr unknown-llm (AI-generated)
"""
test_stimulus_uart_frame — DS-P2.100 / DS-D.38 option a fabric-side
stimulus generator, UART TX generator coverage (STIM-REQ-1), proven against
the REAL fabric RTL (tb_stimulus_gen_loop.vhd: tich.ds_stimulus_gen ->
tich.ds_emio_mux -> {tich.i2c_injector, tich.spi_injector}).

ONE cocotb test per file (DS-P2.100 landing-review Finding A, revised): the
original single-file test_stimulus_gen_loop.py packed 9 tests into one nvc
session and reproducibly hung nvc after the first test (documented in the
DS-P2.100 commit message). The FIRST split attempt regrouped by protocol
(test_stimulus_uart.py with 3 tests, test_stimulus_i2c.py with 2, ...) —
that still hangs: reproduced directly via `rr sim run test_stimulus_i2c`
(2-test file), which ran test 1 to completion and then hung nvc at 100% CPU
on test 2, exactly like the original monolith. The hang is keyed to
"more than one @cocotb.test() sharing an nvc session", not to file/protocol
grouping — so the only split that reliably works through the DOCUMENTED
`rr sim run <module>` invocation (no undocumented COCOTB_TEST_FILTER env
var) is one test, one file, one nvc session.

Run (from the deepskopion dir):  rr sim run test_stimulus_uart_frame
All tests together:               rr sim --tag stimulus_gen_loop
"""
from __future__ import annotations

import cocotb

from _stim_common import (  # noqa: E402 (sets up sys.path for sdk.* below)
    CLK_PERIOD_NS, UartCapture, pulse_write, reset_dut, run_stimulus_gen_loop, start_clock,
)

from sdk.cocotb_helpers import requires  # noqa: E402


@cocotb.test()
@requires("STIM-REQ-1")
async def test_uart_frames_correct_byte(dut):
    await start_clock(dut)
    await reset_dut(dut)

    baud_div = 20  # clk_sys cycles/bit
    bit_time_ns = CLK_PERIOD_NS * baud_div
    dut.STIM_UART_CONTROL.value = 1  # enable
    dut.STIM_UART_BAUD_DIV.value = baud_div

    cap = UartCapture(dut.SRC_UART_TX, bit_time_ns)
    cocotb.start_soon(cap.run())

    payload = 0x5A
    await pulse_write(dut, dut.STIM_UART_DATA, dut.STIM_UART_DATA_VLD, payload)

    await cap.done.wait()
    assert cap.data == payload, f"UART frame decoded {cap.data:#x}, expected {payload:#x}"
    assert cap.stop_bit == 1, "UART generator produced a bad stop bit"


if __name__ == "__main__":
    run_stimulus_gen_loop("test_stimulus_uart_frame")
