# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.333: the README says how Vivado gets on PATH before the first rr command.

The clean-machine rehearsal of the export had to source a local script before
the build: the README never said how Vivado is found, and the shipped
settings64.sh of an install copied to another directory still names the
original one. What ran there, and what the README now documents for that case,
is `XILINX_VIVADO=<install-dir>/Vivado/2024.1` plus `$XILINX_VIVADO/bin` on PATH.
RouteRTL resolves the target's pinned release through `XILINX_VIVADO` (routertl
5.5.0 tool discovery reports the source "env"), and the preflight reads the same
variable first.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FENCE = re.compile(r"^```(?:bash|sh|shell)?\s*$\n(.*?)^```\s*$", re.M | re.S)
SECTION = "### Build and run on the Zybo Z7-20"


def _vivado_version() -> str:
    decl = yaml.safe_load((ROOT / "tools/preflight/supported_environment.yml").read_text(encoding="utf-8"))
    return str(decl["vivado"]["version"])


def _zybo_blocks(text: str) -> list[list[str]]:
    start = text.index(SECTION)
    end = text.find("\n### ", start + len(SECTION))
    section = text[start:end if end != -1 else len(text)]
    blocks = []
    for body in FENCE.findall(section):
        lines = [line.split("#", 1)[0].strip() for line in body.splitlines()]
        blocks.append([line for line in lines if line])
    return blocks


def _setup_before_first_rr(text: str) -> list[str]:
    """Every command in the blocks that come before the first block running `rr`."""
    blocks = _zybo_blocks(text)
    first_rr = next(i for i, b in enumerate(blocks) if any(c.startswith("rr ") for c in b))
    return [c for b in blocks[:first_rr] for c in b]


def test_the_vivado_setup_comes_before_the_first_rr_command():
    version = _vivado_version()
    setup = _setup_before_first_rr((ROOT / "README.md").read_text(encoding="utf-8"))
    assert f"source <install-dir>/Vivado/{version}/settings64.sh" in setup, setup
    assert f"export XILINX_VIVADO=<install-dir>/Vivado/{version}" in setup, setup
    assert 'export PATH="$XILINX_VIVADO/bin:$PATH"' in setup, setup


def test_the_readme_states_the_declared_vivado_release():
    version = _vivado_version()
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text[text.index(SECTION):]
    assert f"Vivado {version}" in section.split("```", 1)[0]
    assert f"`vivado -version` then prints `vivado v{version}`" in text


def test_the_setup_names_no_machine_path():
    setup = " ".join(_setup_before_first_rr((ROOT / "README.md").read_text(encoding="utf-8")))
    assert not re.search(r"/home/|/Users/|/tools/Xilinx", setup), setup


def test_the_check_refuses_a_readme_without_the_setup():
    """Mutant: the README as it was, with the build steps but no Vivado setup."""
    text = (f"{SECTION}\n\n```bash\n# 1. Install\npip install routertl\n"
            "rr target set zybo_datalogger\nrr synth run\n```\n")
    assert _setup_before_first_rr(text) == []
