"""DS-P1.90: the PS-side self-test bench hands the EMIO legs back to the PS.

On the Zybo (deepskopion_top sets G_STIM_DRIVES_EMIO) the fabric stimulus
generator drives a protocol's EMIO legs while that protocol's STIM_*_CONTROL
bit[0] is set, and the stimulus armers leave the bit set. The DS-X5 PS-LOOP
self-test drives the PS EMIO legs, so it would see nothing after any stimulus
arm unless the bench clears the three muxed enables when it opens.
"""
from __future__ import annotations

import pytest

from sw.stream.testbench import emio_inject

# The regbank byte offsets, hard-coded (not read back from Regs) so a moved
# slot fails here instead of moving the test with it.
STIM_UART_CONTROL = 0x0220
STIM_I2C_CONTROL = 0x022C
STIM_SPI_CONTROL = 0x0238


class _FakeMmio:
    DEFAULT_BASE_ADDR = 0x4000_0000

    def __init__(self, base_addr=None):
        self.writes = []
        self.regs = {STIM_UART_CONTROL: 1, STIM_I2C_CONTROL: 0xA1, STIM_SPI_CONTROL: 1}

    def write_u32(self, offset, value):
        self.writes.append((offset, value))
        self.regs[offset] = value

    def read_u32(self, offset):
        return self.regs.get(offset, 0)

    def close(self):
        pass


class _Dev:
    def __init__(self, *a, **k):
        pass

    def close(self):
        pass


@pytest.fixture
def bench(monkeypatch):
    made = []

    def mmio(base_addr=None):
        m = _FakeMmio(base_addr)
        made.append(m)
        return m

    monkeypatch.setattr(emio_inject, "MmioRegbank", mmio)
    for name in ("UART", "I2CMaster", "SPIMaster"):
        monkeypatch.setattr(emio_inject, name, _Dev)
    emio_inject.SelfTestBench(enable_dataloggers=False)
    return made[0]


def test_opening_the_bench_clears_the_three_muxed_generator_enables(bench):
    for offset in (STIM_UART_CONTROL, STIM_I2C_CONTROL, STIM_SPI_CONTROL):
        assert (offset, 0) in bench.writes, (
            f"SelfTestBench did not clear STIM control {offset:#06x}; a prior "
            "stimulus arm would keep the PS EMIO legs away from the dataloggers")
        assert bench.regs[offset] == 0


def test_the_offsets_are_the_regbank_contract():
    regs = emio_inject.Regs
    assert (regs.STIM_UART_CONTROL, regs.STIM_I2C_CONTROL, regs.STIM_SPI_CONTROL) == (
        STIM_UART_CONTROL, STIM_I2C_CONTROL, STIM_SPI_CONTROL)
