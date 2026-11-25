# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P3.258: a target's project.top_sim names a testbench that exists, or the target has no top_sim key.

Six targets named top_sim: tb_deepskopion_top, an entity no tracked file declares, so every rr resolve printed
"auto-sources: declared top_sim 'tb_deepskopion_top' was not found". A warning that is always present hides a
real missing-source warning. No sim of these targets reads project.top_sim: each cocotb module passes its own
top_level to run_simulation (the stimulus modules through _stim_common.run_stimulus_gen_loop).

The first test is static and covers every target file in this checkout. The second runs `rr sources --sim` for
each product target and asserts rr itself prints no top_sim not-found warning.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import PRODUCTS, all_target_files, target_file  # noqa: E402

_HDL_GLOBS = ("*.vhd", "*.vhdl", "*.v", "*.sv")
_NOT_FOUND = "declared top_sim"


def _declared_units() -> set[str]:
    files = subprocess.run(["git", "ls-files", "-z", "--", *_HDL_GLOBS], cwd=ROOT, capture_output=True,
                           check=True).stdout.decode("utf-8", "replace").split("\0")
    units: set[str] = set()
    pattern = re.compile(r"^\s*(?:entity\s+(\w+)\s+is|module\s+(\w+))", re.IGNORECASE | re.MULTILINE)
    for rel in filter(None, files):
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        units.update((a or b).lower() for a, b in pattern.findall(text))
    return units


def test_every_declared_top_sim_names_a_tracked_entity():
    units = _declared_units()
    bad = {}
    for path in all_target_files(ROOT):
        top_sim = ((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("project") or {}).get("top_sim")
        if top_sim is not None and str(top_sim).lower() not in units:
            bad[str(path.relative_to(ROOT))] = top_sim
    assert not bad, f"top_sim names no tracked entity or module: {bad}"


@pytest.mark.parametrize("target", PRODUCTS)
def test_rr_resolve_of_a_product_prints_no_top_sim_not_found_warning(target, tmp_path):
    rr = shutil.which("rr")
    assert rr is not None, "rr is not on PATH; rr is required to run this test (DS-P3.258)"
    env = dict(os.environ, RR_TARGET=str(target_file(target, ROOT).relative_to(ROOT)), TMPDIR=str(tmp_path),
               RR_NO_PYTHON_DEPRECATION_WARNING="1")
    proc = subprocess.run([rr, "sources", "--sim"], cwd=ROOT, capture_output=True, text=True, errors="replace",
                          env=env, stdin=subprocess.DEVNULL, timeout=300)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-2000:]
    assert _NOT_FOUND not in out, [line for line in out.splitlines() if _NOT_FOUND in line]
