# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.365: DeepSkopion declares Python 3.11 or newer, and the README's Ubuntu 22.04 path pins such an interpreter.

RouteRTL drops Python 3.10 in a coming minor release and prints a deprecation line on every rr call under it. On
Ubuntu 22.04, `python3 -m venv .venv` gives 3.10, so the README replaces that line there with a uv-managed Python 3.11
(no sudo, no third-party apt repository). These checks read the committed declaration and README.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
DECL = ROOT / "tools" / "preflight" / "supported_environment.yml"
README = ROOT / "README.md"
FLOOR = (3, 11)
SECTION = "### Python 3.11 on Ubuntu 22.04"
# What `python3` is on each declared Ubuntu release (the distribution's default interpreter).
DEFAULT_PYTHON3 = {"22.04": (3, 10), "24.04": (3, 12)}


def _ver(text: str) -> tuple[int, int]:
    major, minor = text.split(".")[:2]
    return int(major), int(minor)


def _section_block(text: str) -> str:
    start = text.index(SECTION)
    block = re.search(r"```bash\n(.*?)```", text[start:], re.S)
    assert block, f"no bash block under {SECTION!r}"
    return block.group(1)


def test_the_declared_minimum_python_is_at_least_3_11():
    decl = yaml.safe_load(DECL.read_text(encoding="utf-8"))
    assert _ver(str(decl["python"]["min"])) >= FLOOR


def test_the_readme_requirements_row_states_the_declared_minimum():
    decl = yaml.safe_load(DECL.read_text(encoding="utf-8"))
    row = f"| Python | `>= {decl['python']['min']}, < {decl['python']['max_exclusive']}` |"
    assert row in README.read_text(encoding="utf-8")


def test_the_22_04_commands_install_and_pin_python_3_11_or_newer_without_sudo():
    block = _section_block(README.read_text(encoding="utf-8"))
    installs = re.findall(r"uv python install (\d+\.\d+)", block)
    venvs = re.findall(r"uv venv\b[^\n]*--python (\d+\.\d+)[^\n]*\.venv", block)
    assert installs and all(_ver(v) >= FLOOR for v in installs), block
    assert venvs and all(_ver(v) >= FLOOR for v in venvs), block
    assert "--seed" in block, "without --seed the venv has no pip and `pip install` reaches the system Python"
    assert "sudo" not in block and "add-apt-repository" not in block


@pytest.mark.parametrize("release", sorted(DEFAULT_PYTHON3))
def test_every_default_python3_venv_line_names_the_22_04_replacement_where_python3_is_too_old(release):
    text = README.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if re.match(r"\s*python3 -m venv \.venv\b", ln)]
    assert lines, "the README has no `python3 -m venv .venv` step"
    if DEFAULT_PYTHON3[release] < FLOOR:
        for ln in lines:
            assert f"Ubuntu {release}" in ln and SECTION.removeprefix("### ") in ln, ln


def test_every_declared_ubuntu_release_has_a_known_default_python3():
    os_decl = yaml.safe_load(DECL.read_text(encoding="utf-8"))["host"]["os"]
    declared = [str(os_decl["version"])] + [str(v["version"]) for v in os_decl.get("also_supported") or []]
    assert set(declared) <= set(DEFAULT_PYTHON3), declared
