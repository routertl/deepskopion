# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.124: the bench lock check runs the declared floor rr, not whatever `rr` is first on PATH.

The lock records what the declared floor produces (DS-P2.347) and CI's floor leg runs the published
`routertl==<floor>` wheel. The pre-commit leg ran the first `rr` on PATH, on this bench the shared editable
routertl checkout, which follows routertl main: a fix the floor lacks changed the lock the checkout computes while the
checkout still reported the floor's version string, so every commit went red. These run the hook's own leg in a
scratch repository with two `rr` stand-ins: one first on PATH (a newer rr that records a call and fails the check) and
one in the floor venv layout (the floor rr). Each case is red on the leg that ran PATH's rr.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("lock_leg_floor_fixture", HERE / "test_lock_check_leg_floor_ds_p2_347.py")
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

FLOOR = "5.5.0"


def _stub(path: Path, version: str, log: Path, verdict: str) -> Path:
    """An rr stand-in: reports *version*, appends each call to *log*, and answers the lock check with *verdict*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""#!/bin/sh
echo "{version} PP=${{PYTHONPATH:-}} $@" >> {log}
case "$1" in
  --version) echo "RouteRTL SDK, version {version} (2026-11-22, abc1234)"; exit 0;;
esac
{verdict}
""", encoding="utf-8")
    path.chmod(0o755)
    return path


STALE = 'echo "  Lockfile mismatch — 1 difference(s):"; exit 1'
UP_TO_DATE = 'echo "  Lockfile is up to date."; exit 0'


def _run(repo: Path, *, path_dir: Path, home: Path, extra: dict | None = None) -> subprocess.CompletedProcess[str]:
    script = f'set -u\nDS_GUARD_PY="{sys.executable}"\n{_base._hook_leg()}\necho LEG_PASSED\n'
    env = {"PATH": f"{path_dir}:/usr/bin:/bin", "HOME": str(repo), "DS_FLOOR_RR_HOME": str(home), **(extra or {})}
    return subprocess.run(["bash", "-c", script], cwd=repo, text=True, capture_output=True, check=False, env=env)


def _staged_floor_change(repo: Path) -> None:
    _base._write_stage(repo, "requirements.txt", f"# pinned by the floor\nroutertl>={FLOOR}\n")


def test_the_leg_judges_with_the_floor_rr_even_when_a_newer_rr_is_first_on_path(tmp_path):
    repo = _base._repo(tmp_path, floor=FLOOR, locked=FLOOR)
    log = tmp_path / "calls.txt"
    newer = _stub(tmp_path / "pathbin" / "rr", "9.9.9", log, STALE)            # PATH's rr would fail the check
    floor_rr = _stub(tmp_path / "floorhome" / FLOOR / "bin" / "rr", FLOOR, log, UP_TO_DATE)
    _staged_floor_change(repo)
    done = _run(repo, path_dir=newer.parent, home=floor_rr.parents[2], extra={"PYTHONPATH": "/some/routertl/checkout"})
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "LEG_PASSED" in done.stdout, out
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls and all(c.startswith(f"{FLOOR} ") for c in calls), f"PATH's rr was called: {calls}"
    assert all(c.startswith(f"{FLOOR} PP= ") for c in calls), f"the floor rr inherited PYTHONPATH: {calls}"
    assert f"rr: {floor_rr}, routertl {FLOOR}, the declared floor wheel" in out, out


def test_a_floor_venv_that_is_not_the_floor_is_refused_not_used(tmp_path):
    repo = _base._repo(tmp_path, floor=FLOOR, locked=FLOOR)
    log = tmp_path / "calls.txt"
    _stub(tmp_path / "floorhome" / FLOOR / "bin" / "rr", "9.9.9", log, UP_TO_DATE)   # wrong version in the floor slot
    _staged_floor_change(repo)
    done = _run(repo, path_dir=tmp_path / "empty", home=tmp_path / "floorhome")
    out = done.stdout + done.stderr
    assert done.returncode != 0 and "LEG_PASSED" not in done.stdout and "GATE COULD NOT RUN" in out, out
    assert f"not the floor {FLOOR}" in out, out
    assert not any(" --check" in c or "lock" in c for c in log.read_text(encoding="utf-8").splitlines()), "rr judged a lock"


def test_a_floor_rr_that_cannot_be_installed_stops_the_commit(tmp_path):
    repo = _base._repo(tmp_path, floor=FLOOR, locked=FLOOR)
    _staged_floor_change(repo)
    done = _run(repo, path_dir=tmp_path / "empty", home=tmp_path / "floorhome", extra={"DS_FLOOR_RR_PIP": "false"})
    out = done.stdout + done.stderr
    assert done.returncode != 0 and "LEG_PASSED" not in done.stdout and "GATE COULD NOT RUN" in out, out
    assert f"Cannot provide the floor rr" in out and f"routertl=={FLOOR}" in out, out


def test_ds_rr_bin_still_overrides_the_floor_rr(tmp_path):
    repo = _base._repo(tmp_path, floor=FLOOR, locked=FLOOR)
    log = tmp_path / "calls.txt"
    override = _stub(tmp_path / "ov" / "rr", "7.7.7", log, UP_TO_DATE)
    _staged_floor_change(repo)
    done = _run(repo, path_dir=tmp_path / "empty", home=tmp_path / "floorhome", extra={"DS_RR_BIN": str(override)})
    out = done.stdout + done.stderr
    assert done.returncode == 0 and f"rr: {override}, DS_RR_BIN" in out, out
