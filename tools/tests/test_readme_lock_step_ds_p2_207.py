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
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]
# The board selection and build steps moved from the README to the guides it links (docs/getting_started/).
# Every guide is scanned; the ones that select a board must still document the selection.
GUIDES = tuple(p.relative_to(ROOT).as_posix() for p in sorted((p for p in (ROOT / "docs" / "getting_started").rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".markdown"))))
SELECTING = ("CONTRIBUTING.md", "docs/getting_started/zybo_z7_20.md", "docs/getting_started/de25.md",
             "docs/getting_started/polarfire_soc_discovery.md", "docs/getting_started/datalogger_mix.md")
DOCS = ("CONTRIBUTING.md", *GUIDES)
# Any `rr lock` that may write: everything except a read-only --check.
LOCK_WRITE = re.compile(r"^rr lock(?:\s|$)(?!.*--check\b)")
DEFAULT = "<project.yml default>"


# main's fence reading, kept as a second source so every block main read is still read.
FENCE = re.compile(r"^```(?:bash|sh|shell)?\s*$\n(.*?)^```\s*$", re.M | re.S)


def _lines(body: str) -> list[str]:
    """A listing's command lines, read as main reads them: one command per line, comment cut, blank lines dropped."""
    lines = [line.split("#", 1)[0].strip() for line in body.splitlines()]
    return [line for line in lines if line]


def _commands(text: str) -> list[list[str]]:
    """Each shell code block a reader sees, as CommonMark renders it: shell fences (also tilde, and left open at the
    end of a file) and indented code blocks. Each line is read as main's check reads it; _tracked_lock_writes also
    reads main's own fence matches."""
    bodies = []
    for tok in MarkdownIt("commonmark").parse(text):
        if tok.type == "fence" and (tok.info.split() or [""])[0] in ("", "bash", "sh", "shell"):
            bodies.append(tok.content)
        elif tok.type == "code_block":
            bodies.append(tok.content)
    return [_lines(body) for body in bodies]


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
    # main's rule, line by line, over the CommonMark blocks and over main's own fence reading, so every write main
    # reports is still reported (T1 rv-a0a3114da9).
    for block in _commands(text) + [_lines(body) for body in FENCE.findall(text)]:
        selected = DEFAULT
        for cmd in block:
            if cmd.startswith("rr target set "):
                selected = cmd.split()[3]
            elif LOCK_WRITE.match(cmd) and (selected == DEFAULT or selected in committed):
                found.append(f"{selected}: {cmd}")
    return list(dict.fromkeys(found))


