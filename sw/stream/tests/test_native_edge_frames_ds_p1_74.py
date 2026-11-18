"""DS-P1.74 (decision DS-D.49): the daemon's wire bytes are the same on the native edge path.

With DdrRingDataSource(native_edge_drain=...), runs of EDGE_BATCH frames are
walked, cut into EDGE_EVENTS frames and packed in C, and server._drain sends
the packed bytes. The reference is the same daemon with the Python walk: both
run the real server._drain over a socketpair, and the whole p2c byte stream is
compared, HELO to EOF. Dumps: one drain tick of 160-byte EDGE_BATCH frames
across the 48-bit wrap; the mixed dump (bad magic, a malformed batch, a UART
frame); and an interleaved dump of three ticks that starts with a UART frame
and breaks the edge runs with Stream-RLE words (edges Python adds to the same
batch) and UART annotations.

The guard tests cover the native-frame check: one frame in N decoded and
checked word by word, the others on their header, both counted.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path

import msgpack
import pytest

from sw.stream import data_source
from sw.stream import protocol as proto
from sw.stream.native import edge_frames, ring_dump
from sw.stream.wave_stream_guard import LiveConformanceGuard

ROOT = Path(__file__).resolve().parents[3]
NATIVE = ROOT / "sw" / "stream" / "native"
sys.path.insert(0, str(ROOT / "contracts" / "wave_stream_v1"))
import wave_stream_contract as contract  # noqa: E402

CLOCKS = [100_000_000, 125_000_000, 80_000_000, 33_000_000]


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


def _wire(dump: bytes, clk_hz: int, native) -> bytes:
    return ring_dump.daemon_wire(dump, clk_hz, native)


def _frames(stream: bytes):
    return list(msgpack.Unpacker(io.BytesIO(stream), raw=False, strict_map_key=False))


def _edge_frames(stream: bytes):
    return [p for tag, p in _frames(stream) if tag == proto.TAG_EDGE_EVENTS]


def _assert_same_wire(dump: bytes, clk_hz: int, lib) -> bytes:
    python = _wire(dump, clk_hz, None)
    native = _wire(dump, clk_hz, lib)
    assert native == python, f"native wire stream differs from the Python one at {clk_hz} Hz"
    verdict = contract.check_stream(native, "p2c")
    assert verdict.verdict == "ACCEPT", verdict.failures
    return native


@pytest.mark.parametrize("clk_hz", CLOCKS)
def test_one_drain_tick_of_edge_frames_is_byte_exact_on_the_wire(lib, clk_hz):
    stream = _assert_same_wire(ring_dump.edge_dump(4096), clk_hz, lib)
    frames = _edge_frames(stream)
    assert sum(len(f["words"]) for f in frames) == 4096 * ring_dump.ENTRIES_PER_FRAME
    assert len(frames) > 1, "the dump spans more than one anchor's reach, so it is cut"


@pytest.mark.parametrize("clk_hz", CLOCKS)
def test_mixed_dump_is_byte_exact_on_the_wire(lib, clk_hz):
    stream = _assert_same_wire(ring_dump.mixed_dump(), clk_hz, lib)
    tags = [tag for tag, _ in _frames(stream)]
    assert proto.TAG_ANNOTATIONS in tags, "the UART frame went back to the Python walk"


@pytest.mark.parametrize("clk_hz", CLOCKS)
def test_interleaved_rle_and_uart_frames_are_byte_exact_on_the_wire(lib, clk_hz):
    # Three ticks need more bytes than two spans (2 x 2 MiB, DS-P1.100): 32000
    # frames are about 4.68 MB, three ticks.
    stream = _assert_same_wire(ring_dump.interleaved_dump(n_frames=32000), clk_hz, lib)
    words = [w for f in _edge_frames(stream) for w in f["words"]]
    assert any(w >> 27 == ring_dump.RLE_INSTANCE for w in words), "RLE edges reached the wire"
    tags = [tag for tag, _ in _frames(stream)]
    assert tags.count(proto.TAG_ANNOTATIONS) >= 3, "one per tick: the dump spans three ticks"


def test_native_batch_reads_back_the_python_batch_edges(lib):
    dump = ring_dump.interleaved_dump(2000)
    want = ring_dump.daemon_source(dump, 100_000_000, None).next_batch()
    got = ring_dump.daemon_source(dump, 100_000_000, lib).next_batch()
    assert got.native_edges is not None and not got.edge_words
    assert got.has_edges()
    assert got.all_edge_words() == want.all_edge_words() == want.edge_words
    assert got.wire_edge_frames() == want.wire_edge_frames()


def _native_frame(lib, n_words: int = 40, seq: int = 3):
    frames = lib.new_frames()
    for k in range(n_words):
        frames.add(0x0A00_0000 | k, 1_000_000_000 + k * 1000)
    anchor, n = frames.info(0)
    return frames.pack(0, seq, 100), seq, anchor, n


def _primed_guard(every: int) -> LiveConformanceGuard:
    guard = LiveConformanceGuard(native_full_check_every=every)
    helo = proto.make_helo(sample_clk_hz=100_000_000, channels=[], channel_total_bits=0)
    assert guard.check(proto.TAG_HELO, helo) is None
    guard.check(proto.TAG_HELO_OK, {}, direction="c2p")
    return guard


def _word_offsets(frame: bytes, n: int):
    """Byte offset of each word's msgpack encoding (all 0xce uint32 here)."""
    first = len(frame) - 5 * n
    assert all(frame[first + 5 * k] == 0xCE for k in range(n))
    return [first + 5 * k for k in range(n)]


