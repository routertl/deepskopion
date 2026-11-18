# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: CERN-OHL-W-2.0
"""DS-P3.188: files a simulation writes are never tracked.

hw/sim/work.lock was tracked, and every nvc run rewrote the pid in it, so a
sim left the checkout dirty and a broad ``git add`` committed an unrelated
change. These checks fail if a sim-written path is tracked again, or if the
lock stops being ignored.

Waveform files are not matched by extension: docs/ keeps deliberate hardware
captures (.vcd), and sim/waves/*.ltmeta.json are saved RouteWave layouts.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Paths rr sim / nvc / cocotb write during a run.
SIM_OUTPUT = re.compile(
    r"(^|/)work\.lock$"
    r"|^(hw/)?sim/work/"
    r"|^sim/results/"
    r"|(^|/)sim_build/"
    r"|(^|/)__pycache__/"
    r"|(^|/)results\.xml$"
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)


def test_no_sim_output_is_tracked():
    tracked = _git("ls-files").stdout.splitlines()
    assert tracked, "git ls-files returned nothing; not a checkout"
    hits = [p for p in tracked if SIM_OUTPUT.search(p)]
    assert not hits, f"DS-P3.188: sim-written paths are tracked: {hits}"


def test_the_nvc_work_lock_is_ignored():
    run = _git("check-ignore", "-q", "--no-index", "hw/sim/work.lock")
    assert run.returncode == 0, "DS-P3.188: hw/sim/work.lock must match .gitignore"


def test_the_pattern_matches_the_file_that_was_tracked():
    """The guard would have caught the original defect."""
    assert SIM_OUTPUT.search("hw/sim/work.lock")
    assert SIM_OUTPUT.search("sim/results/test_x/results.xml")
    assert not SIM_OUTPUT.search("docs/hardware/evidence/ds_p2_132/pfsoc_ka_rea_capture.vcd")
    assert not SIM_OUTPUT.search("sim/waves/test_uart_datalogger/uart_datalogger.ltmeta.json")
