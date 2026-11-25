# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.357 (fix-forward): the default vfat mount on the PolarFire SoC kit needs its NLS tables built in.

Measured on the kit on 2026-11-18 (DS-P2.353 run4): with FAT and VFAT built in, /proc/filesystems listed vfat, but
`mount -t vfat -o ro /dev/mmcblk0p2 ...` failed "Invalid argument". The built .config sets
CONFIG_FAT_DEFAULT_IOCHARSET="ascii" with CONFIG_NLS_ASCII=m, and the image loads no modules; fs/fat/inode.c fails the
mount when load_nls(iocharset) returns nothing. The earlier DS-P2.357 test pinned FAT_FS, VFAT_FS and two NLS tables
by name and could not see this.

These tests pin the class: whatever default IO charset and codepage the built kernel's vfat uses, the NLS table for
each is built in (=y), and when CONFIG_FAT_DEFAULT_UTF8 is on, NLS_UTF8 too (vfat then loads utf8 by default). The fragment test always runs; the built-.config test runs where a pfsoc kernel build exists.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FRAGMENT = ROOT / "sw" / "linux" / "pfsoc" / "kernel_pfsoc.config-fragment"
BUILT = ROOT / "sw" / "linux" / "pfsoc" / "src" / "linux" / ".config"


def _settings(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("CONFIG_") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v.strip('"')
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            out[line[2:].split(" ", 1)[0]] = "n"
    return out


def nls_symbol_for_iocharset(name: str) -> str:
    """The Kconfig symbol of the NLS table load_nls(<name>) needs (fs/nls/nls_<name>.c)."""
    n = name.lower()
    if n.startswith("iso8859-"):
        return "CONFIG_NLS_ISO8859_" + n.split("-", 1)[1]
    if n.startswith("cp"):
        return "CONFIG_NLS_CODEPAGE_" + n[2:]
    return "CONFIG_NLS_" + n.upper().replace("-", "_")


def test_symbol_mapping():
    assert nls_symbol_for_iocharset("ascii") == "CONFIG_NLS_ASCII"
    assert nls_symbol_for_iocharset("utf8") == "CONFIG_NLS_UTF8"
    assert nls_symbol_for_iocharset("iso8859-1") == "CONFIG_NLS_ISO8859_1"
    assert nls_symbol_for_iocharset("cp437") == "CONFIG_NLS_CODEPAGE_437"


def test_fragment_builds_the_ascii_io_charset_in():
    assert _settings(FRAGMENT.read_text(encoding="utf-8")).get("CONFIG_NLS_ASCII") == "y", \
        f"CONFIG_NLS_ASCII=y missing from {FRAGMENT} (the default vfat IO charset is ascii)"


def default_vfat_nls_needs(cfg: dict[str, str]) -> set[str]:
    """The NLS symbols the default `mount -t vfat` loads: the IO charset, the codepage, and utf8 when
    CONFIG_FAT_DEFAULT_UTF8 is on."""
    needed = {
        nls_symbol_for_iocharset(cfg.get("CONFIG_FAT_DEFAULT_IOCHARSET", "iso8859-1")),
        "CONFIG_NLS_CODEPAGE_" + cfg.get("CONFIG_FAT_DEFAULT_CODEPAGE", "437"),
    }
    if cfg.get("CONFIG_FAT_DEFAULT_UTF8") == "y":
        needed.add("CONFIG_NLS_UTF8")
    return needed


def missing_nls(cfg: dict[str, str]) -> list[str]:
    return sorted(s for s in default_vfat_nls_needs(cfg) if cfg.get(s) != "y")


def test_needs_follow_the_config_defaults():
    base = {"CONFIG_FAT_DEFAULT_IOCHARSET": "ascii", "CONFIG_FAT_DEFAULT_CODEPAGE": "437"}
    assert default_vfat_nls_needs(base) == {"CONFIG_NLS_ASCII", "CONFIG_NLS_CODEPAGE_437"}
    assert default_vfat_nls_needs({**base, "CONFIG_FAT_DEFAULT_UTF8": "y"}) == \
        {"CONFIG_NLS_ASCII", "CONFIG_NLS_CODEPAGE_437", "CONFIG_NLS_UTF8"}
    assert default_vfat_nls_needs({"CONFIG_FAT_DEFAULT_IOCHARSET": "iso8859-15", "CONFIG_FAT_DEFAULT_CODEPAGE": "850"}) == \
        {"CONFIG_NLS_ISO8859_15", "CONFIG_NLS_CODEPAGE_850"}


def test_missing_nls_flags_a_module_or_unset_table():
    ok = {"CONFIG_FAT_DEFAULT_IOCHARSET": "ascii", "CONFIG_FAT_DEFAULT_CODEPAGE": "437",
          "CONFIG_NLS_ASCII": "y", "CONFIG_NLS_CODEPAGE_437": "y"}
    assert missing_nls(ok) == []
    assert missing_nls({**ok, "CONFIG_NLS_ASCII": "m"}) == ["CONFIG_NLS_ASCII"]          # the run4 kit config
    assert missing_nls({**ok, "CONFIG_FAT_DEFAULT_UTF8": "y"}) == ["CONFIG_NLS_UTF8"]
    assert missing_nls({k: v for k, v in ok.items() if k != "CONFIG_NLS_CODEPAGE_437"}) == ["CONFIG_NLS_CODEPAGE_437"]


def test_built_kernel_has_the_nls_tables_its_default_vfat_mount_loads():
    if not BUILT.is_file():
        pytest.skip("host-prerequisite: pfsoc-kernel-build (DS-P2.357) - no pfsoc kernel build in this checkout "
                    "(RR_TARGET=targets/pfsoc_datalogger.yml rr linux kernel writes sw/linux/pfsoc/src/linux/.config)")
    cfg = _settings(BUILT.read_text(encoding="utf-8", errors="replace"))
    assert cfg.get("CONFIG_VFAT_FS") == "y", "vfat is not built in"
    missing = missing_nls(cfg)
    assert not missing, f"the default vfat mount loads NLS tables that are not built in: {missing}"
