# rr unknown-llm (AI-generated)
"""DS-P2.16 — Stream-RLE edge words reach EDGE_EVENTS as ordinary edges.

An edge_datalogger in Stream-RLE mode sends one DOMAIN_EDGE/EVT_EDGE_RLE
(0x0609) frame per RLE word instead of EDGE_BATCH entries. These tests feed the
daemon the bytes that land in DDR and judge what the consumer receives: the
EDGE_EVENTS words a RouteWave edge lane draws. The SYNC anchor arrives as an S=1
level word, every edge as an S=0 word at its absolute time.

All words and times are literals from the format in
hw/rtl/datalogger/nonstd/edge_datalogger/edge_datalogger_protocol.yml
(ROUTERTL-002).
"""

from __future__ import annotations

import itertools

import pytest

from sw.stream import decoders, zs_msg
from sw.stream.data_source import DdrRingDataSource
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.edge_capture import (EdgeRleDecoder, edge_event_word,
                                    rle_words_to_transitions)
from sw.stream.regbank import FakeRegbank, Regs

EVT_EDGE_RLE = 0x09        # zs_msg_pkg.vhd C_ZS_EVT_EDGE_RLE
INST = 1                   # EDGE0 id
RING_BYTES = 1024
NS_PER_TICK = 10           # 100 MHz sample clock

SYNC_LO, SYNC_HI, PACK, LONG, IDLE = 0b000, 0b001, 0b101, 0b110, 0b111


def rle(kind, payload):
    return (INST << 27) | (kind << 24) | payload


# A capture: line high at TIMESTAMP 0x000100, edges after 10, 20, 30 ticks
# (PACK), one after 1000 (LONG), a 0xFFFFFF-tick quiet stretch (IDLE), one more
# edge 4 ticks later (a flushed, zero-padded PACK), then a re-anchor (SYNC_LO
# at 0x000200, as after a lost word).
WORDS = [
    rle(SYNC_HI, 0x000100),
    rle(PACK, (30 << 16) | (20 << 8) | 10),
    rle(LONG, 1000),
    rle(IDLE, 0xFFFFFF),
    rle(PACK, 4),
    rle(SYNC_LO, 0x000200),
]
# The 64-bit frame stamps: the framer stamps each word a few ticks after it was
# written. Bits above 24 are set so the SYNC must be unrolled onto them.
FRAME_TICKS = [0x5_0000_0108, 0x5_0000_0140, 0x5_0000_0500, 0x5_0100_0600,
               0x5_0100_0620, 0x5_0200_0210]
BASE = 0x5_0000_0100
# (tick, level, is_sync)
WANT = [
    (BASE, 1, True),
    (BASE + 10, 0, False), (BASE + 30, 1, False), (BASE + 60, 0, False),
    (BASE + 1060, 1, False),
    (BASE + 1060 + 0xFFFFFF + 4, 0, False),
    (0x5_0200_0200, 0, True),
]


class _CommitMirrorRegbank(FakeRegbank):
    def read_u32(self, offset: int) -> int:
        if offset == Regs.RING_COMMIT_PTR:
            return super().read_u32(Regs.RING_WRITE_PTR) * 4
        return super().read_u32(offset)


def _frame(word: int, tick: int) -> bytes:
    hdr = zs_msg.make_header(datalogger_id=INST, domain=zs_msg.DOMAIN_EDGE,
                             event_id=EVT_EDGE_RLE, severity=zs_msg.SEV_INFO,
                             timestamp=tick, payload_len=4)
    return zs_msg.pack_header(hdr) + word.to_bytes(4, "little")


