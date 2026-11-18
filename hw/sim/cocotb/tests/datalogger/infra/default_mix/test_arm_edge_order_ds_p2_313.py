# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.313: arm_edge must put the edge logger on the stimulus line before it enables it.

DE25 C4 A3 (2026-11-15, image 7041ae2e): with the header pull-up of DS-P2.309 the EDGE#1 header input idles HIGH.
The production armer (``stimulus_armer.arm_edge``) enabled edge logger 1 while EMIO_MUX_SEL was still 0, so the
logger read the pulled-up header pin, and then switched the mux to the stimulus line, which idles LOW. The capture
held two transitions (1, then 0 about 1.13 ms later) before the 4 stimulus transitions, and PKT_CNT_EDGE rose by 7.

Bench: hw/sim/tb_default_mix_core.vhd (the real deepskopion_core, default mix), as the DS-P2.311 test. The register
writes are recorded from the production ``arm_edge`` and replayed over AXI-Lite; its reset sleep is replayed as
20 us. The one difference from DS-P2.311: the EDGE#1 header input jb(2) is held HIGH, as the pull-up holds it.

Pre-stated verdict (before the first run):
  stimulus presence first: stim_edge_w toggles exactly [1, 0, 1, 0], else VOID;
  then edge logger 1 records exactly RISE, FALL, RISE, FALL and nothing before the first stimulus toggle;
  on the current armer order this is RED (an extra edge before the stimulus).

Run (from the deepskopion dir): rr sim run test_arm_edge_order_ds_p2_313
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
EDGE_LIFECYCLE = 0x0608
EDGE_ID = 1
PKT_CNT_EDGE = 0x00E0

# As the DS-P2.311 test, except JB_2 (EDGE#1 header input) is HIGH: the DS-P2.309 pull-up idle level.
IDLE = {
    "JA_0": 1, "JA_1": 1, "JA_2": 1, "JA_3": 1,
    "JA_4": 1, "JA_5": 0, "JA_6": 0, "JA_7": 0,
    "JB_0": 1, "JB_1": 1, "JB_2": 1, "JB_3": 0, "JB_4": 0,
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
async def test_arm_edge_records_only_the_stimulus_on_a_pulled_up_input(dut):
    for name, level in IDLE.items():
        getattr(dut, name).value = level
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, unit="ns").start())
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 16)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 16)
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)

    records: list = []          # (sim time ns, datalogger id, msg type)

    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.PROBE_DOUT_VALID.value) == 1 and int(dut.PROBE_DOUT_REQ.value) == 1:
                records.append((get_sim_time("ns"), int(dut.PROBE_DOUT_DATALOGGER_ID.value),
                                int(dut.PROBE_DOUT_MSG_TYPE.value)))

    stim_toggles: list = []     # (sim time ns, level)

    async def watch_stim():
        sig = dut.u_core.stim_edge_w
        while True:
            await Edge(sig)
            stim_toggles.append((get_sim_time("ns"), int(sig.value)))

    cocotb.start_soon(collect())
    cocotb.start_soon(watch_stim())

    cnt0 = int.from_bytes((await axi.read(PKT_CNT_EDGE, 4)).data, "little")
    ops = _arm_edge_writes()
    dut._log.info(f"arm_edge writes: {[op if op is SLEEP else (hex(op[0]), hex(op[1])) for op in ops]}")
    for op in ops:
        if op is SLEEP:
            await Timer(20, unit="us")
        else:
            await axi.write(op[0], op[1].to_bytes(4, "little"))
    await Timer(100, unit="us")
    cnt1 = int.from_bytes((await axi.read(PKT_CNT_EDGE, 4)).data, "little")

    mine = [(t, m) for t, i, m in records if i == EDGE_ID]
    kinds: dict = {}
    for _, m in mine:
        kinds[hex(m)] = kinds.get(hex(m), 0) + 1
    edges = [(t, m) for t, m in mine if m in (EDGE_RISE, EDGE_FALL)]
    dut._log.info(f"stimulus toggles: {stim_toggles}")
    dut._log.info(f"edge logger {EDGE_ID} records: {[(t, hex(m)) for t, m in mine]}")
    dut._log.info(f"record kinds: {kinds}; PKT_CNT_EDGE {cnt0} -> {cnt1} (delta {cnt1 - cnt0})")

    assert [lv for _, lv in stim_toggles] == [1, 0, 1, 0], (
        f"VOID: the stimulus generator did not drive the 4-transition vector ({stim_toggles})")
    first_stim = stim_toggles[0][0]
    early = [(t, hex(m)) for t, m in edges if t < first_stim]
    assert not early, f"edge logger {EDGE_ID} recorded {early} before the first stimulus toggle at {first_stim} ns"
    assert [m for _, m in edges] == [EDGE_RISE, EDGE_FALL, EDGE_RISE, EDGE_FALL], (
        f"edge logger {EDGE_ID} recorded {[hex(m) for _, m in edges]} for the 4-transition stimulus")
    # Every record kind counted: one lifecycle ENABLED (the disable of an already-disabled logger emits none,
    # measured on the red run), one record per stimulus transition. PKT_CNT_EDGE counts exactly these.
    assert kinds == {hex(EDGE_LIFECYCLE): 1, hex(EDGE_RISE): 2, hex(EDGE_FALL): 2}, f"record kinds {kinds}"
    assert cnt1 - cnt0 == 5, f"PKT_CNT_EDGE delta {cnt1 - cnt0}, expected 5 (1 lifecycle + 4 edges)"


if __name__ == "__main__":
    run_core_seam("test_arm_edge_order_ds_p2_313", top_level=TOP)
