"""DS-P1.86: requirements.txt never pins a package outside what routertl requires.

Measured 2026-11-04: requirements.txt pinned ``cocotb-test==0.2.6`` while
routertl (5.2.0 and main) requires ``cocotb-test>=0.3``. In a fresh venv,
``pip install routertl==5.2.0`` then ``pip install -r requirements.txt`` was
ResolutionImpossible, so the README install step failed for every new user.
The bench never saw it: it runs an editable routertl with cocotb-test 0.3.0
already installed. ``check-constraints`` now refuses any such pin; the
DS-P2.109 gate leg runs it on the committed requirements.txt.
"""

from __future__ import annotations

import importlib.metadata as md
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "cocotb_dependency_floor.py"
REAL_REQUIREMENTS = Path(os.environ.get("DS_COCOTB_REQUIREMENTS") or (ROOT / "requirements.txt"))

_SPEC = importlib.util.spec_from_file_location("cocotb_dependency_floor", TOOL)
G = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(G)


def _run(*args: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(TOOL), *args], cwd=ROOT,
                          capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


def test_real_requirements_admit_every_routertl_requirement():
    conflicts = G.shared_pin_conflicts(
        REAL_REQUIREMENTS.read_text(encoding="utf-8").splitlines(),
        G.routertl_requirements())
    assert conflicts == [], (
        "requirements.txt excludes what the installed routertl requires, so "
        f"`pip install -r requirements.txt` beside it cannot resolve: {conflicts}")


def test_the_shipped_cocotb_test_pin_is_a_conflict():
    assert G.shared_pin_conflicts(
        ["cocotb-test==0.2.6"], [Requirement("cocotb-test>=0.3")]
    ) == [("cocotb-test==0.2.6", "cocotb-test>=0.3")]


def test_a_pin_inside_the_routertl_range_is_not_a_conflict():
    assert G.shared_pin_conflicts(
        ["cocotb-test>=0.3", "msgpack>=1.0.0  # comment"],
        [Requirement("cocotb-test>=0.3"), Requirement("msgpack>=1.0")]) == []


def test_package_names_are_compared_canonically():
    assert G.shared_pin_conflicts(
        ["Cocotb_Test==0.2.6"], [Requirement("cocotb-test>=0.3")]
    ) == [("Cocotb_Test==0.2.6", "cocotb-test>=0.3")]


def test_cocotb_itself_is_left_to_the_floor_checks():
    """``cocotb`` has its own floor logic (DS-P2.109); judging it twice would
    give the synthetic --routertl-floor tests a second, unrelated verdict."""
    assert G.shared_pin_conflicts(
        ["cocotb==2.0.1"], [Requirement("cocotb>=2.1")]) == []


def test_check_constraints_cli_refuses_the_shipped_pin(tmp_path):
    rt_names = {Requirement(r).name.lower() for r in (md.requires("routertl") or [])}
    # Every routertl this tree supports (the floor in requirements.txt and
    # later) requires cocotb-test, so a routertl without it is not a supported
    # host. Fail rather than skip: the conflict below would be unreachable.
    assert "cocotb-test" in rt_names, (
        "the installed routertl declares no cocotb-test requirement; this tree "
        "needs a routertl at or above its declared floor")
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("cocotb>=2.1,<3\ncocotb-test==0.2.6\n", encoding="utf-8")
    rc, out = _run("check-constraints", "--requirements", str(reqs))
    assert rc == 1, out
    assert "cocotb-test==0.2.6" in out and "DS-P1.86" in out, out
