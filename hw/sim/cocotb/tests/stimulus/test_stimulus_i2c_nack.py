"""
test_stimulus_i2c_nack — DS-P2.100 / DS-D.38 option a fabric-side stimulus
generator, I2C master generator adversarial coverage (STIM-REQ-3), proven
against the REAL fabric RTL. See test_stimulus_uart_frame.py's docstring
for why this suite is one @cocotb.test() per file.

Run (from the deepskopion dir):  rr sim run test_stimulus_i2c_nack
All tests together:               rr sim --tag stimulus_gen_loop
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, Event, FallingEdge

from _stim_common import (  # noqa: E402 (sets up sys.path for sdk.* below)
    make_i2c_inj_control, pulse_write, reset_dut, run_stimulus_gen_loop, sample_i2c_bit,
    signal_event, start_clock,
)

from sdk.cocotb_helpers import requires  # noqa: E402


@cocotb.test()
@requires("STIM-REQ-3")
async def test_i2c_write_nacked_when_no_match(dut):
    """Adversarial: injector enabled at a DIFFERENT address must NACK
    (leave SDA released/high) both the address and data ACK slots —
    proves the ACK test_stimulus_i2c_ack.py sees is real, not a stuck-low
    bus."""
    await start_clock(dut)
    await reset_dut(dut)

    dut.I2C_INJ_CONTROL.value = make_i2c_inj_control(0x10, enable=True)  # different address
    await ClockCycles(dut.CLK, 2)

    dut.STIM_I2C_CONTROL.value = (0x50 & 0x7F) << 1 | 1
    dut.STIM_I2C_CLK_DIV.value = 8

    async def wait_start():
        while True:
            await FallingEdge(dut.I2C0_BUS_SDA)
            if dut.I2C0_BUS_SCL.value == 1:
                return

    start_ev = Event()
    cocotb.start_soon(signal_event(wait_start(), start_ev))
    await pulse_write(dut, dut.STIM_I2C_DATA, dut.STIM_I2C_DATA_VLD, 0x00)
    await start_ev.wait()

    bits = [await sample_i2c_bit(dut) for _ in range(9)]  # addr(7) + rw(1) + ack(1)
    addr_ack = bits[8]
    assert addr_ack == 1, "unmatched-address ACK slot read low — false ACK"


if __name__ == "__main__":
    run_stimulus_gen_loop("test_stimulus_i2c_nack")
