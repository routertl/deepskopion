"""DS-P2.192: the system Firmware Design Document passes the rr FDD gate.

The project-root ``requirements.yml`` makes deepskopion a project-level FDD
target, and ``FDD.md`` beside it aggregates that contract, ``ip.yml`` and
``project_regbank.yml``. The check runs at the surface a caller reaches: the
``rr fdd check`` exit code in strict mode, scoped to the system target. It
goes red when the FDD is missing, when a required prose section is empty or
still the scaffold placeholder, and when a declarative input changed without
``rr fdd generate`` (stale digest).

The gate runs on a copy of the tracked files only. ``rr fdd check --ip``
selects a contract of any provenance, so a gitignored or untracked copy of
the repository inside the tree (a build output, a test-power checkout) would
otherwise add targets and findings that are not part of the commit.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SYSTEM_IP = "deepskopion"

GATE_RED = "DS-P2.192 system FDD does not pass rr fdd check --strict"


def _rr() -> str:
    rr = shutil.which("rr")
    assert rr, "PRECONDITION: rr is not on PATH; the FDD gate cannot run"
    return rr


def _tracked_copy(dest: Path) -> Path:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode("utf-8").split("\0")
    for rel in filter(None, listed):
        src = ROOT / rel
        if src.is_file() and not src.is_symlink():
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, out)
    return dest


def test_system_fdd_passes_strict_fdd_check(tmp_path):
    tree = _tracked_copy(tmp_path / "tree")
    proc = subprocess.run(
        [_rr(), "fdd", "check", "--ip", SYSTEM_IP, "--strict", "--json"],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=300,
    )
    report = None
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pass
    assert proc.returncode == 0, (
        f"{GATE_RED} (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    )
    assert report is not None, f"PRECONDITION: rr fdd check --json is not JSON:\n{proc.stdout}"
    assert report.get("checked") == 1, (
        f"{GATE_RED}: expected exactly the system target to be checked, got {report}"
    )
