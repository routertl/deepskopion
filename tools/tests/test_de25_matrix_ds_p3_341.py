# SPDX-License-Identifier: MPL-2.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""DS-P3.341 part 2: the 6-cell matrix on the measured DE25 image, the same verdicts under any init-script judge.

3 image states x 2 reference forms. Each cell is one copy of the image with exactly one planted init script.

    state S0 the image as built      S1 python3 removed      S2 dirname removed
    form  B  the script runs the program by bare name through PATH
          A  the script runs it by absolute path

    S0/B  S0/A  pass.   S1/B  S1/A  S2/B  S2/A  are refused, naming the removed program AND the planted script.

Two conditions make a cell mean something (MC 2026-11-24):
  attribution  the image's own S99 script already names python3 and dirname, so a refusal could come from it; every
               refusal must therefore name the planted script, and the test first asserts which original scripts name
               each program;
  presence     per form, a planted script naming a program absent from every state is refused in S0, so an S0 pass is
               not the scan missing the planted script.

Command (bench; no image in public CI, so it skips there by design and a skip is not evidence):

    DS_DE25_IMAGE=~/.cache/de25-p2w/artifacts/p2img-p316-20261123/rootfs.ext4 python -m pytest tools/tests/test_de25_matrix_ds_p3_341.py -q
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMAGE_SHA256_PREFIX = "14d16638"
DEFAULT_IMAGE = Path.home() / ".cache" / "de25-p2w" / "artifacts" / "p2img-p316-20261123" / "rootfs.ext4"
REMOVE = {"S0": None, "S1": "/bin/python3", "S2": "/usr/bin/dirname"}
PROGRAM = {"S1": "python3", "S2": "dirname"}
ABSOLUTE = {"python3": "/bin/python3", "dirname": "/usr/bin/dirname", "nosuchprog_ds341": "/usr/bin/nosuchprog_ds341"}
CONTROL = "nosuchprog_ds341"


def _tool():
    spec = importlib.util.spec_from_file_location("de25_linux_image_matrix", ROOT / "tools" / "de25_linux_image.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _image() -> Path:
    path = Path(os.environ.get("DS_DE25_IMAGE") or DEFAULT_IMAGE).expanduser()
    if not path.is_file():
        pytest.skip(f"the measured DE25 image (sha256 {IMAGE_SHA256_PREFIX}...) is not on this machine "
                    f"({path}); the matrix runs on the bench, a skip is not evidence")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest.startswith(IMAGE_SHA256_PREFIX), f"{path} is sha256 {digest}, not the measured image"
    return path


def _debugfs(copy: Path, *commands: str) -> None:
    tool = _tool()
    proc = subprocess.run([tool.sbin_tool("debugfs"), "-w", "-f", "-", str(copy)], input="\n".join(commands) + "\n",
                          capture_output=True, text=True, errors="replace", check=False)
    assert proc.returncode == 0, proc.stderr


def _cell(tmp_path: Path, image: Path, state: str, form: str, program: str) -> Path:
    """A copy of *image* in *state*, holding one planted init script that runs *program* in *form*."""
    copy = tmp_path / f"{state}-{form}.ext4"
    shutil.copyfile(image, copy)
    commands = []
    if REMOVE[state]:
        commands.append(f"rm {REMOVE[state]}")
    word = program if form == "B" else ABSOLUTE[program]
    script = tmp_path / f"S98ds341_{form}"
    script.write_text(f"#!/bin/sh\n{word} --version\n", encoding="utf-8")
    script.chmod(0o755)
    commands.append(f"write {script} /etc/init.d/S98ds341_{form}")
    _debugfs(copy, *commands)
    return copy


def _problems(copy: Path) -> list[str]:
    return _tool().rootfs_image_problems(copy, warnings=[])


def _names(problems: list[str], script: str, program: str) -> bool:
    return any(script in line and re.search(rf"\b{re.escape(program)}\b", line) for line in problems)


def test_the_image_is_the_measured_one_and_passes_unmodified(tmp_path):
    image = _image()
    copy = tmp_path / "as-built.ext4"
    shutil.copyfile(image, copy)
    assert _problems(copy) == []


def test_attribution_names_the_original_scripts_that_already_mention_each_program(tmp_path):
    """The image's own S99 script names both programs, so only a refusal naming the PLANTED script is attributable."""
    image = _image()
    tool = _tool()
    tree = tmp_path / "tree"
    tree.mkdir()
    subprocess.run([tool.sbin_tool("debugfs"), "-R", f"rdump /etc/init.d {tree}", str(image)], capture_output=True,
                   check=False)
    originals = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in (tree / "init.d").iterdir()}
    naming = {prog: sorted(n for n, t in originals.items() if re.search(rf"\b{prog}\b", t))
              for prog in ("python3", "dirname")}
    assert naming == {"python3": ["S99deepskopion-stream"], "dirname": ["S99deepskopion-stream"]}, naming


@pytest.mark.parametrize("form", ["B", "A"])
def test_s0_passes_with_the_planted_script_present_and_scanned(tmp_path, form):
    image = _image()
    ok = _cell(tmp_path, image, "S0", form, "python3")                  # python3 exists in S0, in both forms
    assert _problems(ok) == []
    assert (tmp_path / f"S0-{form}.ext4").is_file()
    (tmp_path / "ctl").mkdir()
    control = _cell(tmp_path / "ctl", image, "S0", form, CONTROL)
    assert _names(_problems(control), f"S98ds341_{form}", CONTROL)       # the scan does see the planted script


@pytest.mark.parametrize("state", ["S1", "S2"])
@pytest.mark.parametrize("form", ["B", "A"])
def test_a_removed_program_refuses_naming_it_and_the_planted_script(tmp_path, state, form):
    image = _image()
    program = PROGRAM[state]
    problems = _problems(_cell(tmp_path, image, state, form, program))
    assert _names(problems, f"S98ds341_{form}", program), problems
