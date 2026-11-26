# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""The PolarFire guide's Linux steps stop at the first failure, and select the PolarFire target for real.

A failed `rr linux build` followed by `rr linux flash-sd` would write an older image to the card; an exported
RR_TARGET overrides `rr target set`. The guide's own command text is run here against recording stubs.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "getting_started" / "polarfire_soc_discovery.md"


def _block() -> str:
    return re.findall(r"```bash\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.S)[0]


def _chain() -> str:
    """The guide's steps 5-7 exactly as written: from `CARD=` through the flash command."""
    lines = _block().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("CARD="))
    end = next(i for i, ln in enumerate(lines) if "rr linux flash-sd" in ln)
    return "\n".join(lines[start:end + 1]) + "\n"


def _run(chain: str, tmp_path: Path, failing: str | None) -> list[str]:
    """Run *chain* in a scratch tree; the rr stub and the overlay script record their call, *failing* exits 1."""
    log = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rr").write_text(
        f'#!/bin/sh\necho "rr $*" >> "{log}"\n[ "rr $*" = "{failing}" ] && exit 1\nexit 0\n', encoding="utf-8")
    (bin_dir / "rr").chmod(0o755)
    overlay = tmp_path / "sw" / "linux" / "pfsoc" / "build_python_overlay.sh"
    overlay.parent.mkdir(parents=True)
    name = "sw/linux/pfsoc/build_python_overlay.sh"
    overlay.write_text(
        f'#!/bin/sh\necho "{name}" >> "{log}"\n[ "{name}" = "{failing}" ] && exit 1\nexit 0\n', encoding="utf-8")
    overlay.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    subprocess.run(["bash", "-c", chain], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


ALL = ["rr linux tools install", "sw/linux/pfsoc/build_python_overlay.sh", "rr linux build",
       "rr linux flash-sd --image --device /dev/sdX"]


@pytest.mark.parametrize("failing,expected", [
    (None, ALL),
    ("sw/linux/pfsoc/build_python_overlay.sh", ALL[:2]),
    ("rr linux build", ALL[:3]),
])
def test_the_linux_steps_stop_at_the_first_failure(tmp_path, failing, expected):
    assert _run(_chain(), tmp_path, failing) == expected


def test_a_chain_written_as_separate_lines_is_reported(tmp_path):
    """Control: the same commands on separate lines run the flash after a failed build."""
    separate = _chain().replace(" &&\n  ", "\n")
    assert "rr linux flash-sd --image --device /dev/sdX" in _run(separate, tmp_path, "rr linux build")


def test_the_polarfire_target_is_not_overridden_by_an_exported_rr_target():
    lines = _block().splitlines()
    selected = lines.index("rr target set pfsoc_datalogger")
    unset = next((i for i, ln in enumerate(lines) if re.match(r"unset RR_TARGET\b", ln)), -1)
    first_build = next(i for i, ln in enumerate(lines) if ln.startswith(("rr synth", "rr pkg")))
    assert selected < unset < first_build, (selected, unset, first_build)


def test_the_card_image_alone_names_its_steps_and_the_toolchain_requirement():
    text = GUIDE.read_text(encoding="utf-8")
    assert "The card image alone (no kit attached) needs steps 1-2, then `rr project create`" in text
    assert "a bare-metal RISC-V GCC with a C library (newlib)" in text
    assert "libglib2.0-0t64 libxslt1.1" in text