def _drain(frames: bytes):
    regbank, ddr = _CommitMirrorRegbank(), FakeDdrMmap(RING_BYTES)
    times = iter(itertools.chain([0.0], itertools.count(start=0.05, step=0.05)))
    src = DdrRingDataSource(regbank=regbank, ddr=ddr, sample_clk_hz=100_000_000,
                            poll_period_s=0.05, duration_s=0.0,
                            write_back_read_ptr=True, clock=lambda: next(times))
    src.next_batch()
    ddr.write_bytes(0, frames)
    regbank.set(Regs.RING_WRITE_PTR, (len(frames) + 3) // 4)
    return src.next_batch()


def test_rle_frames_become_edge_events_words():
    batch = _drain(b"".join(_frame(w, t) for w, t in zip(WORDS, FRAME_TICKS)))
    want = [edge_event_word(instance=INST, timestamp_ns=tick * NS_PER_TICK,
                            s=1 if sync else 0, f=0,
                            e=level if sync else 1 - level, clk_mhz=100)
            for tick, level, sync in WANT]
    assert batch.edge_words == want, (
        "Stream-RLE frames did not reach EDGE_EVENTS as the capture's edges:\n"
        f" got  {[hex(w) for w in batch.edge_words]}\n want {[hex(w) for w in want]}")
    assert batch.edge_anchor_ns == BASE * NS_PER_TICK
    assert not [a for a in batch.annotations if a.kind.startswith("edge_rle")], (
        "an RLE word was emitted as an annotation instead of edges")


def test_rle_decoder_transitions():
    assert rle_words_to_transitions(WORDS) == [
        (0x100, 1), (0x10A, 0), (0x11E, 1), (0x13C, 0), (0x524, 1),
        (0x524 + 0xFFFFFF + 4, 0), (0x200, 0)]


def test_rle_decoder_orphans_and_resyncs():
    dec = EdgeRleDecoder()
    assert dec.feed(rle(PACK, 0x010101)) == []
    assert dec.orphans == 1
    dec.feed(rle(SYNC_LO, 5))
    dec.feed(rle(SYNC_HI, 9))
    assert dec.resyncs == 1 and dec.level == 1
    with pytest.raises(ValueError):
        dec.feed(rle(0b010, 0))


def test_reserved_rle_type_drops_sync_not_the_drain():
    """A 0x0609 frame whose word has a reserved type (here 0b010) is corrupt: the
    drain skips it, keeps going, and places no edge until the next SYNC, since
    the level and time base after an undecodable word are unknown."""
    words = [rle(SYNC_HI, 0x000100), rle(PACK, (30 << 16) | (20 << 8) | 10),
             rle(0b010, 0), rle(PACK, 5), rle(SYNC_LO, 0x000200), rle(PACK, 7)]
    ticks = [0x5_0000_0108, 0x5_0000_0140, 0x5_0000_0150, 0x5_0000_0160,
             0x5_0000_0208, 0x5_0000_0210]
    frames = b"".join(_frame(w, t) for w, t in zip(words, ticks))
    try:
        batch = _drain(frames)
    except ValueError as exc:
        raise AssertionError("after a reserved-type word the drain must drop to the "
                             f"next SYNC: next_batch raised {exc}") from exc
    # E is masked: the polarity mapping is test_rle_frames_become_edge_events_words's
    # claim, this one is about which edges survive and when.
    e_bit = edge_event_word(0, 0, 0, 0, 1) ^ edge_event_word(0, 0, 0, 0, 0)
    want = [edge_event_word(instance=INST, timestamp_ns=tick * NS_PER_TICK,
                            s=s, f=0, e=0, clk_mhz=100)
            for tick, s in ((BASE, 1), (BASE + 10, 0), (BASE + 30, 0),
                            (BASE + 60, 0), (0x5_0000_0200, 1),
                            (0x5_0000_0207, 0))]
    got = [w & ~e_bit for w in batch.edge_words]
    assert got == want, (
        "after a reserved-type word the drain must drop to the next SYNC:\n"
        f" got  {[hex(w) for w in got]}\n want {[hex(w) for w in want]}")


def test_decode_frame_names_the_rle_word():
    hdr = zs_msg.make_header(datalogger_id=INST, domain=zs_msg.DOMAIN_EDGE,
                             event_id=EVT_EDGE_RLE, severity=zs_msg.SEV_INFO,
                             timestamp=0, payload_len=4)
    frame = decoders.decode_frame(hdr, rle(LONG, 1000).to_bytes(4, "little"),
                                  100_000_000)
    assert frame.kind == "edge_rle"
    assert frame.fields["word_type"] == "LONG" and frame.fields["payload"] == 1000
