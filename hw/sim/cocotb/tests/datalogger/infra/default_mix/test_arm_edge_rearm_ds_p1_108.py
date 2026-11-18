# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.108: arm_edge must restart the finite edge vector even when STIM_EDGE_CONTROL already holds it.

Zybo silicon (PREREG A2 180763a2, image 7041ae2e, 2026-11-15): the default-mix run drove the edge NEGATIVE leg first
(STIM_EDGE_CONTROL <= 0x3 with EMIO_MUX_SEL 0, the vector played unseen), then the positive leg's production
``arm_edge`` wrote 0x3 again. ``ds_stimulus_gen`` p_edge resets its finite vector only while EDGE_CONTROL(0) = '0', so a
second write of 0x3 does not restart it: edge logger 1 recorded no stimulus transition. DE25 C3 run1 (the DS-P2.311
origin) had the same leg order. The passing DS-P2.311 / DS-P2.313 tests start with the control register at 0.

Bench: hw/sim/tb_default_mix_core.vhd (the real deepskopion_core, default mix, Zybo generics), as DS-P2.313. The test
first plays the vector the way the negative leg's drive does, then replays the production arm_edge writes.

Pre-stated verdict (before the first run):
  the first (negative-leg) play toggles stim_edge_w [1, 0, 1, 0], else VOID;
  after the production arm_edge: stim_edge_w toggles [1, 0, 1, 0] again and edge logger 1 records exactly RISE, FALL,
  RISE, FALL after the arm; on today's arm_edge (no bit-0 clear) this is RED: no toggle and no edge after the arm.

Run (from the deepskopion dir): rr sim run test_arm_edge_rearm_ds_p1_108
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Edge, RisingEdge, Timer
from cocotb.utils import get_sim_time
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

_HERE = Path(__file__).resolve()
_COCOTB = _HERE.parents[4]                          # hw/sim/cocotb
_ROOT = _HERE.parents[7]                            # deepskopion
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_COCOTB / "tests" / "stimulus" / "core_seam"))
from _core_seam_common import run_core_seam  # noqa: E402

TOP = "tb_default_mix_core"
CLK_PERIOD_NS = 10
EDGE_RISE, EDGE_FALL = 0x0600, 0x0601
EDGE_ID = 1
EMIO_MUX_SEL = 0x24
STIM_EDGE_CONTROL = 0x244
EDGE_FINITE_CONTROL = 0x3

IDLE = {
    "JA_0": 1, "JA_1": 1, "JA_2": 1, "JA_3": 1,
    "JA_4": 1, "JA_5": 0, "JA_6": 0, "JA_7": 0,
    "JB_0": 1, "JB_1": 1, "JB_2": 0, "JB_3": 0, "JB_4": 0,
    "JB_5": 1, "JB_6": 0, "JB_7": 0,
}

SLEEP = object()


def _arm_edge_writes() -> list:
    """The register writes stimulus_armer.arm_edge makes, in order, with a SLEEP marker where it sleeps."""
    from sw.stream import stimulus_armer

    ops: list = []

    class _Recorder:
        def write_u32(self, off, val):
            ops.append((int(off), int(val)))

    real_sleep = stimulus_armer.time.sleep
    stimulus_armer.time.sleep = lambda _s: ops.append(SLEEP)
    try:
        stimulus_armer.arm_edge(_Recorder())
    finally:
        stimulus_armer.time.sleep = real_sleep
    return ops


@cocotb.test()
async def test_arm_edge_replays_the_vector_after_an_earlier_play(dut):
    for name, level in IDLE.items():
        getattr(dut, name).value = level
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, unit="ns").start())
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 16)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 16)
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)

    records: list = []
    toggles: list = []

    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.PROBE_DOUT_VALID.value) == 1 and int(dut.PROBE_DOUT_REQ.value) == 1:
                records.append((get_sim_time("ns"), int(dut.PROBE_DOUT_DATALOGGER_ID.value),
                                int(dut.PROBE_DOUT_MSG_TYPE.value)))

    async def watch_stim():
        sig = dut.u_core.stim_edge_w
        while True:
            await Edge(sig)
            toggles.append((get_sim_time("ns"), int(sig.value)))

    cocotb.start_soon(collect())
    cocotb.start_soon(watch_stim())

    # The negative leg's drive: the vector plays with EMIO_MUX_SEL 0 (the logger reads the header, not the stimulus).
    await axi.write(EMIO_MUX_SEL, (0).to_bytes(4, "little"))
    await axi.write(STIM_EDGE_CONTROL, EDGE_FINITE_CONTROL.to_bytes(4, "little"))
    await Timer(40, unit="us")
    first = [lv for _, lv in toggles]
    assert first == [1, 0, 1, 0], f"VOID: the first (negative-leg) play did not toggle [1, 0, 1, 0]: {first}"

    t_arm = get_sim_time("ns")
    ops = _arm_edge_writes()
    dut._log.info(f"arm_edge writes: {[op if op is SLEEP else (hex(op[0]), hex(op[1])) for op in ops]}")
    for op in ops:
        if op is SLEEP:
            await Timer(20, unit="us")
        else:
            await axi.write(op[0], op[1].to_bytes(4, "little"))
    await Timer(100, unit="us")

    second = [lv for t, lv in toggles if t > t_arm]
    edges = [m for t, i, m in records if i == EDGE_ID and t > t_arm and m in (EDGE_RISE, EDGE_FALL)]
    dut._log.info(f"stim toggles after the arm: {second}; edge logger {EDGE_ID} edges after the arm: {[hex(m) for m in edges]}")
    assert second == [1, 0, 1, 0], f"the production arm_edge did not replay the vector after an earlier play: {second}"
    assert edges == [EDGE_RISE, EDGE_FALL, EDGE_RISE, EDGE_FALL], (
        f"edge logger {EDGE_ID} recorded {[hex(m) for m in edges]} after the arm")


if __name__ == "__main__":
    run_core_seam("test_arm_edge_rearm_ds_p1_108", top_level=TOP)
