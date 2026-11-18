"""DS-P2.281: the native edge walk stops at PL zero pad, so the pad rule applies on both walks.

DS-P2.280 put the PL zero-pad rule (pad to the DDR-word boundary is pad, not
resync) on the Python walk. The native C walk (libds_edge_drain) slid over a
zero word after an EDGE_BATCH run and counted it as resync, so on the board,
which walks edge frames in C, a daemon with ds_ring_word_bytes declared still
reported every pad after edge traffic as zs_resync (measured in the DS-P2.311
desk replay, 2026-11-15: 100 B of 100 B pad on the C walk, 0 on the Python walk).

Hard-coded input: two EDGE_BATCH frames of four entries (36 B each, 72 B), 24 B
of zero pad to the 32-byte DE25 word boundary (96), then a third EDGE_BATCH frame.
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
from pathlib import Path

import msgpack
import pytest

from sw.stream import data_source, zs_msg
from sw.stream import protocol as proto
from sw.stream.native import edge_frames, ring_dump

NATIVE = Path(__file__).resolve().parents[1] / "native"
WORD = 32
ANCHORS = (1_000_000, 1_000_400, 1_000_900)


def _frame(anchor: int) -> bytes:
    events = [zs_msg.EdgeEvent(ts_64b=anchor + d, channel=1, s=False, f=False, e=bool(i & 1))
              for i, d in enumerate((10, 30, 62, 90))]
    return zs_msg.pack_edge_batch(datalogger_id=1, channel=1, events=events, anchor_ts=anchor)


RUN = _frame(ANCHORS[0]) + _frame(ANCHORS[1])
PAD = bytes(WORD - len(RUN) % WORD)
DUMP = RUN + PAD + _frame(ANCHORS[2])


def test_the_hard_coded_input_is_the_shape_under_test():
    assert len(RUN) == 72 and len(PAD) == 24 and len(DUMP) == 132


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


def test_native_walk_stops_at_the_pad_with_no_resync(lib):
    walked = lib.new_frames().drain(DUMP, 0, 4096, 10, 100)
    assert walked.frames == 2, walked
    assert walked.consumed == len(RUN), walked
    assert walked.resync_bytes == 0, walked
    assert walked.stop == edge_frames.STOP_OTHER_FRAME, walked


def _wire(dump: bytes, native, monkeypatch):
    orig = data_source.DdrRingDataSource

    class _Declared(orig):
        def __init__(self, *a, **k):
            k.setdefault("ring_word_bytes", WORD)
            super().__init__(*a, **k)

    monkeypatch.setattr(data_source, "DdrRingDataSource", _Declared)
    wire = ring_dump.daemon_wire(dump, 100_000_000, native)
    monkeypatch.setattr(data_source, "DdrRingDataSource", orig)
    words, resync = [], 0
    for tag, p in msgpack.Unpacker(io.BytesIO(wire), raw=False, strict_map_key=False):
        if tag == proto.TAG_EDGE_EVENTS:
            words += p.get("words", [])
        elif tag == proto.TAG_ANNOTATIONS:
            for a in p["annotations"]:
                if a["kind"] == "zs_resync":
                    resync = max(resync, int(re.search(r"total=(\d+)B", a["text"]).group(1)))
    return words, resync


def test_python_and_native_walks_give_equal_output_with_no_resync(lib, monkeypatch):
    py_words, py_resync = _wire(DUMP, None, monkeypatch)
    c_words, c_resync = _wire(DUMP, lib, monkeypatch)
    assert len(py_words) == 12 and py_resync == 0
    assert c_resync == 0, f"the native walk reported {c_resync} B of the {len(PAD)} B pad as zs_resync"
    assert c_words == py_words
