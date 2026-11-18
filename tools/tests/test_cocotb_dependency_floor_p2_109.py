"""DS-P2.109 — regression guard for the cocotb dependency-floor ping-pong.

MEASURED DEFECT (2026-09-03): deepskopion ``requirements.txt`` pinned
``cocotb==2.0.1`` while the routertl distribution it depends on declares
``cocotb>=2.1`` (RTL-P3.1682). Both gates share ONE interpreter's
site-packages, so they downgraded/upgraded cocotb in turn and broke each
other — a ``==`` pin against a moving floor. The fix tracks the floor
(``cocotb>=2.1,<3``) and this test is the guard that keeps it consistent.

The guard under test is ``tools/cocotb_dependency_floor.py``. This module
drives it at the surface a caller actually reaches — the CLI exit code — for
both subcommands, and pins its POWER with the exact historical defect as a
synthetic input.

BASELINE (the exact shipped pin ``cocotb==2.0.1`` against routertl's
``>=2.1``): ``check-constraints`` and ``guard-install`` both exit 1. Proven
here by ``test_baseline_defect_pin_is_red`` — the guard is not vacuous.

FIXED (the repo's real ``requirements.txt`` after this ticket): both exit 0.
Proven by ``test_real_requirements_are_consistent``.

MUTATION (manually verified while writing this): break the satisfiability
probe in ``is_satisfiable`` to ``return True, Version("0")`` and the baseline
test goes GREEN — i.e. the test genuinely depends on the mechanism it guards.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "cocotb_dependency_floor.py"
# The pre-commit leg exports the STAGED requirements.txt and points the suite at
# it (DS_COCOTB_REQUIREMENTS) so the gate judges the index, not the worktree.
REAL_REQUIREMENTS = Path(os.environ.get("DS_COCOTB_REQUIREMENTS") or (ROOT / "requirements.txt"))
HOOK = ROOT / "hooks" / "pre-commit.local"

_SPEC = importlib.util.spec_from_file_location("cocotb_dependency_floor", TOOL)
assert _SPEC and _SPEC.loader
G = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(G)


def _run(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _write_reqs(tmp_path: Path, cocotb_line: str) -> Path:
    p = tmp_path / "requirements.txt"
    p.write_text(f"{cocotb_line}\ncocotb-bus==0.3.0\nnumpy\n", encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# POWER: the guard is RED against the exact shipped defect (BASELINE)
# --------------------------------------------------------------------------- #
def test_baseline_defect_pin_is_red(tmp_path: Path) -> None:
    """`cocotb==2.0.1` vs the routertl `>=2.1` floor must be refused."""
    reqs = _write_reqs(tmp_path, "cocotb==2.0.1")
    rc, out = _run(
        "check-constraints", "--requirements", str(reqs), "--routertl-floor", ">=2.1"
    )
    assert rc == 1, out
    assert "NOT mutually satisfiable" in out

    rc, out = _run(
        "guard-install",
        "--requirements",
        str(reqs),
        "--routertl-floor",
        ">=2.1",
        "--no-installed-check",
    )
    assert rc == 1, out
    assert "REFUSING to install" in out
    assert "2.0.1" in out  # names the offending version
    assert "cocotb>=2.1,<3" in out  # states the remedy


def test_loose_lower_bound_is_red_even_though_satisfiable(tmp_path: Path) -> None:
    """A range that *can* satisfy the floor but also admits versions BELOW it
    is still an install hazard: pip could resolve to a floor-violating build.

    check-constraints is GREEN (2.1 satisfies both) but guard-install is RED —
    this is the extra coverage guard-install adds over check-constraints."""
    reqs = _write_reqs(tmp_path, "cocotb>=1.6")
    rc, _ = _run(
        "check-constraints", "--requirements", str(reqs), "--routertl-floor", ">=2.1"
    )
    assert rc == 0

    rc, out = _run(
        "guard-install",
        "--requirements",
        str(reqs),
        "--routertl-floor",
        ">=2.1",
        "--no-installed-check",
    )
    assert rc == 1, out
    assert "violates the routertl floor" in out


# --------------------------------------------------------------------------- #
# FIXED: the real repo requirements.txt is consistent with routertl
# --------------------------------------------------------------------------- #
def test_real_requirements_are_consistent() -> None:
    """Against the installed routertl floor (whatever it is), the committed
    requirements.txt must be mutually satisfiable AND install-safe. This is the
    invariant the pre-commit gate arms."""
    rc, out = _run("check-constraints")
    assert rc == 0, out

    rc, out = _run("guard-install", "--no-installed-check")
    assert rc == 0, out


def test_real_requirements_bare_guard_install_is_green() -> None:
    """THE DOCUMENTED COMMAND. README.md tells users to run, before installing
    into a shared interpreter, the BARE `guard-install` (no --no-installed-check).
    The pre-commit gate must exercise the SAME command, so the doc and the gate
    can never disagree (landing refusal, DS-P2.109). The real requirements.txt
    is floor-safe, so this must exit 0 no matter what cocotb is installed now —
    a floor-violating installed version is the corrective-upgrade case, not a
    refusal."""
    rc, out = _run("guard-install")
    assert rc == 0, out
    assert "REFUSING to install" not in out


def test_real_pin_tracks_the_floor_not_an_equals() -> None:
    """The committed pin must be a range that tracks the floor, never a bare
    `==` — the defect class this ticket closes."""
    _, spec = G.read_requirements_cocotb(REAL_REQUIREMENTS)
    ops = {s.operator for s in spec}
    assert "==" not in ops, f"cocotb pinned with == again: {spec}"
    assert ">=" in ops, f"cocotb has no floor-tracking lower bound: {spec}"


def test_real_pin_admits_no_version_below_routertl_floor() -> None:
    """Whatever routertl declares as its floor, the committed requirements.txt
    must not admit any version below it (the guard-install subset property),
    checked against the ACTUAL installed routertl floor."""
    floor = G.routertl_cocotb_floor()
    _, ds_spec = G.read_requirements_cocotb(REAL_REQUIREMENTS)
    offender = G.version_admitted_but_violating(ds_spec, floor)
    assert offender is None, (
        f"requirements.txt cocotb{ds_spec} admits {offender}, "
        f"which violates the routertl floor cocotb{floor}"
    )


# --------------------------------------------------------------------------- #
# guard-install judges what the INSTALL WOULD DO, not the pre-existing state.
# Tested deterministically via --routertl-floor override so it does not depend
# on this bench's installed version, EXCEPT where noted below (the note leg
# reads the real installed cocotb — a floor of `>=999` makes whatever is
# installed count as violating, so the premise holds on any bench).
# --------------------------------------------------------------------------- #
def test_installed_violating_but_reqs_floor_safe_is_ALLOWED(tmp_path: Path) -> None:
    """THE REGRESSION (landing refusal, DS-P2.109): a floor-violating cocotb
    already in the shared site-packages must NOT gate a floor-safe install —
    that install is the corrective upgrade. Floor `>=999` guarantees whatever is
    installed violates it, while requirements `cocotb>=999` excludes it and is
    itself floor-safe. The gate must PERMIT (rc 0) and report the upgrade as a
    note, not refuse. This runs WITHOUT --no-installed-check, so it exercises
    the exact bare invocation the README and the pre-commit gate run."""
    reqs = _write_reqs(tmp_path, "cocotb>=999")
    rc, out = _run(
        "guard-install", "--requirements", str(reqs), "--routertl-floor", ">=999"
    )
    assert rc == 0, out
    assert "REFUSING to install" not in out
    assert "corrective action" in out  # reported as a note, not a violation


def test_reqs_admitting_violating_version_is_refused_with_installed_check(
    tmp_path: Path,
) -> None:
    """The gate must still REFUSE when requirements.txt itself admits a
    floor-violating version — and it must do so on the bare invocation (no
    --no-installed-check), because that is what users and the gate run. `>=1.6`
    admits 2.0.1 etc., below the `>=2.1` floor."""
    reqs = _write_reqs(tmp_path, "cocotb>=1.6")
    rc, out = _run(
        "guard-install", "--requirements", str(reqs), "--routertl-floor", ">=2.1"
    )
    assert rc == 1, out
    assert "REFUSING to install" in out
    assert "admits cocotb" in out
    assert "violates the routertl floor" in out


def test_installed_check_passes_when_installed_satisfies_floor(tmp_path: Path) -> None:
    """A floor of `>=0` is satisfied by any installed cocotb, so the gate must
    permit the install with no note and no refusal."""
    reqs = _write_reqs(tmp_path, "cocotb>=0")
    rc, out = _run(
        "guard-install", "--requirements", str(reqs), "--routertl-floor", ">=0"
    )
    assert rc == 0, out
    assert "corrective action" not in out


# --------------------------------------------------------------------------- #
# Fails CLOSED: a guard that cannot look is not a green guard (exit 2)
# --------------------------------------------------------------------------- #
def test_missing_cocotb_line_is_exit_2(tmp_path: Path) -> None:
    p = tmp_path / "requirements.txt"
    p.write_text("numpy\ncocotb-bus==0.3.0\n", encoding="utf-8")
    rc, out = _run(
        "check-constraints", "--requirements", str(p), "--routertl-floor", ">=2.1"
    )
    assert rc == 2, out
    assert "GATE COULD NOT RUN" in out


def test_missing_requirements_file_is_exit_2(tmp_path: Path) -> None:
    rc, out = _run(
        "guard-install",
        "--requirements",
        str(tmp_path / "nope.txt"),
        "--routertl-floor",
        ">=2.1",
        "--no-installed-check",
    )
    assert rc == 2, out
    assert "GATE COULD NOT RUN" in out


# --------------------------------------------------------------------------- #
# Unit-level satisfiability math (the mechanism the CLI tests ride on)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("specs", "expected"),
    [
        (["==2.0.1", ">=2.1"], False),          # the shipped defect
        ([">=2.1,<3", ">=2.1"], True),          # the fix
        ([">=2.1", ">=2.1", "<3"], True),
        (["==2.0.1", "==2.1.0"], False),
        (["<2.0", ">=2.1"], False),
    ],
)
def test_is_satisfiable_math(specs, expected) -> None:
    from packaging.specifiers import SpecifierSet

    ok, _ = G.is_satisfiable([SpecifierSet(s) for s in specs])
    assert ok is expected


def test_pre_commit_leg_is_armed_and_judges_the_index() -> None:
    """Arming guard: hooks/pre-commit.local must run BOTH legs against the
    STAGED requirements.txt. Deleting the DS-P2.109 block, or pointing either
    leg back at the worktree file, goes red here (the first two deliveries had
    a working detector that nothing armed, and a gate that read the worktree)."""
    text = HOOK.read_text(encoding="utf-8")
    assert "git show :requirements.txt" in text, "gate must read the STAGED requirements.txt"
    assert re.search(r"DS_COCOTB_REQUIREMENTS=\"\$ds_staged_reqs\".*test_cocotb_dependency_floor_p2_109\.py", text, re.S), \
        "pytest leg must be pointed at the staged file"
    assert re.search(r"guard-install\s*\\?\s*\n?\s*--requirements \"\$ds_staged_reqs\"", text), \
        "guard-install leg must judge the staged file"
    assert text.count("DS-P2.109") >= 3, "the DS-P2.109 gate block is missing"
