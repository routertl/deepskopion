# rr unknown-llm (AI-generated)
"""Adversarial unit test for ring_watermark (DS-P3.60).

The watermark FSM is the load-bearing, subtle part of the interrupt-driven
drain: a Schmitt-trigger alarm that must fire ONCE per fill excursion above the
HIGH watermark and NOT thrash when the fill hovers in the hysteresis band. The
thrash loop (fire@HIGH → drain a little → fire@HIGH → …) is exactly the failure
mode this test exists to forbid.

Ring shrunk to 8 bits (256-byte) so offsets are small integers:
    HIGH = 3/4 * 256 = 192   LOW = 2/4 * 256 = 128

file-don't-fix: a failure here is a real FSM defect — diagnose, don't soften.

Pipeline note: fill is registered (fill_r) and the alarm logic consumes fill_r,
so an input change takes 2 clk edges to fully propagate to `alarm`. Every check
settles ≥3 edges after driving the inputs.
"""

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
except ImportError:
    print("Error: Could not import engine. Run via `rr sim run`.")
    sys.exit(1)


def _find_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "project.yml").exists() and (p / "hw" / "rtl").is_dir():
            return p
    raise RuntimeError("deepskopion root (project.yml + hw/rtl) not found")


DEEPSKOPION_ROOT = _find_root(Path(__file__).resolve())
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

ADDR_W = 8
FULL = 1 << ADDR_W      # 256
HIGH = FULL * 3 // 4    # 192
LOW = FULL * 2 // 4     # 128
PERIOD_NS = 10

GENERICS = {
    "G_ADDR_WIDTH": ADDR_W,
    "G_HIGH_NUM": 3,
    "G_LOW_NUM": 2,
    "G_DEN": 4,
}


async def _reset(dut):
    dut.rst.value = 1
    dut.commit_off.value = 0
    dut.read_off.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _set_fill(dut, fill_target, read_off=0, settle=4):
    """Drive commit/read so occupancy == fill_target (mod 256), then settle."""
    commit = (read_off + fill_target) % FULL
    dut.read_off.value = read_off
    dut.commit_off.value = commit
    for _ in range(settle):
        await RisingEdge(dut.clk)


@cocotb.test()
async def test_watermark_hysteresis(dut):
    cocotb.start_soon(Clock(dut.clk, PERIOD_NS, units="ns").start())
    await _reset(dut)

    # 1. Post-reset: quiet.
    assert int(dut.alarm.value) == 0, "alarm should be 0 after reset"
    assert int(dut.alarm_cnt.value) == 0, "alarm_cnt should be 0 after reset"

    # 2. Fill below HIGH → no alarm.
    await _set_fill(dut, HIGH - 1)
    assert int(dut.alarm.value) == 0, f"alarm fired below HIGH (fill={HIGH-1})"
    assert int(dut.alarm_cnt.value) == 0

    # 3. Cross HIGH → alarm sets, count == 1.
    await _set_fill(dut, HIGH)
    assert int(dut.alarm.value) == 1, "alarm did not set at HIGH"
    assert int(dut.alarm_cnt.value) == 1, "alarm_cnt != 1 on first excursion"

    # 4. HYSTERESIS HOLD (the anti-thrash invariant): drain into the band
    #    (LOW <= fill < HIGH). Alarm MUST stay asserted; count MUST NOT move.
    await _set_fill(dut, (HIGH + LOW) // 2)   # 160, mid-band
    assert int(dut.alarm.value) == 1, "alarm cleared inside the hysteresis band"
    assert int(dut.alarm_cnt.value) == 1, "alarm_cnt moved inside the band (thrash!)"

    # 5. Re-cross HIGH while still latched → NO new excursion (never re-armed).
    await _set_fill(dut, HIGH + 10)
    await _set_fill(dut, LOW + 1)             # back into the band
    assert int(dut.alarm.value) == 1
    assert int(dut.alarm_cnt.value) == 1, "re-fired without re-arming below LOW (thrash!)"

    # 6. Drain below LOW → alarm clears (re-arm).
    await _set_fill(dut, LOW - 1)
    assert int(dut.alarm.value) == 0, "alarm did not clear below LOW"
    assert int(dut.alarm_cnt.value) == 1, "count changed on clear"

    # 7. Second full excursion → count == 2 (proves re-arm + one-per-excursion).
    await _set_fill(dut, HIGH + 5)
    assert int(dut.alarm.value) == 1
    assert int(dut.alarm_cnt.value) == 2, "second excursion not counted"

    dut._log.info("ring_watermark hysteresis: PASS (set@HIGH, hold in band, clr@LOW, 1/excursion)")


@cocotb.test()
async def test_watermark_modular_fill(dut):
    """fill = (commit - read) mod 256 must hold across the ring wrap."""
    cocotb.start_soon(Clock(dut.clk, PERIOD_NS, units="ns").start())
    await _reset(dut)

    # Wrapped pointers: read near top, commit just past 0 → small fill < LOW.
    await _set_fill(dut, 16, read_off=250)    # commit = (250+16)%256 = 10
    assert int(dut.fill.value) == 16, f"wrapped fill wrong: {int(dut.fill.value)}"
    assert int(dut.alarm.value) == 0, "alarm fired on a small wrapped fill"

    # Wrapped pointers crossing HIGH: fill 206 with read=100 → commit=50.
    await _set_fill(dut, 206, read_off=100)
    assert int(dut.fill.value) == 206
    assert int(dut.alarm.value) == 1, "alarm did not fire on a large wrapped fill"

    dut._log.info("ring_watermark modular fill across wrap: PASS")


def main():
    sources = [f"{DEEPSKOPION_RTL}/ring_watermark.vhd"]
    run_simulation(
        top_level="ring_watermark",
        module="test_ring_watermark",
        custom_libraries={"tich": sources},
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
