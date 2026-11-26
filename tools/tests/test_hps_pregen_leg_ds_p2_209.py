# rr unknown-llm (AI-generated)
"""DS-P2.209: the CI step that runs rr's DE25 HPS pre-synth generate on the installed RouteRTL.

The published routertl 5.2.0 has no ``sdk.engine.hps_regen`` (RTL-P2.2101), so it
cannot make the HPS this tree's ip.yml declares (DS-P1.85). tools/ci/hps_pregen_leg.py
runs the DS-P2.196 suite, which drives rr's real pre-synth step with a stand-in
Quartus, and both CI workflows run it as its own step. These tests run the step
end to end: green on the rr this bench has, red on an rr whose ``sdk.engine.hps_regen``
cannot be imported, which is what 5.2.0 is to this suite.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LEG = ROOT / "tools" / "ci" / "hps_pregen_leg.py"
WORKFLOWS = (ROOT / ".github" / "workflows" / "ci.yml", ROOT / ".forgejo" / "workflows" / "ci.yml")


def _leg_module():
    spec = importlib.util.spec_from_file_location("hps_pregen_leg", LEG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_leg(extra_pythonpath: Path | None = None,
             extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, **(extra_env or {}))
    if extra_pythonpath is not None:
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(extra_pythonpath), env.get("PYTHONPATH", "")) if p)
    return subprocess.run([sys.executable, str(LEG)], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=600)


def _junit(tmp_path: Path, cases: dict[str, str]) -> Path:
    body = {"passed": "", "skipped": "<skipped message='skip'/>",
            "failed": "<failure message='boom'/>", "error": "<error message='boom'/>"}
    xml = "".join(f'<testcase classname="x" name="{n}">{body[o]}</testcase>'
                  for n, o in cases.items())
    path = tmp_path / "junit.xml"
    path.write_text(f'<testsuites><testsuite name="pytest">{xml}</testsuite></testsuites>',
                    encoding="utf-8")
    return path


def test_leg_is_green_on_an_rr_that_has_the_hps_generator():
    pytest.importorskip("sdk.engine.hps_regen",
                        reason="the installed routertl has no HPS generator (RTL-P2.2101)")
    done = _run_leg()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "generate tests passed unskipped: 5/5" in done.stdout, done.stdout


def test_leg_is_red_when_the_installed_rr_cannot_import_the_hps_generator(tmp_path):
    """sdk.engine.hps_regen made unimportable, as it is in the published 5.2.0 wheel."""
    (tmp_path / "sitecustomize.py").write_text(textwrap.dedent("""\
        import sys

        class _NoHpsRegen:
            def find_spec(self, name, path=None, target=None):
                if name == "sdk.engine.hps_regen":
                    raise ModuleNotFoundError("No module named 'sdk.engine.hps_regen'", name=name)
                return None

        sys.meta_path.insert(0, _NoHpsRegen())
        """), encoding="utf-8")
    done = _run_leg(tmp_path)
    assert done.returncode == 1, (
        f"the HPS pre-synth step exited {done.returncode} on an rr without "
        f"sdk.engine.hps_regen:\n{done.stdout}{done.stderr}")
    assert "generate tests passed unskipped: 0/5" in done.stdout, done.stdout


def test_leg_is_red_when_a_generate_test_is_skipped(tmp_path):
    """The default CI command, not only verdict(): one generate test skipped and the
    rest of the suite green (pytest exits 0) must still exit the step 1. A step that
    trusted pytest's exit code would pass here, and a floor whose generate is skipped
    would prove nothing about the installed rr."""
    pytest.importorskip("sdk.engine.hps_regen",
                        reason="the installed routertl has no HPS generator (RTL-P2.2101)")
    (tmp_path / "ds_p2_209_skip_one.py").write_text(textwrap.dedent("""\
        import pytest

        def pytest_collection_modifyitems(items):
            for item in items:
                if item.name == "test_declared_hps_parameters_carry_the_full_io48_pin_mux":
                    item.add_marker(pytest.mark.skip(reason="DS-P2.209 forced skip"))
        """), encoding="utf-8")
    addopts = " ".join(o for o in (os.environ.get("PYTEST_ADDOPTS", ""),
                                   "-p ds_p2_209_skip_one") if o)
    done = _run_leg(tmp_path, {"PYTEST_ADDOPTS": addopts})
    out = done.stdout + done.stderr
    assert ", 1 skipped, 0 failed of " in done.stdout, (
        f"the forced skip did not leave the rest of the suite green:\n{out}")
    assert done.returncode == 1, (
        f"the HPS pre-synth step exited {done.returncode} with a generate test skipped:\n{out}")
    assert "generate tests passed unskipped: 4/5" in done.stdout, done.stdout
    assert "  test_declared_hps_parameters_carry_the_full_io48_pin_mux: skipped" in done.stdout, \
        done.stdout


def test_verdict_refuses_a_skipped_or_missing_generate_test(tmp_path):
    leg = _leg_module()
    names = list(leg.GENERATE_TESTS)
    all_passed = {n: "passed" for n in names}
    assert leg.verdict(_junit(tmp_path, all_passed))[0] == 0

    skipped = dict(all_passed, **{names[0]: "skipped"})
    rc, lines = leg.verdict(_junit(tmp_path, skipped))
    assert rc == 1, "a skipped generate test proves nothing about the installed rr"
    assert f"  {names[0]}: skipped" in lines

    missing = {n: "passed" for n in names[1:]}
    rc, lines = leg.verdict(_junit(tmp_path, missing))
    assert rc == 1, "a generate test that did not run proves nothing about the installed rr"
    assert f"  {names[0]}: not run" in lines

    errored = dict(all_passed, **{names[-1]: "error"})
    assert leg.verdict(_junit(tmp_path, errored))[0] == 1


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.parts[-3])
def test_ci_runs_the_hps_pre_synth_generate_on_both_legs(workflow):
    """The DE25 HPS generate is its own step, not only one test inside the desk run,
    so a floor without rr's HPS generator is red by name on the floor leg.

    Kept out of test_ci_routertl_floor_leg_ds_p2_209.py: the commit gate runs that
    file on every commit, which would refuse the fix-removed mutant this guard's
    proof needs (TIC-P2.588). CI runs this file in the desk tests on both legs."""
    job = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]["desk-checks"]
    assert set(job["strategy"]["matrix"]["routertl"]) == {"latest", "floor"}
    hps = [s for s in job["steps"]
           if s.get("run", "").strip() == "python tools/ci/hps_pregen_leg.py"]
    assert hps, f"no CI step in {workflow.relative_to(ROOT)} runs tools/ci/hps_pregen_leg.py"
    assert "if" not in hps[0], "the HPS pre-synth generate must run on the floor leg too"
