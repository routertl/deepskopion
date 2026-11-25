# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.357: the PolarFire SoC kernel mounts the card's FAT boot partition.

Measured on the kit on 2026-11-18: `mount -t vfat -o ro /dev/mmcblk0p2 ...` failed "No such device";
/proc/filesystems listed ext2/ext3/ext4 and no FAT; /lib/modules did not exist. The base config builds FAT, VFAT and
the NLS tables as modules (=m) and this image loads no modules, so the boot partition could not be read or updated
from Linux. sw/linux/pfsoc/kernel_pfsoc.config-fragment now builds them in.

Two guards: the fragment declares each symbol =y (always runs), and the kernel `rr linux kernel` built from it has
each one =y (runs where a pfsoc kernel build exists in this checkout; a merge that lets the base =m win would show
here and nowhere else).
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FRAGMENT = ROOT / "sw" / "linux" / "pfsoc" / "kernel_pfsoc.config-fragment"
BUILT = ROOT / "sw" / "linux" / "pfsoc" / "src" / "linux" / ".config"
SYMBOLS = ("CONFIG_FAT_FS", "CONFIG_VFAT_FS", "CONFIG_NLS_CODEPAGE_437", "CONFIG_NLS_ISO8859_1")


def _settings(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("CONFIG_") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            out[line[2:].split(" ", 1)[0]] = "n"
    return out


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_fragment_builds_fat_in(symbol):
    assert _settings(FRAGMENT.read_text(encoding="utf-8")).get(symbol) == "y", f"{symbol}=y missing from {FRAGMENT}"


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_built_pfsoc_kernel_has_fat_built_in(symbol):
    if not BUILT.is_file():
        pytest.skip("host-prerequisite: pfsoc-kernel-build (DS-P2.357) - no pfsoc kernel build in this checkout "
                    "(RR_TARGET=targets/pfsoc_datalogger.yml rr linux kernel writes sw/linux/pfsoc/src/linux/.config)")
    assert _settings(BUILT.read_text(encoding="utf-8", errors="replace")).get(symbol) == "y", \
        f"the built pfsoc kernel does not have {symbol}=y"