@pytest.mark.parametrize("doc", DOCS)
def test_no_documented_command_rewrites_a_committed_lock(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert set(SELECTING) <= set(DOCS), "a selecting guide is no longer scanned"
    if doc in SELECTING:
        assert "rr target set" in text, f"{doc} no longer documents target selection"
    assert _tracked_lock_writes(text, _committed_locks()) == [], (
        f"{doc}: a documented command rewrites a committed lock, so a fresh clone "
        "following it ends with a dirty tree")


def test_the_readme_documents_no_lock_write():
    """The README keeps only the simulation quick start; it must not rewrite a committed lock either."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "rr sim run" in text, "README.md no longer documents the simulation quick start"
    assert _tracked_lock_writes(text, _committed_locks()) == []


def test_the_three_products_have_committed_locks():
    """Without this the rule above would pass vacuously on an empty set."""
    assert {"zybo_datalogger", "de25_datalogger", "pfsoc_datalogger"} <= _committed_locks()


def test_the_readme_selects_the_board_before_installing_its_packages():
    text = (ROOT / "docs/getting_started/zybo_z7_20.md").read_text(encoding="utf-8")
    # Every build recipe in the guide, from either reading, must select first (T1 rv-5960ee971c: an example placed
    # before the recipe must not stand in for it).
    # A build recipe is any block that builds (rr synth run), found independently of the selection, so a recipe
    # that lost its selection is still judged (T1 rv-a89f9b8661).
    recipes = [b for b in _commands(text) + [_lines(body) for body in FENCE.findall(text)] if "rr synth run" in b]
    assert recipes, "the Zybo guide has no build recipe"
    for block in recipes:
        assert "rr target set zybo_datalogger" in block, f"a Zybo build recipe does not select the board: {block}"
        assert block.index("rr target set zybo_datalogger") < block.index("rr pkg install")


def test_the_check_finds_a_re_added_lock_update():
    """The mutant the old rule required: `rr lock --update` after a product selection."""
    committed = {"zybo_datalogger"}
    text = "```bash\nrr target set zybo_datalogger\nrr lock --update\nrr pkg install\n```\n"
    assert _tracked_lock_writes(text, committed) == ["zybo_datalogger: rr lock --update"]
    bare = "```bash\nrr pkg install\nrr lock\n```\n"
    assert _tracked_lock_writes(bare, committed) == [f"{DEFAULT}: rr lock"]


def test_a_lock_update_in_an_unclosed_last_fence_is_found():
    """Control (T1 rv-a54f8ac5db): the last block of a guide with its closing fence removed still renders."""
    text = "# Guide\n\n```bash\nrr target set zybo_datalogger\nrr lock --update\n"
    assert _tracked_lock_writes(text, {"zybo_datalogger"}) == ["zybo_datalogger: rr lock --update"]
    # Indented code renders as copyable code too (T1 rv-f2fc046c84).
    indented = "# Guide\n\n    rr target set zybo_datalogger\n    rr lock --update\n"
    assert _tracked_lock_writes(indented, {"zybo_datalogger"}) == ["zybo_datalogger: rr lock --update"]
    # Lines are read as main reads them: a comment ending in a backslash does not swallow the next command
    # (T1 rv-a00ad460bc), and a selection that does not start its line is not taken (T1 rv-bbbf510158,
    # rv-045fb119b4); an indented selection in a loop body is (T1 rv-4e5f8b78e3).
    swallowed = "```bash\nrr target set zybo_datalogger  # select \\\nrr lock --update\n```\n"
    assert _tracked_lock_writes(swallowed, {"zybo_datalogger"}) == ["zybo_datalogger: rr lock --update"]
    skipped = "```bash\nfalse && rr target set my_mix\nrr lock --update\n```\n"
    assert _tracked_lock_writes(skipped, {"zybo_datalogger"}) == [f"{DEFAULT}: rr lock --update"]
    behind_then = "```bash\nif false; then rr target set my_mix; fi\nrr lock --update\n```\n"
    assert _tracked_lock_writes(behind_then, {"zybo_datalogger"}) == [f"{DEFAULT}: rr lock --update"]
    in_body = "```bash\nif false; then\n  :; rr target set my_mix\nfi\nrr lock --update\n```\n"
    assert _tracked_lock_writes(in_body, {"zybo_datalogger"}) == [f"{DEFAULT}: rr lock --update"]
    looped = ("```bash\nrr target set my_mix\nfor step in once; do\n  rr target set zybo_datalogger\ndone\n"
              "rr lock --update\n```\n")
    assert _tracked_lock_writes(looped, {"zybo_datalogger"}) == ["zybo_datalogger: rr lock --update"]
    # T1 rv-ebddd1af57: two trailing backslashes are an escaped backslash, not a continuation.
    escaped = "```bash\nrr target set zybo_datalogger\nprintf '%s\\n' x\\\\\nrr lock --update\n```\n"
    assert _tracked_lock_writes(escaped, {"zybo_datalogger"}) == ["zybo_datalogger: rr lock --update"]
    # No command is made up from a backslash-newline: `rr\\` + newline + `pkg install` is never `rr pkg install`
    # (T1 rv-ebddd1af57).
    assert "rr pkg install" not in _commands("```bash\nrr\\\npkg install\n```\n")[0]


def test_a_read_only_check_and_a_new_targets_lock_are_allowed():
    committed = {"zybo_datalogger"}
    check = "```bash\nrr target set zybo_datalogger\nrr lock --check\n```\n"
    assert _tracked_lock_writes(check, committed) == []
    new_mix = "```bash\nrr target clone zybo_datalogger my_mix\nrr target set my_mix\nrr lock --update\n```\n"
    assert _tracked_lock_writes(new_mix, committed) == []
