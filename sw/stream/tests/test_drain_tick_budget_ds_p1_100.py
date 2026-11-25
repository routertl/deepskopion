# rr unknown-llm (AI-generated)
"""DS-P1.100: a drain tick stops at the byte or time budget, not at a frame count.

DE25 silicon (DS-P1.74 run 3) measured the host drain at exactly 4096 frames of
160 B per tick at 20 ticks/s = 13.1e6 B/s, against a ring written at 20.0e6 B/s:
the fixed _MAX_FRAMES_PER_TICK = 4096 bound the drain below the producer while
the byte budget (_MAX_SPAN_BYTES_PER_TICK) and the time budget (_TICK_BUDGET_S)
both had room. These tests hold the time budget open and give the source more
backlog than one span, so the only limit left is the byte budget.
"""

from __future__ import annotations

import itertools

import pytest

from sw.stream import zs_msg
from sw.stream.data_source import DdrRingDataSource
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.regbank import FakeRegbank, Regs

_BYTES_PER_MSG = 4
_FRAME_BYTES = 160          # the ring frame size measured under the TPUT-001 stimulus


class _MirrorRegbank(FakeRegbank):
    """RING_COMMIT_PTR mirrors RING_WRITE_PTR * 4 (byte offset)."""

    def read_u32(self, offset: int) -> int:
        if offset == Regs.RING_COMMIT_PTR:
            return super().read_u32(Regs.RING_WRITE_PTR) * _BYTES_PER_MSG
        return super().read_u32(offset)


def _frame_160() -> bytes:
    """A 160-byte frame (20-byte header + 140-byte payload) on the generic path."""
    hdr = zs_msg.make_header(datalogger_id=1, domain=zs_msg.DOMAIN_UART,
                             event_id=0x7F, severity=zs_msg.SEV_INFO,
                             timestamp=0, payload_len=_FRAME_BYTES - zs_msg.HEADER_SIZE)
    frame = zs_msg.pack_header(hdr) + bytes(_FRAME_BYTES - zs_msg.HEADER_SIZE)
    assert len(frame) == _FRAME_BYTES
    return frame


def _source(regbank, ddr):
    times = iter(itertools.chain([0.0], itertools.count(start=0.05, step=0.05)))
    return DdrRingDataSource(regbank=regbank, ddr=ddr, sample_clk_hz=100_000_000,
                             poll_period_s=0.05, duration_s=0.0,
                             write_back_read_ptr=True, clock=lambda: next(times))


def _read_ptr_writes(regbank):
    return [v for a, v in regbank.writes if a == Regs.RING_READ_PTR]


@pytest.fixture
def frozen_budget(monkeypatch):
    from sw.stream import data_source as ds_mod
    monkeypatch.setattr(ds_mod, "_budget_clock", lambda: 0.0, raising=False)


def test_tick_stops_at_byte_budget_not_frame_count(frozen_budget):
    """With 4 MiB of 160-byte frames unread and the time budget held open, one
    tick consumes every whole frame inside one span (2 MiB): 13 107 frames,
    2 097 120 B. The old 4096-frame cap stopped it at 655 360 B."""
    span = DdrRingDataSource._MAX_SPAN_BYTES_PER_TICK
    regbank, ddr = _MirrorRegbank(), FakeDdrMmap(8 << 20)
    src = _source(regbank, ddr)
    src.next_batch()                                   # seed at 0

    frame = _frame_160()
    n = (4 << 20) // _FRAME_BYTES
    ddr.write_bytes(0, frame * n)
    regbank.set(Regs.RING_WRITE_PTR, n * _FRAME_BYTES // _BYTES_PER_MSG)

    src.next_batch()
    consumed = _read_ptr_writes(regbank)[-1]
    expected = (span // _FRAME_BYTES) * _FRAME_BYTES   # 13107 * 160 = 2 097 120
    assert expected == 2_097_120
    assert consumed == expected, (
        f"one tick consumed {consumed} B ({consumed // _FRAME_BYTES} frames); "
        f"the byte budget allows {expected} B ({span // _FRAME_BYTES} frames)")


def test_per_tick_drain_exceeds_measured_write_rate(frozen_budget):
    """At 20 ticks/s the per-tick drain must exceed the 20.0e6 B/s ring write
    measured on DE25 silicon under the TPUT-001 stimulus (1 000 000 B/tick)."""
    regbank, ddr = _MirrorRegbank(), FakeDdrMmap(4 << 20)
    src = _source(regbank, ddr)
    src.next_batch()

    frame = _frame_160()
    n = (2 << 20) // _FRAME_BYTES
    ddr.write_bytes(0, frame * n)
    regbank.set(Regs.RING_WRITE_PTR, n * _FRAME_BYTES // _BYTES_PER_MSG)

    src.next_batch()
    per_tick = _read_ptr_writes(regbank)[-1]
    assert per_tick * 20 > 20_000_000, (
        f"{per_tick} B/tick x 20 ticks/s = {per_tick * 20} B/s "
        "does not exceed the 20.0e6 B/s ring write")


def test_frame_cap_never_binds_before_the_byte_budget():
    """The frame cap is derived from the byte budget: a span cannot hold more
    frames than it allows, the smallest frame being a bare header."""
    assert (DdrRingDataSource._MAX_FRAMES_PER_TICK
            >= DdrRingDataSource._MAX_SPAN_BYTES_PER_TICK // zs_msg.HEADER_SIZE)


def _bare_sys_frames(n: int) -> bytes:
    """*n* 20-byte frames (a header, no payload): the smallest frame, so one
    1 MiB span holds the most frames a tick can take. DOMAIN_SYS decodes each
    one to its own annotation (sys_zs_frame), with no error collapse."""
    return b"".join(zs_msg.pack_header(zs_msg.make_header(
        datalogger_id=1, domain=zs_msg.DOMAIN_SYS, event_id=1,
        severity=zs_msg.SEV_INFO, timestamp=100 * i, payload_len=0))
        for i in range(n))


def test_full_span_tick_reaches_the_wire_whole(frozen_budget):
    """The batch a tick hands the consumer is now bounded by the 2 MiB span,
    not by 4096 frames. A full span of the smallest frame is 104 857 frames in
    one tick; every one reaches the wire as an annotation and the stream passes
    the wave_stream_v1 contract check, so nothing downstream caps the batch."""
    import io
    import sys
    from pathlib import Path

    import msgpack

    from sw.stream import protocol as proto
    from sw.stream.native import ring_dump

    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root / "contracts" / "wave_stream_v1"))
    import wave_stream_contract as contract  # noqa: E402

    n = DdrRingDataSource._MAX_SPAN_BYTES_PER_TICK // zs_msg.HEADER_SIZE
    assert n == 104_857
    dump = _bare_sys_frames(n)

    src = ring_dump.daemon_source(dump, 100_000_000, None)
    batch = src.next_batch()
    assert src._read_byte_ofs == n * zs_msg.HEADER_SIZE, "one tick takes the whole span"
    assert sum(a.kind == "sys_zs_frame" for a in batch.annotations) == n

    stream = ring_dump.daemon_wire(dump, 100_000_000, None)
    anns = [a for tag, p in msgpack.Unpacker(io.BytesIO(stream), raw=False,
                                             strict_map_key=False)
            if tag == proto.TAG_ANNOTATIONS for a in p["annotations"]]
    assert sum(a["kind"] == "sys_zs_frame" for a in anns) == n, "frames lost on the wire"
    verdict = contract.check_stream(stream, "p2c")
    assert verdict.verdict == "ACCEPT", verdict.failures
