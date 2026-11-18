# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P3.187: a commit that leaves routertl.lock stale is refused.

These tests run the hook's own leg, cut out of hooks/pre-commit.local, in a
scratch repository, with a stand-in for rr whose verdict each test sets. The
real `rr lock --check` is exercised by the commit gate itself on this tree.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "pre-commit.local"
LEG_START = "# ── DS-P3.187 — routertl.lock stays in step with the tree ──"
LOCK = """version: 1
sources:
- path: hw/rtl/core.vhd
  sha256: aa
constraints:
- path: hw/sdc/top.sdc
  sha256: bb
dependencies:
- files:
  - libs/pkg/pkg.vhd
"""
STALE = "  Lockfile mismatch — 1 difference(s):\n     sources: hw/rtl/core.vhd (sha256: aa → cc)\n"


def _hook_leg() -> str:
    text = HOOK.read_text(encoding="utf-8")
    return text[text.index(LEG_START):]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / "tools").mkdir(parents=True)
    shutil.copy(ROOT / "tools" / "lock_check_leg.py", work / "tools")
    # DS-P2.287: the per-target lock layout of routertl >= 5.5.0 (targets/<t>.routertl.lock, no root lock).
    for rel, body in (("targets/x.routertl.lock", LOCK), ("targets/x.yml", "project:\n  name: x\n"),
                      ("hw/rtl/core.vhd", "-- core\n"),
                      ("hw/sdc/top.sdc", "# sdc\n"), ("libs/pkg/pkg.vhd", "-- pkg\n"),
                      ("docs/guide.md", "# Guide\n"), ("project.yml", "target: targets/x.yml\n")):
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    return work


def _fake_rr(tmp_path: Path, rc: int, out: str) -> Path:
    log = tmp_path / "rr_calls.txt"
    script = tmp_path / "rr"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\nprintf '%s' '{out}'\nexit {rc}\n",
                      encoding="utf-8")
    script.chmod(0o755)
    return script


def _run_leg(repo: Path, rr: Path) -> subprocess.CompletedProcess[str]:
    script = f'set -u\nDS_GUARD_PY="{sys.executable}"\n{_hook_leg()}\necho LEG_PASSED\n'
    env = {"PATH": "/usr/bin:/bin", "DS_RR_BIN": str(rr), "HOME": str(repo)}
    return subprocess.run(["bash", "-c", script], cwd=repo, text=True,
                          capture_output=True, check=False, env=env)


def _edit(repo: Path, rel: str, stage: bool = True) -> None:
    path = repo / rel
    path.write_text(path.read_text(encoding="utf-8") + "-- edit\n", encoding="utf-8")
    if stage:
        _git(repo, "add", rel)


def test_a_stale_lock_refuses_the_commit_naming_the_entry(repo, tmp_path):
    _edit(repo, "hw/rtl/core.vhd")
    proc = _run_leg(repo, _fake_rr(tmp_path, 1, STALE))
    assert proc.returncode == 1
    assert "LEG_PASSED" not in proc.stdout
    assert "sources: hw/rtl/core.vhd" in proc.stdout
    assert "routertl.lock is stale for this commit" in proc.stdout
    assert "GATE COULD NOT RUN" not in proc.stdout


def test_a_fresh_lock_passes(repo, tmp_path):
    _edit(repo, "hw/rtl/core.vhd")
    proc = _run_leg(repo, _fake_rr(tmp_path, 0, "  Lockfile is up to date.\n"))
    assert "LEG_PASSED" in proc.stdout, proc.stdout + proc.stderr
    assert (tmp_path / "rr_calls.txt").read_text() == "lock --check\n"


@pytest.mark.parametrize("rel", ["hw/sdc/top.sdc", "libs/pkg/pkg.vhd", "project.yml",
                                 "targets/x.routertl.lock"])
def test_every_kind_of_covered_path_runs_the_check(repo, tmp_path, rel):
    _edit(repo, rel)
    proc = _run_leg(repo, _fake_rr(tmp_path, 1, STALE))
    assert proc.returncode == 1, proc.stdout


def test_a_commit_outside_the_lock_does_not_run_rr(repo, tmp_path):
    _edit(repo, "docs/guide.md")
    proc = _run_leg(repo, _fake_rr(tmp_path, 1, STALE))
    assert "LEG_PASSED" in proc.stdout, proc.stdout + proc.stderr
    assert not (tmp_path / "rr_calls.txt").exists()


def test_unstaged_changes_to_covered_paths_refuse(repo, tmp_path):
    _edit(repo, "hw/rtl/core.vhd")
    _edit(repo, "hw/sdc/top.sdc", stage=False)
    proc = _run_leg(repo, _fake_rr(tmp_path, 0, "  Lockfile is up to date.\n"))
    assert proc.returncode == 1
    assert "hw/sdc/top.sdc" in proc.stdout
    assert not (tmp_path / "rr_calls.txt").exists()


def test_an_rr_failure_without_a_mismatch_cannot_pass(repo, tmp_path):
    _edit(repo, "hw/rtl/core.vhd")
    proc = _run_leg(repo, _fake_rr(tmp_path, 3, "Traceback (most recent call last):\n"))
    assert proc.returncode == 1
    assert "GATE COULD NOT RUN" in proc.stdout


def test_a_missing_rr_cannot_pass(repo, tmp_path):
    _edit(repo, "hw/rtl/core.vhd")
    proc = _run_leg(repo, tmp_path / "no-such-rr")
    assert proc.returncode == 1
    assert "GATE COULD NOT RUN" in proc.stdout
