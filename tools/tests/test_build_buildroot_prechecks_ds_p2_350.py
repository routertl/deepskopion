# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.350 / DS-P2.351: sw/linux/build_buildroot.sh refuses early, and says why.

A host without one of the programs Buildroot 2024.11.4 requires, or a PATH with
whitespace (WSL appends Windows entries such as '/mnt/c/Program Files/...'),
used to stop only after the clone and the defconfig, with Buildroot's own
one-problem-at-a-time message. The script now checks first.

Every test runs the real script (bash, no copy) with PATH set to a scratch
directory of symlinks to the real tools. git and make are stubs that print
STUB-REACHED and exit 97, so a run that passes the checks stops at the first
network or build step and can never clone or build anything.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "sw" / "linux" / "build_buildroot.sh"
README = ROOT / "README.md"
ZYBO_GUIDE = ROOT / "docs" / "getting_started" / "zybo_z7_20.md"

# The script's own list; the test keeps its copy so that a program dropped from
# the script is a red test, not a silent shrink.
REQUIRED = ["bash", "bc", "cmp", "cpio", "file", "find", "g++", "gcc", "git", "make",
            "patch", "perl", "rsync", "sed", "tar", "unzip", "wget", "which", "xargs"]
STUBBED = {"git", "make"}
SCRIPT_OWN = ["dirname", "cp"]  # used by the script before and after the checks
ONE_LINER = """export PATH="$(printf '%s' "$PATH" | tr ':' '\\n' | grep -v '[[:space:]]' | paste -sd:)\""""


def _link_tools(bindir: Path, omit: set[str]) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    for prog in REQUIRED + SCRIPT_OWN:
        if prog in omit:
            continue
        if prog in STUBBED:
            stub = bindir / prog
            stub.write_text("#!/bin/sh\necho STUB-REACHED\nexit 97\n")
            stub.chmod(0o755)
            continue
        real = shutil.which(prog)
        if real is None:
            pytest.fail(f"the host running this test lacks {prog}; the harness needs the full set")
        (bindir / prog).symlink_to(real)


def _run(path: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": path, "HOME": os.environ.get("HOME", "/")}
    return subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True,
                          errors="replace", timeout=60)


@pytest.fixture
def bindir(tmp_path: Path) -> Path:
    d = tmp_path / "bin"
    _link_tools(d, omit=set())
    return d


def test_control_a_complete_host_passes_the_checks(bindir: Path):
    """Positive control: with every program present the script reaches the first git call.
    Without it, the refusals below would also pass against a script that refuses everything."""
    r = _run(str(bindir))
    assert r.returncode == 97 and "STUB-REACHED" in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert "ERROR" not in r.stderr


@pytest.mark.parametrize("prog", REQUIRED)
def test_a_missing_host_program_is_refused_by_name_before_any_clone(tmp_path: Path, prog: str):
    d = tmp_path / "bin"
    _link_tools(d, omit={prog})
    r = _run(str(d))
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "STUB-REACHED" not in r.stdout, "the script went past the check"
    assert "host programs missing" in r.stderr and f" {prog}" in r.stderr.split("missing", 1)[1].split("\n")[0]


def test_every_missing_program_is_named_in_one_message(tmp_path: Path):
    gone = {"cpio", "unzip", "rsync", "bc"}
    d = tmp_path / "bin"
    _link_tools(d, omit=gone)
    r = _run(str(d))
    line = r.stderr.split("missing", 1)[1].split("\n")[0]
    assert r.returncode == 2 and all(f" {p}" in line for p in gone), (r.returncode, r.stderr)
    assert "docs/getting_started/zybo_z7_20.md" in r.stderr and "Host requirements of step 4" in r.stderr


def test_a_path_with_whitespace_is_refused_and_names_the_readme_one_liner(tmp_path: Path, bindir: Path):
    spaced = tmp_path / "Program Files" / "x"
    spaced.mkdir(parents=True)
    r = _run(f"{bindir}:{spaced}")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "STUB-REACHED" not in r.stdout
    assert "whitespace" in r.stderr
    assert ONE_LINER in r.stderr, r.stderr
    assert "docs/getting_started/zybo_z7_20.md" in r.stderr, r.stderr


def test_the_one_liner_the_script_prints_is_the_one_in_the_readme():
    assert ONE_LINER in ZYBO_GUIDE.read_text(encoding="utf-8")
    assert ONE_LINER.replace("\\n", "\\\\n").replace('"', '\\"').replace("$", "\\$") in SCRIPT.read_text(encoding="utf-8")


def test_the_one_liner_removes_the_spaced_entry(tmp_path: Path, bindir: Path):
    spaced = tmp_path / "Program Files"
    spaced.mkdir()
    r = subprocess.run(["/bin/bash", "-c", ONE_LINER + '; printf "%s" "$PATH"'],
                       env={"PATH": f"{bindir}:{spaced}:/usr/bin"}, capture_output=True, text=True)
    assert r.returncode == 0 and str(bindir) in r.stdout and "Program Files" not in r.stdout, r.stdout
