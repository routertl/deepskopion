"""prbs_gen unit test (DS-P2.66, Phase 2 quick-win).

Confirms the RTL PRBS generator emits EXACTLY the Python golden sequence the
checker's expectations are built on — closing the loop on Phase 1 by proving
gen and golden agree bit-for-bit. If this drifts, every prbs_check expectation
(error-multiplication = taps+1, the bathtub BER axis) would silently rest on a
mismatched reference.

The golden mirrors prbs_gen.vhd: o[n] = sr(N-1) xor sr(M-1), seed all-ones
(ROUTERTL-002 — the expectation is independent of the DUT).

Run (ROUTERTL-001):  rr sim run test_prbs_gen
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

test_dir = Path(__file__).resolve().parent
os.environ["ROUTERTL_WAVES_DIR"] = str(test_dir.parents[3] / "waves")

import sim.cocotb  # noqa: E402
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation  # noqa: E402

ORDER = 7


def prbs_golden(order: int, n_bits: int):
    """Mirror of prbs_gen.vhd: o[n] = sr(N-1) xor sr(M-1); sr <<= 1 | o (seed all-ones)."""
    m = {7: 6, 15: 14, 23: 18, 31: 28}[order]
    mask = (1 << order) - 1
    sr = mask
    for _ in range(n_bits):
        o = ((sr >> (order - 1)) & 1) ^ ((sr >> (m - 1)) & 1)
        yield o
        sr = ((sr << 1) | o) & mask


async def _reset(dut):
    dut.EN.value = 0
    dut.RST.value = 1
    await RisingEdge(dut.CLK)
    await RisingEdge(dut.CLK)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


@cocotb.test()
async def dout_matches_golden_sequence(dut):
    """Every registered DOUT bit must equal the next golden PRBS bit."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset(dut)

    n = 2000
    golden = list(prbs_golden(ORDER, n))

    # Raise EN, then one settle edge: a write issued from a RisingEdge-resumed
    # coroutine only takes effect at the NEXT edge (cocotb write-apply latency),
    # so the DUT first sees EN=1 from the edge after this one — where golden[0]
    # lands on the registered DOUT.
    dut.EN.value = 1
    await RisingEdge(dut.CLK)
    got = []
    for _ in range(n):
        await RisingEdge(dut.CLK)
        got.append(int(dut.DOUT.value))   # capture starts exactly at golden[0]

    # DOUT is registered: after the k-th EN edge it holds golden[k-1], so the
    # captured stream is golden[0..n-1] (the first sample is golden[0]).
    assert got == golden, (
        f"prbs_gen diverged from golden at index {next(i for i in range(n) if got[i] != golden[i])}"
    )

    # Adversarial sanity: a maximal PRBS-7 is NOT constant and has near-balanced
    # ones density (2^6 ones / (2^7-1) per period) — guards against a stuck gen
    # that would also "match" a buggy all-zero golden.
    assert 0 < sum(got) < n, "PRBS output must toggle (not stuck), and not be all-ones"
    period = (1 << ORDER) - 1
    ones = sum(got[:period])
    assert ones == (1 << (ORDER - 1)), \
        f"PRBS-{ORDER} must have {1 << (ORDER - 1)} ones per {period}-bit period, got {ones}"
    dut._log.info(f"✓ prbs_gen == golden over {n} bits; {ones} ones/period (maximal-length)")


def main():
    run_simulation(
        top_level="prbs_gen",
        module="test_prbs_gen",
        top_level_lang="vhdl",
        generics={"G_ORDER": ORDER},
        waves=False,
        work_lib="tich",
        custom_libraries={"tich": [str(test_dir.parents[4] / "hw/rtl/eye/prbs_gen.vhd")]},
    )


if __name__ == "__main__":
    main()
