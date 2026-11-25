# rr unknown-llm (AI-generated)
"""DS-P2.209: public CI runs the desk checks on exactly the declared RouteRTL floor.

Measured 2026-11-04 (DS-P1.85): the tree declared ``routertl>=5.2.0``, but its
ip.yml needs ``sdk.engine.hps_regen`` (RTL-P2.2101), which landed after the
5.2.0 tag. CI installed the newest wheel and stayed green; the published 5.2.0
wheel fails ``rr synth run`` on DE25. These guards keep a floor leg in CI and
keep every place that states the floor in agreement with it.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _floor_module():
    spec = importlib.util.spec_from_file_location(
        "routertl_floor", ROOT / "tools" / "ci" / "routertl_floor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FLOOR = _floor_module()


def _desk_job() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["desk-checks"]


def test_requirements_and_preflight_declare_one_floor():
    assert re.fullmatch(r"\d+\.\d+\.\d+", FLOOR.declared_floor())


@pytest.mark.parametrize("path, pattern", [
    ("README.md", r"\| RouteRTL \| `routertl >= ([0-9.]+)` \|"),
    ("docs/SUPPORTED_ENVIRONMENT.md", r"\| RouteRTL \| ≥ ([0-9.]+) \(`pip install routertl`\)"),
    ("docs/SUPPORTED_ENVIRONMENT.md", r"RouteRTL ≥ ([0-9.]+), Vivado"),
])
def test_documented_floor_is_the_declared_floor(path, pattern):
    found = re.findall(pattern, (ROOT / path).read_text(encoding="utf-8"))
    assert found, f"{path}: no RouteRTL floor statement matching {pattern!r}"
    assert set(found) == {FLOOR.declared_floor()}, (
        f"{path} states RouteRTL floor {sorted(set(found))}, the tree declares "
        f"{FLOOR.declared_floor()}")


def test_floors_that_disagree_are_refused(tmp_path):
    (tmp_path / "tools" / "preflight").mkdir(parents=True)
    (tmp_path / "requirements.txt").write_text("routertl>=5.3.0\n", encoding="utf-8")
    (tmp_path / "tools" / "preflight" / "supported_environment.yml").write_text(
        "routertl:\n  min: '5.2.0'\n", encoding="utf-8")
    with pytest.raises(FLOOR.FloorError, match="disagree"):
        FLOOR.declared_floor(tmp_path)


def test_two_floor_lines_are_refused():
    with pytest.raises(FLOOR.FloorError, match="exactly one"):
        FLOOR.requirements_floor("routertl>=5.2.0\nroutertl>=5.3.0\n")


def test_ci_runs_the_desk_checks_on_latest_and_on_the_floor():
    job = _desk_job()
    assert set(job["strategy"]["matrix"]["routertl"]) == {"latest", "floor"}
    assert job["strategy"].get("fail-fast") is False, (
        "a red floor leg must not cancel the latest leg, or neither result is read")
    steps = job["steps"]
    install = next(s for s in steps if "pip install" in s.get("run", "") and "routertl" in s["run"])
    run = install["run"]
    assert 'floor="$(python tools/ci/routertl_floor.py)"' in run
    assert 'spec="routertl==$floor"' in run, "the floor leg must install exactly the floor"
    assert 'spec="routertl>=$floor"' in run, "the latest leg must not go below the floor"
    assert "matrix.routertl" in run
    assert "git+" not in run, "public CI installs the published wheel, never a git URL"
    desk = next(s for s in steps if "run_desk_tests.sh" in s.get("run", ""))
    assert "if" not in desk, "the desk tests must run on both legs"


def test_ci_checks_the_lock_on_the_floor_leg():
    steps = _desk_job()["steps"]
    lock = [s for s in steps if s.get("run", "").strip() == "python tools/lock_check_leg.py --all"]
    assert lock and lock[0].get("if") == "matrix.routertl == 'floor'"
