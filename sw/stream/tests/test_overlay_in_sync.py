"""Overlay-drift guard (DS-P3.112), rebased on the DS-P2.81 staging model.

The board-deployed daemon is a COPY of ``sw/stream/`` baked into
``sw/linux/rootfs_overlay/usr/local/share/deepskopion/sw/stream/`` by
``sw/linux/stage_daemon.sh`` (rsync, excluding ``tests/`` / ``__pycache__`` /
``*.pyc``). That is how the CAN-FD path (``can_reassembler.py`` + the FD-aware
``data_source.py``) never reached silicon: the deployed daemon predated the
whole feature.

WHAT CHANGED, AND WHY THIS FILE HAD TO CHANGE WITH IT. That copy used to be
COMMITTED, and this guard's job was to prove the committed copy matched. DS-P2.81
untracked and gitignored it — it is a BUILD ARTEFACT now — and each target
declares ``linux.rootfs.stage_command``, so ``rr linux rootfs`` re-stages it
itself and ABORTS if staging fails (RTL-P1.117). This file was not updated with
it, which left two defects: on a fresh clone the artefact does not exist yet, so
every file read as MISSING and the guard failed for the most normal state a
checkout can be in; and its remedy line still told you to ``git add`` a tree
that is now ignored.

So the coverage is split to match where the truth now lives:

* absent artefact = not built yet, which is not drift — SKIP.
* present artefact must still be byte-identical: a stale tree on disk misleads
  anyone who inspects it to answer "what is on the board?".
* the structural guarantee that REPLACED "the copy is committed" — every target
  that builds a Linux rootfs declares ``stage_command`` — is asserted directly,
  below. Without it a new target silently skips staging and DS-P3.112 returns
  with nothing to catch it.

Re-stage with ``bash sw/linux/stage_daemon.sh`` (no ``git add`` — it is ignored).

Mirrors the rsync contract in ``stage_daemon.sh`` exactly — keep the two in
step if the exclude set ever changes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# tests/ -> stream/ -> sw/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "sw" / "stream"
_DST = (
    _REPO_ROOT
    / "sw" / "linux" / "rootfs_overlay"
    / "usr" / "local" / "share" / "deepskopion"
    / "sw" / "stream"
)
_TARGETS_DIR = _REPO_ROOT / "targets"
_STAGE_SCRIPT = "stage_daemon.sh"

# DS-P2.264: bench tool images (deployment_contract.yml ``tool_images``) carry no
# daemon by design. The exemption is READ from the contract, never named here,
# and holds only while the contract checker accepts each tool image (autostart
# none, no etc/init.d/S*, etc/rc*.d/ or inittab in its overlay). The import
# needs yaml; if it is missing this file errors at collection, it never passes.
sys.path.insert(0, str(_REPO_ROOT / "sw" / "linux"))
import deployment_contract as _C                           # noqa: E402

# stage_daemon.sh excludes tests/ / __pycache__ / *.pyc. We additionally skip
# .pytest_cache (a run-to-run cache dir) and guard only *.py — the daemon code
# whose drift actually breaks the deployed daemon (the DS-P3.112 footgun).
# testbench/ IS staged onto the board (on-silicon self-test harness), so it is
# guarded too.
_EXCLUDE_DIRS = {"tests", "__pycache__", ".pytest_cache"}


def _staged_sources() -> list[Path]:
    """Every .py file stage_daemon.sh would copy, relative to sw/stream."""
    out: list[Path] = []
    for p in _SRC.rglob("*.py"):
        if not p.is_file():
            continue
        rel = p.relative_to(_SRC)
        if any(part in _EXCLUDE_DIRS for part in rel.parts):
            continue
        out.append(rel)
    return out


def test_board_overlay_matches_daemon_source():
    """A PRESENT staged tree must be byte-identical to sw/stream/.

    Skipped when the tree does not exist: it is a build artefact (DS-P2.81),
    and "never built in this checkout" is not drift. A tree that IS there and
    disagrees is still loud — it is what anyone inspecting the repo will read
    as "the code on the board".
    """
    if not _DST.exists():
        pytest.skip(
            "staged daemon tree not built in this checkout — it is a gitignored "
            "build artefact (DS-P2.81); `rr linux rootfs` stages it. Nothing to "
            "compare, and an unbuilt artefact is not drift."
        )

    missing: list[str] = []
    stale: list[str] = []

    for rel in _staged_sources():
        dst = _DST / rel
        if not dst.exists():
            missing.append(str(rel))
        elif dst.read_bytes() != (_SRC / rel).read_bytes():
            stale.append(str(rel))

    problems = []
    if missing:
        problems.append("MISSING from overlay: " + ", ".join(sorted(missing)))
    if stale:
        problems.append("STALE in overlay: " + ", ".join(sorted(stale)))

    assert not problems, (
        "Board daemon overlay has drifted from sw/stream/ — the deployed "
        "daemon would run stale code (DS-P3.112). Re-stage:\n"
        "    bash sw/linux/stage_daemon.sh\n"
        "(do NOT `git add` it — the staged tree is gitignored, DS-P2.81.)\n  "
        + "\n  ".join(problems)
    )


def _targets_with_a_linux_rootfs() -> list[Path]:
    """Target ymls that build a Linux rootfs, so must stage the daemon into it.

    Parsed as text rather than via yaml: this guard must not go quiet just
    because a parser or a dependency is missing in some environment, and the
    thing being asserted is the literal presence of a declaration.
    """
    out: list[Path] = []
    # DS-P1.103: products in targets/, the rest in bench/targets/ (the contract checker's own scan).
    for yml in _C._target_files(_REPO_ROOT):
        if "rootfs:" in yml.read_text(encoding="utf-8", errors="replace"):
            out.append(yml)
    return out


def _tool_image_targets(decl: dict | None = None, root: Path = _REPO_ROOT) -> set[str]:
    """Target names the contract and its sidecars declare as bench tool images (DS-P2.264, DS-P1.103)."""
    names, fails = _C.all_tool_images(_C.load_declaration() if decl is None else decl, root)
    assert not fails, "a declared tool image fails the deployment contract, so it " \
        "gets no daemon exemption: " + "; ".join(f"{f.what}: {f.detail}" for f in fails)
    return names


def _missing_stage(targets: list[Path], exempt: set[str]) -> list[str]:
    return [
        t.name for t in targets
        if t.stem not in exempt
        and _STAGE_SCRIPT not in t.read_text(encoding="utf-8", errors="replace")
    ]


def test_every_linux_target_stages_the_daemon_into_its_rootfs():
    """The invariant that REPLACED "the staged copy is committed" (DS-P2.81).

    Staging is no longer a step anyone remembers: each target declares
    ``linux.rootfs.stage_command`` and `rr linux rootfs` runs it, aborting on
    failure (RTL-P1.117). A target that omits it builds an image with a stale
    (or absent) daemon while every build command reports success — the exact
    DS-P3.112 failure, which put a five-week-old daemon on the board.
    """
    # DS-P1.103 split: the shipped products, with no exemption. The bench targets, with the
    # tool-image exemption, run in the _bench.py sibling.
    targets = [t for t in _targets_with_a_linux_rootfs() if t.parent.resolve() == (_REPO_ROOT / "targets").resolve()]
    assert targets, (
        f"no target under {_TARGETS_DIR} declares a linux rootfs — either the "
        "targets moved or this guard is now looking in the wrong place, and "
        "either way it is no longer guarding anything"
    )

    missing = _missing_stage(targets, set())
    assert not missing, (
        "target(s) build a Linux rootfs without staging the daemon into it: "
        f"{', '.join(missing)}.\n"
        "Add to the target's linux.rootfs block:\n"
        f"    stage_command: ./sw/linux/{_STAGE_SCRIPT}\n"
        "Without it the image gets whatever stale copy happens to be on disk "
        "(DS-P3.112 / DS-P2.81)."
    )


@pytest.mark.parametrize("plant", ["etc/init.d/S99daemon", "etc/rc5.d/S01x", "etc/inittab"])
def test_a_tool_image_that_autostarts_is_refused_so_gets_no_exemption(tmp_path, plant):
    """DS-P2.264 red-first: a tool image whose overlay ships an autostart file is
    refused by the contract checker, and _tool_image_targets() then refuses the
    exemption rather than hiding a daemon-less normal image."""
    (tmp_path / "ov" / plant).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "ov" / "init").write_text("#!/bin/sh\n")
    (tmp_path / "ov" / plant).write_text("#!/bin/sh\n")
    decl = {"tool_images": [{"target": "x", "overlay": "ov", "pid1": "init", "autostart": "none"}]}
    _names, fails = _C._tool_images(decl, tmp_path)
    assert any(plant in f.what for f in fails), fails
    decl["tool_images"][0]["autostart"] = "S99daemon"
    assert any("autostart must be 'none'" in f.detail for f in _C._tool_images(decl, tmp_path)[1])
    with pytest.raises(AssertionError, match="gets no daemon exemption"):
        _tool_image_targets(decl, tmp_path)
