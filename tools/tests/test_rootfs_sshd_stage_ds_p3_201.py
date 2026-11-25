# rr unknown-llm (AI-generated)
"""DS-P3.201: a Zynq-family rootfs cannot be built without the sshd binary.

2026-11-05 (DS-P2.211): a canfd_demo rootfs was built without running
sw/linux/stage_dropbear_overlay.sh, so S49dropbear found no /usr/sbin/dropbear
and the board had no SSH. The stage now runs inside linux.rootfs.stage_command,
which `rr linux rootfs` runs first and aborts on (RTL-P1.117), and its refusal
names the step.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files, with_bench  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
STAGE = ROOT / "sw" / "linux" / "stage_dropbear_overlay.sh"


def _zynq_targets():
    """Targets whose rootfs is the Zynq overlay (sw/linux/python_overlay)."""
    out = []
    for path in all_target_files(ROOT):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rootfs = (doc.get("linux") or {}).get("rootfs") or {}
        if rootfs.get("python_overlay") == "sw/linux/python_overlay":
            out.append(pytest.param(path.stem, rootfs, id=path.stem))
    return out


def test_the_zynq_family_is_found():
    names = {p.values[0] for p in _zynq_targets()}
    assert with_bench({"zybo_datalogger"}, {"canfd_demo", "classic_eye"}, ROOT) <= names, names


@pytest.mark.parametrize("name,rootfs", _zynq_targets())
def test_each_zynq_target_stages_dropbear_in_its_rootfs_build(name, rootfs):
    steps = [s.strip() for s in str(rootfs.get("stage_command", "")).split("&&")]
    assert "./sw/linux/stage_dropbear_overlay.sh" in steps, (
        f"{name}: linux.rootfs.stage_command does not stage the sshd binary: {steps}")


def _copy_stage(tmp_path: Path) -> Path:
    here = tmp_path / "sw" / "linux"
    here.mkdir(parents=True)
    shutil.copy2(STAGE, here / STAGE.name)
    return here / STAGE.name


def _run(script: Path, **env) -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items() if k != "DS_BUILDROOT_OUTPUT"}
    e.update(env)
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=e,
                          check=False)


def test_without_a_buildroot_dropbear_the_stage_refuses_and_names_itself(tmp_path):
    script = _copy_stage(tmp_path)
    proc = _run(script)
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert "stage_dropbear_overlay.sh" in out and "no sshd" in out, out
    assert not (script.parent / "rootfs_overlay" / "usr" / "sbin" / "dropbear").exists()


def test_with_a_buildroot_dropbear_the_stage_installs_it(tmp_path):
    script = _copy_stage(tmp_path)
    br = tmp_path / "br-output"
    src = br / "target" / "usr" / "sbin" / "dropbear"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"\x7fELF fixture")
    src.chmod(0o755)
    proc = _run(script, DS_BUILDROOT_OUTPUT=str(br))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    staged = script.parent / "rootfs_overlay" / "usr" / "sbin" / "dropbear"
    assert staged.read_bytes() == b"\x7fELF fixture"
