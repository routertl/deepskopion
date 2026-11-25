# rr unknown-llm (AI-generated)
"""DS-P2.335 (replaces the DS-P2.207 rule): no documented command rewrites a committed lock.

DS-P2.207 made every documented `rr target set` follow with `rr lock --update`,
when the project kept ONE routertl.lock and a target switch left it describing
the previous board. Since DS-P2.287 every target keeps its own committed
targets/<t>.routertl.lock, so after `rr target set` the lock already matches,
and `rr lock --update` only stamps the machine into a tracked file: on a clean
machine (routertl 5.5.0 from PyPI) it rewrote locked_at, sdk.commit ("unknown"
on a wheel) and simulation.version (the local nvc), and the tree read dirty.
Measured on a fresh clone of the export with 5.5.0: `rr target set
zybo_datalogger`, `rr pkg install`, then `rr lock --check` is "up to date"
(rc 0) with git status empty.

A lock write stays allowed for a target with no committed lock (a mix the
reader creates with `rr target clone`): that lock is the reader's new file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ("README.md", "CONTRIBUTING.md")
FENCE = re.compile(r"^```(?:bash|sh|shell)?\s*$\n(.*?)^```\s*$", re.M | re.S)
# Any `rr lock` that may write: everything except a read-only --check.
LOCK_WRITE = re.compile(r"^rr lock(?:\s|$)(?!.*--check\b)")
DEFAULT = "<project.yml default>"


def _commands(text: str) -> list[list[str]]:
    """Each fenced shell block as its list of command lines (comments dropped)."""
    blocks = []
    for body in FENCE.findall(text):
        lines = [line.split("#", 1)[0].strip() for line in body.splitlines()]
        blocks.append([line for line in lines if line])
    return blocks


def _committed_locks() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", "targets/*.routertl.lock"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return {Path(p).name[: -len(".routertl.lock")] for p in out}


def _tracked_lock_writes(text: str, committed: set[str]) -> list[str]:
    """`<target>: <command>` for each documented lock write that lands on a committed lock.

    The target is the last `rr target set` before the command in its block;
    with none, the command acts on project.yml's default target, whose lock is
    committed.
    """
    found = []
    for block in _commands(text):
        selected = DEFAULT
        for cmd in block:
            if cmd.startswith("rr target set "):
                selected = cmd.split()[3]
            elif LOCK_WRITE.match(cmd) and (selected == DEFAULT or selected in committed):
                found.append(f"{selected}: {cmd}")
    return found


@pytest.mark.parametrize("doc", DOCS)
def test_no_documented_command_rewrites_a_committed_lock(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert "rr target set" in text, f"{doc} no longer documents target selection"
    assert _tracked_lock_writes(text, _committed_locks()) == [], (
        f"{doc}: a documented command rewrites a committed lock, so a fresh clone "
        "following it ends with a dirty tree")


def test_the_three_products_have_committed_locks():
    """Without this the rule above would pass vacuously on an empty set."""
    assert {"zybo_datalogger", "de25_datalogger", "pfsoc_datalogger"} <= _committed_locks()


def test_the_readme_selects_the_board_before_installing_its_packages():
    block = next(b for b in _commands((ROOT / "README.md").read_text(encoding="utf-8"))
                 if "rr target set zybo_datalogger" in b and "rr synth run" in b)
    assert block.index("rr target set zybo_datalogger") < block.index("rr pkg install")


def test_the_check_finds_a_re_added_lock_update():
    """The mutant the old rule required: `rr lock --update` after a product selection."""
    committed = {"zybo_datalogger"}
    text = "```bash\nrr target set zybo_datalogger\nrr lock --update\nrr pkg install\n```\n"
    assert _tracked_lock_writes(text, committed) == ["zybo_datalogger: rr lock --update"]
    bare = "```bash\nrr pkg install\nrr lock\n```\n"
    assert _tracked_lock_writes(bare, committed) == [f"{DEFAULT}: rr lock"]


def test_a_read_only_check_and_a_new_targets_lock_are_allowed():
    committed = {"zybo_datalogger"}
    check = "```bash\nrr target set zybo_datalogger\nrr lock --check\n```\n"
    assert _tracked_lock_writes(check, committed) == []
    new_mix = "```bash\nrr target clone zybo_datalogger my_mix\nrr target set my_mix\nrr lock --update\n```\n"
    assert _tracked_lock_writes(new_mix, committed) == []
