# rr unknown-llm (AI-generated)
"""
test_stimulus_spi — DS-P2.100 / DS-D.38 option a fabric-side stimulus
generator, SPI master generator coverage (STIM-REQ-5, STIM-REQ-6), proven
against the REAL fabric RTL (tb_stimulus_gen_loop.vhd: tich.ds_stimulus_gen
-> tich.ds_emio_mux -> {tich.i2c_injector, tich.spi_injector}).

Split out of the original single-file test_stimulus_gen_loop.py — see
test_stimulus_uart.py's docstring for why. Mode 0, event-driven off SCLK.

Run (from the deepskopion dir):  rr sim run test_stimulus_spi
All four files together:         rr sim --tag stimulus_gen_loop
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, Event, FallingEdge, RisingEdge

from _stim_common import (  # noqa: E402 (sets up sys.path for sdk.* below)
    INJ_CTRL_ENABLE, pulse_write, reset_dut, run_stimulus_gen_loop, signal_event, start_clock,
)

from sdk.cocotb_helpers import requires  # noqa: E402


@cocotb.test()
@requires("STIM-REQ-5")
@requires("STIM-REQ-6")
async def test_spi_transfer_and_miso_readback(dut):
    await start_clock(dut)
    await reset_dut(dut)

    tx_byte = 0xA5
    rx_byte = 0x3C  # preloaded into spi_injector's FIFO

    dut.SPI_INJ_CONTROL.value = INJ_CTRL_ENABLE
    await pulse_write(dut, dut.SPI_INJ_DATAIN, dut.SPI_INJ_DATAIN_VLD, rx_byte)
    await ClockCycles(dut.CLK, 2)

    dut.STIM_SPI_CONTROL.value = 1
    dut.STIM_SPI_CLK_DIV.value = 6

    # Arm the CS-falling wait BEFORE triggering the transfer — the trigger
    # (pulse_write) advances the clock, and a wait started afterwards could
    # miss an edge that already happened.
    cs_ev = Event()
    cocotb.start_soon(signal_event(FallingEdge(dut.SPI0_CS_GPIO), cs_ev))
    await pulse_write(dut, dut.STIM_SPI_DATA, dut.STIM_SPI_DATA_VLD, tx_byte)
    await cs_ev.wait()
    assert dut.SPI0_CS_GPIO.value == 0

    got_mosi = 0
    got_miso = 0
    for _ in range(8):
        await RisingEdge(dut.SPI0_BUS_SCLK)
        got_mosi = (got_mosi << 1) | int(dut.SPI0_BUS_MOSI.value)
        got_miso = (got_miso << 1) | int(dut.SPI0_BUS_MISO.value)

    assert got_mosi == tx_byte, f"SPI MOSI on the wire = {got_mosi:#04x}, expected {tx_byte:#04x}"
    assert got_miso == rx_byte, f"SPI MISO read back = {got_miso:#04x}, expected {rx_byte:#04x} (spi_injector preload)"

    await RisingEdge(dut.SPI0_CS_GPIO)  # CS released after the 8th bit
    assert dut.SPI0_CS_GPIO.value == 1


if __name__ == "__main__":
    run_stimulus_gen_loop("test_stimulus_spi")
