# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P3.311: a post-synth guard run leaves the checkout it reads unchanged.

The 07:25 cron runs tools/post_synth_guard.py in the shared deepskopion checkout. Its tests load the
fetched-packages fixture, which ran ``rr workspace sync`` there when a fetched package was missing, and on
2026-11-21 that sync rewrote the tracked ip.lock. The guard now runs its tests with DS_FETCH_CHECK_ONLY=1:
a missing package is a red that names the missing files, and no sync runs.

The check uses a scratch git repository holding the real fixture module, an ip.lock that pins one fetched
package whose file is absent, and a fake ``rr`` on PATH that rewrites ip.lock and leaves a sentinel, as the
real sync did. The positive control shows the fake ``rr`` does mutate the scratch repository when the fixture
is allowed to sync, so an unchanged git status is evidence and not a harness that cannot see a change.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import post_synth_guard as psg  # noqa: E402

MISSING = "libs/routertl/demo/rtl/demo_top.vhd"
PROBE = "tools/tests/test_probe.py::test_probe"

LOCK = f"""version: 1
routertl/demo:
  resolved_version: 1.0.0
  url: https://example.invalid/demo.git
  commit: 0123456789abcdef0123456789abcdef01234567
  local_path: libs/routertl/demo
  library: demo
  file_hashes:
    {MISSING}: {"0" * 64}
"""

CONFTEST = """import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.fixture(scope="session", autouse=True)
def _fetched_packages():
    sys.path.insert(0, str(HERE))
    from fetched_packages import ensure_all_fetched
    ensure_all_fetched(HERE.parents[1])
"""

FAKE_RR = """#!/bin/sh
echo "rr $*" >> "$DS311_SENTINEL"
echo "# rewritten by a sync" >> ip.lock
exit 1
"""

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, env={**os.environ, **GIT_ENV}, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, errors="replace", check=True).stdout


def _scratch(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    repo = tmp_path / "checkout"
    tests = repo / "tools" / "tests"
    tests.mkdir(parents=True)
    (repo / "ip.lock").write_text(LOCK, encoding="utf-8")
    shutil.copy2(ROOT / "tools" / "tests" / "fetched_packages.py", tests / "fetched_packages.py")
    (tests / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (tests / "test_probe.py").write_text("def test_probe():\n    pass\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "scratch")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rr").write_text(FAKE_RR, encoding="utf-8")
    (bin_dir / "rr").chmod(0o755)
    sentinel = tmp_path / "rr_calls.txt"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("DS311_SENTINEL", str(sentinel))
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("DS_FETCH_CHECK_ONLY", raising=False)
    return repo, sentinel


def test_positive_control_a_syncing_fixture_mutates_the_scratch_checkout(tmp_path, monkeypatch):
    repo, sentinel = _scratch(tmp_path, monkeypatch)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", PROBE], cwd=repo,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, errors="replace", check=False)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert sentinel.read_text(encoding="utf-8").splitlines() == ["rr workspace sync"]
    assert _git(repo, "status", "--porcelain").splitlines() == [" M ip.lock"]


def test_guard_run_leaves_the_checkout_it_reads_unchanged(tmp_path, monkeypatch):
    repo, sentinel = _scratch(tmp_path, monkeypatch)
    monkeypatch.setattr(psg, "ROOT", repo)
    monkeypatch.setattr(psg, "POST_SYNTH_TESTS", (PROBE,))
    ok, log = psg.run_tests(tmp_path / "pll_root")
    assert _git(repo, "status", "--porcelain") == ""
    assert not sentinel.exists()
    assert ok is False
    assert MISSING in log
    assert "DS_FETCH_CHECK_ONLY=1, so rr workspace sync was not run" in log
