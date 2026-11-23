# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""edge_datalogger: state the edge detector held before a reset or a disable never writes a record by itself.

PolarFire SoC (DS-P1.113 run9/run10, L-3): at logger 3's first enable after power-on, two S=0 MISO records, both
level 0, one 10 ns tick apart, with the line idle low (2 of 2 sessions on one fabric; 0 of 9 re-enables). The gather's
glitch_filter (window >= 2) keeps a 1-sample pulse off EDGE_IN, so the pair is not a line pulse. In RTL it is
reproduced by edge_detector holding Q_r = 1 and D_r = 1 at the enable: i_edge_event had R unconnected and updates
D_r/Q_r only while CE = 1, so neither RST nor a disabled logger cleared them, and the first enabled cycle wrote the
held detection (record 1, level 0) and then D xor the held D_r (record 2, level 0).

Behaviour these cases pin (Mission Control 2026-11-19):
  - RST clears the detector to its init value (D_r = 0, Q_r = 0);
  - a detection held from before an enable is never written; at a re-enable at most the net level change since the
    last enabled sample is written, with the line's level;
  - unchanged: a line already high at the first enable after RST writes one rise; a re-enable on a line held high
    writes nothing.
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation  # noqa: E402

from hw.sim.portable_fifo_sources import olo_fifo_sources  # noqa: E402

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

GENERICS = {"G_ED_ID_WIDTH": 5, "G_ED_DEV_ID": 3, "G_TIMESTAMP_WIDTH": 16, "G_CONFIG_WIDTH": 16,
            "G_ERROR_WIDTH": 2, "G_ENABLE_INFILTER": 0, "G_ILA_EN": 0,
            "G_MAXFREQ_WIDTH": 24, "G_MAXFREQ_MIN_INTERVAL": 2}
DEV = 3
CFG_DISABLE = (DEV << 11) | 0b00
CFG_ENABLE = (DEV << 11) | 0b01


def _lifecycle_flag(dut) -> int:
    try:
        return int(dut.ED_MSG_LIFECYCLE.value)
    except AttributeError:
        return 0


async def _timestamp(dut):
    ts = 0
    while True:
        await RisingEdge(dut.CLK)
        ts = (ts + 1) & 0xFFFF
        dut.TIMESTAMP.value = ts


async def _collect(dut, sink):
    """(cycle, word, lifecycle flag) of every word the consumer accepts."""
    cyc = 0
    while True:
        await RisingEdge(dut.CLK)
        cyc += 1
        await ReadOnly()
        if int(dut.ED_MSG_VLD.value) == 1 and int(dut.ED_MSG_RDY.value) == 1:
            sink.append((cyc, int(dut.ED_MSG_OUT.value), _lifecycle_flag(dut)))


def _edges(sink):
    """S=0 edge records as (cycle, level, ts24); level = 1 - E (bit 24), as the host decodes it."""
    return [(c, 1 - ((w >> 24) & 1), w & 0xFFFFFF) for c, w, lf in sink if not lf and not (w >> 26) & 1]


def _no_same_level_pair(e):
    return all(x[1] != y[1] for x, y in zip(e, e[1:]))


async def _clocks(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    cocotb.start_soon(_timestamp(dut))
    sink = []
    cocotb.start_soon(_collect(dut, sink))
    dut.ED_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    return sink


async def _start(dut, level):
    sink = await _clocks(dut)
    dut.RST.value = 1
    dut.EDGE_IN.value = level
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 64)          # FIFO reset-busy window
    return sink


async def _arm(dut, gap=50):
    """stimulus_armer._enable_datalogger: disable, wait, enable."""
    dut.CONFIG.value = CFG_DISABLE
    await ClockCycles(dut.CLK, gap)
    dut.CONFIG.value = CFG_ENABLE
    await ClockCycles(dut.CLK, 60)


def _report(dut, name, sink):
    e = _edges(sink)
    dut._log.info(f"{name}: edge records (cycle, level, ts) {e}")
    return e


@cocotb.test()
async def first_enable_line_low_writes_no_edge(dut):
    """RST with EDGE_IN low, then disable -> enable: no edge record."""
    sink = await _start(dut, 0)
    await _arm(dut)
    e = _report(dut, "first enable, line low", sink)
    assert e == [], f"edge records at the first enable with the line low: {e}"


@cocotb.test()
async def first_enable_line_high_writes_one_rise(dut):
    """Unchanged behaviour: RST with EDGE_IN high, then the first enable writes one rise (level 1)."""
    sink = await _start(dut, 1)
    await _arm(dut)
    e = _report(dut, "first enable, line high", sink)
    assert [lv for _, lv, _ in e] == [1], f"expected one level-1 record, got {e}"