def test_a_sampled_native_frame_with_a_corrupted_word_is_refused(lib):
    frame, seq, anchor, n = _native_frame(lib)
    guard = _primed_guard(every=1024)
    at = _word_offsets(frame, n)[17]
    # a word packed as a 64-bit value: the bytes still decode, the word count holds
    bad = frame[:at] + b"\xcf" + (0x1_0000_0000 + 5).to_bytes(8, "big") + frame[at + 5:]
    violation = guard.check_native_edge_frame(bad, seq, 100, anchor, n)
    assert violation is not None and "words[17]" in violation, violation
    assert guard.refused == 1 and guard.native_full_checked == 0
    # rolled back: the same seq is still accepted once the frame is right
    assert guard.check_native_edge_frame(frame, seq, 100, anchor, n) is None
    assert guard.native_full_checked == 1


def test_one_native_frame_in_n_is_checked_word_by_word(lib):
    guard = _primed_guard(every=4)
    for seq in range(10):
        frame, _, anchor, n = _native_frame(lib, seq=seq)
        assert guard.check_native_edge_frame(frame, seq, 100, anchor, n) is None
    assert (guard.native_full_checked, guard.native_header_checked) == (3, 7)


@pytest.mark.parametrize("field,declared", [("seq", 4), ("clk_mhz", 125),
                                            ("anchor_ns", 7), ("n_words", 39)])
def test_a_header_checked_frame_that_differs_from_its_declaration_is_refused(
        lib, field, declared):
    frame, seq, anchor, n = _native_frame(lib)
    guard = _primed_guard(every=1024)
    assert guard.check_native_edge_frame(frame, seq, 100, anchor, n) is None  # the full one
    args = {"seq": seq + 1, "clk_mhz": 100, "anchor_ns": anchor, "n_words": n}
    frame = _native_frame(lib, seq=seq + 1)[0]
    args[field] = declared if field != "seq" else seq + 2
    violation = guard.check_native_edge_frame(frame, **args)
    assert violation is not None and "declared" in violation, violation
    assert guard.native_header_checked == 0 and guard.refused == 1


def test_a_header_checked_frame_short_of_its_words_is_refused(lib):
    frame, seq, anchor, n = _native_frame(lib)
    guard = _primed_guard(every=1024)
    assert guard.check_native_edge_frame(frame, seq, 100, anchor, n) is None
    frame = _native_frame(lib, seq=seq + 1)[0]
    # fewer word bytes than words: n words take at least n bytes
    violation = guard.check_native_edge_frame(frame[:-(4 * n + 1)], seq + 1, 100, anchor, n)
    assert violation is not None and "word bytes" in violation, violation


def test_a_header_checked_frame_still_obeys_seq_order(lib):
    guard = _primed_guard(every=1024)
    frame, _, anchor, n = _native_frame(lib, seq=5)
    assert guard.check_native_edge_frame(frame, 5, 100, anchor, n) is None
    violation = guard.check_native_edge_frame(frame, 5, 100, anchor, n)
    assert violation is not None and "seq" in violation, violation


_UINT_EDGES = [0, 1, 127, 128, 255, 256, 65535, 65536, (1 << 32) - 1, 1 << 32, (1 << 64) - 1]


@pytest.mark.parametrize("n_words", [1, 15, 16, 65535, 65536])
def test_packed_frame_equals_pack_frame_at_every_msgpack_size_boundary(lib, n_words):
    words = [_UINT_EDGES[k % 9] for k in range(n_words)]  # every uint32 width
    frames = lib.new_frames()
    for w in words:
        frames.add(w, 5_000)  # one anchor: all in reach
    assert frames.frame_count() == 1
    for seq, clk, anchor in [(0, 1, 0), (127, 127, 127), (128, 128, 128), (255, 255, 255),
                             (256, 256, 65535), (65536, 65536, (1 << 32) - 1),
                             ((1 << 32) - 1, 100, 1 << 32), (1 << 32, 100, (1 << 64) - 1)]:
        frames_a = lib.new_frames()
        for w in words:
            frames_a.add(w, anchor)
        want = proto.pack_frame(proto.TAG_EDGE_EVENTS,
                                proto.make_edge_events(seq, words, clk_mhz=clk, anchor_ns=anchor))
        assert frames_a.pack(0, seq, clk) == want, (seq, clk, anchor, n_words)


