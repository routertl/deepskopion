# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""edge4_live_stimulus.run never queues a MISO byte while the previous transfer is running.

DS-P2.386: the trailing 0x00 pad is gone, so a MISO byte queued before the previous transfer's CS has
risen would be consumed at that transfer's trailing SCLK edge and lost. A loop that runs late (the period
shorter than a transfer) must therefore be held back to one transfer, plus a margin, between a transfer's
start and the next preload. The recording rb and clock are fakes; nothing touches a device.
"""
from __future__ import annotations

import sys
from pathlib import Path

_STREAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_STREAM.parent.parent))

from sw.stream.testbench import edge4_live_stimulus as e4  # noqa: E402

SPI_INJ_DATAIN = e4.SPI_INJ_DATAIN
STIM_SPI_DATA = e4.STIM_SPI_DATA


class _Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += int(seconds * 1e9)


class _Rb:
    def __init__(self, clock, start_delay_ns=0):
        self.clock = clock
        self.start_delay_ns = start_delay_ns   # host stall just before the STIM_SPI_DATA store lands
        self.writes = []   # (offset, value, ns)

    def write_u32(self, offset, value):
        if offset == STIM_SPI_DATA:
            self.clock.now += self.start_delay_ns
        self.clock.now += 1_000   # one register write costs 1 us
        self.writes.append((offset, value, self.clock.now))


def _run(phase, monkeypatch, start_delay_ns=0):
    clock = _Clock()
    rb = _Rb(clock, start_delay_ns)
    monkeypatch.setattr(e4, "arm", lambda rb_, phase_, sleep_: None)
    monkeypatch.setattr(e4, "snapshot", lambda rb_: {})
    e4.run(rb, phase, clock_ns=clock, sleep=clock.sleep)
    return rb.writes, phase


def test_late_loop_never_preloads_during_the_previous_transfer(monkeypatch):
    # period 1 us: far shorter than a transfer (18 * 200 / CLK_HZ), so every transfer is "late".
    late = e4.Phase(name="late", clk_div=200, transfers=((0x11, 0x91), (0x22, 0xD2), (0x33, 0x27)),
                    period_s=1e-6, settle_s=0.0)
    # A 40 us host stall before each start store (longer than a 36 us transfer): a guard timestamped before
    # the store would measure from a moment earlier than the transfer's real start and let the next byte in.
    writes, phase = _run(late, monkeypatch, start_delay_ns=40_000)
    starts = [t for off, _v, t in writes if off == STIM_SPI_DATA]
    preloads = [t for off, _v, t in writes if off == SPI_INJ_DATAIN]
    assert len(starts) == len(preloads) == 3, "all three transfers must be issued (nothing exercised otherwise)"
    transfer_ns = int(phase.transfer_s * 1e9)
    assert transfer_ns > 10_000, "the fake transfer must be long enough to make the gap check meaningful"
    for k in range(1, 3):
        gap = preloads[k] - starts[k - 1]
        assert gap >= transfer_ns, (
            f"transfer {k}: MISO byte queued {gap} ns after the previous start, "
            f"inside its {transfer_ns} ns transfer")


def test_each_transfer_queues_only_its_miso_byte(monkeypatch):
    phase = e4.Phase(name="one", clk_div=200, transfers=((0x11, 0x91), (0x22, 0xD2)),
                     period_s=0.01, settle_s=0.0)
    writes, _ = _run(phase, monkeypatch)
    assert [(off, v) for off, v, _t in writes] == [
        (SPI_INJ_DATAIN, 0x91), (STIM_SPI_DATA, 0x11),
        (SPI_INJ_DATAIN, 0xD2), (STIM_SPI_DATA, 0x22)]
