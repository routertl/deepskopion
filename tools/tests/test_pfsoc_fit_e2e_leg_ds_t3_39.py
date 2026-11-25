# rr unknown-llm (AI-generated)
"""DS-T3.39: a named CI leg runs the PolarFire fit e2e guard unskipped.

tools/tests/test_pfsoc_fit_e2e_ds_p2_151.py needs DS_P2_151_E2E_IMPL=1,
Microchip Libero SoC 2025.2 at the tool_path declared in
targets/pfsoc_datalogger.yml, and the FlexLM daemon on 1702@localhost. Without
DS_P2_151_E2E_IMPL=1 the test skips, and no CI or scheduled leg set that
variable or ran the suite.

The leg tools/ci/pfsoc_fit_e2e_leg.py verifies that rr and the configured
Libero binary exist, exports DS_P2_151_E2E_IMPL=1 and LM_LICENSE_FILE with
1702@localhost, runs the suite, prints the counts, and fails when any test in
the suite fails or when test_pfsoc_fit_reaches_constrained_timing is skipped or
did not run. .forgejo/workflows/ci.yml runs it as the job `pfsoc-fit-e2e` on
`bench-host`, and hooks/pre-commit.local runs this guard in its DS-P2.197 leg.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
LEG = ROOT / "tools/ci/pfsoc_fit_e2e_leg.py"
FORGE = ROOT / ".forgejo/workflows/ci.yml"
HOOK = ROOT / "hooks/pre-commit.local"

CLASS = "tools.tests.test_pfsoc_fit_e2e_ds_p2_151"
FIT_E2E_TEST = "test_pfsoc_fit_reaches_constrained_timing"
SKIP_REASON = (
    "DS-T3.39: e2e Libero fit — set DS_P2_151_E2E_IMPL=1 "
    "(needs the bench Libero + license)"
)


def _case(name: str, outcome: str = "passed") -> str:
    body = {
        "passed": "",
        "skipped": f'<skipped type="pytest.skip" message="{SKIP_REASON}"/>',
        "failed": '<failure message="assert 1 == 2">assert 1 == 2</failure>',
    }[outcome]
    return f'<testcase classname="{CLASS}" name="{name}" time="0.1">{body}</testcase>'


def _junit_text(cases: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites>'
        '<testsuite name="pytest">' + "".join(cases) + "</testsuite></testsuites>"
    )


def _junit(tmp_path: Path, cases: list[str]) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(_junit_text(cases), encoding="utf-8")
    return path


def _verdict(junit: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LEG), "verdict", str(junit)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_leg_passes_and_prints_the_counts_when_fit_e2e_ran(tmp_path: Path) -> None:
    junit = _junit(tmp_path, [_case(FIT_E2E_TEST)])
    run = _verdict(junit)
    out = run.stdout + run.stderr
    assert run.returncode == 0 and (
        "1 passed, 0 skipped, 0 failed of 1" in run.stdout
        and "fit-e2e tests passed unskipped: 1/1" in run.stdout
    ), (
        "the leg must pass and print the counts when fit-e2e ran: "
        f"rc={run.returncode}\n{out}"
    )


def test_leg_fails_when_fit_e2e_test_is_skipped(tmp_path: Path) -> None:
    junit = _junit(tmp_path, [_case(FIT_E2E_TEST, "skipped")])
    run = _verdict(junit)
    out = run.stdout + run.stderr
    assert run.returncode == 1 and f"{FIT_E2E_TEST}: skipped" in run.stdout, (
        f"the leg accepted a skipped fit-e2e test: rc={run.returncode}\n{out}"
    )


def test_leg_fails_when_fit_e2e_test_did_not_run(tmp_path: Path) -> None:
    junit = _junit(tmp_path, [_case("test_other_fit_helper")])
    run = _verdict(junit)
    out = run.stdout + run.stderr
    assert run.returncode == 1 and f"{FIT_E2E_TEST}: not run" in run.stdout, (
        f"the leg accepted a run without the fit-e2e test: rc={run.returncode}\n{out}"
    )


def test_leg_fails_when_a_test_in_the_suite_failed(tmp_path: Path) -> None:
    junit = _junit(
        tmp_path,
        [
            _case(FIT_E2E_TEST),
            _case("test_other_fit_helper", "failed"),
        ],
    )
    run = _verdict(junit)
    out = run.stdout + run.stderr
    assert run.returncode == 1 and "1 passed, 0 skipped, 1 failed of 2" in run.stdout, (
        f"the leg passed a suite with a failed test: rc={run.returncode}\n{out}"
    )


def _load_leg():
    """Load tools/ci/pfsoc_fit_e2e_leg.py, or return None if absent."""
    if not LEG.is_file():
        return None
    spec = importlib.util.spec_from_file_location("pfsoc_fit_e2e_leg", LEG)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_leg_enforces_prerequisites_and_sets_fit_e2e_env_before_running_suite(
    tmp_path: Path, monkeypatch
) -> None:
    """The leg must verify rr and the configured Libero binary exist, export
    DS_P2_151_E2E_IMPL=1 and LM_LICENSE_FILE with 1702@localhost, and run
    check_prerequisites before invoking the pytest suite."""
    leg = _load_leg()
    calls: list[tuple] = []
    rc = None
    env_default: dict[str, str] = {}
    env_chained: dict[str, str] = {}
    missing_rr_err = ""
    missing_libero_err = ""

    if leg is not None:
        fake_tool_root = tmp_path / "libero_root"
        fake_libero = fake_tool_root / "Designer" / "bin64" / "libero"
        fake_target = tmp_path / "pfsoc_datalogger.yml"
        fake_target.write_text(
            yaml.safe_dump({"hardware": {"tool_path": str(fake_tool_root)}}),
            encoding="utf-8",
        )

        monkeypatch.setattr(leg.shutil, "which", lambda name: None)
        try:
            leg.check_prerequisites(fake_target)
        except RuntimeError as exc:
            missing_rr_err = str(exc)

        monkeypatch.setattr(leg.shutil, "which", lambda name: "/usr/bin/rr")
        try:
            leg.check_prerequisites(fake_target)
        except RuntimeError as exc:
            missing_libero_err = str(exc)

        fake_libero.parent.mkdir(parents=True, exist_ok=True)
        fake_libero.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_libero.chmod(0o755)
        prereq_pair = leg.check_prerequisites(fake_target)

        env_default = leg.suite_env({})
        env_chained = leg.suite_env({"LM_LICENSE_FILE": "2100@flex"})

        monkeypatch.setattr(
            leg,
            "check_prerequisites",
            lambda: calls.append(("prereq", prereq_pair)) or prereq_pair,
        )

        def fake_run_suite(junit: Path) -> int:
            calls.append(("suite", leg.SUITE))
            junit.write_text(_junit_text([_case(FIT_E2E_TEST)]), encoding="utf-8")
            return 0

        monkeypatch.setattr(leg, "run_suite", fake_run_suite)
        rc = leg.main([])

    assert (
        leg is not None
        and "rr CLI not on PATH" in missing_rr_err
        and "Libero binary not found" in missing_libero_err
        and env_default.get("DS_P2_151_E2E_IMPL") == "1"
        and env_default.get("LM_LICENSE_FILE") == "1702@localhost"
        and env_chained.get("DS_P2_151_E2E_IMPL") == "1"
        and env_chained.get("LM_LICENSE_FILE") == "2100@flex:1702@localhost"
        and len(calls) == 2
        and calls[0][0] == "prereq"
        and calls[1] == ("suite", "tools/tests/test_pfsoc_fit_e2e_ds_p2_151.py")
        and rc == 0
    ), (
        "the leg must verify rr and Libero and pass DS_P2_151_E2E_IMPL=1 to the suite: "
        f"missing_rr={missing_rr_err!r} missing_libero={missing_libero_err!r} "
        f"env_default={env_default!r} env_chained={env_chained!r} calls={calls!r} rc={rc!r}"
    )


def _leg_job(path: Path) -> dict | None:
    return (yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}).get(
        "pfsoc-fit-e2e"
    )


def _run_lines(job: dict) -> list[list[str]]:
    return [
        [
            ln.strip()
            for ln in step["run"].splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        for step in job.get("steps", [])
        if "run" in step and step.get("name") != "Checkout"
    ]


def test_forge_ci_and_precommit_wire_the_pfsoc_fit_e2e_leg() -> None:
    """The bench Forgejo workflow runs the leg as its own named job on
    bench-host, and hooks/pre-commit.local runs this guard in its DS-P2.197 leg."""
    forge = _leg_job(FORGE)
    forge_runs = _run_lines(forge) if forge else []
    hook = HOOK.read_text(encoding="utf-8")
    m = re.search(
        r"^# ── DS-P2\.197 — the public CI workflow.*?^echo \"✓ Public CI",
        hook,
        re.M | re.S,
    )
    hook_leg = m.group(0) if m else ""
    guard_rel = "tools/tests/test_pfsoc_fit_e2e_leg_ds_t3_39.py"
    assert (
        forge is not None
        and forge.get("runs-on") == "bench-host"
        and forge.get("if") == "github.event_name == 'workflow_dispatch'"
        and ["rr workspace sync"] in forge_runs
        and forge_runs[-1] == ["python3 tools/ci/pfsoc_fit_e2e_leg.py"]
        and guard_rel in hook_leg
    ), (
        "the pfsoc-fit-e2e CI leg and DS-P2.197 pre-commit guard must be wired: "
        f"forge={forge!r} forge_runs={forge_runs!r} in_hook={guard_rel in hook_leg}"
    )
