"""
test_stimulus_i2c_ack — DS-P2.100 / DS-D.38 option a fabric-side stimulus
generator, I2C master generator coverage (STIM-REQ-3, STIM-REQ-4), proven
against the REAL fabric RTL (tb_stimulus_gen_loop.vhd: tich.ds_stimulus_gen
-> tich.ds_emio_mux -> {tich.i2c_injector, tich.spi_injector}).

ONE cocotb test per file — see test_stimulus_uart_frame.py's docstring for
why (nvc multi-test-session hang, reproduced independent of file/protocol
grouping — `rr sim run test_stimulus_i2c`, the original 2-test i2c shard,
hung nvc at 100% CPU on its second test the same way the 9-test monolith
did).

Run (from the deepskopion dir):  rr sim run test_stimulus_i2c_ack
All tests together:               rr sim --tag stimulus_gen_loop
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, Event, FallingEdge, RisingEdge, Timer, with_timeout

from _stim_common import (  # noqa: E402 (sets up sys.path for sdk.* below)
    make_i2c_inj_control, pulse_write, reset_dut, run_stimulus_gen_loop, sample_i2c_bit,
    signal_event, start_clock,
)

from sdk.cocotb_helpers import requires  # noqa: E402


@cocotb.test()
@requires("STIM-REQ-3")
@requires("STIM-REQ-4")
@requires("STIM-REQ-9")
@requires("STIM-REQ-10")
async def test_i2c_write_transaction_is_acked(dut):
    await start_clock(dut)
    await reset_dut(dut)

    addr = 0x50
    data_bytes = [0xAB, 0xCD, 0x3E]

    # i2c_injector ENABLED and matching address — must ACK both slots.
    dut.I2C_INJ_CONTROL.value = make_i2c_inj_control(addr, enable=True)
    await ClockCycles(dut.CLK, 2)

    dut.STIM_I2C_CONTROL.value = ((len(data_bytes) - 1) << 8) | (addr << 1) | 1
    dut.STIM_I2C_CLK_DIV.value = 8

    # Watch for the START condition (SDA falls while SCL high) before
    # arming the bit sampler, then trigger the transaction.
    async def wait_start():
        while True:
            await FallingEdge(dut.I2C0_BUS_SDA)
            if dut.I2C0_BUS_SCL.value == 1:
                return

    start_ev = Event()
    cocotb.start_soon(signal_event(wait_start(), start_ev))
    packed = sum(byte << (8 * i) for i, byte in enumerate(data_bytes))
    await pulse_write(dut, dut.STIM_I2C_DATA, dut.STIM_I2C_DATA_VLD, packed)
    await start_ev.wait()

    # A timeout is converted into an explicit transaction-shape assertion so
    # the historical one-byte-then-STOP implementation fails for behavior,
    # rather than hanging the simulator while waiting for a nonexistent bit.
    try:
        bits = [await with_timeout(sample_i2c_bit(dut), 20, "us") for _ in range(36)]
    except Exception as exc:
        raise AssertionError(
            "I2C transaction ended before address + three ACK-bound data bytes; "
            "one-byte-then-STOP behavior has returned") from exc
    addr_bits, rw_bit, addr_ack = bits[0:7], bits[7], bits[8]
    data_groups = [(bits[9 + 9*i:17 + 9*i], bits[17 + 9*i]) for i in range(3)]

    # sda_low_r <= not addr_bit: bit='1' releases SDA (bus reads '1'), bit='0'
    # pulls it low (bus reads '0') -> the bus level equals the transmitted
    # bit directly (no inversion).
    got_addr = 0
    for b in addr_bits:
        got_addr = (got_addr << 1) | b
    assert got_addr == addr, f"I2C address on the wire = {got_addr:#04x}, expected {addr:#04x}"
    assert rw_bit == 0, "R/W bit should read '0' (write) on the wire"
    assert addr_ack == 0, "i2c_injector did not ACK the address (SDA should be pulled low)"

    got_data = []
    for data_bits, data_ack in data_groups:
        byte = 0
        for b in data_bits:
            byte = (byte << 1) | b
        got_data.append(byte)
        assert data_ack == 0, "i2c_injector did not ACK every data byte"
    assert got_data == data_bytes, f"I2C data run {got_data!r}, expected {data_bytes!r}"

    # STOP: SDA rises while SCL is high (the remaining DATA_ACK phases plus
    # all 4 STOP phases still have to run their STIM_I2C_CLK_DIV ticks after
    # the 18th sampled bit — wait for the actual STOP edge rather than a
    # guessed cycle count).
    await RisingEdge(dut.I2C0_BUS_SDA)
    await Timer(1, unit="ns")
    assert dut.I2C0_BUS_SCL.value == 1 and dut.I2C0_BUS_SDA.value == 1, \
        "I2C bus did not return to idle-high after STOP"


if __name__ == "__main__":
    run_stimulus_gen_loop("test_stimulus_i2c_ack")
