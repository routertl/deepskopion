"""DS-P1.89: the dumps for cost figures model the RTL edge pipeline.

ring_dump.edge_dump and interleaved_dump give each channel its own tick, so the
channels drift apart (hundreds of ms), which no hardware batch can do. The RTL
(deepskopion_core.vhd u_edge_agg, edge_batcher.vhd) runs one batcher per channel
on one tick and flushes a batch before an event more than 0xFFFF ticks after
its anchor, at 32 entries, or 10 000 ticks after its last event.
rtl_batches / rtl_edge_dump / rtl_interleaved_dump follow those rules.

The Stream-RLE words of rtl_interleaved_dump carry all elapsed time, as
edge_datalogger.vhd's do (PACK deltas 1..255, LONG 256..2^24-1, IDLE for a
full 2^24-1 without an edge; edge time = SYNC + sum of the deltas), so each
decoded RLE edge lies just before the tick of the frame that carries it.

Values are hard-coded (ROUTERTL-002): the RTL limits and the recorded frame
counts (EDGE_EVENTS frames on the wire, cutter of 067ecfc4).
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path

import msgpack
import pytest

from sw.stream import edge_capture, zs_msg
from sw.stream import protocol as proto
from sw.stream.native import edge_frames, ring_dump

ROOT = Path(__file__).resolve().parents[3]
NATIVE = ROOT / "sw" / "stream" / "native"
sys.path.insert(0, str(ROOT / "contracts" / "wave_stream_v1"))
import wave_stream_contract as contract  # noqa: E402

N_MAX = 32            # deepskopion_core.vhd: G_N_MAX => C_PAYLOAD_WORDS_MAX = 32
SPAN_MAX = 0xFFFF     # edge_batcher.vhd: delta-overflow flush
T_FLUSH = 10_000      # deepskopion_core.vhd: G_T_FLUSH_CYCLES => 10_000
CHANNELS = {1, 2, 3, 6}
CLK_HZ = 100_000_000
#: Two consecutively emitted batches: a batch is emitted at most T_FLUSH ticks
#: after its last event, which is at most SPAN_MAX ticks after its anchor.
MAX_ANCHOR_STEP = SPAN_MAX + T_FLUSH

#: The three short deltas of a PACK word: an RLE edge is at most this many
#: ticks before the header tick of the frame that carries it.
RLE_LAG_MAX = 3 * 255

#: Recorded with the cutter of 067ecfc4 (EDGE_EVENTS frames on the wire).
#: RTL_EDGE_FRAMES was 11 while a tick took at most 4096 frames: the
#: 692 340-byte rtl_edge_dump (5945 frames) drained in two ticks, and the tick
#: boundary after frame 4096 (byte 477 268) cut the seventh wire frame into
#: 11 613 + 14 584 words. Since DS-P1.100 a tick is bounded by the 1 MiB span,
#: the dump drains in one tick, nothing is cut there, and the seventh to tenth
#: wire frames carry 14 459 / 14 618 / 14 607 / 12 452 words: 10 frames, the
#: same 143 360 words.
RTL_EDGE_FRAMES = 10
#: RTL_INTERLEAVED_FRAMES was 21 with the 1 MiB span (one tick boundary cut the
#: interleaved dump); with the 2 MiB span (DS-P1.100) it drains in one tick: 20.
RTL_INTERLEAVED_FRAMES = 20


@pytest.fixture(scope="module")
def batches():
    return ring_dump.rtl_batches(285_000, 3)


def test_every_batch_obeys_the_rtl_flush_limits(batches):
    for at, ch, anchor, events in batches:
        ts = [e.ts_64b for e in events]
        assert ch in CHANNELS and all(e.channel == ch for e in events)
        assert 1 <= len(events) <= N_MAX
        assert ts[0] == anchor and ts == sorted(ts)
        assert ts[-1] - anchor <= SPAN_MAX, (anchor, ts[-1])
        assert all(b - a < T_FLUSH for a, b in zip(ts, ts[1:])), "an idle gap inside a batch"
        assert ts[-1] <= at <= ts[-1] + T_FLUSH


def test_all_three_flush_rules_occur(batches):
    capacity = sum(1 for *_, ev in batches if len(ev) == N_MAX)
    timer = sum(1 for at, _, _, ev in batches if len(ev) < N_MAX and at == ev[-1].ts_64b + T_FLUSH)
    overflow = sum(1 for at, _, _, ev in batches if len(ev) < N_MAX and at != ev[-1].ts_64b + T_FLUSH)
    assert capacity and timer and overflow, (capacity, timer, overflow)


def test_consecutive_batches_are_close_in_time(batches):
    """No drift: the anchors of batches emitted one after the other are within
    one batch span plus one timer period."""
    anchors = [anchor for _, _, anchor, _ in batches]
    worst = max(abs(b - a) for a, b in zip(anchors, anchors[1:]))
    assert worst <= MAX_ANCHOR_STEP, worst


def test_stream_rle_edge_times_track_the_tick():
    """Each RLE-decoded edge (instance 7) is at most RLE_LAG_MAX ticks before
    its frame's header tick, and never after it."""
    dump = ring_dump.rtl_interleaved_dump()
    dec = edge_capture.EdgeRleDecoder()
    cursor, edges, worst = 0, 0, 0
    while cursor < len(dump):
        hdr = zs_msg.parse_header(dump, cursor)
        if hdr.domain == zs_msg.DOMAIN_EDGE and hdr.event_id == ring_dump.EVT_EDGE_RLE:
            word = int.from_bytes(dump[cursor + zs_msg.HEADER_SIZE:cursor + zs_msg.HEADER_SIZE + 4],
                                  "little")
            frame_tick = hdr.timestamp & ((1 << 48) - 1)
            for tick, _ in dec.feed(word, frame_tick=frame_tick):
                lag = frame_tick - tick
                assert 0 <= lag <= RLE_LAG_MAX, (frame_tick, tick)
                worst = max(worst, lag)
                edges += 1
        cursor += hdr.total_size
    assert edges > 1000, edges


