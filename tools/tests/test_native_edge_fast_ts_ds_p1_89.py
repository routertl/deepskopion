# rr unknown-llm (AI-generated)
"""DS-P1.89: the per-frame timestamp base in edge_drain.c is exact.

walk() computes each frame's anchor in ns, whole us and ns remainder once,
and per edge adds d * ns_per_tick with a 32-bit divide by 1000. Two inputs
keep the per-edge 64-bit path: an anchor within 0xFFFF ticks of the 48-bit
wrap, and ns_per_tick > 2^16. The bench clocks (8..30 ns per tick) and the
cross-check dumps reach neither, so this test does: every word and ts_ns
from the host-built library must equal the Python reference
(edge_capture.edge_event_word over ((anchor + d) & 48 bits) * ns_per_tick).
"""
from __future__ import annotations

import ctypes
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sw.stream import edge_capture, zs_msg  # noqa: E402
from sw.stream.native import edge_frames  # noqa: E402

MASK48 = (1 << 48) - 1
MASK64 = (1 << 64) - 1  # ts_ns is uint64 in edge_drain.h; huge anchor x ns_per_tick wraps
CLK_MHZ = 100


class _Edge(ctypes.Structure):
    _fields_ = [("word", ctypes.c_uint32), ("ts_ns", ctypes.c_uint64)]


@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        pytest.fail("no C compiler (cc/gcc) on PATH")
    out = tmp_path_factory.mktemp("lib") / "libds_edge_drain.so"
    src = ROOT / "sw/stream/native/edge_drain.c"
    subprocess.run([cc, "-O2", "-std=c11", "-fPIC", "-shared", "-o", str(out), str(src), "-lm"],
                   check=True, capture_output=True, text=True)
    so = ctypes.CDLL(str(out))
    so.ds_edge_drain.restype = ctypes.c_int
    return so


def _frame(anchor: int, deltas: list[int], rng: random.Random):
    events, want = [], []
    for d in deltas:
        flags = rng.choice((0, 1, 3, 4, 5))
        s, f, e = bool(flags & 4), bool(flags & 2), bool(flags & 1)
        events.append(zs_msg.EdgeEvent(ts_64b=anchor + d, channel=1, s=s, f=f, e=e))
        want.append((d, int(s), int(f), int(e)))
    frame = zs_msg.pack_edge_batch(datalogger_id=1, channel=1, events=events, anchor_ts=anchor)
    return frame, want


def _drain(lib, data: bytes, ns_per_tick: int, cap: int):
    cfg = edge_frames._Cfg(ns_per_tick=ns_per_tick, clk_mhz=CLK_MHZ, max_frames=1 << 20)
    res = edge_frames._Result()
    out = (_Edge * cap)()
    buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
    lib.ds_edge_drain(buf, ctypes.c_size_t(len(data)), ctypes.byref(cfg), out,
                      ctypes.c_size_t(cap), ctypes.byref(res))
    return [(out[i].word, out[i].ts_ns) for i in range(res.edges)]


ANCHORS = [
    0,
    123_456_789,
    MASK48 - 0xFFFF,          # last anchor on the per-frame path
    MASK48 - 0xFFFE,          # first anchor on the 64-bit path
    MASK48 - 5,               # the frame wraps the 48-bit counter
    (1 << 48) + 77,           # bits above 48 in the anchor are dropped
]
NS_PER_TICK = [8, 10, 30, 65_536, 65_537, 1_000_000]


@pytest.mark.parametrize("ns_per_tick", NS_PER_TICK)
@pytest.mark.parametrize("anchor", ANCHORS)
def test_words_and_times_equal_the_python_reference(lib, anchor, ns_per_tick):
    rng = random.Random(anchor ^ ns_per_tick)
    deltas = [0, 1, 999, 1000, 0x7FFF, 0xFFFE, 0xFFFF] + sorted(rng.randrange(0, 0x10000)
                                                              for _ in range(25))
    frame, want = _frame(anchor, deltas, rng)
    got = _drain(lib, frame, ns_per_tick, cap=len(deltas))
    assert len(got) == len(want)
    for (word, ts_ns), (d, s, f, e) in zip(got, want):
        ref_ns = (((anchor + d) & MASK48) * ns_per_tick) & MASK64
        ref_word = edge_capture.edge_event_word(1, ref_ns, s, f, e, clk_mhz=CLK_MHZ)
        assert (ts_ns, word) == (ref_ns, ref_word), (hex(anchor), ns_per_tick, d)


def test_random_anchors_at_the_bench_clocks(lib):
    rng = random.Random(89)
    for _ in range(200):
        anchor = rng.randrange(0, 1 << 48)
        ns_per_tick = rng.choice((8, 10, 12, 30))
        deltas = sorted(rng.randrange(0, 0x10000) for _ in range(16))
        frame, want = _frame(anchor, deltas, rng)
        got = _drain(lib, frame, ns_per_tick, cap=len(deltas))
        for (word, ts_ns), (d, s, f, e) in zip(got, want):
            ref_ns = (((anchor + d) & MASK48) * ns_per_tick) & MASK64
            assert (ts_ns, word) == (ref_ns, edge_capture.edge_event_word(
                1, ref_ns, s, f, e, clk_mhz=CLK_MHZ)), (hex(anchor), ns_per_tick, d)
