# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.407: the commit gate judges the STAGED .gitignore rules against the STAGED pins.

ebd7d70d added a .gitignore rule without updating test_export_dotfiles_ds_p2_339's pinned count and hash, and main
went red unseen because no gate leg ran that guard. These tests cut the DS-P2.407 leg out of hooks/pre-commit.local
and run it in a scratch git repository holding the real guard, its helper modules and the repository's .gitignore,
with a real index: a staged rule is refused even when the working tree is clean, and a working-tree-only change is
not judged.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "pre-commit.local"
FILES = (".gitignore", "tools/tests/test_export_dotfiles_ds_p2_339.py", "tools/export_github.py",
         "tools/public_artifact_hygiene.py")


def _leg() -> str:
    m = re.search(r"^# ── DS-P2\.407 .*?(?=\n# ── |\Z)", HOOK.read_text(encoding="utf-8"), re.DOTALL | re.MULTILINE)
    assert m is not None, "DS-P2.407 leg missing from hooks/pre-commit.local"
    return m.group(0)


def _env(**extra: str) -> dict[str, str]:
    """No GIT_* from a calling hook (it would point git at the gate's own index)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "PYTEST_ADDOPTS"}
    env.update(extra)
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], env=_env(), check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    for rel in FILES:
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, r / rel)
    # The interpreter as the hook selects it: the RELATIVE .venv/bin/python3 of the repository.
    venv_py = r / ".venv" / "bin" / "python3"
    venv_py.parent.mkdir(parents=True)
    # DS_P2_407_EXTRA_ARGS lets a test append pytest options past the leg's own command line.
    venv_py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@" $DS_P2_407_EXTRA_ARGS\n', encoding="utf-8")
    venv_py.chmod(0o755)
    _git(r, "init", "-q")
    _git(r, "add", "-f", *FILES)
    return r


def _run_leg(repo: Path, **env: str) -> subprocess.CompletedProcess[str]:
    script = f'set -u\nDS_GUARD_PY=".venv/bin/python3"\n{_leg()}\necho LEG_PASSED\n'
    return subprocess.run(["bash", "-c", script], cwd=repo, env=_env(**env), text=True, capture_output=True,
                          check=False)


def _refused(proc: subprocess.CompletedProcess[str]) -> bool:
    return (proc.returncode == 1 and "LEG_PASSED" not in proc.stdout
            and "do not match their pins" in proc.stdout and "Commit aborted" in proc.stdout)


def _stage_gitignore(repo: Path, extra: str) -> None:
    """Stage .gitignore plus *extra*, then put the committed bytes back in the working tree."""
    path = repo / ".gitignore"
    clean = path.read_bytes()
    path.write_bytes(clean + extra.encode())
    _git(repo, "add", "-f", ".gitignore")
    path.write_bytes(clean)


def test_the_leg_passes_on_the_committed_rules(repo):
    proc = _run_leg(repo)
    assert proc.returncode == 0 and "LEG_PASSED" in proc.stdout, proc.stdout + proc.stderr


@pytest.mark.parametrize("extra", ["ds-p2-407-unpinned-rule/\n", " #ds-p2-407-pattern-not-a-comment\n", "\t\n",
                                   "\u00a0\n"], ids=["rule", "space-hash", "tab-only", "nbsp-only"])
def test_a_staged_rule_without_its_pin_is_refused_with_a_clean_working_tree(repo, extra):
    _stage_gitignore(repo, extra)
    assert (repo / ".gitignore").read_bytes() == (ROOT / ".gitignore").read_bytes()
    proc = _run_leg(repo)
    assert _refused(proc), proc.stdout + proc.stderr


def test_a_working_tree_only_rule_is_not_judged(repo):
    """Control for the index: the same rule, unstaged, leaves the staged rules matching their pins."""
    with (repo / ".gitignore").open("a", encoding="utf-8") as fh:
        fh.write("ds-p2-407-unstaged-rule/\n")
    proc = _run_leg(repo)
    assert proc.returncode == 0 and "LEG_PASSED" in proc.stdout, proc.stdout + proc.stderr


def test_unstaged_pins_cannot_pass_a_staged_rule(repo):
    """The working tree holds the rule AND matching pins; the index holds the rule with the old pins. The leg judges
    the index and refuses. Control: the guard run on the working tree passes, so the refusal is the index's."""
    extra = "ds-p2-407-unpinned-rule/\n"
    _stage_gitignore(repo, extra)
    mutant = (repo / ".gitignore").read_text(encoding="utf-8") + extra
    rules = [ln for ln in mutant.splitlines() if ln.strip() and not ln.startswith("#")]
    digest = hashlib.sha256("".join(r + "\n" for r in rules).encode()).hexdigest()
    guard = repo / "tools/tests/test_export_dotfiles_ds_p2_339.py"
    text = re.sub(r"(?m)^GITIGNORE_RULES = \d+", f"GITIGNORE_RULES = {len(rules)}", guard.read_text(encoding="utf-8"))
    text = re.sub(r'(?m)^GITIGNORE_RULES_SHA256 = "[0-9a-f]{64}"', f'GITIGNORE_RULES_SHA256 = "{digest}"', text)
    guard.write_text(text, encoding="utf-8")
    (repo / ".gitignore").write_text(mutant, encoding="utf-8")  # the working tree alone is consistent
    proc = _run_leg(repo)
    assert _refused(proc), proc.stdout + proc.stderr
    env = dict(_env(), PYTHONDONTWRITEBYTECODE="1")
    control = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                              "tools/tests/test_export_dotfiles_ds_p2_339.py::test_the_gitignore_rules_are_unchanged"],
                             cwd=repo, env=env, capture_output=True, text=True, check=False)
    assert control.returncode == 0, control.stdout + control.stderr


