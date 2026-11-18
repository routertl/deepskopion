"""glitch_filter unit test (DS-T3.5).

Proves the input conditioner that fixes the uart_datalogger's silicon mis-decode:
  - a short glitch (< G_STABLE_CYCLES) on the async input is REJECTED (Q holds);
  - a real, sustained level change PROPAGATES (Q follows after the debounce);
  - idle level is held through reset.

Run (ROUTERTL-001):  rr sim run test_glitch_filter
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
MAX_FREQ_HZ = 1_000_000   # UART ~1 Mbaud — the auto-cal ceiling at 100 MHz clk_sys


def _window(clk_hz, max_freq, glitch=4):
    """Mirror glitch_filter.vhd's window math: clamp(2, glitch, min_half-1)."""
    min_half = max(1, clk_hz // (2 * max_freq))
    return max(2, min(glitch, min_half - 1))


STABLE = _window(CLK_HZ, MAX_FREQ_HZ)   # = 4 for 1 Mbaud @ 100 MHz


async def _reset(dut):
    dut.D_ASYNC.value = 1            # UART idle high
    dut.RST.value = 1
    await RisingEdge(dut.CLK)
    await RisingEdge(dut.CLK)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


@cocotb.test()
async def glitch_rejected_real_edge_passes(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset(dut)

    # Idle: Q must be high.
    for _ in range(STABLE + 3):
        await RisingEdge(dut.CLK)
    assert int(dut.Q.value) == 1, "idle output should be high"

    # A short glitch low for (STABLE-1) cycles then back high → REJECTED.
    dut.D_ASYNC.value = 0
    for _ in range(STABLE - 1):
        await RisingEdge(dut.CLK)
    dut.D_ASYNC.value = 1
    for _ in range(STABLE + 4):
        await RisingEdge(dut.CLK)
    assert int(dut.Q.value) == 1, (
        f"a {STABLE-1}-cycle glitch (< G_STABLE_CYCLES={STABLE}) must NOT pass — Q flipped"
    )

    # A real, sustained low (well beyond STABLE, +2 sync cycles) → PROPAGATES.
    dut.D_ASYNC.value = 0
    for _ in range(STABLE + 6):
        await RisingEdge(dut.CLK)
    assert int(dut.Q.value) == 0, "a sustained level change must propagate to Q"

    # Back high, sustained → returns high.
    dut.D_ASYNC.value = 1
    for _ in range(STABLE + 6):
        await RisingEdge(dut.CLK)
    assert int(dut.Q.value) == 1, "a sustained return-high must propagate to Q"

    dut._log.info("✓ glitch rejected, real edges pass — DS-T3.5 input conditioning OK")


@cocotb.test()
async def repeated_glitches_never_pass(dut):
    """A burst of repeated short glitches (each < STABLE) must never flip Q."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset(dut)
    for _ in range(20):
        dut.D_ASYNC.value = 0
        for _ in range(STABLE - 1):
            await RisingEdge(dut.CLK)
        dut.D_ASYNC.value = 1
        await RisingEdge(dut.CLK)   # only 1 high cycle between glitches
    await Timer(50, units="ns")
    assert int(dut.Q.value) == 1, "repeated sub-threshold glitches must not flip Q"
    dut._log.info("✓ repeated short glitches rejected")


@cocotb.test()
async def idle_low_no_reset_startup_edge(dut):
    """DS-T2.45 regression guard: a line held LOW through reset must bring Q out
    of reset already LOW — no spurious startup '1'->'0' convergence.

    The old reset forced filt_r/sync_r/meta_r to '1' (an idle-HIGH assumption),
    so Q started high and an idle-LOW line produced a startup falling edge —
    captured by an EDGE datalogger enabled in the settling window as a phantom edge.
    The fix tracks the line through reset (filt_r <= sync_r). Here Q must be 0 at
    reset deassert and STAY 0 — never a spurious high pulse."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    dut.D_ASYNC.value = 0           # line idles LOW (e.g. a quiet edge trigger)
    dut.RST.value = 1
    for _ in range(6):              # hold reset long enough for the sync to fill
        await RisingEdge(dut.CLK)
    dut.RST.value = 0

    # From reset deassert onward, Q must already be low and never pulse high.
    for i in range(STABLE + 12):
        await RisingEdge(dut.CLK)
        assert int(dut.Q.value) == 0, (
            f"DS-T2.45: idle-LOW line produced a spurious high on Q at cycle {i} "
            f"after reset — the glitch_filter reset must track the line, not force '1'")

    # Sanity: a real sustained high still propagates (filter not stuck low).
    dut.D_ASYNC.value = 1
    for _ in range(STABLE + 6):
        await RisingEdge(dut.CLK)
    assert int(dut.Q.value) == 1, "a sustained high after an idle-low reset must still propagate"
    dut._log.info("✓ idle-low line: no startup edge, real edges still pass — DS-T2.45 guard OK")


def main():
    ds_root = test_dir.parents[4]   # deepskopion/
    run_simulation(
        top_level="glitch_filter",
        module="test_glitch_filter",
        top_level_lang="vhdl",
        generics={"G_CLK_HZ": CLK_HZ, "G_MAX_FREQ_HZ": MAX_FREQ_HZ},  # → window 4 clks
        waves=False,
        work_lib="tich",
        # Scope the compile to just this leaf entity (no deps but ieee) so the
        # project's testbenches — one of which has a pre-existing unmapped-port
        # error — are not pulled in.
        custom_libraries={"tich": [str(ds_root / "hw/rtl/emio/glitch_filter.vhd")]},
    )


if __name__ == "__main__":
    main()
