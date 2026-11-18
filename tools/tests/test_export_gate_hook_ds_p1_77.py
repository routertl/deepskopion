# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.77: a finding is refused at the commit that introduces it.

These tests run the hook's own export-gate leg, cut out of hooks/pre-commit.local,
in a scratch repository. Asserting only that the gate's detector works is how a
guard ships green without ever stopping a commit.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "pre-commit.local"
LEG_START = "# ── DS-P1.77 — public export gate ──"
LEG_END = "# ── DS-X2 (DS-P1.31)"
TICKET = "DS-" + "P9.999"


def _hook_leg() -> str:
    text = HOOK.read_text(encoding="utf-8")
    start = text.index(LEG_START)
    return text[start:text.index(LEG_END, start)]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / "tools").mkdir(parents=True)
    (work / "docs").mkdir()
    for name in ("export_github.py", "public_artifact_hygiene.py"):
        shutil.copy(ROOT / "tools" / name, work / "tools" / name)
    (work / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (work / "context.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "add", "-A")
    return work


def _run_leg(repo: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    names = tmp_path / "names.yml"
    names.write_text("- name: Zzyzx" + "corp\n", encoding="utf-8")
    script = f'set -u\nDS_GUARD_PY="{sys.executable}"\n{_hook_leg()}\necho LEG_PASSED\n'
    return subprocess.run(
        ["bash", "-c", script], cwd=repo, text=True, capture_output=True, check=False,
        env=dict(os.environ, RESTRICTED_NAMES_FILE=str(names)),
    )


def test_a_clean_staged_tree_passes(repo, tmp_path):
    proc = _run_leg(repo, tmp_path)
    assert "LEG_PASSED" in proc.stdout, proc.stdout + proc.stderr


def test_a_ticket_id_in_a_staged_doc_refuses_the_commit(repo, tmp_path):
    (repo / "docs" / "guide.md").write_text(f"# Guide\nFixed in {TICKET}.\n", encoding="utf-8")
    _git(repo, "add", "-A")
    proc = _run_leg(repo, tmp_path)
    assert proc.returncode == 1
    assert "LEG_PASSED" not in proc.stdout
    assert f"docs/guide.md:2: ticket narrative in docs: {TICKET}" in proc.stdout
    assert "Commit aborted" in proc.stdout


def test_the_leg_judges_the_index_not_the_worktree(repo, tmp_path):
    (repo / "docs" / "guide.md").write_text(f"Fixed in {TICKET}.\n", encoding="utf-8")
    proc = _run_leg(repo, tmp_path)
    assert "LEG_PASSED" in proc.stdout, proc.stdout + proc.stderr


def test_a_finding_in_a_dropped_path_does_not_refuse(repo, tmp_path):
    evidence = repo / "docs" / "evidence" / "run.md"
    evidence.parent.mkdir()
    evidence.write_text(f"Measured for {TICKET}.\n", encoding="utf-8")
    _git(repo, "add", "-A")
    proc = _run_leg(repo, tmp_path)
    assert "LEG_PASSED" in proc.stdout, proc.stdout + proc.stderr


def test_a_gate_that_cannot_run_refuses_the_commit(repo, tmp_path):
    _git(repo, "rm", "-q", "--cached", "tools/public_artifact_hygiene.py")
    (repo / "tools" / "public_artifact_hygiene.py").unlink()
    proc = _run_leg(repo, tmp_path)
    assert proc.returncode == 1
    assert "LEG_PASSED" not in proc.stdout
    assert "GATE COULD NOT RUN" in proc.stdout


def test_a_public_clone_has_nothing_to_protect(repo, tmp_path):
    _git(repo, "rm", "-q", "--cached", "context.json")
    proc = _run_leg(repo, tmp_path)
    assert "LEG_PASSED" in proc.stdout, proc.stdout + proc.stderr


def _commit(repo: Path) -> None:
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-q", "-m", "base")


def test_deleting_context_json_from_a_private_tree_refuses_the_commit(repo, tmp_path):
    _commit(repo)
    _git(repo, "rm", "-q", "context.json")
    proc = _run_leg(repo, tmp_path)
    assert proc.returncode == 1
    assert "LEG_PASSED" not in proc.stdout
    assert "removes context.json" in proc.stdout


def test_a_private_tree_keeping_context_json_still_runs_the_gate(repo, tmp_path):
    _commit(repo)
    (repo / "context.json").write_text('{"a": 1}\n', encoding="utf-8")
    _git(repo, "add", "-A")
    proc = _run_leg(repo, tmp_path)
    assert "PUBLIC EXPORT GATE PASS" in proc.stdout
    assert "LEG_PASSED" in proc.stdout


def test_a_public_clone_with_history_commits_without_the_leg(repo, tmp_path):
    _git(repo, "rm", "-q", "--cached", "context.json")
    (repo / "context.json").unlink()
    _commit(repo)
    (repo / "docs" / "guide.md").write_text("# Guide\nMore.\n", encoding="utf-8")
    _git(repo, "add", "-A")
    proc = _run_leg(repo, tmp_path)
    assert "LEG_PASSED" in proc.stdout, proc.stdout + proc.stderr
    assert "public export gate" not in proc.stdout


def test_the_leg_has_no_opt_out_or_warn_mode():
    leg = _hook_leg()
    code = "\n".join(line for line in leg.splitlines() if not line.lstrip().startswith("#"))
    assert "DS_EXPORT_GATE" not in HOOK.read_text(encoding="utf-8")
    assert "${" not in code.replace("${DS_GUARD_PY", "")
    assert "exit 1" in code
    assert "|| true" not in code
