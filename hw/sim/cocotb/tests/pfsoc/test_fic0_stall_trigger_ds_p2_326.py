# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""
test_fic0_stall_trigger_ds_p2_326 — DS-P2.326: the stall trigger that arms the PolarFire FIC0 read analyzer.

fic0_stall_trigger watches four handshake bits of one AXI read channel and sets trig_o (sticky) once a read has
been stalled for G_STALL_CYCLES consecutive clocks: an AR offered and not accepted, or an AR accepted whose R has
not arrived. The unit sees only those four bits, so they are driven directly; the bus path through the bench top
is covered with the shipped Axi4Master in the bench-top test.

Expected values are hard-coded from the generics below (ROUTERTL-002): N = 8 stall cycles, heartbeat bit 2
(toggles every 4 clocks).

Run:  rr sim run test_fic0_stall_trigger_ds_p2_326
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

import routertl_core as _rrc

sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from rr_cocotb_tb.env import TbEnv  # noqa: E402

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[5]
N = 8            # G_STALL_CYCLES
HB_BIT = 2       # G_HEARTBEAT_BIT
GENERICS = {"G_STALL_CYCLES": N, "G_HEARTBEAT_BIT": HB_BIT}


async def start(dut):
    tb = TbEnv(dut, clk="clk_i", rst="rst_i", period_ns=10, auto_attach_from_env=False)
    for s in ("arvalid_i", "arready_i", "rvalid_i", "rready_i"):
        getattr(dut, s).value = 0
    await tb.start_clock()
    await tb.reset(cycles=4, active_low=False)
    await RisingEdge(dut.clk_i)
    return tb


def bus(dut, arvalid=0, arready=0, rvalid=0, rready=0):
    dut.arvalid_i.value = arvalid
    dut.arready_i.value = arready
    dut.rvalid_i.value = rvalid
    dut.rready_i.value = rready


async def sample(dut):
    await ReadOnly()
    return {k: int(getattr(dut, f"{k}_o").value) for k in ("stall", "outstanding", "trig", "heartbeat")}


async def read(dut, latency: int):
    """One single-beat read: AR accepted in one clock, R handshake `latency` clocks later."""
    bus(dut, arvalid=1, arready=1)
    await RisingEdge(dut.clk_i)
    bus(dut)
    for _ in range(latency - 1):
        await RisingEdge(dut.clk_i)
    bus(dut, rvalid=1, rready=1)
    await RisingEdge(dut.clk_i)
    bus(dut)


@cocotb.test()
async def test_normal_reads_never_trigger(dut):
    """300 reads with R latency 1..N-1 and random gaps: no trigger; outstanding is 0 between reads."""
    await start(dut)
    rng = random.Random(326)
    for _ in range(300):
        await read(dut, rng.randrange(1, N))
        for _ in range(rng.randrange(0, 5)):
            await RisingEdge(dut.clk_i)
        s = await sample(dut)
        assert s["trig"] == 0 and s["outstanding"] == 0, s
        await RisingEdge(dut.clk_i)


@cocotb.test()
async def test_accepted_read_without_r_triggers_after_exactly_n_cycles(dut):
    """AR accepted, R never comes: outstanding and stall from the next clock; trig after exactly N stalled clocks."""
    await start(dut)
    bus(dut, arvalid=1, arready=1)
    await RisingEdge(dut.clk_i)
    bus(dut)
    for k in range(1, N + 3):
        await RisingEdge(dut.clk_i)
        s = await sample(dut)
        assert s["outstanding"] == 1 and s["stall"] == 1, (k, s)
        assert s["trig"] == (1 if k >= N else 0), (k, s)


@cocotb.test()
async def test_ar_not_accepted_triggers_after_exactly_n_cycles(dut):
    """ARVALID held with ARREADY low: stall without an outstanding read; trig after exactly N clocks."""
    await start(dut)
    bus(dut, arvalid=1, arready=0)
    for k in range(1, N + 3):
        await RisingEdge(dut.clk_i)
        s = await sample(dut)
        assert s["stall"] == 1 and s["outstanding"] == 0, (k, s)
        assert s["trig"] == (1 if k >= N else 0), (k, s)


@cocotb.test()
async def test_a_stall_one_short_of_n_resets_the_count(dut):
    """Two stalls of N-1 clocks separated by one idle clock never trigger: the count restarts."""
    await start(dut)
    for _ in range(2):
        bus(dut, arvalid=1, arready=0)
        for _ in range(N - 1):
            await RisingEdge(dut.clk_i)
        bus(dut)
        await RisingEdge(dut.clk_i)
        s = await sample(dut)
        assert s["trig"] == 0 and s["stall"] == 0, s
        await RisingEdge(dut.clk_i)


@cocotb.test()
async def test_trigger_is_sticky_until_reset(dut):
    """After a trigger, completed reads keep trig at 1; a reset clears it."""
    tb = await start(dut)
    bus(dut, arvalid=1, arready=0)
    await ClockCycles(dut.clk_i, N + 1)
    bus(dut, arvalid=1, arready=1)            # the stuck AR is finally accepted
    await RisingEdge(dut.clk_i)
    bus(dut, rvalid=1, rready=1)
    await RisingEdge(dut.clk_i)
    bus(dut)
    for _ in range(20):
        await read(dut, 2)
    s = await sample(dut)
    assert s["trig"] == 1 and s["outstanding"] == 0, s
    await RisingEdge(dut.clk_i)
    await tb.reset(cycles=2, active_low=False)
    await RisingEdge(dut.clk_i)
    s = await sample(dut)
    assert s["trig"] == 0, s


@cocotb.test()
async def test_new_ar_in_the_clock_of_the_previous_r_stays_outstanding(dut):
    """An R handshake and a new accepted AR in the same clock leave one read outstanding."""
    await start(dut)
    bus(dut, arvalid=1, arready=1)
    await RisingEdge(dut.clk_i)
    bus(dut, arvalid=1, arready=1, rvalid=1, rready=1)
    await RisingEdge(dut.clk_i)
    bus(dut)
    await RisingEdge(dut.clk_i)
    s = await sample(dut)
    assert s["outstanding"] == 1, s
    await RisingEdge(dut.clk_i)
    bus(dut, rvalid=1, rready=1)
    await RisingEdge(dut.clk_i)
    bus(dut)
    await RisingEdge(dut.clk_i)
    s = await sample(dut)
    assert s["outstanding"] == 0 and s["trig"] == 0, s


@cocotb.test()
async def test_heartbeat_toggles_every_2_pow_bit_clocks(dut):
    """heartbeat_o is bit 2 of a free-running counter: it toggles every 4 clocks."""
    await start(dut)
    values = []
    for _ in range(32):
        await RisingEdge(dut.clk_i)
        values.append((await sample(dut))["heartbeat"])
    edges = [i for i in range(1, len(values)) if values[i] != values[i - 1]]
    gaps = {b - a for a, b in zip(edges, edges[1:])}
    assert len(edges) >= 6 and gaps == {4}, (values, edges)


if __name__ == "__main__":
    from engine.simulation import run_simulation

    run_simulation(
        top_level="fic0_stall_trigger",
        module="test_fic0_stall_trigger_ds_p2_326",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="work",
        custom_libraries={"work": [f"{DEEPSKOPION_ROOT}/hw/rtl/pfsoc/fic0_stall_trigger.vhd"]},
    )
