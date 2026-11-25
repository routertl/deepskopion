# rr unknown-llm (AI-generated)
"""DS-T3.37: a named CI leg runs the DE25 toolchain stage tests unskipped.

Three tests in sw/linux/de25/test_de25_toolchain_stage.py need the pinned
144 MB ARM GNU toolchain archive in sw/linux/de25/.cache, and one of them also
needs the toolchain staged from it. Without the archive they skip, and no CI
or scheduled run fetched it, so they never ran outside this bench.

The leg tools/ci/de25_toolchain_stage_leg.py fetches and stages the archive
with the stager's own checksum-verified stage(), runs the suite, prints the
counts, and fails when any of the three did not pass. A skip is a failure.
.forgejo/workflows/ci.yml runs it as the job `de25-toolchain-stage`.

These tests drive the leg's verdict through its CLI on synthetic junit
reports, and its staging order with the stage and the suite run replaced, so
they need neither the archive nor the network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
LEG = ROOT / "tools/ci/de25_toolchain_stage_leg.py"
FORGE = ROOT / ".forgejo/workflows/ci.yml"
PUBLIC = ROOT / ".github/workflows/ci.yml"

CLASS = "sw.linux.de25.test_de25_toolchain_stage"
CACHED_ARCHIVE_TESTS = (
    "test_stage_extracts_a_real_aarch64_standalone_gcc",
    "test_corrupt_cached_archive_fails_without_touching_a_prior_good_stage",
    "test_staged_toolchain_on_this_bench_beats_vitis",
)
SKIP_REASON = (
    "DS-T3.37: no cached toolchain archive on this host and this test must "
    "not trigger a fresh 144 MB download of a shared fixture")


def _case(name: str, outcome: str = "passed") -> str:
    body = {
        "passed": "",
        "skipped": f'<skipped type="pytest.skip" message="{SKIP_REASON}"/>',
        "failed": '<failure message="assert 1 == 2">assert 1 == 2</failure>',
    }[outcome]
    return f'<testcase classname="{CLASS}" name="{name}" time="0.1">{body}</testcase>'


def _junit_text(cases: list[str]) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites>'
            '<testsuite name="pytest">' + "".join(cases) + "</testsuite></testsuites>")


def _junit(tmp_path: Path, cases: list[str]) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(_junit_text(cases), encoding="utf-8")
    return path


def _verdict(junit: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LEG), "verdict", str(junit)],
        cwd=ROOT, capture_output=True, text=True, check=False)


def test_leg_passes_and_prints_the_counts_when_all_three_ran(tmp_path):
    junit = _junit(tmp_path, [
        _case(CACHED_ARCHIVE_TESTS[0]),
        _case(CACHED_ARCHIVE_TESTS[1]),
        _case(CACHED_ARCHIVE_TESTS[2]),
        _case("test_build_environment_reaches_cross_compiler_python_and_openssl"),
        _case("test_pinned_runtime_glibc_max_matches_its_bytes", "skipped"),
    ])
    run = _verdict(junit)
    out = run.stdout + run.stderr
    assert run.returncode == 0 and (
        "4 passed, 1 skipped, 0 failed of 5" in run.stdout
        and "cached-archive tests passed unskipped: 3/3" in run.stdout), (
        f"the leg must pass and print the counts when all three ran: "
        f"rc={run.returncode}\n{out}")


def test_leg_fails_when_a_cached_archive_test_is_skipped(tmp_path):
    junit = _junit(tmp_path, [
        _case(CACHED_ARCHIVE_TESTS[0]),
        _case(CACHED_ARCHIVE_TESTS[1], "skipped"),
        _case(CACHED_ARCHIVE_TESTS[2]),
    ])
    run = _verdict(junit)
    out = run.stdout + run.stderr
    assert run.returncode == 1 and (
        f"{CACHED_ARCHIVE_TESTS[1]}: skipped" in run.stdout), (
        f"the leg accepted a skipped cached-archive test: "
        f"rc={run.returncode}\n{out}")


def test_leg_fails_when_a_cached_archive_test_did_not_run(tmp_path):
    junit = _junit(tmp_path, [
        _case(CACHED_ARCHIVE_TESTS[0]),
        _case(CACHED_ARCHIVE_TESTS[1]),
    ])
    run = _verdict(junit)
    out = run.stdout + run.stderr
    assert run.returncode == 1 and (
        f"{CACHED_ARCHIVE_TESTS[2]}: not run" in run.stdout), (
        f"the leg accepted a run without a cached-archive test: "
        f"rc={run.returncode}\n{out}")


def test_leg_fails_when_another_test_in_the_suite_failed(tmp_path):
    junit = _junit(tmp_path, [
        _case(CACHED_ARCHIVE_TESTS[0]),
        _case(CACHED_ARCHIVE_TESTS[1]),
        _case(CACHED_ARCHIVE_TESTS[2]),
        _case("test_elf_glibc_needs_reads_the_version_needs", "failed"),
    ])
    run = _verdict(junit)
    out = run.stdout + run.stderr
    assert run.returncode == 1 and "3 passed, 0 skipped, 1 failed of 4" in run.stdout, (
        f"the leg passed a suite with a failed test: rc={run.returncode}\n{out}")


def _load_leg():
    """The leg module, or None when the file does not exist."""
    if not LEG.is_file():
        return None
    spec = importlib.util.spec_from_file_location("de25_toolchain_stage_leg", LEG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_leg_stages_the_pinned_toolchain_where_the_suite_reads_it_first(
        tmp_path, monkeypatch):
    """The suite reads the archive from sw/linux/de25/.cache and the staged
    toolchain from sw/linux/de25/toolchain/<release>; the leg must stage both
    there before it runs the suite, or the three tests skip."""
    calls: list[tuple] = []
    rc = None
    leg = _load_leg()
    if leg is not None:
        monkeypatch.setattr(
            leg, "stage", lambda cache, dest: calls.append(("stage", cache, dest)))

        def run_suite(junit: Path) -> int:
            calls.append(("suite",))
            junit.write_text(_junit_text(
                [_case(name) for name in CACHED_ARCHIVE_TESTS]), encoding="utf-8")
            return 0
        monkeypatch.setattr(leg, "run_suite", run_suite)
        rc = leg.main([])
    assert calls == [
        ("stage", ROOT / "sw/linux/de25/.cache",
         ROOT / "sw/linux/de25/toolchain"
         / "arm-gnu-toolchain-13.3.rel1-x86_64-aarch64-none-linux-gnu"),
        ("suite",),
    ] and rc == 0, (
        f"the leg must stage the pinned toolchain where the suite reads it "
        f"before running the suite: calls={calls} rc={rc}")


def _leg_job(path: Path) -> dict | None:
    return (yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}).get(
        "de25-toolchain-stage")


def _run_lines(job: dict) -> list[list[str]]:
    return [[ln.strip() for ln in step["run"].splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
            for step in job["steps"]
            if "run" in step and step.get("name") != "Checkout"]


def test_forge_ci_names_a_leg_that_runs_the_suite_through_the_leg_script():
    """The bench Forgejo runs the leg as its own named job on main pushes,
    and the public workflow carries the same steps (DS-P2.209 parity)."""
    forge, public = _leg_job(FORGE), _leg_job(PUBLIC)
    forge_runs = _run_lines(forge) if forge else []
    public_runs = _run_lines(public) if public else []
    assert (
        forge is not None
        and forge.get("runs-on") == "ci-docker"
        and forge_runs and forge_runs[-1] == ["python tools/ci/de25_toolchain_stage_leg.py"]
        and public_runs == [[ln.replace("sudo ", "") for ln in run] for run in forge_runs]
    ), (
        "the de25-toolchain-stage CI leg must run "
        "python tools/ci/de25_toolchain_stage_leg.py on ci-docker, with the "
        f"same steps in the public workflow: forge={forge_runs} public={public_runs}")