def test_a_guard_missing_from_the_index_fails_closed(repo):
    _git(repo, "rm", "-q", "--cached", "tools/export_github.py")
    proc = _run_leg(repo)
    assert proc.returncode == 1 and "GATE COULD NOT RUN" in proc.stdout, proc.stdout + proc.stderr


def test_a_line_of_spaces_is_not_a_rule(repo):
    """Control for the whitespace cases: git trims trailing spaces, so a line of spaces is no pattern and the pins
    still hold."""
    _stage_gitignore(repo, "   \n")
    proc = _run_leg(repo)
    assert proc.returncode == 0 and "LEG_PASSED" in proc.stdout, proc.stdout + proc.stderr


def test_inherited_pytest_options_cannot_skip_the_guard(repo):
    """PYTEST_ADDOPTS=--collect-only would collect the guard and run nothing; the leg clears it, so a stale pin is
    still refused and the committed rules still pass."""
    assert _run_leg(repo, PYTEST_ADDOPTS="--collect-only").returncode == 0
    _stage_gitignore(repo, "ds-p2-407-unpinned-rule/\n")
    proc = _run_leg(repo, PYTEST_ADDOPTS="--collect-only")
    assert _refused(proc), proc.stdout + proc.stderr


def test_a_guard_run_that_executes_nothing_is_not_a_pass(repo):
    """Options the leg cannot clear (here appended by the interpreter) that collect without running: exit 0 with no
    test passed is not a pass, so the leg fails closed."""
    proc = _run_leg(repo, DS_P2_407_EXTRA_ARGS="--collect-only")
    assert proc.returncode == 1 and "LEG_PASSED" not in proc.stdout, proc.stdout + proc.stderr
    assert "did not report 1 passed" in proc.stdout and "GATE COULD NOT RUN" in proc.stdout, proc.stdout


def test_a_staged_symlink_gitignore_is_refused(repo):
    """git does not read a symlinked .gitignore; a symlink whose target holds the pinned bytes must not pass."""
    (repo / "pinned_rules").write_bytes((ROOT / ".gitignore").read_bytes())
    _git(repo, "add", "-f", "pinned_rules")
    (repo / ".gitignore").unlink()
    (repo / ".gitignore").symlink_to("pinned_rules")
    _git(repo, "add", "-f", ".gitignore")
    proc = _run_leg(repo)
    assert _refused(proc) and "not a regular file (index mode 120000)" in proc.stdout, proc.stdout + proc.stderr


def test_a_leading_bom_keeps_the_pins(repo):
    """Control: git skips an initial UTF-8 BOM, so the same rules behind a BOM still match the pins."""
    path = repo / ".gitignore"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    _git(repo, "add", "-f", ".gitignore")
    proc = _run_leg(repo)
    assert proc.returncode == 0 and "LEG_PASSED" in proc.stdout, proc.stdout + proc.stderr


def test_a_smudge_filter_cannot_rewrite_the_judged_rules(repo):
    """A configured smudge filter that restores a removed rule must not make a staged deletion with stale pins pass:
    the leg judges the raw staged blob."""
    path = repo / ".gitignore"
    clean = path.read_text(encoding="utf-8")
    kept = [ln for ln in clean.split("\n") if ln != "!docs/captures/*.fst"]
    assert len(kept) == len(clean.split("\n")) - 1, "the pinned capture rule is not in .gitignore"
    (repo / ".gitattributes").write_text(".gitignore filter=ds407\n", encoding="utf-8")
    _git(repo, "add", "-f", ".gitattributes")
    pinned = repo.parent / "pinned.gitignore"  # the pinned bytes, which the filter emits whatever the blob holds
    pinned.write_text(clean, encoding="utf-8")
    restore = repo.parent / "restore.sh"
    restore.write_text(f'#!/bin/sh\ncat > /dev/null\ncat "{pinned}"\n', encoding="utf-8")
    restore.chmod(0o755)
    _git(repo, "config", "filter.ds407.smudge", str(restore))
    _git(repo, "config", "filter.ds407.clean", "cat")
    path.write_text("\n".join(kept), encoding="utf-8")
    _git(repo, "add", "-f", ".gitignore")
    proc = _run_leg(repo)
    assert _refused(proc), proc.stdout + proc.stderr


def test_no_scratch_directory_fails_closed_before_any_copy(repo, tmp_path):
    """mktemp failing (an unwritable TMPDIR) aborts the leg; nothing is written outside the repository."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        proc = _run_leg(repo, TMPDIR=str(blocked))
    finally:
        blocked.chmod(0o700)
    assert proc.returncode == 1 and "LEG_PASSED" not in proc.stdout, proc.stdout + proc.stderr
    assert "no scratch directory" in proc.stdout, proc.stdout
