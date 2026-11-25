# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.100: the per-tick byte budget leaves real headroom over the TPUT-001 ring write.

DS-DE25-TPUT-001 writes the DE25 ring at 20.0e6 B/s (DS-P1.74 run 3). The drain
copies at most _MAX_SPAN_BYTES_PER_TICK per poll tick, so span / poll_period is
its nominal ceiling. At 1 MiB and 50 ms that is 20.97e6 B/s, 4.9 % above the
write rate. The span is sized for at least twice the write rate, and must still
hold the largest frame (HEADER_SIZE + 0xFFFF) or that frame could never complete.

The silicon block of the DE25 PREREG measures the rate actually reached; these
tests hold the budget, not the silicon rate.
"""
from __future__ import annotations

import inspect
import itertools

import pytest

from sw.stream import zs_msg
from sw.stream.data_source import DdrRingDataSource
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.regbank import FakeRegbank, Regs

TPUT001_WRITE_BPS = 20_000_000      # DS-DE25-TPUT-001 ring write, DS-P1.74 run 3
HEADROOM = 2                         # the drain budget is at least 2x the write rate
_BYTES_PER_MSG = 4
_FRAME_BYTES = 160                   # the ring frame size measured under the TPUT-001 stimulus


class _MirrorRegbank(FakeRegbank):
    """RING_COMMIT_PTR mirrors RING_WRITE_PTR * 4 (byte offset)."""

    def read_u32(self, offset: int) -> int:
        if offset == Regs.RING_COMMIT_PTR:
            return super().read_u32(Regs.RING_WRITE_PTR) * _BYTES_PER_MSG
        return super().read_u32(offset)


def _frame_160() -> bytes:
    hdr = zs_msg.make_header(datalogger_id=1, domain=zs_msg.DOMAIN_UART,
                             event_id=0x7F, severity=zs_msg.SEV_INFO,
                             timestamp=0, payload_len=_FRAME_BYTES - zs_msg.HEADER_SIZE)
    return zs_msg.pack_header(hdr) + bytes(_FRAME_BYTES - zs_msg.HEADER_SIZE)


def _default_poll_period_s() -> float:
    return inspect.signature(DdrRingDataSource.__init__).parameters["poll_period_s"].default


@pytest.fixture
def frozen_budget(monkeypatch):
    from sw.stream import data_source as ds_mod
    monkeypatch.setattr(ds_mod, "_budget_clock", lambda: 0.0, raising=False)


def test_byte_budget_is_at_least_twice_the_tput001_write_rate():
    """span / default poll period >= 2 x 20.0e6 B/s (40.0e6 B/s)."""
    span = DdrRingDataSource._MAX_SPAN_BYTES_PER_TICK
    poll = _default_poll_period_s()
    assert poll == 0.05
    ceiling = span / poll
    assert ceiling >= HEADROOM * TPUT001_WRITE_BPS, (
        f"per-tick byte budget {span} B / {poll} s = {ceiling:.4g} B/s, below "
        f"{HEADROOM} x the {TPUT001_WRITE_BPS:.3g} B/s TPUT-001 ring write")


def test_span_still_holds_the_largest_frame():
    """A span smaller than the largest frame would leave that frame unfinished forever."""
    assert DdrRingDataSource._MAX_SPAN_BYTES_PER_TICK > zs_msg.HEADER_SIZE + 0xFFFF


def test_one_tick_drains_the_headroom_budget_from_a_full_backlog(frozen_budget):
    """With 4 MiB of 160-byte frames unread and the time budget held open, one
    tick consumes at least 2 x 20.0e6 B/s x 50 ms = 2 000 000 B."""
    regbank, ddr = _MirrorRegbank(), FakeDdrMmap(8 << 20)
    times = iter(itertools.chain([0.0], itertools.count(start=0.05, step=0.05)))
    src = DdrRingDataSource(regbank=regbank, ddr=ddr, sample_clk_hz=100_000_000,
                            poll_period_s=0.05, duration_s=0.0,
                            write_back_read_ptr=True, clock=lambda: next(times))
    src.next_batch()                                   # seed at 0

    n = (4 << 20) // _FRAME_BYTES
    ddr.write_bytes(0, _frame_160() * n)
    regbank.set(Regs.RING_WRITE_PTR, n * _FRAME_BYTES // _BYTES_PER_MSG)

    src.next_batch()
    consumed = [v for a, v in regbank.writes if a == Regs.RING_READ_PTR][-1]
    assert consumed >= 2_000_000, (
        f"one tick consumed {consumed} B; the headroom budget needs 2 000 000 B "
        f"(2 x {TPUT001_WRITE_BPS} B/s x 0.05 s)")
