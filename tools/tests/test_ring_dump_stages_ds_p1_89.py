# rr unknown-llm (AI-generated)
"""DS-P1.89: ring_dump --stages splits the daemon edge path into cumulative
stages (walk, pack, guard, full) so a silicon run can name the step that costs."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sw.stream.native import ring_dump  # noqa: E402


def test_stage_times_reports_each_cumulative_stage():
    st = ring_dump.stage_times(ring_dump.edge_dump(16), 100_000_000, native=None, repeat=1)
    assert list(st) == ["walk", "pack", "guard", "full"], st
    assert all(v > 0 for v in st.values()), st


def test_the_cli_prints_the_stage_line(tmp_path):
    dump = ring_dump.write(tmp_path / "edge.bin", ring_dump.edge_dump(16))
    proc = subprocess.run(
        [sys.executable, "-m", "sw.stream.native.ring_dump", str(dump), "100000000",
         "--repeat", "1", "--stages"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("stages ns_per_edge_best:"))
    assert [kv.split("=")[0] for kv in line.split(": ", 1)[1].split()] == [
        "walk", "pack", "guard", "full"], line
