#!/usr/bin/env python3
"""The CI leg that runs the DE25 toolchain stage tests with their archive (DS-T3.37).

Three tests in sw/linux/de25/test_de25_toolchain_stage.py need the pinned
144 MB ARM GNU toolchain archive in sw/linux/de25/.cache, and
test_staged_toolchain_on_this_bench_beats_vitis also needs the toolchain
staged from it. Without them the three skip, and desk-checks does not fetch
the archive. This leg:

1. fetches the archive, checks its pinned sha256 and stages it, with
   tools/de25_toolchain_stage.py's own stage(), into the paths the suite reads;
2. runs the whole suite;
3. prints the counts and fails when any test failed, or when any of the three
   was skipped or did not run.

.forgejo/workflows/ci.yml runs it as the job `de25-toolchain-stage`.

    python tools/ci/de25_toolchain_stage_leg.py                  # the leg
    python tools/ci/de25_toolchain_stage_leg.py verdict JUNIT    # step 3 only
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.de25_toolchain_stage import TOOLCHAIN_DIR_NAME, stage  # noqa: E402

SUITE = "sw/linux/de25/test_de25_toolchain_stage.py"
CACHE = ROOT / "sw/linux/de25/.cache"
DEST = ROOT / "sw/linux/de25/toolchain" / TOOLCHAIN_DIR_NAME
CACHED_ARCHIVE_TESTS = (
    "test_stage_extracts_a_real_aarch64_standalone_gcc",
    "test_corrupt_cached_archive_fails_without_touching_a_prior_good_stage",
    "test_staged_toolchain_on_this_bench_beats_vitis",
)


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
    counts = {k: sum(1 for v in seen.values() if v == k)
              for k in ("passed", "skipped", "failed")}
    lines = [f"DE25 toolchain stage leg: {counts['passed']} passed, "
             f"{counts['skipped']} skipped, {counts['failed']} failed "
             f"of {len(seen)}"]
    unproven = [f"  {name}: {seen.get(name, 'not run')}"
                for name in CACHED_ARCHIVE_TESTS if seen.get(name) != "passed"]
    good = len(CACHED_ARCHIVE_TESTS) - len(unproven)
    lines.append(f"cached-archive tests passed unskipped: "
                 f"{good}/{len(CACHED_ARCHIVE_TESTS)}")
    lines.extend(unproven)
    failed = counts["failed"] > 0 or bool(unproven)
    return (1 if failed else 0), lines


def unprivileged_path(raw_path: str) -> str:
    """Strip sbin directories (/sbin, /usr/sbin, /usr/local/sbin, etc.) from PATH (DS-P3.177)."""
    return os.pathsep.join(
        entry
        for entry in raw_path.split(os.pathsep)
        if entry and "sbin" not in Path(entry).parts
    )


def unprivileged_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy *environ* (default os.environ) with sbin directories stripped from PATH."""
    env = dict(os.environ if environ is None else environ)
    env["PATH"] = unprivileged_path(env.get("PATH", ""))
    return env


def run_suite(junit: Path) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "-rs", "-p", "no:cacheprovider",
         "--tb=short", f"--junitxml={junit}"],
        cwd=ROOT, env=unprivileged_env(), check=False).returncode


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

    print(f"staging {TOOLCHAIN_DIR_NAME} from {CACHE}", flush=True)
    stage(CACHE, DEST)
    with tempfile.TemporaryDirectory(prefix="de25-toolchain-leg-") as tmp:
        junit = Path(tmp) / "junit.xml"
        pytest_rc = run_suite(junit)
        if not junit.is_file():
            print(f"DE25 toolchain stage leg: pytest wrote no report (rc={pytest_rc})")
            return 1
        rc, lines = verdict(junit)
    print("\n".join(lines))
    return rc if pytest_rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
