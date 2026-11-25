# rr unknown-llm (AI-generated)
"""DS-P1.89: ring_dump --stages also splits the guard stage into the session's
first frame (checked word by word) and frames 1..N (checked by header), so a
short benchmark dump is not read as steady-state cost.

On the Zybo (2026-11-10) the guard step of edge.bin measured 969.9 ns/edge;
a host profile showed nearly all of it is the first frame's full word check,
because the benchmark starts a fresh session per dump and the dump has 24
frames. edge_dump(4096) is that dump: 24 frames, 21 542 words in frame 0.
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import msgpack

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sw.stream import protocol as proto  # noqa: E402
from sw.stream.native import ring_dump  # noqa: E402

DUMP = ring_dump.edge_dump(4096)
FRAMES = 24
FRAME0_WORDS = 21542
TOTAL_WORDS = 143360


def _wire_frames(dump: bytes) -> list:
    wire = ring_dump.daemon_wire(dump, 100_000_000, None)
    return [len(pl["words"]) for tag, pl in msgpack.Unpacker(io.BytesIO(wire), raw=False)
            if tag == proto.TAG_EDGE_EVENTS]


def test_the_dump_is_the_board_shape():
    words = _wire_frames(DUMP)
    assert (len(words), words[0], sum(words)) == (FRAMES, FRAME0_WORDS, TOTAL_WORDS)


def test_the_split_parts_add_up_to_the_stream():
    sp = ring_dump.guard_split(DUMP, 100_000_000, native=None, repeat=1)
    assert sp["frames"] == FRAMES
    assert sp["start_words"] == FRAME0_WORDS
    assert sp["steady_words"] == TOTAL_WORDS - FRAME0_WORDS
    assert sp["start_s"] > 0 and sp["steady_s"] > 0
    # Both parts are timed inside the same run, walk included in run_s only.
    assert sp["start_s"] + sp["steady_s"] < sp["run_s"]


def test_the_cli_prints_the_split_line(tmp_path):
    dump = ring_dump.write(tmp_path / "edge.bin", DUMP)
    proc = subprocess.run(
        [sys.executable, "-m", "sw.stream.native.ring_dump", str(dump), "100000000",
         "--repeat", "1", "--stages"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("guard split"))
    assert f"session_start frame 0 words={FRAME0_WORDS} " in line, line
    assert f"steady_state frames={FRAMES - 1} words={TOTAL_WORDS - FRAME0_WORDS} " in line, line
