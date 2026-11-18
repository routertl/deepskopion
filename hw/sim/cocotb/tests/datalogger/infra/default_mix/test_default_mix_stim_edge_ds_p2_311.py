# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.311: the production edge stimulus through the default-mix core.

DE25 C3 run 1 (2026-11-15, image bb0a0f50): with the edge logger armed by the
production armer (``stimulus_armer.arm_edge``: edge logger 1 disable/enable,
EMIO_MUX_SEL 0x80, STIM_EDGE_CONTROL 0x3), edge logger 1 emitted ONE edge record
(a falling edge 38.4 ms after arm) where the stimulus contract drives four
(``stimulus_contract.EDGE_TRANSITIONS``). PKT_CNT_EDGE matched the wire (2
lifecycle + 1 edge), so the record was lost at or before the logger.

Bench: hw/sim/tb_default_mix_core.vhd (the real deepskopion_core with the default
mix). The register writes are the ones ``arm_edge`` makes, recorded from the
production function and replayed over the core's AXI-Lite port; the armer's 5 ms
reset gap is replayed as 20 us (config_control latches on the value change).
The EDGE#1 header input jb(2) idles LOW and is not driven: with EMIO_MUX_SEL
bit 7 set the core routes the stimulus onto that logger input.

Pre-stated verdict (DS-P2.311, before the first run):
  stimulus presence first: ds_stimulus_gen EDGE_O toggles exactly 4 times, else
  VOID; then 4 edge records on logger 1 (rise, fall, rise, fall) = the cause is
  outside the simulated path; 1 or 0 = red-first reproduction in the core.

Run (from the deepskopion dir): rr sim run test_default_mix_stim_edge_ds_p2_311
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Edge, RisingEdge, Timer
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

IDLE = {
    "JA_0": 1, "JA_1": 1, "JA_2": 1, "JA_3": 1,
    "JA_4": 1, "JA_5": 0, "JA_6": 0, "JA_7": 0,
    "JB_0": 1, "JB_1": 1, "JB_2": 0, "JB_3": 0, "JB_4": 0,
    "JB_5": 1, "JB_6": 0, "JB_7": 0,
}

SLEEP = object()


def _arm_edge_writes() -> list:
    """The register writes stimulus_armer.arm_edge makes, in order, with a SLEEP
    marker where it sleeps."""
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
async def test_production_edge_stimulus_reaches_edge_logger_1(dut):
    for name, level in IDLE.items():
        getattr(dut, name).value = level
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, unit="ns").start())
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 16)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 16)
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)

    records: list = []

    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.PROBE_DOUT_VALID.value) == 1 and int(dut.PROBE_DOUT_REQ.value) == 1:
                records.append((int(dut.PROBE_DOUT_DATALOGGER_ID.value),
                                int(dut.PROBE_DOUT_MSG_TYPE.value)))

    stim_toggles: list = []

    async def watch_stim():
        sig = dut.u_core.stim_edge_w
        while True:
            await Edge(sig)
            stim_toggles.append(int(sig.value))

    cocotb.start_soon(collect())
    cocotb.start_soon(watch_stim())

    ops = _arm_edge_writes()
    dut._log.info(f"arm_edge writes: {[op if op is SLEEP else (hex(op[0]), hex(op[1])) for op in ops]}")
    for op in ops:
        if op is SLEEP:
            await Timer(20, unit="us")
        else:
            await axi.write(op[0], op[1].to_bytes(4, "little"))
    await Timer(100, unit="us")

    edges = [t for i, t in records if i == EDGE_ID and t in (EDGE_RISE, EDGE_FALL)]
    dut._log.info(f"stimulus EDGE_O levels after each toggle: {stim_toggles}")
    dut._log.info(f"edge logger {EDGE_ID} records: {[hex(t) for t in edges]}; all records {len(records)}")
    assert stim_toggles == [1, 0, 1, 0], (
        f"VOID: the stimulus generator did not drive the 4-transition vector (EDGE_O levels {stim_toggles})")
    assert edges == [EDGE_RISE, EDGE_FALL, EDGE_RISE, EDGE_FALL], (
        f"edge logger {EDGE_ID} recorded {[hex(t) for t in edges]} for the 4-transition stimulus")


if __name__ == "__main__":
    run_core_seam("test_default_mix_stim_edge_ds_p2_311", top_level=TOP)