@pytest.fixture(scope="module")
def lib(tmp_path_factory) -> edge_frames.EdgeDrainLib:
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        pytest.fail("no C compiler (cc/gcc) on PATH; the native edge path cannot be built")
    so = tmp_path_factory.mktemp("native") / edge_frames.LIB_NAME
    subprocess.run([cc, "-O2", "-Wall", "-Wextra", "-Werror", "-std=c11", "-fPIC", "-shared",
                    "-o", str(so), str(NATIVE / "edge_drain.c"), "-lm"],
                   check=True, capture_output=True, text=True)
    return edge_frames.load(so, required=True)


def _edge_frame_words(stream: bytes) -> list:
    return [len(p["words"]) for tag, p in msgpack.Unpacker(io.BytesIO(stream), raw=False,
                                                           strict_map_key=False)
            if tag == proto.TAG_EDGE_EVENTS]


@pytest.mark.parametrize("name,dump_fn,frames,words", [
    ("rtl_edge", ring_dump.rtl_edge_dump, RTL_EDGE_FRAMES, 143_360),
    ("rtl_interleaved", ring_dump.rtl_interleaved_dump, RTL_INTERLEAVED_FRAMES, None),
])
def test_native_and_python_wire_are_equal_and_the_frame_count_is_recorded(lib, name, dump_fn,
                                                                         frames, words):
    dump = dump_fn()
    python = ring_dump.daemon_wire(dump, CLK_HZ, None)
    native = ring_dump.daemon_wire(dump, CLK_HZ, lib)
    assert native == python, f"{name}: native wire differs from the Python one"
    verdict = contract.check_stream(native, "p2c")
    assert verdict.verdict == "ACCEPT", verdict.failures
    counts = _edge_frame_words(native)
    assert len(counts) == frames, f"{name}: {len(counts)} EDGE_EVENTS frames, recorded {frames}"
    if words is not None:
        assert sum(counts) == words
