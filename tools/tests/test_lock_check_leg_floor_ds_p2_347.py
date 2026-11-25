# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.347: a routertl floor change re-checks every target lock, and every lock records the floor version.

The 5.6.0 floor bump (5d324ddc) staged requirements.txt and tools/preflight/supported_environment.yml and left all
13 targets/<t>.routertl.lock at sdk.version 5.5.0. The lock leg printed "this commit stages nothing a target lock
covers" and passed, so `rr lock --check` under the floor rr failed for every target until 8ce1c03d re-locked them.

These tests run the hook's own leg, cut out of hooks/pre-commit.local, in a scratch repository with two targets and
a floor declaration, with a stand-in rr that records every call. Each must-fail case is red on the previous leg.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "pre-commit.local"
LEG_START = "# ── DS-P3.187 — routertl.lock stays in step with the tree ──"
TARGETS = ("alpha", "beta")
FLOOR_FILE = "tools/preflight/supported_environment.yml"


def _lock(target: str, sdk_version: str) -> str:
    return f"""version: 1
target: {target}
sources:
- path: hw/rtl/{target}.vhd
  sha256: aa
sdk:
  commit: unknown
  version: {sdk_version}
"""


def _floor(version: str) -> str:
    return f"routertl:\n  # comment line\n  min: '{version}'\n  install: pip install routertl\n"


def _hook_leg() -> str:
    text = HOOK.read_text(encoding="utf-8")
    return text[text.index(LEG_START):]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path, *, floor: str = "5.5.0", locked: str = "5.5.0") -> Path:
    work = tmp_path / "work"
    (work / "tools").mkdir(parents=True)
    shutil.copy(ROOT / "tools" / "lock_check_leg.py", work / "tools")
    files = {"project.yml": "target: targets/alpha.yml\n", "docs/guide.md": "# Guide\n",
             "requirements.txt": f"routertl>={floor}\n", FLOOR_FILE: _floor(floor)}
    for t in TARGETS:
        files[f"targets/{t}.yml"] = f"project:\n  name: {t}\n"
        files[f"hw/rtl/{t}.vhd"] = f"-- {t}\n"
        files[f"targets/{t}.routertl.lock"] = _lock(t, locked)
    for rel, body in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    return work


def _fake_rr(tmp_path: Path) -> tuple[Path, Path]:
    """rr stand-in: every lock is in step with the tree; each call is appended to the returned log."""
    log = tmp_path / "rr_calls.txt"
    script = tmp_path / "rr"
    script.write_text(f"""#!/bin/sh
echo "RR_TARGET=${{RR_TARGET:-}} $@" >> {log}
echo "  Lockfile is up to date."; exit 0
""", encoding="utf-8")
    script.chmod(0o755)
    return script, log


def _run_leg(repo: Path, rr: Path) -> subprocess.CompletedProcess[str]:
    script = f'set -u\nDS_GUARD_PY="{sys.executable}"\n{_hook_leg()}\necho LEG_PASSED\n'
    env = {"PATH": "/usr/bin:/bin", "DS_RR_BIN": str(rr), "HOME": str(repo)}
    return subprocess.run(["bash", "-c", script], cwd=repo, text=True,
                          capture_output=True, check=False, env=env)


def _write_stage(repo: Path, rel: str, body: str) -> None:
    (repo / rel).write_text(body, encoding="utf-8")
    _git(repo, "add", rel)


def _rr_calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_a_floor_raised_without_relocking_is_refused_naming_targets_and_versions(tmp_path):
    """The 5d324ddc shape: the floor goes 5.5.0 -> 5.6.0, the locks stay at 5.5.0."""
    repo = _repo(tmp_path)
    rr, log = _fake_rr(tmp_path)
    _write_stage(repo, "requirements.txt", "routertl>=5.6.0\n")
    _write_stage(repo, FLOOR_FILE, _floor("5.6.0"))
    done = _run_leg(repo, rr)
    out = done.stdout + done.stderr
    assert done.returncode == 1, out
    assert "LEG_PASSED" not in done.stdout, out
    assert "Commit aborted" in out, out
    for t in TARGETS:
        assert f"targets/{t}.routertl.lock: sdk.version 5.5.0, floor 5.6.0" in out, out
    assert _rr_calls(log) == [], "the floor check is parse-only; rr must not be called"


