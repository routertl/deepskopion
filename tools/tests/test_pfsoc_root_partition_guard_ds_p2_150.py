# rr unknown-llm (AI-generated)
"""DS-P2.150: the pfsoc bootargs root= must name the DEPLOYED card's ext4 rootfs
(``/dev/mmcblk0p3``), and no comment in the target may claim a contradictory
root= partition.

WHY THIS GUARD EXISTS (the defect it would have caught). DS-P2.150 is a RETRY: a
prior dispatch (discarded commit ``3f65616``) changed the bootargs
``root=/dev/mmcblk0p3 -> p2`` and shipped a GREEN suite. That was a REGRESSION —
``p2`` is the FAT boot partition, ``p3`` the ext4 rootfs, so ``root=p2`` VFS-panics
and reaches no shell. It was proven on silicon this leg: a plain cold boot loads
``/boot.scr`` from ``mmc 0:2`` (FAT boot = partition 2), and the DS-P2.148 root
login shell booted ``root=/dev/mmcblk0p3`` (the ext4 rootfs at partition 3). The
deployed card is a >=3-partition GPT layout (p1 / p2 FAT boot / p3 ext4 rootfs).
The in-tree assembler ``tools/pfsoc_boot_media.py`` USED to lay a DIFFERENT
2-partition MBR (p1 FAT + p2 ext4) — conflating the two is what misled the prior
dispatch — but DS-P2.149 reconciled it onto the SAME GPT layout as the deployed
card, so both now agree on ``root=/dev/mmcblk0p3``. This guard is layout-agnostic:
it pins the target's live bootargs and comments (p3), whichever assembler runs.

The regression was SEEDED by a stale COMMENT in the target that still claimed
``root=/dev/mmcblk0p2`` while the live bootargs used ``p3``. This guard therefore
asserts both halves:

  1. the live bootargs ``root=`` names the ext4 rootfs ``/dev/mmcblk0p3`` (never
     ``p2``, the FAT boot partition); and
  2. NO comment in the target file claims a ``root=/dev/mmcblkNpM`` that differs
     from the live bootargs — a stale/contradictory comment is the seed the prior
     dispatch followed.

INPUT DOMAIN: the committed ``targets/pfsoc_datalogger.yml``, read as the image
build reads it (bootargs) plus its raw comment text. Pure file+regex — no dtc, no
hardware, no network — so it runs identically under cron/CI and a fresh clone.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"

# The ext4 rootfs of the DEPLOYED bootable card. Measured DS-P2.150: U-Boot loads
# /boot.scr from `mmc 0:2` (FAT boot = p2) and the DS-P2.148 shell booted p3.
EXT4_ROOTFS_PART = "/dev/mmcblk0p3"
FAT_BOOT_PART = "/dev/mmcblk0p2"

# Tolerant of optional whitespace around '=' so a stale "root = /dev/mmcblk0p2"
# comment cannot slip past on spacing alone.
_ROOT_RE = re.compile(r"root\s*=\s*(/dev/mmcblk\d+p\d+)")


def _target_text() -> str:
    return TARGET.read_text(encoding="utf-8")


def _bootargs() -> str:
    return str((yaml.safe_load(_target_text()).get("linux") or {})["bootargs"])


def _bootargs_root() -> str:
    m = _ROOT_RE.search(_bootargs())
    assert m, f"no root=/dev/mmcblkNpM in bootargs: {_bootargs()!r}"
    return m.group(1)


def test_bootargs_root_is_the_deployed_ext4_rootfs_not_the_fat_boot_partition():
    """root= must be the ext4 rootfs (p3), never the FAT boot partition (p2)."""
    root = _bootargs_root()
    assert root != FAT_BOOT_PART, (
        f"bootargs root={root} is the FAT boot partition (p2) — the ext4 rootfs "
        f"of the deployed card is {EXT4_ROOTFS_PART}; root=p2 VFS-panics and "
        "reaches no shell (the DS-P2.150 prior-dispatch regression)"
    )
    assert root == EXT4_ROOTFS_PART, (
        f"bootargs root={root} but the deployed card's ext4 rootfs is "
        f"{EXT4_ROOTFS_PART} (measured: FAT boot at mmc 0:2, ext4 rootfs at p3; "
        "the DS-P2.148 login shell booted p3)"
    )


def test_no_target_comment_claims_a_root_partition_contradicting_bootargs():
    """A comment claiming a different root= partition than the live bootargs is
    exactly what seeded the prior DS-P2.150 root=p3->p2 regression. Forbid it."""
    live_root = _bootargs_root()
    for lineno, line in enumerate(_target_text().splitlines(), 1):
        # Only the part after '#' is a comment (no value in this file contains
        # '#', so this partition is exact for targets/pfsoc_datalogger.yml).
        _code, sep, comment = line.partition("#")
        if not sep:
            continue
        for cm in _ROOT_RE.finditer(comment):
            claimed = cm.group(1)
            assert claimed == live_root, (
                f"target comment L{lineno} claims root={claimed} but the live "
                f"bootargs use root={live_root}: a stale root= claim "
                "contradicting the bootargs is what seeded the DS-P2.150 "
                "root=p3->p2 regression — fix or remove the comment"
            )
