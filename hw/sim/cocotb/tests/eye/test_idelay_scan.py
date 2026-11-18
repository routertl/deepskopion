"""idelay_scan tap-delay test — REAL IDELAYE2 under nvc (DS-P3.114).

Proves the eye's HORIZONTAL (timing) axis on the *silicon* architecture. Since
RTL-P3.861 nvc can elaborate the unisim primitives, the model/xilinx split was
collapsed (DS-P3.114): idelay_scan.vhd now carries both `model` and `xilinx`
architectures, and nvc default-binds the LAST analysed one — `xilinx` — so this
test drives the genuine IDELAYE2 + IDELAYCTRL, not the idealised transport model.

What changes vs the old model test:
  * a 200 MHz REFCLK must run and IDELAYCTRL must calibrate (RDY) before the
    delay line is valid — the model asserted RDY='1' unconditionally;
  * tap 0 is NOT zero-delay — the real IDELAYE2 has an intrinsic base delay;
  * the per-tap delay is the primitive's actual sim timing, hard-pinned below
    from an observed run (ROUTERTL-002 — fixed expectations, not DUT-derived).

The invariant that matters for the bathtub is preserved and asserted:
monotonic, repeatable, load-held tap delay.

Run (ROUTERTL-001):  rr sim run test_idelay_scan
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer, Edge, First

from cocotb.utils import get_sim_time

test_dir = Path(__file__).resolve().parent
os.environ["ROUTERTL_WAVES_DIR"] = str(test_dir.parents[3] / "waves")

import sim.cocotb  # noqa: E402
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation  # noqa: E402

TAP_W = 5

# ── Observed IDELAYE2 sim timing (hard-pinned, ROUTERTL-002) ────────────────
# Filled in from an observe-mode run; see OBSERVE flag below. Each entry is the
# measured DIN→DOUT propagation delay (ps) for that tap under nvc's unisim
# IDELAYE2 with REFCLK_FREQUENCY=200.0. PINNED so a future toolchain change that
# shifts the tap resolution is caught, not silently absorbed.
OBSERVE = os.environ.get("IDELAY_OBSERVE") == "1"
# Observed under nvc 1.19.3 + unisim IDELAYE2 (REFCLK_FREQUENCY=200.0), DS-P3.114.
# The real primitive = 600 ps intrinsic base delay + exactly 78 ps/tap. The old
# idealised `model` arch asserted tap*78 with tap0=0 — it OMITTED the IDELAYE2's
# ~600 ps base insertion delay. Pinning the genuine numbers is strictly stronger.
EXPECT_PS = {
    0:  600.0,   # base delay (NOT transparent — real primitive)
    1:  678.0,   # +78
    5:  990.0,   # 600 + 5*78
    10: 1380.0,  # 600 + 10*78
    20: 2160.0,  # 600 + 20*78
    31: 3018.0,  # 600 + 31*78
}

SWEEP_TAPS = (0, 1, 5, 10, 20, 31)


async def _calibrate(dut):
    """Start CLK (load domain) + 200 MHz REFCLK, pulse RST, wait for IDELAYCTRL RDY."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())       # 100 MHz load/C domain
    cocotb.start_soon(Clock(dut.REFCLK, 5, units="ns").start())     # 200 MHz IDELAYCTRL ref
    dut.RST.value = 1
    dut.LD.value = 0
    dut.TAP.value = 0
    dut.DIN.value = 0
    for _ in range(8):
        await RisingEdge(dut.CLK)
    dut.RST.value = 0
    # IDELAYCTRL needs REFCLK cycles to calibrate; bound the wait so a dead
    # controller fails loudly instead of hanging the suite.
    for _ in range(200):
        await RisingEdge(dut.REFCLK)
        if int(dut.RDY.value) == 1:
            break
    assert int(dut.RDY.value) == 1, "IDELAYCTRL never asserted RDY (calibration failed)"


async def _load_tap(dut, tap):
    """Latch a tap value via LD in the CLK (C) domain."""
    dut.TAP.value = tap
    dut.LD.value = 1
    await RisingEdge(dut.CLK)
    dut.LD.value = 0
    await RisingEdge(dut.CLK)


async def _measure_edge_delay(dut, new_level):
    """Drive DIN to new_level away from a CLK edge; return DOUT propagation delay (ps)."""
    await Timer(3, units="ns")
    t_in = get_sim_time("ps")
    dut.DIN.value = new_level
    await First(Edge(dut.DOUT), Timer(50, units="ns"))
    t_out = get_sim_time("ps")
    assert int(dut.DOUT.value) == new_level, "DOUT never followed DIN"
    return t_out - t_in


@cocotb.test()
async def idelayctrl_calibrates(dut):
    """The real IDELAYCTRL must calibrate (RDY high) before the line is usable."""
    await _calibrate(dut)
    dut._log.info("✓ IDELAYCTRL calibrated (RDY=1)")


@cocotb.test()
async def tap_delay_monotone_and_pinned(dut):
    await _calibrate(dut)

    level = 1
    prev = -1
    measured = {}
    for tap in SWEEP_TAPS:
        await _load_tap(dut, tap)
        d = await _measure_edge_delay(dut, level)
        measured[tap] = d
        # Invariant that the bathtub abscissa rests on: delay rises with tap.
        assert d >= prev, f"tap {tap}: delay {d} ps must not decrease (prev {prev})"
        prev = d
        level ^= 1
        dut._log.info(f"  tap {tap:2d}: {d} ps")

    if OBSERVE:
        dut._log.warning(f"[OBSERVE] measured IDELAYE2 tap delays (ps): {measured}")
        return

    assert EXPECT_PS, "EXPECT_PS not pinned — run once with IDELAY_OBSERVE=1 and fill it in"
    for tap in SWEEP_TAPS:
        assert measured[tap] == EXPECT_PS[tap], (
            f"tap {tap}: expected {EXPECT_PS[tap]} ps (pinned), got {measured[tap]} ps"
        )
    dut._log.info("✓ tap delays match pinned IDELAYE2 sim timing")


@cocotb.test()
async def load_holds_until_next_ld(dut):
    """A tap stays applied until the NEXT LD — TAP changes without LD are ignored."""
    await _calibrate(dut)

    await _load_tap(dut, 10)
    d_held = await _measure_edge_delay(dut, 1)
    # Change TAP WITHOUT LD — must not take effect.
    dut.TAP.value = 31
    await RisingEdge(dut.CLK)
    d_after = await _measure_edge_delay(dut, 0)
    assert d_after == d_held, (
        f"unloaded TAP change must be ignored; held {d_held} ps, got {d_after} ps"
    )
    dut._log.info(f"✓ tap held at 10 ({d_held} ps) despite TAP=31 without LD")


def main():
    run_simulation(
        top_level="idelay_scan",          # nvc binds the last arch analysed → xilinx
        module="test_idelay_scan",
        top_level_lang="vhdl",
        generics={"G_TAP_W": TAP_W},
        waves=False,
        work_lib="tich",
        custom_libraries={"tich": [str(test_dir.parents[4] / "hw/rtl/eye/idelay_scan.vhd")]},
    )


if __name__ == "__main__":
    main()
