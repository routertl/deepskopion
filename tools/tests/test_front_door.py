# rr unknown-llm (AI-generated)
"""DS-C0 (DS-P2.88) — the visible front door of a fresh clone.

A stranger's first `ls` after `git clone` must show no agent config, debug
scratch, or logs at the top level, and none of it may silently come back:
untracked scratch that isn't gitignored will get committed again the first
time someone runs a broad `git add`.

This test is the regression guard: it runs `git ls-files`/`git check-ignore`
against the real repo, so it fails the moment either invariant breaks again -
a re-added file, or a gitignore pattern that gets deleted.

DS-T2.75 refinement: `debug/` is NOT pure agent scratch — it also holds the
project's TRACKED RouteRTL debug-core declarations (`debug/<name>.yml`) that
`rr ila` / `rr hw bit-sha` resolve the on-chip REA node from (the same
convention rr-vfifo / examples/de25_rea_demo use). So the ban on `debug/` is
now scoped: untracked scratch under `debug/` must stay gitignored, but tracked
`debug/*.yml` core files are a legitimate front-door artifact. The bare-`debug`
blanket ban is replaced by that scoped pair of checks below.
"""
from __future__ import annotations

import pathlib
import subprocess

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Scratch that must never be visible at the tracked repo root (DS-P2.88).
# Not exhaustive of "logs" or "debug" everywhere - just the specific paths
# that were tracked at the root when this ticket was measured. `debug` is NOT
# here: it is handled by the scoped debug-core checks (DS-T2.75), which still
# ban debug SCRATCH while allowing tracked debug/*.yml core declarations.
_BANNED_ROOT_PATHS = [
    "GEM" + "INI.md",
    "debug_cocotbext.py",
    "logs",
]


def _git(*args):
    return subprocess.run(
        ["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True, check=False
    )


def _tracked_root_entries():
    out = _git("ls-files").stdout
    return {line.split("/", 1)[0] for line in out.splitlines() if line}


def test_no_agent_config_debug_or_logs_tracked_at_root():
    tracked = _tracked_root_entries()
    leaked = tracked & set(_BANNED_ROOT_PATHS)
    assert not leaked, (
        f"scratch/agent-config tracked at repo root: {sorted(leaked)} "
        "(DS-P2.88 front-door regression)"
    )


def test_banned_root_paths_are_gitignored():
    # If one of these paths exists untracked on disk (e.g. a local agent
    # regenerates agent scratch files), git must ignore it - otherwise it comes back
    # the next time someone runs a broad `git add`.
    for name in _BANNED_ROOT_PATHS:
        # Trailing slash tells git to treat the arg as a directory even when
        # nothing currently exists on disk at that path.
        probe = name if "." in name else f"{name}/"
        result = _git("check-ignore", "-q", probe)
        assert result.returncode == 0, (
            f"{name!r} is not covered by .gitignore at the repo root "
            "(DS-P2.88: untracked scratch must be gitignored so it can't "
            "silently return)"
        )


def test_pfsoc_linux_build_dirs_are_gitignored():
    """PF-L0 boot session (DS-P2.125): the pfsoc rr-linux build dirs stay ignored.

    `rr linux hss|uboot|kernel` clones hart-software-services / u-boot / linux into
    ``sw/linux/pfsoc/src/`` and writes artifacts to ``sw/linux/pfsoc/out/`` — multi-GB
    output that must never be tracked. ``sw/linux/src/`` and ``sw/linux/out/`` do NOT
    cover those nested paths, so the pfsoc pair was added to .gitignore. That add
    landed unguarded: deleting the two lines left the suite green, so a first
    ``rr linux uboot`` clone becomes stage-able again unnoticed. These probes pin it.
    """
    for path in ("sw/linux/pfsoc/src/", "sw/linux/pfsoc/out/"):
        result = _git("check-ignore", "-q", path)
        assert result.returncode == 0, (
            f"{path!r} is not gitignored — a first `rr linux` clone/build writes "
            "multi-GB output there that a broad `git add` would stage (DS-P2.125)."
        )


def test_debug_dir_tracks_only_debug_core_yml_not_scratch():
    """DS-T2.75: `debug/` holds tracked RouteRTL debug-core declarations
    (`debug/<name>.yml`) AND must still keep agent scratch out of the front
    door. Two teeth: (a) every TRACKED path under `debug/` is a `.yml` core
    file — no scratch `.py`/`.log`/`.vcd` may sneak in; (b) untracked scratch
    under `debug/` is still gitignored so a broad `git add` can't re-leak it,
    while a debug-core `*.yml` is explicitly NOT ignored (so it can be
    committed)."""
    tracked_under_debug = [
        line for line in _git("ls-files").stdout.splitlines()
        if line.startswith("debug/")
    ]
    non_yml = [p for p in tracked_under_debug if not p.endswith(".yml")]
    assert not non_yml, (
        f"non-core scratch tracked under debug/: {non_yml} — only "
        "debug/<name>.yml debug-core declarations belong there (DS-T2.75)."
    )

    # The deliverable itself must be present and tracked. Without this the
    # test passed with debug/de25_datalogger.yml DELETED (the empty-set checks
    # above are vacuously true, and the gitignore-pattern check below asks
    # about a path, not a file) — the exact hole the landing review found. A
    # deleted core file means `rr hw bit-sha` has no node to resolve, so the
    # whole ticket regresses; guard the file's existence, not just its policy.
    assert "debug/de25_datalogger.yml" in tracked_under_debug, (
        "debug/de25_datalogger.yml must exist and be git-tracked — it is the "
        "REA debug-core declaration `rr hw bit-sha`/`rr ila` resolve the "
        "on-chip node from (DS-T2.75). Deleting it silently un-does the ticket."
    )
    # `git ls-files` lists a tracked path even after `rm` (an unstaged
    # deletion is still in the index) — the landing review reproduced this
    # guard staying GREEN with the file deleted. Ask the filesystem too.
    core_file = _REPO_ROOT / "debug" / "de25_datalogger.yml"
    assert core_file.is_file(), (
        f"{core_file} is tracked but ABSENT from the worktree — a deleted "
        "debug-core declaration leaves `rr hw bit-sha` nothing to resolve (DS-T2.75)."
    )

    # (b) scratch under debug/ stays ignored; a core *.yml does not.
    scratch = _git("check-ignore", "-q", "debug/agent_scratch.log")
    assert scratch.returncode == 0, (
        "untracked scratch under debug/ must be gitignored (DS-P2.88) — the "
        "/debug/* rule covers it."
    )
    core_yml = _git("check-ignore", "-q", "debug/de25_datalogger.yml")
    assert core_yml.returncode == 1, (
        "debug/<name>.yml debug-core declarations must NOT be gitignored — "
        "the !/debug/*.yml exception must keep them trackable (DS-T2.75)."
    )
