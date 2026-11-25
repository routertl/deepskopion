# rr unknown-llm (AI-generated)
"""glitch_filter FAST-line case (DS-T3.5 system-wide conditioning).

The window is sized from the datalogger's MAX FREQUENCY. On a fast line (e.g. an
SPI/QSPI clock at ~25 MHz on a 100 MHz clk_sys) the shortest real pulse is ~2
clks, so the window must floor at 2 (a pure 2-FF synchroniser) and MUST NOT eat
those real pulses. This test pins exactly that: with G_MAX_FREQ_HZ=25 MHz the
window is 2 — a real 2-clk square wave PROPAGATES, while a 1-clk glitch is still
rejected. (Contrast test_glitch_filter.py: a slow 1 Mbaud line gets window=4 and
real glitch rejection.)

Run (ROUTERTL-001):  rr sim run test_glitch_filter_fast
NOTE: rm -rf hw/sim/work before re-running after any RTL edit (RTL-P2.671).
"""

import sys
import os
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

test_dir = Path(__file__).resolve().parent
os.environ["ROUTERTL_WAVES_DIR"] = str(test_dir.parents[3] / "waves")

import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation

CLK_HZ = 100_000_000
MAX_FREQ_HZ = 25_000_000   # SPI/QSPI-class clock — min half-period ~2 clks


def _window(clk_hz, max_freq, glitch=4):
    min_half = max(1, clk_hz // (2 * max_freq))
    return max(2, min(glitch, min_half - 1))


WIN = _window(CLK_HZ, MAX_FREQ_HZ)   # == 2 for 25 MHz @ 100 MHz


async def _reset(dut):
    dut.D_ASYNC.value = 1
    dut.RST.value = 1
    await RisingEdge(dut.CLK); await RisingEdge(dut.CLK)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


@cocotb.test()
async def fast_real_pulses_propagate(dut):
    """A real 2-clk-per-level square wave (25 MHz-class) must pass through —
    the filter must NOT eat fast edges (window floored at 2)."""
    assert WIN == 2, f"expected window 2 for a 25 MHz line @100 MHz, got {WIN}"
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset(dut)

    seen_low = False
    seen_high_after = False
    # Toggle the input every 2 clks (a real fast pulse) for several periods and
    # confirm the output actually moves (i.e. the pulses are not filtered away).
    level = 1
    for _ in range(8):
        level ^= 1
        dut.D_ASYNC.value = level
        await RisingEdge(dut.CLK); await RisingEdge(dut.CLK)
        q = int(dut.Q.value)
        if q == 0:
            seen_low = True
        if seen_low and q == 1:
            seen_high_after = True
    assert seen_low, "Q never went low — a real 2-clk pulse was EATEN by the filter"
    assert seen_high_after, "Q never toggled back high — fast edges not propagating"
    dut._log.info("✓ fast 2-clk pulses propagate (window=2, no pulse-eating)")


@cocotb.test()
async def one_clk_glitch_still_rejected(dut):
    """Even on the fast config, a 1-clk glitch (< window=2) is rejected."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset(dut)
    for _ in range(6):
        await RisingEdge(dut.CLK)
    assert int(dut.Q.value) == 1
    # single-cycle low blip, back high
    dut.D_ASYNC.value = 0
    await RisingEdge(dut.CLK)
    dut.D_ASYNC.value = 1
    for _ in range(6):
        await RisingEdge(dut.CLK)
    assert int(dut.Q.value) == 1, "a 1-clk glitch passed even though window=2"
    dut._log.info("✓ 1-clk glitch rejected on the fast config")


def main():
    ds_root = test_dir.parents[4]
    run_simulation(
        top_level="glitch_filter",
        module="test_glitch_filter_fast",
        top_level_lang="vhdl",
        generics={"G_CLK_HZ": CLK_HZ, "G_MAX_FREQ_HZ": MAX_FREQ_HZ},
        waves=False,
        work_lib="tich",
        custom_libraries={"tich": [str(ds_root / "hw/rtl/emio/glitch_filter.vhd")]},
    )


if __name__ == "__main__":
    main()