@cocotb.test()
async def reset_clears_detector_state_before_the_first_enable(dut):
    """Held Q_r=1/D_r=1 (power-up state or an earlier session), then RST, then the first enable with EDGE_IN low
    throughout: no edge record. Before the fix RST did not reach i_edge_event and two level-0 records were written."""
    sink = await _clocks(dut)
    dut.EDGE_IN.value = 0
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 2)
    dut.i_edge_event.Q_r.value = 1
    dut.i_edge_event.D_r.value = 1
    await ClockCycles(dut.CLK, 2)
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 64)
    dut._log.info(f"after RST: Q_r={int(dut.i_edge_event.Q_r.value)} D_r={int(dut.i_edge_event.D_r.value)}")
    await _arm(dut)
    e = _report(dut, "held state, RST, first enable, line low", sink)
    assert e == [], f"edge records at the first enable after RST with EDGE_IN low throughout: {e}"


@cocotb.test()
async def held_power_up_state_without_rst_never_writes_a_same_level_pair(dut):
    """Held Q_r=1/D_r=1 at the first enable with no RST in between and EDGE_IN low: the held detection is not
    written; at most the net change (line low vs held D_r=1) is, as one level-0 record. Before the fix: two level-0
    records one tick apart (the run9/run10 L-3 signature)."""
    sink = await _start(dut, 0)
    dut.i_edge_event.Q_r.value = 1
    dut.i_edge_event.D_r.value = 1
    await ClockCycles(dut.CLK, 2)
    await _arm(dut)
    e = _report(dut, "held Q_r=1 D_r=1, no RST, first enable, line low", sink)
    assert _no_same_level_pair(e) and len(e) <= 1 and all(lv == 0 for _, lv, _ in e), (
        f"held state wrote {e} at the first enable with EDGE_IN low")


@cocotb.test()
async def held_state_across_a_disable_never_writes_a_same_level_pair(dut):
    """Enabled, line low. EDGE_IN rises k cycles after the disable CONFIG write (k < 0: before it), falls while the
    logger is disabled, then re-enable with the line low. At the re-enable: no same-level pair, at most one record,
    at the line's level. Before the fix k = 1 (the rise is the last enabled sample) wrote two level-0 records."""
    sink = await _start(dut, 0)
    await _arm(dut)
    bad = {}
    for k in range(-3, 6):
        dut.EDGE_IN.value = 0
        await ClockCycles(dut.CLK, 20)
        if k < 0:
            dut.EDGE_IN.value = 1
            await ClockCycles(dut.CLK, -k)
            dut.CONFIG.value = CFG_DISABLE
        else:
            dut.CONFIG.value = CFG_DISABLE
            await ClockCycles(dut.CLK, k)
            dut.EDGE_IN.value = 1
        await ClockCycles(dut.CLK, 30)
        dut.EDGE_IN.value = 0                     # the line returns low while the logger is disabled
        await ClockCycles(dut.CLK, 30)
        mark = len(sink)
        ts_enable = int(dut.TIMESTAMP.value)
        dut.CONFIG.value = CFG_ENABLE
        await ClockCycles(dut.CLK, 40)
        at_enable = _edges(sink[mark:])
        dut._log.info(f"k={k}: enable write at ts {ts_enable}; records at the re-enable {at_enable}")
        if not (_no_same_level_pair(at_enable) and len(at_enable) <= 1 and all(lv == 0 for _, lv, _ in at_enable)):
            bad[k] = at_enable
        if k == 1:
            # The rise was the last enabled sample and the fall happened while disabled: the fall is reported once,
            # at the re-enable, with level 0, timestamped at the re-enable (a few clocks after the CONFIG write).
            assert len(at_enable) == 1 and at_enable[0][1] == 0 and 0 < at_enable[0][2] - ts_enable <= 8, (
                f"k=1: expected exactly one level-0 record timestamped at the re-enable, got {at_enable} "
                f"(enable write at ts {ts_enable})")
    assert not bad, f"re-enable after a disable wrote a same-level pair or a wrong level: {bad}"


@cocotb.test()
async def reenable_on_a_line_held_high_writes_nothing(dut):
    """Unchanged behaviour: the line is high before, during and after a disable; the re-enable writes no record (the
    frozen L-2 gate allows one SCL edge outside the burst, and SCL's is the routing edge)."""
    sink = await _start(dut, 0)
    await _arm(dut)
    dut.EDGE_IN.value = 1
    await ClockCycles(dut.CLK, 30)
    dut.CONFIG.value = CFG_DISABLE
    await ClockCycles(dut.CLK, 30)
    mark = len(sink)
    dut.CONFIG.value = CFG_ENABLE
    await ClockCycles(dut.CLK, 40)
    e = _edges(sink[mark:])
    dut._log.info(f"re-enable on a high line: {e}")
    assert e == [], f"records at a re-enable on a line held high: {e}"


def main():
    run_simulation(
        top_level="edge_datalogger",
        module=Path(__file__).stem,
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/edge_detector/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/nonstd/edge_datalogger/edge_datalogger.vhd",
            ]
        },
        generics=GENERICS,
        waves=False,
    )


if __name__ == "__main__":
    main()
