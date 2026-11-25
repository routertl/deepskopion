# rr unknown-llm (AI-generated)
"""can_destuff FIXED-mode + adjacency test (DS-P2.64 step 3, CAN-REQ-17).

The CAN-FD stuff-count and CRC fields use FIXED stuffing: a stuff bit precedes the
field and recurs after every 4 bits (stream positions 0,5,10,…), each the INVERSE
of the immediately preceding wire bit. This test drives fixed-stuffed streams and
asserts the de-stuffed output equals the ORIGINAL real bits (hard-coded,
ROUTERTL-002), with FORM_ERROR only when a fixed stuff bit is malformed.

Adversarial corners (each able to FIND a real bug):
  1. dynamic→fixed ADJACENCY — the dynamic region ends on five identical bits,
     right before the stuff count. ISO 11898-1:2015 paragraph 10.5: "There shall
     be a fixed stuff bit before the first bit of the stuff count, even if the
     last bits of the preceding field are a sequence of five consecutive bits of
     identical value, there shall be only the fixed stuff bit, there shall not be
     two consecutive stuff bits." So the wire carries ONE stuff bit there, the
     fixed one, and the fixed counter anchors on it. A block that expects a
     dynamic stuff first eats the first real bit of the stuff count.
  2. long identical run INSIDE fixed mode — 7 identical real bits, which are LEGAL
     in fixed mode (no dynamic stuffing there). If the block still ran the 5-run
     rule it would wrongly raise STUFF_ERROR / eat a real bit.
  3. corrupt fixed stuff — a fixed stuff bit equal to its predecessor must raise
     FORM_ERROR, never a silent drop.

`fixed_stuff()` is STIMULUS (the encode side, like a BFM); the checker asserts
against the hard-coded ORIGINAL, never a value re-derived from the DUT.
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

DS_ROOT = Path(__file__).resolve().parents[6]
RTL = DS_ROOT / "hw" / "rtl" / "datalogger" / "can" / "can_destuff.vhd"


def fixed_stuff(real_bits, last_wire_bit):
    """Encode a CAN-FD fixed-stuffed stream: a stuff bit (inverse of the preceding
    wire bit) at stream positions 0,5,10,…, with `real_bits` filling the 4-bit gaps.
    Returns (wire_stream, stuff_indices). Stimulus only."""
    out, stuff_idx, prev, ri = [], [], last_wire_bit, 0
    while ri < len(real_bits):
        s = 1 - prev                       # fixed stuff = inverse of preceding bit
        stuff_idx.append(len(out))
        out.append(s); prev = s
        for _ in range(4):
            if ri >= len(real_bits):
                break
            out.append(real_bits[ri]); prev = real_bits[ri]; ri += 1
    return out, stuff_idx


# A 21-bit "real" field (stuff-count + CRC-17 worth of bits) containing a 7-long
# run of identical bits — legal under FIXED stuffing, illegal under dynamic.
REAL_FIXED = [1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1]


async def feed_bit(dut, b):
    dut.IN_BIT.value = b
    dut.IN_VALID.value = 1
    await RisingEdge(dut.CLK)
    dut.IN_VALID.value = 0
    await ClockCycles(dut.CLK, 3)


async def setup(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
    dut.RST.value = 1
    dut.STUFF_EN.value = 0
    dut.STUFF_MODE.value = 0
    dut.IN_VALID.value = 0
    dut.IN_BIT.value = 1
    await ClockCycles(dut.CLK, 4)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


def _collector(dut, out, errs):
    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.OUT_VALID.value) == 1:
                out.append(int(dut.OUT_BIT.value))
            if int(dut.STUFF_ERROR.value) == 1:
                errs["stuff"] += 1
            if int(dut.FORM_ERROR.value) == 1:
                errs["form"] += 1
    return collect


@cocotb.test()
async def fixed_only_recovers_real(dut):
    """Pure fixed region (no dynamic prefix): stuffs removed, 7-run passes (corner 2)."""
    await setup(dut)
    out, errs = [], {"stuff": 0, "form": 0}
    cocotb.start_soon(_collector(dut, out, errs)())

    # After reset prev='1'; encode with last_wire_bit=1 to match.
    wire, _ = fixed_stuff(REAL_FIXED, last_wire_bit=1)
    dut.STUFF_EN.value = 1
    dut.STUFF_MODE.value = 1
    for b in wire:
        await feed_bit(dut, b)
    dut.STUFF_EN.value = 0
    dut.STUFF_MODE.value = 0
    await ClockCycles(dut.CLK, 5)

    assert out == REAL_FIXED, f"fixed de-stuff != real: got {out}, expected {REAL_FIXED}"
    assert errs["form"] == 0 and errs["stuff"] == 0, (
        f"unexpected errors on a well-formed fixed stream: {errs}")
    dut._log.info(f"can_destuff FD PASS — fixed region recovered {len(out)} real bits "
                  f"({len(wire) - len(out)} fixed stuffs removed), incl. a 7-run, 0 errors")


@cocotb.test()
async def dynamic_to_fixed_adjacency(dut):
    """Corner 1: the dynamic data ends on five identical bits, then the fixed region.

    ISO 11898-1:2015 paragraph 10.5 (quoted verbatim in the Linux kernel commit
    "can: length: fix bitstuffing count", 2023): "In the CRC field of FD Frames,
    the stuff bits shall be inserted at fixed positions; they are called fixed
    stuff bits. There shall be a fixed stuff bit before the first bit of the stuff
    count, even if the last bits of the preceding field are a sequence of five
    consecutive bits of identical value, there shall be only the fixed stuff bit,
    there shall not be two consecutive stuff bits. A further fixed stuff bit shall
    be inserted after each fourth bit of the CRC field [...]"

    So the wire after the five 0s is ONE stuff bit (the fixed one, =1), then the
    fixed region, with a fixed stuff after each 4 real bits. The wire is hard-coded
    (the CTU CAN FD core sends this shape, DS-T2.96). The stuff bits are removed,
    the real bits recovered, no errors."""
    await setup(dut)
    out, errs = [], {"stuff": 0, "form": 0}
    cocotb.start_soon(_collector(dut, out, errs)())

    # Dynamic phase: de-stuffed data ending in five 0s (forces a dynamic stuff=1).
    dyn_data = [0, 1, 0, 0, 0, 0, 0]
    dut.STUFF_EN.value = 1
    dut.STUFF_MODE.value = 0
    for b in dyn_data:
        await feed_bit(dut, b)

    # Boundary: run is now 5. Raise STUFF_MODE; the wire carries ONLY the first
    # fixed stuff (=1, inverse of the 0-run, ISO 10.5), then 4 real bits, a fixed
    # stuff, 4 real bits, a fixed stuff, 3 real bits.
    dut.STUFF_MODE.value = 1
    fixed_real = [1, 1, 0, 1, 0, 0, 1, 0, 1, 1, 0]  # stuff-count+CRC real bits
    wire = [1, 1, 1, 0, 1, 0, 0, 0, 1, 0, 1, 1, 1, 0]
    for b in wire:
        await feed_bit(dut, b)
    dut.STUFF_EN.value = 0
    dut.STUFF_MODE.value = 0
    await ClockCycles(dut.CLK, 5)

    expected = dyn_data + fixed_real
    assert out == expected, f"adjacency de-stuff != expected: got {out}, expected {expected}"
    assert errs["stuff"] == 0 and errs["form"] == 0, (
        f"unexpected errors across the dynamic→fixed boundary: {errs}")
    dut._log.info(f"can_destuff FD PASS — dynamic→fixed adjacency clean: the single "
                  f"fixed stuff anchors the fixed region, {len(out)} real bits recovered")


@cocotb.test()
async def corrupt_fixed_stuff_raises_form_error(dut):
    """Corner 3: a fixed stuff bit equal to its predecessor → exactly one FORM_ERROR."""
    await setup(dut)
    out, errs = [], {"stuff": 0, "form": 0}
    cocotb.start_soon(_collector(dut, out, errs)())

    wire, stuff_idx = fixed_stuff(REAL_FIXED, last_wire_bit=1)
    # Corrupt the SECOND fixed stuff (index stuff_idx[1]) to equal its predecessor.
    bad = stuff_idx[1]
    wire[bad] = wire[bad - 1]                       # make stuff == preceding bit (illegal)

    dut.STUFF_EN.value = 1
    dut.STUFF_MODE.value = 1
    for b in wire:
        await feed_bit(dut, b)
    dut.STUFF_EN.value = 0
    dut.STUFF_MODE.value = 0
    await ClockCycles(dut.CLK, 5)

    assert errs["form"] == 1, f"expected exactly 1 FORM_ERROR on a corrupt fixed stuff, got {errs}"
    assert errs["stuff"] == 0, f"a fixed-stuff fault must NOT raise dynamic STUFF_ERROR: {errs}"
    dut._log.info("can_destuff FD PASS — corrupt fixed stuff raised FORM_ERROR exactly once")


def main():
    run_simulation(
        top_level="can_destuff",
        module="test_can_destuff_fd",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [str(RTL)]},
    )


if __name__ == "__main__":
    main()
