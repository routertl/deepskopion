"""DS-T1.24: a fixture that scripts the routertl version follows the declared floor.

tools/golden_path/fixtures/plan_pass_zybo_canfd/plan.json scripts the routertl
version the scripted GP-R0 run reports, and that version must meet
tools/preflight/supported_environment.yml routertl.min. The guard for it lives
in tools/golden_path/tests, which a commit that changes only the declaration
does not run: DS-P1.69 raised the floor to 5.2.0 with the plan at 4.9.3, and
DS-P1.85 (e7715359) raised it to 5.3.0 with the plan at 5.2.0. Both times 52
golden_path tests went red on main at step 1, found by the nightly suite.

This test sits beside the declaration's own tests, so a floor change runs it
in the same commit gate.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
DECLARATION = ROOT / "tools/preflight/supported_environment.yml"
# Every committed fixture that scripts a routertl version for a run judged
# against the declaration.
FLOOR_FIXTURES = {
    "tools/golden_path/fixtures/plan_pass_zybo_canfd/plan.json":
        lambda doc: doc["toolchain"]["routertl_version"],
}


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in str(v).split("."))


def test_every_floor_fixture_meets_the_declared_floor():
    floor = str(yaml.safe_load(DECLARATION.read_text(encoding="utf-8"))["routertl"]["min"])
    low = {}
    for rel, get in FLOOR_FIXTURES.items():
        scripted = get(json.loads((ROOT / rel).read_text(encoding="utf-8")))
        if _ver(scripted) < _ver(floor):
            low[rel] = scripted
    assert not low, (
        f"declared routertl floor is {floor}, but these fixtures script a lower "
        f"version and every test derived from them fails at step 1: {low}. "
        "Raise them in the same commit as the floor.")


def test_the_fixture_list_still_names_files_that_script_a_version():
    """The guard must not go vacuous if a fixture moves or drops the field."""
    for rel, get in FLOOR_FIXTURES.items():
        path = ROOT / rel
        assert path.is_file(), f"{rel} is gone: update FLOOR_FIXTURES"
        assert get(json.loads(path.read_text(encoding="utf-8"))), f"{rel} scripts no version"
