# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.117: the native edge walk consumes the PL zero pad itself, so a padded run is one C call per tick.

At 1e14348e (DS-P2.281) the C walk stopped at every zero word right after a frame and returned it to the
Python tick loop, whose _pad_run judged it. On DE25 the PL writes each 148 B EDGE_BATCH frame plus 12 B of
zero pad to the 32 B DDR word, so that became one C call per frame: 13 092 C calls per tick on the b2 board
capture, against 1 at 142bc455 (DS-P1.117 b2 result), and T1 fell from >=15e6 to ~0.86e6 B/s.

The fix gives the C walk the word width and the ring offset (ds_drain_cfg, ABI 2), and C applies
_pad_run's rule: a zero run that starts right after a frame, NOT on a word boundary, up to the next
boundary, is pad. Every other zero word after a frame still goes back to Python (STOP_OTHER_FRAME), which
resyncs it. What DS-P2.281 wanted is kept: the C and Python walks give the same edges, the same resync
total and the same pad total.

Inputs:
- fixtures/ds_p1_117/de25_ring_head64k.bin: the first 65 536 B of the b2 DE25 ring capture (README there).
- Hard-coded synthetic spans for the boundary cases and a tick in which both a pad and a stray zero
  word occur.
"""
from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
from pathlib import Path

import msgpack
import pytest

from sw.stream import data_source, zs_msg
from sw.stream import protocol as proto
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.native import edge_frames, ring_dump

NATIVE = Path(__file__).resolve().parents[1] / "native"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ds_p1_117" / "de25_ring_head64k.bin"
FIXTURE_SHA256 = "613a0f20c8242d3964419165a936cc7f37f3b5e81298f10c59b0849549d811d0"
WORD = 32           # DE25 G_DDR_DATA_WIDTH 256 / 8
CLK_HZ = 100_000_000


def _frame(anchor: int, entries: int) -> bytes:
    """One EDGE_BATCH frame of 20 + 4 * entries bytes."""
    events = [zs_msg.EdgeEvent(ts_64b=anchor + 7 * (i + 1), channel=1, s=False, f=False, e=bool(i & 1))
              for i in range(entries)]
    return zs_msg.pack_edge_batch(datalogger_id=1, channel=1, events=events, anchor_ts=anchor)


# A frame that ends ON a word boundary (3 entries = 32 B), then a whole zero word that starts on the
# boundary: not pad by _pad_run (zeros that start on a boundary), so both walks resync it, 32 B.
ON_BOUNDARY = _frame(1_000_000, 3) + bytes(WORD) + _frame(1_000_100, 4)
# A 36 B frame, its 28 B pad to the boundary at 64, then 32 more zero bytes (DDR-T2.24's extra zero
# word): the pad stops at the boundary, the zeros past it are resynced, 32 B.
PAST_BOUNDARY = _frame(1_000_000, 4) + bytes(28) + bytes(WORD) + _frame(1_000_100, 4)
# One tick with both paths: frame 36 B + pad 28 B (to 64), frame 32 B (ends on the boundary at 96), a
# stray 4 B zero word on the boundary (not pad: resync 4), frame 36 B (100..136), pad 24 B (to 160),
# frame 36 B (160..196).
MIXED = (_frame(1_000_000, 4) + bytes(28) + _frame(1_000_100, 3) + bytes(4)
         + _frame(1_000_200, 4) + bytes(24) + _frame(1_000_300, 4))


def test_the_hard_coded_inputs_are_the_shapes_under_test():
    assert len(_frame(0, 3)) == 32 and len(_frame(0, 4)) == 36
    assert len(ON_BOUNDARY) == 32 + 32 + 36
    assert len(PAST_BOUNDARY) == 36 + 28 + 32 + 36
    assert len(MIXED) == 196
    data = FIXTURE.read_bytes()
    assert len(data) == 65_536
    assert hashlib.sha256(data).hexdigest() == FIXTURE_SHA256


def _build(tmp: Path, source: str) -> edge_frames.EdgeDrainLib:
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        pytest.fail("no C compiler (cc/gcc) on PATH; the native edge path cannot be built")
    src = tmp / "edge_drain.c"
    src.write_text(source, encoding="utf-8")
    shutil.copy(NATIVE / "edge_drain.h", tmp / "edge_drain.h")
    so = tmp / edge_frames.LIB_NAME
    subprocess.run([cc, "-O2", "-Wall", "-Wextra", "-Werror", "-std=c11", "-fPIC", "-shared",
                    "-o", str(so), str(src), "-lm"], check=True, capture_output=True, text=True)
    return edge_frames.load(so, required=True)


@pytest.fixture(scope="module")
def lib(tmp_path_factory) -> edge_frames.EdgeDrainLib:
    return _build(tmp_path_factory.mktemp("native"),
                  (NATIVE / "edge_drain.c").read_text(encoding="utf-8"))


def _replay(dump: bytes, native) -> dict:
    """Drain *dump* through DdrRingDataSource (word width declared) to the end: the C calls per tick,
    the resync and pad totals, the final read offset and every C walk result."""
    ddr = FakeDdrMmap(len(dump) + 4096)
    ddr.write_bytes(0, dump)
    regbank = ring_dump._CommitRegbank()
    src = data_source.DdrRingDataSource(
        regbank=regbank, ddr=ddr, sample_clk_hz=CLK_HZ, poll_period_s=0.0, duration_s=0.0,
        write_back_read_ptr=True, clock=lambda: 0.0, native_edge_drain=native,
        ring_word_bytes=WORD)
    src._TICK_BUDGET_S = 1e9   # every frame is decoded; time is not what is compared
    src.next_batch()           # seeds the read pointer at commit 0
    regbank.commit = len(dump)
    calls, stops = [0], []
    orig_drain = edge_frames.NativeEdgeFrames.drain

    def drain(self, *a, **k):
        calls[0] += 1
        r = orig_drain(self, *a, **k)
        stops.append(r)
        return r

    edge_frames.NativeEdgeFrames.drain = drain
    per_tick = []
    try:
        for _ in range(64):
            before = src._read_byte_ofs
            calls[0] = 0
            src.next_batch()
            if src._read_byte_ofs == before:
                break
            per_tick.append(calls[0])
    finally:
        edge_frames.NativeEdgeFrames.drain = orig_drain
    return {"calls_per_tick": per_tick, "resync": src._resync_total,
            "pad": src._pad_total, "read": src._read_byte_ofs, "stops": stops}


def _wire_words(dump: bytes, native, monkeypatch) -> list:
    orig = data_source.DdrRingDataSource

    class _Declared(orig):
        def __init__(self, *a, **k):
            k.setdefault("ring_word_bytes", WORD)
            super().__init__(*a, **k)

    monkeypatch.setattr(data_source, "DdrRingDataSource", _Declared)
    try:
        wire = ring_dump.daemon_wire(dump, CLK_HZ, native)
    finally:
        monkeypatch.setattr(data_source, "DdrRingDataSource", orig)
    words = []
    for tag, p in msgpack.Unpacker(io.BytesIO(wire), raw=False, strict_map_key=False):
        if tag == proto.TAG_EDGE_EVENTS:
            words += p.get("words", [])
    return words


def _parity(dump: bytes, native, monkeypatch) -> list:
    """The ways the native walk differs from the Python walk on *dump* (empty = parity)."""
    py, c = _replay(dump, None), _replay(dump, native)
    diffs = [f"{k}: python {py[k]} native {c[k]}" for k in ("resync", "pad", "read") if py[k] != c[k]]
    if _wire_words(dump, None, monkeypatch) != _wire_words(dump, native, monkeypatch):
        diffs.append("wire EDGE_EVENTS words differ")
    return diffs


def test_capture_slice_needs_no_c_call_per_pad(lib):
    """The red-on-1e14348e test. The slice opens with four edge-domain frames that are not EDGE_BATCH
    (event 0x8 at byte 0, event 0x2 at 64, 128 and 156), which the Python walk takes by design: one C
    stop at each, then one call walks the remaining 408 EDGE_BATCH frames and their pads to the partial
    frame at the slice end. 5 C calls in the one tick; at 1e14348e every pad also returned to Python."""
    r = _replay(FIXTURE.read_bytes(), lib)
    assert r["calls_per_tick"] == [5], f"C calls per tick {r['calls_per_tick']}"
    tick = r["stops"][:5]   # a later call, on the held partial frame, walks nothing
    assert [w.stop for w in tick] == [edge_frames.STOP_OTHER_FRAME] * 4 + [edge_frames.STOP_PARTIAL], tick
    assert tick[4].frames == 408, tick[4]
    assert (r["resync"], r["pad"], r["read"]) == (0, 4932, 65472), r


def test_capture_slice_parity_with_the_python_walk(lib, monkeypatch):
    assert _parity(FIXTURE.read_bytes(), lib, monkeypatch) == []


def test_c_consumes_the_pad_after_a_frame(lib):
    """Direct, one call over PAST_BOUNDARY: frame, its 28 B pad (consumed in C), the 32 zero bytes past the
    boundary (no longer right after a frame, so resynced 4 B at a time in C, as the Python walk does), the
    last frame, end of span."""
    w = lib.new_frames().drain(PAST_BOUNDARY, 0, 4096, 10, 100, ring_word_bytes=WORD,
                               ring_bytes=1 << 20, ring_ofs=0)
    assert (w.frames, w.consumed, w.pad_bytes, w.resync_bytes) == (2, 132, 28, 32), w
    assert w.stop == edge_frames.STOP_END and w.after_frame is True, w


def test_c_stops_at_a_zero_word_on_the_boundary(lib):
    """Direct, one call over ON_BOUNDARY: the zero word after the 32 B frame starts on the boundary, so it
    is not pad and goes back to Python (STOP_OTHER_FRAME), as at DS-P2.281."""
    w = lib.new_frames().drain(ON_BOUNDARY, 0, 4096, 10, 100, ring_word_bytes=WORD,
                               ring_bytes=1 << 20, ring_ofs=0)
    assert (w.frames, w.consumed, w.pad_bytes, w.resync_bytes) == (1, 32, 0, 0), w
    assert w.stop == edge_frames.STOP_OTHER_FRAME and w.after_frame is True, w


def test_zero_word_on_a_boundary_is_resynced_on_both_walks(lib, monkeypatch):
    py = _replay(ON_BOUNDARY, None)
    assert (py["resync"], py["pad"], py["read"]) == (32, 0, len(ON_BOUNDARY)), py
    assert _parity(ON_BOUNDARY, lib, monkeypatch) == []


def test_zeros_past_the_boundary_are_resynced_on_both_walks(lib, monkeypatch):
    py = _replay(PAST_BOUNDARY, None)
    assert (py["resync"], py["pad"], py["read"]) == (32, 28, len(PAST_BOUNDARY)), py
    assert _parity(PAST_BOUNDARY, lib, monkeypatch) == []


def test_mixed_tick_pad_in_c_and_stray_zero_word_in_python(lib, monkeypatch):
    py = _replay(MIXED, None)
    assert (py["resync"], py["pad"], py["read"]) == (4, 52, len(MIXED)), py
    c = _replay(MIXED, lib)
    assert c["calls_per_tick"] == [2], c["calls_per_tick"]
    first, second = c["stops"]
    # Call 1: frame, pad 28 (consumed in C), frame, then the stray zero word on the boundary -> Python.
    assert (first.frames, first.pad_bytes, first.stop) == (2, 28, edge_frames.STOP_OTHER_FRAME), first
    # Call 2, after Python resynced the 4 B: frame, pad 24 (consumed in C), frame, end.
    assert (second.frames, second.pad_bytes, second.stop) == (2, 24, edge_frames.STOP_END), second
    assert _parity(MIXED, lib, monkeypatch) == []


# Mutants of the boundary arithmetic in pad_run (edge_drain.c). Each must break parity on the input
# that exercises it, or the parity assert above could not see that defect.
_MUTANTS = {
    "pad_starting_on_boundary_is_consumed": (
        "    if (rem == 0)\n        return 0;\n    const size_t n = w - rem;\n",
        "    const size_t n = w - rem;\n",
        ON_BOUNDARY),
    "pad_running_past_boundary_is_consumed": (
        "    const size_t n = w - rem;\n",
        "    const size_t n = w - rem + w;\n",
        PAST_BOUNDARY),
}


@pytest.mark.parametrize("name", sorted(_MUTANTS))
def test_boundary_mutant_breaks_parity(name, tmp_path, monkeypatch):
    find, replace, dump = _MUTANTS[name]
    source = (NATIVE / "edge_drain.c").read_text(encoding="utf-8")
    assert source.count(find) == 1, f"mutant {name}: its target text is not in edge_drain.c once"
    mutant = _build(tmp_path, source.replace(find, replace))
    diffs = _parity(dump, mutant, monkeypatch)
    assert any(d.startswith(("resync:", "pad:")) for d in diffs), \
        f"mutant {name} kept parity on its input; the parity assert cannot see it: {diffs}"


def test_library_of_another_abi_is_refused(tmp_path):
    source = (NATIVE / "edge_drain.c").read_text(encoding="utf-8")
    find = "int ds_edge_drain_abi(void) { return DS_EDGE_DRAIN_ABI; }"
    assert source.count(find) == 1
    with pytest.raises(OSError, match="ABI 1"):
        _build(tmp_path, source.replace(find, "int ds_edge_drain_abi(void) { return 1; }"))
