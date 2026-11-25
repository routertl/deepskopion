# rr unknown-llm (AI-generated)
"""DS-P2.236: uart_playback replays each recorded interval for exactly its
distance.

The datalogger stores every edge as (level, distance since the previous edge,
in prescaled ticks) and uart_playback rebuilds the waveform from those
entries, one tick per clock (PB_CLK = '1', as uart_datalogger wires it). The
sampler (uart_baudrate_gen) then reads that rebuilt waveform at a fixed bit
period. If an interval replays longer than its distance, a frame grows with
its number of edges and its stop-bit sample lands early: the 1 Mbaud loss of
DS-P2.236, where 25-tick intervals replayed as 27.

Distances below are each baud's bit time in ticks at 100 MHz, prescaler 4:
9600 -> 2604, 115200 -> 217, 230400 -> 108, 921600 -> 27, 1 Mbaud -> 25.
"""

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation  # noqa: E402

from hw.sim.portable_fifo_sources import olo_fifo_sources  # noqa: E402

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"
CLK_PERIOD_NS = 10
GENERICS = {"G_DIST_WIDTH": 16, "G_FIFO_DEPTH": 8, "G_TIME_EN": 0}
N_INTERVALS = 6


async def _init(dut):
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, unit="ns").start())
    dut.RST.value = 1
    dut.ENABLE.value = 0
    dut.PB_CLK.value = 0
    dut.TIMESTAMP.value = 0
    dut.LEVEL.value = 1
    dut.DISTANCE.value = 0
    dut.VLD.value = 0
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 20)      # past the playback FIFO's reset-busy window


async def _replayed_intervals(dut, distances):
    """Queue (alternating level, distance) entries, play them back, and return
    the clock count between consecutive WAVE level changes."""
    await _init(dut)
    for i, d in enumerate(distances):
        dut.LEVEL.value = i % 2          # 0, 1, 0, ...: every entry is a level change
        dut.DISTANCE.value = d
        dut.VLD.value = 1
        await RisingEdge(dut.CLK)
    dut.VLD.value = 0
    await ClockCycles(dut.CLK, 4)
    dut.PB_CLK.value = 1
    dut.ENABLE.value = 1
    changes, prev, cyc = [], int(dut.WAVE.LEVEL.value), 0
    limit = sum(distances) + 20 * len(distances) + 100
    while cyc < limit and len(changes) < len(distances):
        await RisingEdge(dut.CLK)
        cyc += 1
        lvl = int(dut.WAVE.LEVEL.value)
        if lvl != prev:
            changes.append(cyc)
            prev = lvl
    return [b - a for a, b in zip(changes, changes[1:])]


@cocotb.test()
@cocotb.parametrize(distance=[25, 27, 108, 217, 2604])
async def every_interval_replays_for_exactly_its_distance(dut, distance):
    got = await _replayed_intervals(dut, [distance] * N_INTERVALS)
    dut._log.info(f"distance={distance} replayed intervals={got}")
    assert len(got) == N_INTERVALS - 1, f"only {len(got) + 1} level changes replayed: {got}"
    assert got == [distance] * (N_INTERVALS - 1), (
        f"distance {distance} replayed as {got} clocks")


@cocotb.test()
async def mixed_intervals_replay_for_exactly_their_distances(dut):
    """One- to four-bit runs at 1 Mbaud (25, 50, 75, 100 ticks)."""
    distances = [25, 50, 25, 75, 100, 25, 50]
    got = await _replayed_intervals(dut, distances)
    dut._log.info(f"distances={distances} replayed intervals={got}")
    assert got == distances[:-1], f"distances {distances[:-1]} replayed as {got} clocks"


def main():
    run_simulation(
        top_level="uart_playback",
        module="test_uart_playback_replay_length_ds_p2_236",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/uart_pkg.vhd",
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/uart/uart_playback.vhd",
            ]
        },
        generics=GENERICS,
        waves=False,
    )


if __name__ == "__main__":
    main()
