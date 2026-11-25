# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.235: a scoped sim compiles the generated runtime sources it needs.

DS-P2.231 made hw/rtl/pkg/rom_info_pkg.vhd a gitignored build output: the
pre_sim rom-info-sim hook writes it before every sim. hw/sim/cocotb/
ds_sim_sources.py scoped_libraries() takes its file set from routertl's
DependencyResolver, which skips gitignored files by default, so the package
the hook had just written never reached the compile set. Every scoped
testbench that reaches system_regbank_pkg failed at analysis with "design
unit ROM_INFO_PKG not found in library TICH" (the nine datalogger_gather
sims, measured 2026-11-08).

The fix adds the outputs tools/generated_sources.yml declares under
runtime_families to the resolver as explicit files, so a declared generated
source is compiled when the DUT reaches it, and the gitignore filter still
applies to everything else.

POWER: both tests are RED on the pre-fix scoped_libraries (the package is
missing from the tich list) and GREEN after.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "hw" / "sim" / "cocotb"))
pytest.importorskip("routertl_core.dependency_resolver")
from ds_sim_sources import scoped_libraries  # noqa: E402

ROM_INFO = "hw/rtl/pkg/rom_info_pkg.vhd"


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return env


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A committed project whose tb reaches a gitignored, declared runtime
    package through a tracked one, as tb_datalogger_gather reaches
    rom_info_pkg through system_regbank_pkg."""
    r = tmp_path / "proj"
    _write(r, ".gitignore", f"{ROM_INFO}\n")
    _write(r, "tools/generated_sources.yml",
           "runtime_families:\n  rom-info:\n    hook: regbank-regen\n"
           f"    outputs:\n      - {ROM_INFO}\n")
    _write(r, "hw/rtl/pkg/system_regbank_pkg.vhd",
           "library tich;\nuse tich.rom_info_pkg.all;\n"
           "package system_regbank_pkg is\n"
           "  constant C_WORDS : natural := C_ROM_WORDS;\n"
           "end package system_regbank_pkg;\n")
    _write(r, "hw/sim/tb_uses_regbank.vhd",
           "library tich;\nuse tich.system_regbank_pkg.all;\n"
           "entity tb_uses_regbank is end entity tb_uses_regbank;\n"
           "architecture sim of tb_uses_regbank is\nbegin\n"
           "end architecture sim;\n")
    subprocess.run(["git", "init", "-q"], cwd=r, env=_git_env(), check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, env=_git_env(), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, env=_git_env(), check=True)
    # Written after the commit, as the pre_sim hook writes it: on disk, ignored.
    _write(r, ROM_INFO, "package rom_info_pkg is\n"
                        "  constant C_ROM_WORDS : natural := 4;\n"
                        "end package rom_info_pkg;\n")
    ignored = subprocess.run(["git", "check-ignore", "-q", ROM_INFO], cwd=r,
                             env=_git_env())
    assert ignored.returncode == 0, "the fixture package must be gitignored"
    return r


def _tich(libs: dict) -> list[str]:
    return [Path(p).as_posix() for p in libs.get("tich", [])]


def test_a_declared_gitignored_runtime_package_is_in_the_compile_set(project):
    tich = _tich(scoped_libraries("tb_uses_regbank", root=project))
    assert any(p.endswith(ROM_INFO) for p in tich), tich
    rom = next(i for i, p in enumerate(tich) if p.endswith(ROM_INFO))
    regbank = next(i for i, p in enumerate(tich)
                   if p.endswith("hw/rtl/pkg/system_regbank_pkg.vhd"))
    assert rom < regbank, f"rom_info_pkg must compile before its user: {tich}"
    assert sum(p.endswith(ROM_INFO) for p in tich) == 1, tich


def test_an_undeclared_gitignored_file_stays_out(project):
    """The gitignore filter still applies to everything the manifest does not
    declare: an ignored file the tb names is not compiled, while the declared
    runtime package next to it is. (Turning the filter off for the whole
    resolve would compile both; this case is RED for that.)"""
    _write(project, ".gitignore", f"{ROM_INFO}\nhw/rtl/stray_pkg.vhd\n")
    _write(project, "hw/rtl/stray_pkg.vhd",
           "package stray_pkg is\nend package stray_pkg;\n")
    _write(project, "hw/sim/tb_uses_stray.vhd",
           "library tich;\nuse tich.system_regbank_pkg.all;\nuse tich.stray_pkg.all;\n"
           "entity tb_uses_stray is end entity tb_uses_stray;\n"
           "architecture sim of tb_uses_stray is\nbegin\n"
           "end architecture sim;\n")
    tich = _tich(scoped_libraries("tb_uses_stray", root=project))
    assert any(p.endswith(ROM_INFO) for p in tich), tich
    assert not any(p.endswith("stray_pkg.vhd") for p in tich), tich


def test_the_gather_tb_compile_set_carries_rom_info_pkg():
    """The real tree: tb_datalogger_gather reaches system_regbank_pkg, and
    rom_info_pkg.vhd is gitignored there. Generate it as the pre_sim hook
    does when the checkout has none yet."""
    ignored = subprocess.run(["git", "check-ignore", "-q", ROM_INFO], cwd=ROOT,
                             env=_git_env())
    assert ignored.returncode == 0, f"{ROM_INFO} is expected to be gitignored"
    if not (ROOT / ROM_INFO).is_file():
        subprocess.run(["bash", "tools/regen_regbank.sh", "--rom-info-only",
                        "--output-dir", "hw/rtl/pkg", "--regbank-dir",
                        "hw/rtl/regbank", "--library", "tich"],
                       cwd=ROOT, env=_git_env(), check=True,
                       capture_output=True)
    tich = _tich(scoped_libraries("tb_datalogger_gather"))
    assert any(p.endswith("hw/rtl/pkg/system_regbank_pkg.vhd") for p in tich)
    assert any(p.endswith(ROM_INFO) for p in tich), tich
