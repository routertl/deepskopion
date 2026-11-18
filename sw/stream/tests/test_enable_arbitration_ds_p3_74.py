"""DS-P3.74: host CONFIG.enable vs trigger enable-mask arbitration.

Rule (datalogger_gather_contract.md §"Enable arbitration"): the host word is the
baseline and the ceiling; a trigger supplies a transient ALLOW mask and the
effective enable is ``host_enable AND allow``. A trigger can never enable a
datalogger the host disabled; a host write during an active mask is still
gated; releasing the mask restores the LATEST host word; the trigger only
touches bit 0; a CAN data-bank word is not an enable word.

Every assertion reads the DATALOGGER_CONFIG register (FakeRegbank) — the word
config_control latches on silicon — not an arbiter return value.
"""

from __future__ import annotations

import itertools

from sw.stream.data_source import (
    DdrRingDataSource, RegbankDataSource, _make_enable_arbiter)
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.regbank import FakeRegbank, Regs

ALL = 0xFFFFFFFF


def _cfg_writes(rb: FakeRegbank) -> list[int]:
    return [v for off, v in rb.writes if off == Regs.DATALOGGER_CONFIG]


def _word(dl_id: int, payload: int) -> int:
    return (dl_id << 11) | payload


def test_trigger_cannot_enable_host_disabled_datalogger():
    rb = FakeRegbank()
    arb = _make_enable_arbiter(rb)
    assert arb.apply_host(5, 0x8) is None           # host: id 5 DISABLED (CPHA-less, enable=0)
    assert arb.apply_trigger_mask(1 << 5) is None   # trigger asks for id 5 allowed / on
    # No write ever raised id 5's enable bit: host-disable is absolute.
    enabled_5 = [w for w in _cfg_writes(rb) if w >> 11 == 5 and w & 1]
    assert enabled_5 == [], f"trigger re-enabled host-disabled id 5: {enabled_5}"
    assert rb.read_u32(Regs.DATALOGGER_CONFIG) == _word(5, 0x8)


def test_host_write_during_trigger_gate_stays_gated():
    rb = FakeRegbank()
    arb = _make_enable_arbiter(rb)
    arb.apply_host(3, 0x1)
    arb.apply_host(5, 0x1)
    assert arb.apply_trigger_mask(ALL & ~(1 << 5)) is None    # trigger gates id 5 off
    assert rb.read_u32(Regs.DATALOGGER_CONFIG) == _word(5, 0x0)
    # Host re-sends id 5 enabled + CPHA while the gate is active: the CPHA bit
    # lands, the enable stays gated.
    assert arb.apply_host(5, 0x3) is None
    assert rb.read_u32(Regs.DATALOGGER_CONFIG) == _word(5, 0x2), \
        "host write bypassed the active trigger gate"


def test_trigger_release_restores_latest_host_baseline():
    rb = FakeRegbank()
    arb = _make_enable_arbiter(rb)
    arb.apply_host(5, 0x1)
    arb.apply_trigger_mask(ALL & ~(1 << 5))
    arb.apply_host(5, 0x3)                  # new host word while gated
    assert arb.release_trigger_mask() is None
    assert rb.read_u32(Regs.DATALOGGER_CONFIG) == _word(5, 0x3), \
        "release did not restore the latest host baseline"


def test_trigger_gate_does_not_touch_can_data_bank_word():
    rb = FakeRegbank()
    arb = _make_enable_arbiter(rb)
    arb.apply_host(20, 0x033)                       # CAN nominal bank: presc + enable
    arb.apply_trigger_mask(ALL & ~(1 << 20))        # trigger gates CAN
    assert arb.apply_host(20, 0x400 | 21) is None   # data bank: data_cyc = 21 (bit 0 set)
    assert rb.read_u32(Regs.DATALOGGER_CONFIG) == _word(20, 0x400 | 21), \
        "data-bank data_cyc[0] was treated as an enable bit"
    assert arb.release_trigger_mask() is None       # nominal word comes back enabled
    assert rb.read_u32(Regs.DATALOGGER_CONFIG) == _word(20, 0x033)


def _drive_gate_and_release(src, rb):
    assert src.apply_config(5, 0x1) is None
    assert src.apply_trigger_enable_mask(ALL & ~(1 << 5)) is None
    assert rb.read_u32(Regs.DATALOGGER_CONFIG) == _word(5, 0x0), \
        "host CONTROL did not reach the enable arbiter"
    assert src.release_trigger_enable_mask() is None
    assert rb.read_u32(Regs.DATALOGGER_CONFIG) == _word(5, 0x1)
    # Validation still precedes arbitration: a reject writes nothing.
    n = len(_cfg_writes(rb))
    assert src.apply_config(5, 0x8001)
    assert src.apply_config(5, 0x1, op="arm")
    assert len(_cfg_writes(rb)) == n


def test_regbank_source_host_control_goes_through_arbiter():
    rb = FakeRegbank()
    src = RegbankDataSource(regbank=rb, clock=lambda: 0.0)
    _drive_gate_and_release(src, rb)


def test_ddr_ring_source_host_control_goes_through_arbiter():
    rb = FakeRegbank()
    times = iter(itertools.chain([0.0], itertools.count(start=0.05, step=0.05)))
    src = DdrRingDataSource(regbank=rb, ddr=FakeDdrMmap(4096),
                            sample_clk_hz=100_000_000, poll_period_s=0.05,
                            duration_s=0.0, clock=lambda: next(times))
    _drive_gate_and_release(src, rb)
