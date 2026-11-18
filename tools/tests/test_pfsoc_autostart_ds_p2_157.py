# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.157 — a plain cold boot of the PolarFire kit must autostart the daemon.

Measured on the kit 2026-09-23 (fabric DS-P1.60 14949d5), each defect below
blocked the on-kit daemon on its own:

  * bootargs lacked ``init=/init``: the kit boots its ext4 root with no initrd,
    so the kernel ran busybox ``/sbin/init`` (inittab respawns ``/bin/sh``) and
    the deepskopion ``/init`` that runs ``/etc/init.d/S*`` never executed.
  * bootargs carried the DE25-copied ``mem=512M``. On this kit's reference DTB
    that drops the 0x14_1200_0000 RAM fragment, so the ``linux,dma-default``
    pool fails to allocate ("failed to allocate memory for node
    'non-cached-high-buffer'") and macb eth0 fails "Unable to allocate DMA
    memory (error -12)" (mmc0 also loses its ADMA buffers).
  * the riscv64 glibc loader searches ``/lib64`` and ``/usr/lib``, never
    ``/lib``, and the python overlay shipped no ``/lib64`` link: python3 died on
    ``libm.so.6``. ``stage_daemon.sh`` now creates ``lib64 -> lib``.

BASELINE (b327765): all three tests RED. MUTATION: dropping ``init=/init``,
re-adding ``mem=512M``, or deleting the ``lib64`` block in stage_daemon.sh each
turns exactly its own test RED.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"
STAGE = ROOT / "sw" / "linux" / "stage_daemon.sh"
PY_OVERLAY = ROOT / "sw" / "linux" / "pfsoc" / "python_overlay"
TARBALL = ROOT / "sw" / "linux" / "pfsoc" / "python_overlay.tar.gz"


def _bootargs() -> list[str]:
    target = yaml.safe_load(TARGET.read_text(encoding="utf-8"))
    return str(target["linux"]["bootargs"]).split()


def test_bootargs_run_the_deepskopion_init():
    tokens = _bootargs()
    assert "init=/init" in tokens, (
        "pfsoc bootargs lack init=/init: with no initrd the kernel runs busybox "
        "/sbin/init and /etc/init.d/S99deepskopion-stream never starts "
        f"(DS-P2.157). bootargs: {' '.join(tokens)}"
    )


def test_bootargs_do_not_truncate_the_dma_default_pool():
    mem = [t for t in _bootargs() if t.startswith("mem=")]
    assert not mem, (
        f"pfsoc bootargs carry {mem}: on the Discovery Kit reference DTB mem= "
        "keeps the first N MiB of five RAM fragments in address order and drops "
        "0x14_1200_0000, where the linux,dma-default pool lives — eth0 then "
        "fails DMA alloc (-12) (DS-P2.157)"
    )


def test_stage_daemon_links_lib64_to_the_overlay_lib():
    if not PY_OVERLAY.is_dir() and (
        not TARBALL.is_file() or TARBALL.stat().st_size < 1_000_000
    ):
        pytest.skip("DS-P2.157: riscv64 python_overlay not materialized "
                    "(unfetched git-lfs tarball; run `git lfs pull`)")
    proc = subprocess.run(
        ["bash", str(STAGE)], cwd=str(ROOT), capture_output=True, text=True,
        errors="replace", check=False, env=os.environ.copy(),
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"stage_daemon.sh exited {proc.returncode}:\n{out}"
    lib64 = PY_OVERLAY / "lib64"
    assert lib64.is_symlink() and os.readlink(lib64) == "lib", (
        f"{lib64.relative_to(ROOT)} must be a relative link to lib — the riscv64 "
        "loader never searches /lib, so python3 fails on libm.so.6 (DS-P2.157)"
    )
    # The loader-visible effect: the libraries python3 needs resolve via /lib64.
    for name in ("libc.so.6", "libm.so.6"):
        assert (lib64 / name).is_file(), f"/lib64/{name} does not resolve"


def test_sd_rootfs_is_sized_explicitly_with_room_to_write():
    """MEASURED on the kit 2026-09-23: `sdcard.rootfs_mib: 0` (rr autosize =
    du + 8 MiB) built a 51 MiB ext4 at 100% use, 0 bytes free. The daemon's log
    write hit ENOSPC, and S99's python self-test refused to start it."""
    target = yaml.safe_load(TARGET.read_text(encoding="utf-8"))
    mib = int(target["linux"]["sdcard"].get("rootfs_mib") or 0)
    assert mib >= 96, (
        f"linux.sdcard.rootfs_mib = {mib}: the pfsoc rootfs must be sized "
        "explicitly (>= 96 MiB; staged tree ~50 MiB) — autosize left 0 bytes "
        "free and the daemon could not start (DS-P2.157)"
    )
