"""
test_level_frames_per_change: one level change must produce exactly one level
payload frame, whatever the arbiter's LEVEL_MSG_RDY pattern.

DS-T3.42. In level_datalogger.vhd the RDY branch of pr_capture clears
pinChange_r and takes LEVEL_PINS as the new baseline, but in that same cycle
`levelPins_r` still held the OLD value, so the following
`if levelPins_r /= LEVEL_PINS then pinChange_r <= '1'` re-armed the very change
being delivered and a second identical frame followed. It needed RDY to be low
when the change happened, so a steadily ready arbiter never showed it, and the
older level tests only look at the FIRST payload frame after a change.

Counting rule: a frame is DELIVERED on a cycle with LEVEL_MSG_VLD=1 and
LEVEL_MSG_RDY=1 (level-payload type [26:24]=0). VLD held high while RDY is low
is one waiting frame, not several. The test never holds RDY high to avoid the
duplicate: every pattern below must give exactly one frame per change.

Patterns: RDY held high; RDY pulsed one cycle in four at each of the four grant
phases; RDY pseudo-random (fixed seeds, so a red is reproducible).

Presence checks: a run that delivers no frame at all fails as "no frame", never
as a pass, and the multi-change runs compare the delivered pins with the pins
that were driven, in order.
"""
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
except ImportError:
    print("Error: Could not import engine. Run via `rr sim run`.")
    sys.exit(1)

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

GENERICS = {
    "G_LEVEL_ID_WIDTH": 5,
    "G_LEVEL_DEV_ID": 24,
    "G_TIMESTAMP_WIDTH": 16,
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_SIM_EN": 0,
}
DEV_ID = GENERICS["G_LEVEL_DEV_ID"]
WINDOW_CYCLES = 60            # per change: long enough for any delayed frame
# Each value differs from the one before it (the first differs from 0).
PINS_SEQUENCE = [0x5A, 0xA5, 0x01, 0xFF, 0x00, 0x3C, 0xC3, 0x80]
RANDOM_SEEDS = (1, 2, 3)


def _cfg_word(dev_id: int, payload: int) -> int:
    """DATALOGGER_CONFIG = [device_id(15:11) | payload(10:0)]."""
    return ((dev_id & 0x1F) << 11) | (payload & 0x7FF)


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    dut.RST.value = 1
    dut.LEVEL_PINS.value = 0
    dut.LEVEL_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0x1111
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 3)
    dut.CONFIG.value = _cfg_word(DEV_ID, 0x001)    # enable
    await ClockCycles(dut.CLK, 20)                 # ENABLED frame drains


async def _drive_changes(dut, rdy_pattern, pins_sequence):
    """Make one LEVEL_PINS change per entry; return the delivered payload frames.

    rdy_pattern(cycle) -> 0/1 drives LEVEL_MSG_RDY on every cycle of the run.
    Returns [(cycle, raw)] for every delivered level-payload frame.
    """
    frames = []
    cycle = 0
    for pins in pins_sequence:
        dut.LEVEL_PINS.value = pins
        for _ in range(WINDOW_CYCLES):
            rdy = rdy_pattern(cycle)
            dut.LEVEL_MSG_RDY.value = rdy
            await RisingEdge(dut.CLK)
            # Delivered = VLD and RDY on the same edge.
            if int(dut.LEVEL_MSG_VLD.value) == 1 and rdy == 1:
                raw = int(dut.LEVEL_MSG_OUT.value)
                if ((raw >> 24) & 0x7) == 0:          # level payload type "000"
                    frames.append((cycle, raw))
            cycle += 1
    return frames


def _describe(frames):
    return ", ".join(f"cycle {c} raw={r:#010x}" for c, r in frames) or "none"


def _check(dut, frames, pins_sequence, scenario):
    delivered = [raw & 0xFF for _c, raw in frames]
    dut._log.info(f"{scenario}: {len(frames)} frame(s) for {len(pins_sequence)} change(s): "
                  f"{_describe(frames)}")
    assert frames, f"{scenario}: no level payload frame at all (nothing exercised)"
    assert len(frames) == len(pins_sequence), (
        f"{scenario}: {len(pins_sequence)} LEVEL_PINS change(s) produced "
        f"{len(frames)} delivered payload frame(s), expected exactly "
        f"{len(pins_sequence)}: {_describe(frames)}")
    assert delivered == pins_sequence, (
        f"{scenario}: delivered pins {[hex(p) for p in delivered]} differ from "
        f"the pins driven {[hex(p) for p in pins_sequence]}")


@cocotb.test()
async def test_one_change_one_frame_rdy_held_high(dut):
    await _bring_up(dut)
    frames = await _drive_changes(dut, lambda _c: 1, PINS_SEQUENCE[:1])
    _check(dut, frames, PINS_SEQUENCE[:1], "RDY held high, one change")


@cocotb.test()
async def test_one_change_one_frame_rdy_pulsed_one_in_four(dut):
    """The ticket's case: a single change, RDY a one-cycle grant every fourth
    cycle. Swept over the four phases so the change meets the grant at every
    alignment (one of them is the steady-state coincidence)."""
    failures = []
    for phase in range(4):
        await _bring_up(dut)
        frames = await _drive_changes(dut, lambda c, p=phase: 1 if c % 4 == p else 0,
                                      PINS_SEQUENCE[:1])
        try:
            _check(dut, frames, PINS_SEQUENCE[:1], f"RDY pulsed 1-in-4 phase {phase}")
        except AssertionError as exc:
            failures.append(str(exc))
    assert not failures, "\n".join(failures)


@cocotb.test()
async def test_many_changes_rdy_pulsed_one_in_four(dut):
    await _bring_up(dut)
    frames = await _drive_changes(dut, lambda c: 1 if c % 4 == 3 else 0, PINS_SEQUENCE)
    _check(dut, frames, PINS_SEQUENCE, "RDY pulsed 1-in-4, many changes")


@cocotb.test()
async def test_many_changes_rdy_random(dut):
    failures = []
    for seed in RANDOM_SEEDS:
        await _bring_up(dut)
        rng = random.Random(seed)
        pattern = [1 if rng.random() < 0.5 else 0 for _ in range(len(PINS_SEQUENCE) * WINDOW_CYCLES)]
        frames = await _drive_changes(dut, lambda c, pat=pattern: pat[c], PINS_SEQUENCE)
        try:
            _check(dut, frames, PINS_SEQUENCE, f"RDY random 50% seed {seed}")
        except AssertionError as exc:
            failures.append(str(exc))
    assert not failures, "\n".join(failures)


def main():
    run_simulation(
        top_level="level_datalogger",
        module="test_level_frames_per_change",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/nonstd/level_datalogger/level_datalogger.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
    )


if __name__ == "__main__":
    main()
