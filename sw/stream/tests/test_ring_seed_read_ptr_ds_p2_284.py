# rr unknown-llm (AI-generated)
"""DS-P2.284: the daemon writes RING_READ_PTR at its seed tick, not only after its first drain.

Before the fix, the seed tick set the read head to RING_COMMIT_PTR and returned without touching the register,
and a tick with nothing unread returned before the write-back. A ring that was full when the daemon started got
no further commit (the PL is backpressured on RING_READ_PTR), so no tick drained and the register was never
written: the ring stayed full for the life of the daemon.

Expected values are hard-coded (ring 256 B; the seed skips the back-history and never parses it).
"""

from __future__ import annotations

import itertools
import logging

from sw.stream.data_source import DdrRingDataSource
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.regbank import FakeRegbank, Regs

RING_BYTES = 256


def _source(regbank, ddr, **kw):
    times = iter(itertools.chain([0.0], itertools.count(start=0.05, step=0.05)))
    args = dict(sample_clk_hz=100_000_000, poll_period_s=0.05, duration_s=0.0,
                write_back_read_ptr=True, clock=lambda: next(times))
    args.update(kw)
    return DdrRingDataSource(regbank=regbank, ddr=ddr, **args)


def _read_ptr_writes(regbank):
    return [v for a, v in regbank.writes if a == Regs.RING_READ_PTR]


def test_full_ring_at_start_gets_its_read_ptr_at_the_seed_and_keeps_it():
    """Register 64, commit 32 (= 64 + 256 - 32 mod 256): 224 B unread, one 32-B DDR word short of the ring size,
    the largest distance the mod-ring arithmetic can tell from empty. No further commit arrives."""
    rb = FakeRegbank()
    rb.set(Regs.RING_READ_PTR, 64)
    rb.set(Regs.RING_COMMIT_PTR, 32)
    src = _source(rb, FakeDdrMmap(RING_BYTES))
    batch = src.next_batch()
    assert batch.annotations == []                         # the back-history is skipped, not parsed
    assert rb.read_u32(Regs.RING_READ_PTR) == 32
    assert _read_ptr_writes(rb) == [32]
    for _ in range(3):                                     # no commit moves: the register stays at the seed
        src.next_batch()
    assert rb.read_u32(Regs.RING_READ_PTR) == 32


def test_non_full_ring_with_back_history_seed_at_320():
    """S-before-3 shape: the register reads 0 while the commit is 320 (ring 1024 B)."""
    rb = FakeRegbank()
    rb.set(Regs.RING_COMMIT_PTR, 320)
    src = _source(rb, FakeDdrMmap(1024))
    src.next_batch()
    assert rb.read_u32(Regs.RING_READ_PTR) == 320
    assert _read_ptr_writes(rb) == [320]


def test_seed_logs_one_line_naming_the_skipped_bytes(caplog):
    rb = FakeRegbank()
    rb.set(Regs.RING_READ_PTR, 64)
    rb.set(Regs.RING_COMMIT_PTR, 32)
    src = _source(rb, FakeDdrMmap(RING_BYTES))
    with caplog.at_level(logging.INFO, logger="sw.stream.data_source"):
        src.next_batch()
        src.next_batch()
    seed_lines = [r.getMessage() for r in caplog.records if "ring seed" in r.getMessage()]
    assert seed_lines == ["ring seed: RING_READ_PTR 64 -> 32, 224 bytes of back-history skipped (DS-P2.284)"]


def test_no_seed_write_when_write_back_is_off():
    rb = FakeRegbank()
    rb.set(Regs.RING_COMMIT_PTR, 320)
    src = _source(rb, FakeDdrMmap(1024), write_back_read_ptr=False)
    src.next_batch()
    assert _read_ptr_writes(rb) == []
