# rr unknown-llm (AI-generated)
"""DS-P1.74 (decision DS-D.49): the native EDGE_BATCH drain is byte-exact with Python.

sw/stream/native/edge_drain.c walks a DDR ring span the way
DdrRingDataSource.next_batch does and produces, per edge, the (word, ts_ns)
pair Python hands Batch.add_edge. The reference is the production drain
itself, run over the same dump (ring_dump.oracle_records), not a re-derived
formula. Dumps: 4096 EDGE_BATCH frames at the 160 B/frame DS-T1.23 measured
(one full drain tick, crossing the 48-bit timestamp wrap), and a mixed dump
with a bad-magic word, a malformed batch and a trailing non-edge frame.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sw.stream.native import ring_dump

NATIVE = Path(__file__).resolve().parents[1] / "native"


@pytest.fixture(scope="module")
def bench(tmp_path_factory) -> Path:
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        pytest.fail("no C compiler (cc/gcc) on PATH; the native drain cannot be built")
    exe = tmp_path_factory.mktemp("native") / "edge_drain_bench"
    subprocess.run([cc, "-O2", "-Wall", "-Wextra", "-Werror", "-std=c11",
                    "-D_POSIX_C_SOURCE=199309L", "-o", str(exe),
                    str(NATIVE / "edge_drain.c"), str(NATIVE / "edge_drain_bench.c"), "-lm"],
                   check=True, capture_output=True, text=True)
    return exe


def _native(bench: Path, dump: bytes, clk_hz: int, tmp_path: Path) -> tuple[bytes, str]:
    src = tmp_path / "dump.bin"
    src.write_bytes(dump)
    rec = tmp_path / "native.rec"
    proc = subprocess.run([str(bench), str(src), str(clk_hz), "--out", str(rec)],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return rec.read_bytes(), proc.stdout


@pytest.mark.parametrize("clk_hz", [100_000_000, 125_000_000, 80_000_000, 33_000_000])
def test_one_drain_tick_of_edge_frames_is_byte_exact(bench, tmp_path, clk_hz):
    dump = ring_dump.edge_dump(4096)
    assert len(dump) == 4096 * 160
    native, out = _native(bench, dump, clk_hz, tmp_path)
    oracle = ring_dump.oracle_records(dump, sample_clk_hz=clk_hz)
    assert len(oracle) == 4096 * 35 * ring_dump.RECORD.size
    assert native == oracle, f"native drain differs from the Python drain at {clk_hz} Hz"
    assert "resync_bytes=0" in out and "other_frame_at=-1" in out, out


def test_resync_malformed_and_foreign_frames_match_python(bench, tmp_path):
    dump = ring_dump.mixed_dump()
    native, out = _native(bench, dump, 100_000_000, tmp_path)
    assert native == ring_dump.oracle_records(dump)
    # 4 bytes of bad magic plus one 160-byte malformed batch; the trailing
    # UART frame stops the native walk and goes back to Python.
    assert "resync_bytes=164" in out, out
    assert f"other_frame_at={len(dump) - 24}" in out, out
