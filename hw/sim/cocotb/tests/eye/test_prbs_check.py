"""prbs_check unit test (DS-P2.66, Phase 1).

Proves the self-synchronising PRBS checker that measures BER for the PMOD
eye-scan loopback:
  - a clean PRBS-7 stream LOCKS and reports ZERO errors;
  - a stuck-at-0 line (disconnected loopback) NEVER locks — no false green
    (the adversarial guard: a real PRBS-N never has N identical bits);
  - injected line errors are detected with the descrambler's known
    error-multiplication factor of (taps+1) = 3 per isolated bit error.

The Python PRBS golden mirrors prbs_gen.vhd EXACTLY (output recurrence
o[n] = o[n-N] xor o[n-M], seed all-ones), so the expected counts are
independent of the DUT (ROUTERTL-002).

Run (ROUTERTL-001):  rr sim run test_prbs_check
NOTE: rm -rf hw/sim/work before re-running after any RTL edit (RTL-P2.671).
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
SECOND_TAP = {7: 6, 15: 14, 23: 18, 31: 28}[ORDER]
TAPS = 2                       # 2-tap polynomial → error-multiplication = TAPS + 1
MULT = TAPS + 1                # = 3 detected errors per isolated injected bit error
LOCK_OK = 32


def prbs_golden(order: int, n_bits: int):
    """Mirror of prbs_gen.vhd: o[n] = sr(N-1) xor sr(M-1); sr <<= 1 | o (seed all-ones)."""
    m = {7: 6, 15: 14, 23: 18, 31: 28}[order]
    mask = (1 << order) - 1
    sr = mask  # all-ones seed
    for _ in range(n_bits):
        o = ((sr >> (order - 1)) & 1) ^ ((sr >> (m - 1)) & 1)
        yield o
        sr = ((sr << 1) | o) & mask


async def _reset(dut):
    dut.EN.value = 0
    dut.RX.value = 0
    dut.RST.value = 1
    await RisingEdge(dut.CLK)
    await RisingEdge(dut.CLK)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


async def _drive(dut, bits):
    """Drive a list/iterable of bits into RX, one per clock, EN high."""
    dut.EN.value = 1
    for b in bits:
        dut.RX.value = int(b)
        await RisingEdge(dut.CLK)


@cocotb.test()
async def clean_stream_locks_zero_errors(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset(dut)

    bits = list(prbs_golden(ORDER, 2000))
    await _drive(dut, bits)
    await RisingEdge(dut.CLK)

    assert int(dut.LOCKED.value) == 1, "clean PRBS must lock"
    assert int(dut.ERROR_COUNT.value) == 0, \
        f"clean PRBS must have 0 errors, got {int(dut.ERROR_COUNT.value)}"
    assert int(dut.BIT_COUNT.value) > 1000, \
        f"expected a healthy post-lock bit count, got {int(dut.BIT_COUNT.value)}"
    dut._log.info(f"✓ clean: LOCKED, errors=0, bits={int(dut.BIT_COUNT.value)}")


@cocotb.test()
async def stuck_at_zero_never_locks(dut):
    """Adversarial: a dead/disconnected loopback (constant 0) must NOT false-lock."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset(dut)

    await _drive(dut, [0] * 2000)
    await RisingEdge(dut.CLK)

    assert int(dut.LOCKED.value) == 0, \
        "stuck-at-0 must NEVER lock (no false green) — degenerate-state guard"
    assert int(dut.ERROR_COUNT.value) == 0 and int(dut.BIT_COUNT.value) == 0, \
        "no measurement should accrue while unlocked"
    dut._log.info("✓ stuck-at-0: never locked, no false-green BER")


@cocotb.test()
async def single_isolated_error_multiplies_by_three(dut):
    """One injected line error → exactly MULT(=3) detected errors (descrambler)."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset(dut)

    # Lock on clean bits, then inject ONE flip well inside the window so all 3
    # multiplications (at p, p+M, p+N) land before we stop counting.
    bits = list(prbs_golden(ORDER, 1500))
    flip_at = 1000                     # post-lock, with > ORDER trailing clean bits
    bits[flip_at] ^= 1
    await _drive(dut, bits)
    await RisingEdge(dut.CLK)

    assert int(dut.LOCKED.value) == 1, "must stay locked through one isolated error"
    got = int(dut.ERROR_COUNT.value)
    assert got == MULT, f"one isolated bit error → {MULT} detected (got {got})"
    dut._log.info(f"✓ isolated error: {got} detected (=taps+1), lock held")


@cocotb.test()
async def k_separated_errors_count_exactly(dut):
    """K well-separated line errors → exactly MULT*K detected errors."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset(dut)

    bits = list(prbs_golden(ORDER, 3000))
    k = 5
    # space flips by 100 (>> ORDER) so their multiplications never overlap,
    # and keep them away from the window ends.
    flip_positions = [500 + 100 * i for i in range(k)]
    for p in flip_positions:
        bits[p] ^= 1
    await _drive(dut, bits)
    await RisingEdge(dut.CLK)

    got = int(dut.ERROR_COUNT.value)
    assert got == MULT * k, f"{k} separated errors → {MULT*k} detected (got {got})"
    dut._log.info(f"✓ {k} separated errors → {got} detected (= {MULT}×{k})")


def main():
    run_simulation(
        top_level="prbs_check",
        module="test_prbs_check",
        top_level_lang="vhdl",
        generics={"G_ORDER": ORDER, "G_LOCK_OK": LOCK_OK, "G_CNT_W": 48},
        waves=False,
        work_lib="tich",
        custom_libraries={"tich": [str(test_dir.parents[4] / "hw/rtl/eye/prbs_check.vhd")]},
    )


if __name__ == "__main__":
    main()
