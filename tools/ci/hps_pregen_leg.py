#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""The CI step that runs rr's DE25 HPS pre-synth generate on the installed RouteRTL (DS-P2.209).

ip.yml declares the DE25 HPS and its io96b EMIF by parameter (``hps_regenerate:
true``, DS-P2.196). rr makes the two .ip variants from that declaration in its
pre-synth step, ``sdk.engine.hps_gen.generate_hps_pre_synth``, which needs
``sdk.engine.hps_regen`` (RTL-P2.2101). A RouteRTL release without that code
cannot build the DE25 target from this tree. The published routertl 5.2.0 is one
(DS-P1.85).

tools/tests/test_de25_hps_regenerate_ds_p2_196.py drives that real pre-synth step
on a copy of this ip.yml with a stand-in Quartus. This step runs that suite on
the rr the CI leg installed, prints the counts and the installed release, and is
red when any test failed, or when any GENERATE_TESTS test was skipped or did not
run. On the floor leg the installed rr is exactly the declared floor, so a floor
older than the code ip.yml needs is red here, by name, and not only somewhere in
the desk tests.

    python tools/ci/hps_pregen_leg.py                  # the step
    python tools/ci/hps_pregen_leg.py verdict JUNIT    # judge a report only
"""
from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
SUITE = "tools/tests/test_de25_hps_regenerate_ds_p2_196.py"
#: The suite's tests that call rr's real pre-synth generate. Each must pass unskipped.
GENERATE_TESTS = (
    "test_real_generator_renders_add_component_not_add_instance",
    "test_regenerated_variants_are_made_from_the_declaration_in_the_build_tree",
    "test_declared_hps_parameters_carry_the_full_io48_pin_mux",
    "test_hps_io_conduit_is_reexported_by_the_real_generator",
    "test_a_clean_tree_reaches_the_full_system_generate",
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
    lines = [f"DE25 HPS pre-synth generate: {counts['passed']} passed, "
             f"{counts['skipped']} skipped, {counts['failed']} failed of {len(seen)}"]
    unproven = [f"  {name}: {seen.get(name, 'not run')}"
                for name in GENERATE_TESTS if seen.get(name) != "passed"]
    lines.append(f"generate tests passed unskipped: "
                 f"{len(GENERATE_TESTS) - len(unproven)}/{len(GENERATE_TESTS)}")
    lines.extend(unproven)
    return (1 if counts["failed"] or unproven else 0), lines


def installed_routertl() -> str:
    try:
        return metadata.version("routertl")
    except metadata.PackageNotFoundError:
        return "not installed"


def run_suite(junit: Path) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "-rs", "-p", "no:cacheprovider",
         "--tb=short", f"--junitxml={junit}"],
        cwd=ROOT, check=False).returncode


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

    print(f"routertl {installed_routertl()}: rr's DE25 HPS pre-synth generate on ip.yml "
          f"with a stand-in Quartus ({SUITE})", flush=True)
    with tempfile.TemporaryDirectory(prefix="hps-pregen-leg-") as tmp:
        junit = Path(tmp) / "junit.xml"
        pytest_rc = run_suite(junit)
        if not junit.is_file():
            print(f"DE25 HPS pre-synth generate: pytest wrote no report (rc={pytest_rc})")
            return 1
        rc, lines = verdict(junit)
    print("\n".join(lines))
    return rc if pytest_rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
