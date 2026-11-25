# rr unknown-llm (AI-generated)
"""DS-T2.84 — an edge lifecycle frame is an annotation, never an EDGE_EVENTS word.

On silicon (DS-P1.71, deepskopion 28da400) the DS-T2.43 ENABLED/DISABLED frame
was batched like an edge event. A batch entry keeps only S/F/E, and the
lifecycle type "100" is S=1/F=0/E=0, so it reached EDGE_EVENTS as a readback of
level 0. Every consumer that seeds a lane from S=1 words drew idle-high CS low.

The fabric now frames it on its own (msg_type 0x0608). These tests feed the
daemon the bytes that land in DDR — the lifecycle frame, then the readback in
an EDGE_BATCH — and judge what the consumer receives: the only S=1 word on
EDGE_EVENTS is the readback, and the marker arrives as an ``edge_lifecycle``
annotation that names its state.
"""

from __future__ import annotations

import itertools

import pytest

from sw.stream import zs_msg
from sw.stream.data_source import DdrRingDataSource
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.edge_capture import decode_edge_word
from sw.stream.regbank import FakeRegbank, Regs

# Literals (ROUTERTL-002): zs_msg_pkg.vhd C_ZS_EVT_EDGE_LIFECYCLE and the
# edge_datalogger.vhd lifecycle codes.
EVT_EDGE_LIFECYCLE = 0x08
CODE_ENABLED = 0x03
CODE_DISABLED = 0x00
CS_INSTANCE = 6            # DS-P1.71: EDGE id 6 <- CS (EMIO_MUX_SEL 0x0C)
RING_BYTES = 512


class _CommitMirrorRegbank(FakeRegbank):
    """RING_COMMIT_PTR (bytes) follows RING_WRITE_PTR (words), as in
    test_ddr_ring_data_source.py."""

    def read_u32(self, offset: int) -> int:
        if offset == Regs.RING_COMMIT_PTR:
            return super().read_u32(Regs.RING_WRITE_PTR) * 4
        return super().read_u32(offset)


def _source(regbank, ddr):
    times = iter(itertools.chain([0.0], itertools.count(start=0.05, step=0.05)))
    return DdrRingDataSource(regbank=regbank, ddr=ddr, sample_clk_hz=100_000_000,
                             poll_period_s=0.05, duration_s=0.0,
                             write_back_read_ptr=True, clock=lambda: next(times))


def _lifecycle_frame(code: int, timestamp: int) -> bytes:
    """The single-word zs_msg the core frames for a lifecycle marker: payload is
    the edge_datalogger word DEV & "100" & ts16 & code, little-endian."""
    word = (CS_INSTANCE << 27) | (0b100 << 24) | ((timestamp & 0xFFFF) << 8) | code
    hdr = zs_msg.make_header(datalogger_id=CS_INSTANCE, domain=zs_msg.DOMAIN_EDGE,
                             event_id=EVT_EDGE_LIFECYCLE, severity=zs_msg.SEV_INFO,
                             timestamp=timestamp, payload_len=4)
    return zs_msg.pack_header(hdr) + word.to_bytes(4, "little")


def _drain(frames: bytes):
    regbank, ddr = _CommitMirrorRegbank(), FakeDdrMmap(RING_BYTES)
    src = _source(regbank, ddr)
    src.next_batch()  # seed the read pointer
    ddr.write_bytes(0, frames)
    regbank.set(Regs.RING_WRITE_PTR, (len(frames) + 3) // 4)
    return src.next_batch()


@pytest.mark.parametrize("code,state", [(CODE_ENABLED, "enabled"),
                                        (CODE_DISABLED, "disabled")])
def test_lifecycle_frame_is_an_annotation_not_a_level_word(code, state):
    anchor = 0x3CFA1A
    readback = zs_msg.EdgeEvent(ts_64b=anchor + 5, channel=CS_INSTANCE,
                                s=True, f=False, e=True)       # CS idles high
    span = _lifecycle_frame(code, anchor)
    span += b"\x00" * (-len(span) % 4)
    span += zs_msg.pack_edge_batch(datalogger_id=CS_INSTANCE, channel=CS_INSTANCE,
                                   events=[readback], anchor_ts=anchor + 5)

    batch = _drain(span)

    lifecycle = [a for a in batch.annotations if a.kind == "edge_lifecycle"]
    assert len(lifecycle) == 1, (
        "lifecycle frame was not decoded as an edge_lifecycle annotation: got "
        f"{[(a.kind, a.text) for a in batch.annotations]}")
    assert lifecycle[0].fields == {"channel": CS_INSTANCE, "state": state,
                                   "code": code}, lifecycle[0].fields
    levels = [decode_edge_word(w) for w in batch.edge_words]
    assert [(s.instance, s.s, s.level) for s in levels] == [(CS_INSTANCE, 1, 1)], (
        "EDGE_EVENTS must carry exactly the CS readback (S=1, level 1), got "
        f"{[hex(w) for w in batch.edge_words]}")
