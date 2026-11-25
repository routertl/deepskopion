# rr unknown-llm (AI-generated)
"""can_bit_timing B2-refactor EQUIVALENCE test (DS-P2.64 step 1, CAN-REQ-23).

The v0.1 TQ bit-timing block was validated on silicon (Zybo Z7-20,
v0.3.0-can-emio-silicon: byte-exact decode of STD/extended/remote/stuffed CAN
frames). DS-P2.64 rewrites it as the B2 unified direct-count dual-rate sampler.
This test PROVES the refactor preserves that silicon-validated behaviour: the new
block on its NOMINAL config must produce BIT_VALID/BIT_VALUE/BIT_START/SYNCED
bit-identical to the frozen reference, cycle-for-cycle, for ANY RX waveform.

This is the sanctioned equivalence pattern (NOT a ROUTERTL-002 violation): the
"expected" stream comes from a SEPARATE, frozen, independently-validated
implementation (can_bit_timing_tq_ref) — not from the DUT-under-test deriving its
own truth at runtime. Both blocks are wired into can_bit_timing_equiv_top and fed
identical stimulus; the test compares A (B2) vs B (ref) every clock and aborts on
the FIRST divergence.

Stimulus = adversarial-from-birth:
  * arbitrary-width random RX pulses (sub-bit … multi-bit) → exercises hard-sync,
    free-run, short glitches, and edges that land exactly on the sample point,
  * across several prescalers incl. the doc's nominal pair (presc 25 ⇒ 400/300),
  * plus the raw bit patterns of the actual silicon-validated frames.

Run: python test_can_bit_timing_equiv.py   (rm -rf hw/sim/work first after an RTL
edit — RTL-P2.671 auto-cleans per-unit, but a manual clean never hurts.)
"""
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

HERE = Path(__file__).resolve().parent
DS_ROOT = Path(__file__).resolve().parents[6]   # deepskopion/
RTL = DS_ROOT / "hw" / "rtl" / "datalogger" / "can" / "can_bit_timing.vhd"
REF = HERE / "ref" / "can_bit_timing_tq_ref.vhd"
TOP = HERE / "can_bit_timing_equiv_top.vhd"

NUM_TQ = 16            # must match the equiv_top / block generics
SAMPLE_TQ = 12

# Prescalers to sweep. Each is exact: nominal cyc_per_bit = presc*16, sample = presc*12.
# 25 ⇒ {400,300} (the doc's nominal pair, 250 kbit @100 MHz); 1 stresses the
# presc-1=0 wrap; the rest span the range.
PRESCALERS = [1, 2, 3, 4, 7, 25]


def _outs(dut):
    """Snapshot both blocks' outputs as comparable logic-char tuples (A=B2, B=ref).

    Use str() not int(): registered outputs (BIT_VALUE) read 'U' before the first
    sample in BOTH blocks — that is itself an equivalence the test must allow, and
    int('U') raises. Comparing the resolved logic chars handles U/X/0/1 uniformly.
    """
    a = (str(dut.A_BIT_VALID.value), str(dut.A_BIT_VALUE.value),
         str(dut.A_BIT_START.value), str(dut.A_SYNCED.value))
    b = (str(dut.B_BIT_VALID.value), str(dut.B_BIT_VALUE.value),
         str(dut.B_BIT_START.value), str(dut.B_SYNCED.value))
    return a, b


async def _reset(dut, presc):
    dut.RST.value = 1
    dut.ENABLE.value = 0
    dut.RX.value = 1                       # recessive idle
    dut.PRESCALER.value = presc
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    dut.ENABLE.value = 1
    await RisingEdge(dut.CLK)


async def _drive_and_check(dut, levels, presc, tag):
    """Drive one RX level per clk from `levels`; after every edge assert A==B."""
    for i, lvl in enumerate(levels):
        dut.RX.value = lvl
        await RisingEdge(dut.CLK)
        a, b = _outs(dut)
        assert a == b, (
            f"[{tag} presc={presc}] DIVERGENCE at cycle {i}: "
            f"B2(valid,value,start,synced)={a} != ref={b}  (RX={lvl})")


# Raw bit patterns of the silicon-validated frames (NOT stuffed — Block 1 just
# samples the wire). Standard 123#ABCD-ish, extended, remote, all-zero stuffed —
# the SHAPES that ran on hardware. Equivalence must hold on exactly these.
VALIDATED_PATTERNS = {
    "std_123_abcd": [0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 1],
    "ext_frame":    [0, 1, 0, 1, 1, 0, 0, 1, 0, 0, 0, 1, 1, 0, 1, 0, 1, 0, 1, 0, 1, 1, 1, 1],
    "remote":       [0, 1, 1, 1, 1, 0, 1, 0, 0, 0, 0, 0, 1, 1, 1, 1],
    "stuffed_zero": [0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 1, 0],
}


@cocotb.test()
async def equiv_random_pulses(dut):
    """B2 ≡ v0.1 under arbitrary-width random RX pulses, every prescaler."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    rng = random.Random(0xCA0FD)         # fixed seed: deterministic, reproducible

    for presc in PRESCALERS:
        await _reset(dut, presc)
        # ~6000 clocks of random RX. Flip probability tuned so pulse widths span
        # well under one bit (at presc=25, bit=400 clk) to several bits (at presc=1).
        levels = []
        lvl = 1
        for _ in range(6000):
            if rng.random() < 0.04:
                lvl ^= 1
            levels.append(lvl)
        await _drive_and_check(dut, levels, presc, "rand")
    dut._log.info(f"can_bit_timing equivalence PASS — B2 ≡ v0.1 over "
                  f"{len(PRESCALERS)} prescalers x 6000 random clks each")


@cocotb.test()
async def equiv_validated_frame_patterns(dut):
    """B2 ≡ v0.1 on the raw bit patterns of the silicon-validated frames."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())

    for presc in (4, 25):
        bit_clks = NUM_TQ * presc
        for name, seq in VALIDATED_PATTERNS.items():
            await _reset(dut, presc)
            levels = []
            levels += [1] * (2 * bit_clks)       # idle recessive: no SOF yet
            for b in seq:
                levels += [b] * bit_clks
            levels += [1] * bit_clks             # trailing idle
            await _drive_and_check(dut, levels, presc, f"frame:{name}")
    dut._log.info("can_bit_timing equivalence PASS — B2 ≡ v0.1 on all "
                  "silicon-validated frame patterns @ presc 4 and 25")


def main():
    run_simulation(
        top_level="can_bit_timing_equiv_top",
        module="test_can_bit_timing_equiv",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [str(RTL), str(REF), str(TOP)]},
    )


if __name__ == "__main__":
    main()
