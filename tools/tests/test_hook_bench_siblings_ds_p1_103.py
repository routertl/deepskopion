# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.103: the commit hook runs a split test file's bench sibling when the checkout has it, and only then.

DS-P1.103 moved bench-subject tests out of sw/linux/de25/test_de25_linux_image.py and
tools/tests/test_pkg_install_clean_clone_p3_150.py into *_bench.py siblings, which the export drops. The hook's
DE25 leg and DS-P3.150 leg name their files explicitly, so each adds its sibling only when the file exists. These
tests cut each leg's pytest command out of hooks/pre-commit.local and run it under `set -u` with a stand-in
interpreter that records what it was given: with the sibling present it is passed, without it it is not (the
export case). A last test runs the real collection in this checkout and finds the siblings' node ids.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "pre-commit.local"
#: leg name -> (first line of the cut, last line of the cut, the sibling it adds)
LEGS = {
    "de25": ("ds_de25_rc=0", "    2>&1) || ds_de25_rc=$?", "sw/linux/de25/test_de25_linux_image_bench.py"),
    "pkg": ("ds_pkg_rc=0", "${ds_pkg_bench[@]+\"${ds_pkg_bench[@]}\"} 2>&1) || ds_pkg_rc=$?",
            "tools/tests/test_pkg_install_clean_clone_p3_150_bench.py"),
}


def _cut(leg: str) -> str:
    start, end, _ = LEGS[leg]
    lines = HOOK.read_text(encoding="utf-8").splitlines()
    i = lines.index(start)
    j = next(k for k in range(i, len(lines)) if lines[k].rstrip().endswith(end.strip()))
    return "\n".join(lines[i:j + 1])


def _run(leg: str, cwd: Path, guard: Path) -> subprocess.CompletedProcess[str]:
    script = f'set -u\nDS_GUARD_PY="{guard}"\nds_de25_unpriv_path="/usr/bin:/bin"\n{_cut(leg)}\n' \
             f'printf "%s\\n" "${{ds_{leg}_out}}"\n'
    return subprocess.run(["bash", "-c", script], cwd=cwd, text=True, capture_output=True, check=False)


def _recorder(tmp_path: Path) -> Path:
    guard = tmp_path / "guard"
    guard.write_text('#!/bin/sh\nprintf "ARG %s\\n" "$@"\n', encoding="utf-8")
    guard.chmod(0o755)
    return guard


@pytest.mark.parametrize("leg", sorted(LEGS))
@pytest.mark.parametrize("present", [True, False], ids=["sibling-present", "sibling-absent"])
def test_a_leg_passes_its_bench_sibling_only_when_the_checkout_has_it(tmp_path, leg, present):
    sibling = LEGS[leg][2]
    tree = tmp_path / "tree"
    tree.mkdir()
    if present:
        (tree / sibling).parent.mkdir(parents=True)
        (tree / sibling).write_text("", encoding="utf-8")
    r = _run(leg, tree, _recorder(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    args = [line[4:] for line in r.stdout.splitlines() if line.startswith("ARG ")]
    assert "pytest" in args, r.stdout                     # the leg ran its pytest command
    assert (sibling in args) is present, args


@pytest.mark.parametrize("leg", sorted(LEGS))
def test_the_real_leg_collects_the_sibling_node_ids_in_this_checkout(tmp_path, leg):
    sibling = LEGS[leg][2]
    if not (ROOT / sibling).is_file():
        pytest.skip("the export has no bench sibling; the absent case is covered above")
    guard = tmp_path / "guard"
    guard.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@" --collect-only\n', encoding="utf-8")
    guard.chmod(0o755)
    r = _run(leg, ROOT, guard)
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(line.startswith(f"{sibling}::") for line in r.stdout.splitlines()), r.stdout[-2000:]
