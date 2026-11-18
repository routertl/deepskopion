"""fifo_sync boundary: words written come out in order (DS-P2.137).

Runs the portable FIFO boundary on its own under ``rr sim run --ip hw/rtl/fifo``.
The closure is the manifest's alone: fifo_sync.vhd plus the registry
open-logic package.
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from engine.simulation import run_simulation  # noqa: E402

WORDS = [0xDEADBEEF, 0x00000001, 0x80000000, 0x12345678]


def main():
    run_simulation(
        top_level="fifo_sync",
        module="test_fifo_sync",
        generics={"G_FIFO_DEPTH": 4, "G_FIFO_WIDTH": 32, "G_FIFO_FWFT": 1},
        waves=True,
    )


@cocotb.test()
async def test_fwft_words_come_out_in_write_order(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    dut.RST.value = 1
    dut.WR_EN.value = 0
    dut.RD_EN.value = 0
    dut.DIN.value = 0
    await ClockCycles(dut.CLK, 4)
    dut.RST.value = 0
    while int(dut.RST_BUSY.value):
        await RisingEdge(dut.CLK)
    await ClockCycles(dut.CLK, 2)
    assert int(dut.EMPTY.value) == 1, "FIFO must be empty after reset"

    for word in WORDS:
        dut.DIN.value = word
        dut.WR_EN.value = 1
        await RisingEdge(dut.CLK)
    dut.WR_EN.value = 0
    await ClockCycles(dut.CLK, 4)
    assert int(dut.EMPTY.value) == 0, "FIFO empty after writing 4 words"

    got = []
    for _ in WORDS:
        got.append(int(dut.DOUT.value))
        dut.RD_EN.value = 1
        await RisingEdge(dut.CLK)
        dut.RD_EN.value = 0
        await RisingEdge(dut.CLK)
    assert got == WORDS, f"FIFO order wrong: {[hex(g) for g in got]}"


if __name__ == "__main__":
    main()