def test_a_floor_raised_with_every_lock_relocked_checks_every_target(tmp_path):
    """Positive control and selection: the same floor change with the locks re-written passes, and the floor files
    select every target for `rr lock --check`."""
    repo = _repo(tmp_path)
    rr, log = _fake_rr(tmp_path)
    _write_stage(repo, "requirements.txt", "routertl>=5.6.0\n")
    _write_stage(repo, FLOOR_FILE, _floor("5.6.0"))
    for t in TARGETS:
        _write_stage(repo, f"targets/{t}.routertl.lock", _lock(t, "5.6.0"))
    done = _run_leg(repo, rr)
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "LEG_PASSED" in done.stdout, out
    assert "declares the routertl floor every lock records" in out, out
    assert "routertl locks hold for 2 target(s)" in out, out
    checked = sorted(c.split()[0] for c in _rr_calls(log) if "--check" in c)
    assert checked == ["RR_TARGET=targets/alpha.yml", "RR_TARGET=targets/beta.yml"], _rr_calls(log)


def test_requirements_txt_alone_selects_every_target(tmp_path):
    repo = _repo(tmp_path)
    rr, log = _fake_rr(tmp_path)
    _write_stage(repo, "requirements.txt", "# pinned by the floor\nroutertl>=5.5.0\n")
    done = _run_leg(repo, rr)
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "LEG_PASSED" in done.stdout, out
    assert "Checking 2 target lock(s) (requirements.txt declares the routertl floor" in out, out


def test_a_lock_newer_than_the_floor_is_refused_on_an_unrelated_commit(tmp_path):
    """Equality, not >=: a lock written by an rr newer than the floor fails `rr lock --check` on the floor."""
    repo = _repo(tmp_path, floor="5.6.0", locked="5.6.0")
    _write_stage(repo, "targets/beta.routertl.lock", _lock("beta", "5.7.0"))
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "newer rr")
    rr, log = _fake_rr(tmp_path)
    _write_stage(repo, "docs/guide.md", "# Guide\n\nmore\n")
    done = _run_leg(repo, rr)
    out = done.stdout + done.stderr
    assert done.returncode == 1, out
    assert "targets/beta.routertl.lock: sdk.version 5.7.0, floor 5.6.0" in out, out
    assert "targets/alpha.routertl.lock" not in out, out
    assert _rr_calls(log) == []


def test_a_lock_with_no_sdk_version_is_refused(tmp_path):
    repo = _repo(tmp_path, floor="5.6.0", locked="5.6.0")
    rr, _ = _fake_rr(tmp_path)
    _write_stage(repo, "targets/alpha.routertl.lock", "version: 1\ntarget: alpha\nsources: []\n")
    done = _run_leg(repo, rr)
    assert done.returncode == 1, done.stdout
    assert "targets/alpha.routertl.lock: sdk.version (none), floor 5.6.0" in done.stdout, done.stdout


def test_a_floor_file_without_routertl_min_cannot_be_judged(tmp_path):
    repo = _repo(tmp_path, floor="5.6.0", locked="5.6.0")
    rr, _ = _fake_rr(tmp_path)
    _write_stage(repo, FLOOR_FILE, "routertl:\n  install: pip install routertl\n")
    done = _run_leg(repo, rr)
    assert done.returncode == 1, done.stdout
    assert "sets no routertl.min" in done.stdout, done.stdout
    assert "GATE COULD NOT RUN" in done.stdout, done.stdout


def test_the_shipped_tree_holds_the_invariant():
    """Every tracked lock in this repository records the declared floor (8ce1c03d re-locked all 13)."""
    import yaml

    floor = str(yaml.safe_load((ROOT / FLOOR_FILE).read_text(encoding="utf-8"))["routertl"]["min"])
    locks = sorted((ROOT / "targets").glob("*.routertl.lock"))
    assert locks, "no target lock found"
    versions = {p.name: str((yaml.safe_load(p.read_text(encoding="utf-8")).get("sdk") or {}).get("version"))
                for p in locks}
    assert set(versions.values()) == {floor}, versions