def test_the_reach_cut_matches_batch_add_edge_at_its_boundary(lib):
    reach_ns = proto.EDGE_ANCHOR_REACH_US * 1000
    base = 7_000_000_123
    times = [base, base + reach_ns - 1, base + reach_ns - 1000, base + reach_ns,
             base + reach_ns + 999, base - reach_ns + 1000, base - reach_ns,
             base + 3 * reach_ns, base + 4 * reach_ns - 1001, base + 4 * reach_ns - 1000]
    python, native = data_source.Batch(), data_source.Batch(native_edges=lib.new_frames())
    for k, t in enumerate(times):
        python.add_edge(0x0100_0000 + k, t)
        native.add_edge(0x0100_0000 + k, t)
    assert len(python.wire_edge_frames()) > 3
    assert native.wire_edge_frames() == python.wire_edge_frames()


def _python_calls(dump: bytes, native) -> int:
    """Python and C function calls made on this thread while the daemon sends *dump*."""
    calls = 0

    def count(_frame, event, _arg):
        nonlocal calls
        if event in ("call", "c_call"):
            calls += 1

    sys.setprofile(count)
    try:
        _wire(dump, 100_000_000, native)
    finally:
        sys.setprofile(None)
    return calls


def test_the_native_path_makes_no_python_call_per_edge(lib):
    # Same frame count, twice the edges per frame over the same time span, so
    # any difference in calls comes from the edges, not the frames. Each dump
    # leads with one edge a second earlier, out of reach of the rest: the
    # session's first EDGE_EVENTS frame, the one the guard checks word by
    # word, is then one word in both.
    start = (1 << 48) - 5_000_000
    lead = ring_dump.edge_dump(1, channels=1, entries=1, start_tick=start - 100_000_000)
    small = lead + ring_dump.edge_dump(1024, entries=35, max_delta=1800, start_tick=start)
    large = lead + ring_dump.edge_dump(1024, entries=70, max_delta=900, start_tick=start)
    extra_edges = 1024 * 35
    python = (_python_calls(large, None) - _python_calls(small, None)) / extra_edges
    native = (_python_calls(large, lib) - _python_calls(small, lib)) / extra_edges
    # the counter sees per-edge work where there is some: the Python walk
    assert python > 5, python
    assert abs(native) < 0.05, (native, python)  # per-frame calls only; one per edge is 1.0


def test_load_refuses_a_missing_library_only_when_required(tmp_path, monkeypatch):
    monkeypatch.delenv(edge_frames.ENV_LIB, raising=False)
    missing = tmp_path / edge_frames.LIB_NAME
    assert edge_frames.load(missing) is None
    with pytest.raises(FileNotFoundError):
        edge_frames.load(missing, required=True)
    monkeypatch.setenv(edge_frames.ENV_LIB, str(missing))
    with pytest.raises(FileNotFoundError):
        edge_frames.load(required=True)


def test_capture_edges_tees_the_words_of_a_native_batch(lib):
    from sw.stream.__main__ import _CapturingSource

    class Writer:
        def __init__(self):
            self.words = []

        def append(self, words):
            self.words.extend(words)

    dump = ring_dump.edge_dump(64)
    writer = Writer()
    batch = _CapturingSource(ring_dump.daemon_source(dump, 100_000_000, lib), writer).next_batch()
    assert batch.native_edges is not None
    want = ring_dump.daemon_source(dump, 100_000_000, None).next_batch().edge_words
    assert writer.words == want and len(want) == 64 * ring_dump.ENTRIES_PER_FRAME


def test_a_header_check_reads_only_the_header_bytes(lib, monkeypatch):
    import sw.stream.wave_stream_guard as wsg

    fed = []
    real = wsg.msgpack.Unpacker

    class Recording(real):
        def feed(self, data):
            fed.append(len(data))
            return super().feed(data)

    guard = _primed_guard(every=1024)
    frame, seq, anchor, n = _native_frame(lib, n_words=20000)
    assert guard.check_native_edge_frame(frame, seq, 100, anchor, n) is None  # the full one
    frame = _native_frame(lib, n_words=20000, seq=seq + 1)[0]
    monkeypatch.setattr(wsg.msgpack, "Unpacker", Recording)
    assert guard.check_native_edge_frame(frame, seq + 1, 100, anchor, n) is None
    assert guard.native_header_checked == 1
    assert fed == [wsg.NATIVE_HEADER_MAX] and len(frame) > 5 * 20000
