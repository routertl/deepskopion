# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.106: the Zybo (Zynq-7000) python overlay is built, never taken from a stale tarball.

Measured 2026-11-16 on 74fa34e5: the committed sw/linux/python_overlay.tar.gz (June) did carry
libpython3.12.so.1.0, but the Zybo target's own stage gate refused it.
`sw/linux/clean_python_overlay.py --check` found 5 files with build-host data, and the tarball had no
native edge library (tools/native_edge_lib.py). Both repairs need the Buildroot output, so the
overlay is derived instead: sw/linux/build_buildroot.sh && sw/linux/extract_python_overlay.sh. The
tarball is not published either (docs/REDISTRIBUTION.md item 1), so no public clone ever had it.

1. Any Zynq overlay tarball in the tree must pass the gates the Zybo target runs on its overlay.
   Red on 74fa34e5 (the June tarball); the fix removes the tarball.
2. Positive control: the target declares the derived, ignored directory, and the derive script runs
   the native edge library install and the build-host-data fix the gates check.
"""
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TARBALL = ROOT / "sw/linux/python_overlay.tar.gz"
OVERLAY = "sw/linux/python_overlay"
EXTRACT = ROOT / "sw/linux/extract_python_overlay.sh"
LFS_HEADER = b"version https://git-lfs.github.com/spec/v1"


def test_a_zynq_overlay_tarball_in_the_tree_passes_the_zybo_stage_gates(tmp_path):
    if not TARBALL.exists():
        return  # derived only (DS-P1.106); the positive control below holds the derive path
    data = TARBALL.read_bytes()
    assert data[:len(LFS_HEADER)] != LFS_HEADER, "an unfetched git-lfs pointer is not an overlay"
    with tarfile.open(TARBALL) as tar:
        tar.extractall(tmp_path, filter="tar")
    check = subprocess.run([sys.executable, str(ROOT / "sw/linux/clean_python_overlay.py"), "--check",
                            str(tmp_path)], capture_output=True, text=True, errors="replace", check=False)
    assert check.returncode == 0, check.stdout + check.stderr
    assert (tmp_path / "usr/local/lib/deepskopion/libds_edge_drain.so").is_file()
    assert (tmp_path / "etc/deepskopion-native-edge").is_file()


def test_the_zybo_target_builds_its_overlay_from_buildroot():
    doc = yaml.safe_load((ROOT / "targets/zybo_datalogger.yml").read_text(encoding="utf-8"))
    rootfs = doc["linux"]["rootfs"]
    assert rootfs["python_overlay"] == OVERLAY
    assert "clean_python_overlay.py --check sw/linux/python_overlay" in rootfs["stage_command"]
    assert "native_edge_lib.py check --profile zybo" in rootfs["stage_command"]
    ignored = subprocess.run(["git", "check-ignore", "-q", f"{OVERLAY}/x"], cwd=ROOT, check=False)
    assert ignored.returncode == 0, f"{OVERLAY}/ must be an ignored build product"
    script = EXTRACT.read_text(encoding="utf-8")
    assert 'native_edge_lib.py" install --profile zybo' in script
    assert 'clean_python_overlay.py" --fix' in script
    assert "buildroot/output/target" in script
