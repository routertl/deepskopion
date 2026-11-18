#!/usr/bin/env python3
"""The CI leg that runs the PolarFire fit e2e guard unskipped (DS-T3.39).

tools/tests/test_pfsoc_fit_e2e_ds_p2_151.py needs DS_P2_151_E2E_IMPL=1,
Microchip Libero SoC 2025.2 at the tool_path declared in
targets/pfsoc_datalogger.yml, and the FlexLM daemon on 1702@localhost. Without
DS_P2_151_E2E_IMPL=1 it skips, and containerised desk-checks does not carry
Libero. This leg:

1. verifies that rr is on PATH and the Libero binary declared in
   targets/pfsoc_datalogger.yml exists;
2. runs tools/tests/test_pfsoc_fit_e2e_ds_p2_151.py with DS_P2_151_E2E_IMPL=1
   and 1702@localhost in LM_LICENSE_FILE while preserving the checkout's
   .routertl/local/active-target;
3. prints the counts and fails when any test failed, or when
   test_pfsoc_fit_reaches_constrained_timing was skipped or did not run.

.forgejo/workflows/ci.yml runs it as the job `pfsoc-fit-e2e` on `bench-host`.

    python3 tools/ci/pfsoc_fit_e2e_leg.py                  # the leg
    python3 tools/ci/pfsoc_fit_e2e_leg.py verdict JUNIT    # step 3 only
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import yaml

ROOT = Path(__file__).resolve().parents[2]
SUITE = "tools/tests/test_pfsoc_fit_e2e_ds_p2_151.py"
TARGET = ROOT / "targets/pfsoc_datalogger.yml"
ACTIVE_TARGET = ROOT / ".routertl/local/active-target"
FIT_E2E_TESTS = (
    "test_pfsoc_fit_reaches_constrained_timing",
)


def libero_binary(target: Path = TARGET) -> Path:
    cfg = yaml.safe_load(target.read_text(encoding="utf-8"))
    return Path(cfg["hardware"]["tool_path"]) / "Designer" / "bin64" / "libero"


def check_prerequisites(target: Path = TARGET) -> tuple[str, Path]:
    rr = shutil.which("rr")
    if rr is None:
        raise RuntimeError("rr CLI not on PATH")
    libero = libero_binary(target)
    if not libero.is_file():
        raise RuntimeError(f"Libero binary not found at {libero}")
    return rr, libero


def suite_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env["DS_P2_151_E2E_IMPL"] = "1"
    lic = env.get("LM_LICENSE_FILE", "")
    if not lic:
        env["LM_LICENSE_FILE"] = "1702@localhost"
    elif "1702@" not in lic:
        env["LM_LICENSE_FILE"] = f"{lic}:1702@localhost"
    return env


def outcomes(junit: Path) -> dict[str, str]:
    """Test name -> passed | skipped | failed, from a pytest junit report."""
    result: dict[str, str] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        if case.find("skipped") is not None:
            outcome = "skipped"
        elif case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        else:
            outcome = "passed"
        result[case.get("name", "")] = outcome
    return result


def verdict(junit: Path) -> tuple[int, list[str]]:
    """(exit code, report lines) for a suite run."""
    seen = outcomes(junit)
    counts = {
        k: sum(1 for v in seen.values() if v == k)
        for k in ("passed", "skipped", "failed")
    }
    lines = [
        f"PolarFire fit e2e leg: {counts['passed']} passed, "
        f"{counts['skipped']} skipped, {counts['failed']} failed "
        f"of {len(seen)}"
    ]
    unproven = [
        f"  {name}: {seen.get(name, 'not run')}"
        for name in FIT_E2E_TESTS
        if seen.get(name) != "passed"
    ]
    good = len(FIT_E2E_TESTS) - len(unproven)
    lines.append(f"fit-e2e tests passed unskipped: {good}/{len(FIT_E2E_TESTS)}")
    lines.extend(unproven)
    failed = counts["failed"] > 0 or bool(unproven)
    return (1 if failed else 0), lines


def run_suite(junit: Path) -> int:
    had_active = ACTIVE_TARGET.exists()
    prior_active = ACTIVE_TARGET.read_bytes() if had_active else b""
    try:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                SUITE,
                "-q",
                "-rs",
                "-p",
                "no:cacheprovider",
                "--tb=short",
                f"--junitxml={junit}",
            ],
            cwd=ROOT,
            env=suite_env(),
            check=False,
        ).returncode
    finally:
        if had_active:
            ACTIVE_TARGET.parent.mkdir(parents=True, exist_ok=True)
            ACTIVE_TARGET.write_bytes(prior_active)
        elif ACTIVE_TARGET.exists():
            ACTIVE_TARGET.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    report = sub.add_parser("verdict", help="judge an existing junit report")
    report.add_argument("junit", type=Path)
    args = parser.parse_args(argv)

    if args.command == "verdict":
        rc, lines = verdict(args.junit)
        print("\n".join(lines))
        return rc

    check_prerequisites()
    with tempfile.TemporaryDirectory(prefix="pfsoc-fit-e2e-leg-") as tmp:
        junit = Path(tmp) / "junit.xml"
        pytest_rc = run_suite(junit)
        if not junit.is_file():
            print(f"PolarFire fit e2e leg: pytest wrote no report (rc={pytest_rc})")
            return 1
        rc, lines = verdict(junit)
    print("\n".join(lines))
    return rc if pytest_rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
