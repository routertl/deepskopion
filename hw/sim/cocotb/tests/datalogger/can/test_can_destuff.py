# rr unknown-llm (AI-generated)
"""can_destuff unit test (DS-P3.100, Block 2).

Drives the (stuffed) on-wire bit stream and asserts the de-stuffed output equals
the ORIGINAL data — and that a malformed 6-in-a-row run raises STUFF_ERROR.

The `stuff()` helper is STIMULUS generation (the encode side, like a BFM); the
checker asserts against the hard-coded original (ROUTERTL-002), never against a
value re-derived from the DUT.
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

DS_ROOT = Path(__file__).resolve().parents[6]   # deepskopion/
RTL = DS_ROOT / "hw" / "rtl" / "datalogger" / "can" / "can_destuff.vhd"


def stuff(bits):
    """Standard CAN stuffing: after 5 identical bits insert one complementary
    bit (which seeds the next run). Stimulus only."""
    out, run, prev = [], 0, None
    for b in bits:
        run = run + 1 if b == prev else 1
        prev = b
        out.append(b)
        if run == 5:
            s = 1 - b
            out.append(s)
            prev, run = s, 1
    return out


# Hard-coded original data with a 6-zero run and a 6-one run (each forces a stuff
# bit) plus a no-run tail. The de-stuffed DUT output must equal THIS.
ORIGINAL = [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 1, 0, 0, 1]


async def feed_bit(dut, b):
    dut.IN_BIT.value = b
    dut.IN_VALID.value = 1
    await RisingEdge(dut.CLK)
    dut.IN_VALID.value = 0
    await ClockCycles(dut.CLK, 3)        # inter-bit gap (mimics the sample cadence)


async def setup(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    dut.RST.value = 1
    dut.STUFF_EN.value = 0
    dut.STUFF_MODE.value = 0          # dynamic (classic) mode for these vectors
    dut.IN_VALID.value = 0
    dut.IN_BIT.value = 1
    await ClockCycles(dut.CLK, 4)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


@cocotb.test()
async def destuff_recovers_original(dut):
    await setup(dut)
    out, errs = [], 0

    async def collect():
        nonlocal errs
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.OUT_VALID.value) == 1:
                out.append(int(dut.OUT_BIT.value))
            if int(dut.STUFF_ERROR.value) == 1:
                errs += 1
    cocotb.start_soon(collect())

    dut.STUFF_EN.value = 1
    for b in stuff(ORIGINAL):
        await feed_bit(dut, b)
    dut.STUFF_EN.value = 0
    await ClockCycles(dut.CLK, 5)

    assert out == ORIGINAL, f"de-stuffed != original: got {out}, expected {ORIGINAL}"
    assert errs == 0, f"unexpected STUFF_ERROR on a well-formed stream ({errs})"
    dut._log.info(f"can_destuff PASS — recovered {len(out)} original bits, "
                  f"{len(stuff(ORIGINAL)) - len(ORIGINAL)} stuff bits removed, 0 errors")


@cocotb.test()
async def destuff_flags_six_run(dut):
    """A 6-identical-bit run (a stuff violation a healthy bus can't produce) must
    raise STUFF_ERROR — CAN's first line of corruption detection."""
    await setup(dut)
    errs = 0

    async def collect():
        nonlocal errs
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.STUFF_ERROR.value) == 1:
                errs += 1
    cocotb.start_soon(collect())

    dut.STUFF_EN.value = 1
    for b in [0, 0, 0, 0, 0, 0]:          # 6 dominant with no stuff bit -> illegal
        await feed_bit(dut, b)
    await ClockCycles(dut.CLK, 5)

    assert errs == 1, f"expected exactly 1 STUFF_ERROR on a 6-run, got {errs}"
    dut._log.info("can_destuff PASS — 6-in-a-row raised STUFF_ERROR exactly once")


def main():
    run_simulation(
        top_level="can_destuff",
        module="test_can_destuff",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [str(RTL)]},
    )


if __name__ == "__main__":
    main()
