# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P3.337: the pre-commit leg runs the copy-block guard on the STAGED telemetry doc.

The leg is extracted from hooks/pre-commit.local and run in a scratch repository holding the
guard, the exporter sources and the doc. A broken staged doc with a corrected unstaged copy must
be refused; a correct staged doc with a broken unstaged copy passes, because the index is what
the commit records. (Separate from the guard file: the leg runs that file, so a hook test inside
it would run itself.)
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC_REL = "docs/software/telemetry_exporter.md"
GUARD_REL = "tools/tests/test_telemetry_copy_blocks_execute_ds_p3_337.py"


@pytest.fixture(autouse=True)
def _guard_prerequisites():
    """DS-P3.339: the guard's own rule. A missing bubblewrap or markdown-it-py fails on the bench
    and in CI and skips elsewhere; without it a skipped guard would read here as a passing leg."""
    spec = importlib.util.spec_from_file_location("ds_p3_337_guard", ROOT / GUARD_REL)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard._prerequisite(guard._bwrap_problem())
    guard._prerequisite(guard._unshare_problem())            # DS-P3.345: the broker
    if importlib.util.find_spec("markdown_it") is None:
        guard._prerequisite("markdown-it-py is not installed")


def _leg() -> str:
    text = (ROOT / "hooks/pre-commit.local").read_text(encoding="utf-8")
    m = re.search(r"^# ── DS-P3\.337 — .*?\n(?=^# ── )", text, flags=re.S | re.M)
    assert m, "the DS-P3.337 leg is missing from hooks/pre-commit.local"
    return m.group(0)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    for rel in (GUARD_REL, DOC_REL, "tools/tests/copy_block_broker.py", "tools/tests/__init__.py"):   # DS-P3.345: the broker
        src = ROOT / rel
        if src.exists():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, repo / rel)
    shutil.copytree(ROOT / "sw/telemetry", repo / "sw/telemetry", ignore=shutil.ignore_patterns("tests", "__pycache__"))
    (repo / "context.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "add", "-A")
    return repo


def _run_leg(repo: Path, tmp_path: Path, path: str = "/usr/bin:/bin") -> subprocess.CompletedProcess[str]:
    # HOME stays the real one: the hook's interpreter may find pytest in the user site.
    # Neither CI nor RR_BENCH_CONFIG is passed: the leg itself must make the prerequisites required.
    env = {"PATH": path, "DS_GUARD_PY": sys.executable, "HOME": os.environ["HOME"],
           "TMPDIR": str(tmp_path)}
    return subprocess.run(["bash", "-c", _leg()], cwd=repo, env=env, capture_output=True, text=True, check=False)


def _broken(doc: str) -> str:
    broken = doc.replace("( set -e\n", "(\n")
    assert broken != doc
    return broken


def test_a_broken_staged_doc_is_refused_even_with_a_corrected_worktree_copy(tmp_path):
    repo = _repo(tmp_path)
    good = (repo / DOC_REL).read_text(encoding="utf-8")
    (repo / DOC_REL).write_text(_broken(good), encoding="utf-8")
    _git(repo, "add", DOC_REL)
    (repo / DOC_REL).write_text(good, encoding="utf-8")          # unstaged correction
    proc = _run_leg(repo, tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "does not do what the doc says" in proc.stdout
    assert "prerequisite missing" not in proc.stdout, "refused for a missing tool, not for the doc"


def test_a_correct_staged_doc_passes_whatever_the_worktree_holds(tmp_path):
    repo = _repo(tmp_path)
    good = (repo / DOC_REL).read_text(encoding="utf-8")
    (repo / DOC_REL).write_text(_broken(good), encoding="utf-8")  # unstaged breakage
    proc = _run_leg(repo, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "do what the doc says (DS-P3.337)" in proc.stdout


def test_the_leg_refuses_when_bubblewrap_is_missing(tmp_path):
    """DS-P3.339: with no bwrap on PATH and neither CI nor RR_BENCH_CONFIG set, the leg still fails:
    it sets DS_COPY_BLOCKS_REQUIRED, so the guard cannot skip into a passing commit."""
    repo = _repo(tmp_path)
    bindir = tmp_path / "nobwrap"
    bindir.mkdir()
    for tool in ("bash", "git", "mktemp", "rm", "cat"):
        (bindir / tool).symlink_to(shutil.which(tool))
    proc = _run_leg(repo, tmp_path, path=str(bindir))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "bubblewrap (bwrap) is not installed" in proc.stdout
