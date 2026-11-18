# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.206: the commit gate runs the PolarFire RouteWave render checks.

test_pfsoc_mmio_routewave_p2_128.py and test_pfsoc_rea_mmio_rr_capture_ds_p2_128.py
were red on main and nothing executed them: the gate runs named legs only, and
public CI has neither RouteWave nor the evidence. This pins the leg that runs
both in the private tree and refuses a skipped run.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "pre-commit.local"
NODES = (
    "tools/tests/test_pfsoc_mmio_routewave_p2_128.py::test_routewave_renders_known_answer_pattern",
    "tools/tests/test_pfsoc_rea_mmio_rr_capture_ds_p2_128.py::test_routewave_renders_the_rr_capture",
)


def _leg() -> str:
    text = HOOK.read_text(encoding="utf-8")
    m = re.search(r"^# ── DS-P2\.206 .*?^fi$", text, re.M | re.S)
    assert m, "hooks/pre-commit.local has no DS-P2.206 RouteWave render leg"
    return m.group(0)


def test_gate_runs_both_render_tests_in_the_private_tree():
    leg = _leg()
    assert "git cat-file -e :context.json" in leg
    for node in NODES:
        assert node in leg, f"the DS-P2.206 leg does not run {node}"


def test_gate_refuses_a_skipped_render_run():
    leg = _leg()
    assert re.search(r'grep -q " skipped"', leg), "a skipped render test must not pass the leg"
    assert "exit 1" in leg
