# rr unknown-llm (AI-generated)
"""DS-P2.149 — the in-tree pfsoc assembler must build onto the DEPLOYED GPT layout.

WHY THIS GUARD EXISTS (the defect it catches). DS-P2.148/150 proved the kit
boots only off a >=3-partition **GPT** card — P1 raw HSS payload, P2 FAT boot,
P3 ext4 rootfs — and the target's live bootargs pin ``root=/dev/mmcblk0p3``
accordingly. But the in-tree assembler ``tools/pfsoc_boot_media.py`` still laid a
2-partition **MBR** (``label: dos``: p1 FAT carrying payload.bin as a FILE, p2
ext4). That is the exact divergence ``targets/pfsoc_datalogger.yml`` flagged as a
"KNOWN DISCREPANCY … NOT reconciled": an operator who ran the in-tree assembler
got a card whose rootfs is p2, so a ``root=/dev/mmcblk0p3`` kernel VFS-panics and
reaches no shell — and conflating the two layouts is precisely what seeded the
discarded DS-P2.150 ``root=p3->p2`` regression. DS-P2.149 reconciles the in-tree
assembler onto the same GPT layout the RTL-P2.1271 ``rr linux sdcard`` path and
the deployed card use, so both agree and ``root=p3`` is correct for the image
this tool emits.

These guards assert at the CLI surface a caller actually reaches
(``python3 tools/pfsoc_boot_media.py assemble|inspect``) on a real assembled
image — no board:

  * the on-disk partition table is a GPT (primary header ``EFI PART``), never an
    MBR ``label: dos``;
  * the HSS payload is a RAW GPT partition of the yocto HSS-payload type
    (``21686148-6449-6E6F-744E-656564454649``) at partition 1 carrying the
    0xb007c0de magic — NOT a FAT file (the HSS ``CONFIG_SERVICE_BOOT_MMC_USE_GPT``
    scans the partition type, never a FAT filename);
  * the ext4 rootfs is partition 3, matching the target's live bootargs
    ``root=/dev/mmcblk0p3`` — so the image this tool emits boots where the
    deployed card boots.

BASELINE (pre-fix, MBR assembler): assemble emits ``label: dos`` with p1 FAT / p2
ext4 and payload.bin as a FAT file; inspect prints "P1 FAT boot" / "P2 ext4
rootfs" and no P3. ``test_assemble_emits_gpt_hss_payload_partition_layout`` fires
with intended_assertion ``"GPT P3 ext4 rootfs"``.

MUTATION (why the guard is non-vacuous): reverting ``_assemble_gpt`` to the MBR
``label: dos`` layout makes the GPT primary-header / P3-rootfs assertions RED
again; the raw-payload-partition assertion goes RED if payload.bin is put back
into the FAT instead of its own raw partition.
"""
from __future__ import annotations

import os
import stat
import struct
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "pfsoc_boot_media.py"
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"
LAYOUT = ROOT / "sw" / "linux" / "pfsoc" / "boot_media.yml"

# The yocto ``mpfs-*.wks`` HSS-payload GPT type the disco-kit HSS scans
# (CONFIG_SERVICE_BOOT_MMC_USE_GPT); mirrors RTL-P2.1271 _pfsoc.py.
HSS_PAYLOAD_GPT_TYPE = "21686148-6449-6E6F-744E-656564454649"
FAT_GPT_TYPE = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LINUX_FS_GPT_TYPE = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
# 0xB007C0DE little-endian — the HSS-payload magic the raw P1 bytes must carry.
HSS_PAYLOAD_MAGIC_LE = bytes.fromhex("dec007b0")
_SECTOR = 512


def _gpt_partitions(blob: bytes) -> list[tuple[str, int, int]]:
    """INDEPENDENT GPT parse for the receipt identity — deliberately NOT the
    tool's own ``parse_gpt``/``inspect`` so a receipt that flips only the tool's
    report strings cannot keep this green. Returns (type_guid_UPPER, first_lba,
    last_lba) per non-empty partition entry, in on-disk order.
    """
    assert blob[_SECTOR:_SECTOR + 8] == b"EFI PART", "no GPT primary header at LBA1"
    hdr = blob[_SECTOR:_SECTOR + 92]
    part_lba = struct.unpack_from("<Q", hdr, 72)[0]
    num = struct.unpack_from("<I", hdr, 80)[0]
    esize = struct.unpack_from("<I", hdr, 84)[0]
    base = part_lba * _SECTOR
    out: list[tuple[str, int, int]] = []
    for i in range(num):
        ent = blob[base + i * esize:base + (i + 1) * esize]
        if len(ent) < 56 or ent[0:16] == b"\x00" * 16:
            continue
        d1 = struct.unpack_from("<I", ent, 0)[0]
        d2 = struct.unpack_from("<H", ent, 4)[0]
        d3 = struct.unpack_from("<H", ent, 6)[0]
        guid = (
            f"{d1:08X}-{d2:04X}-{d3:04X}-"
            f"{ent[8:10].hex().upper()}-{ent[10:16].hex().upper()}"
        )
        first = struct.unpack_from("<Q", ent, 32)[0]
        last = struct.unpack_from("<Q", ent, 40)[0]
        out.append((guid, first, last))
    return out

ROOTFS_MARKERS = (
    "etc/init.d/S99deepskopion-stream",
    "usr/local/share/deepskopion/sw/stream/__main__.py",
    "usr/bin/python3.12",
)

_STUB_GENERATOR = r"""#!/usr/bin/env python3
import sys
from pathlib import Path
MAGIC = bytes.fromhex("dec007b0")
args = sys.argv[1:]
cfg = Path(args[args.index("-c") + 1])
out = Path(args[-1])
next_path = None
lines = cfg.read_text(encoding="utf-8").splitlines()
for i, line in enumerate(lines):
    if line.startswith("payloads:") and i + 1 < len(lines):
        next_path = Path(lines[i + 1].strip().rstrip(":"))
        break
body = next_path.read_bytes() if next_path and next_path.is_file() else b"WRAP"
out.write_bytes(MAGIC + body)
"""


def _cli(*args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=str(ROOT), capture_output=True, text=True, check=False, env=env,
    )


def _slim_root(tmp: Path) -> Path:
    dest = tmp / "root"
    dest_layout = dest / "sw" / "linux" / "pfsoc"
    dest_layout.mkdir(parents=True)
    data = yaml.safe_load(LAYOUT.read_text(encoding="utf-8"))
    data["layout"]["fat_mib"] = 4
    data["layout"]["rootfs_headroom_mib"] = 8
    dest_layout.joinpath("boot_media.yml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    return dest


def _good_staging(tmp: Path) -> Path:
    staging = tmp / "stage"
    staging.mkdir()
    (staging / "u-boot.bin").write_bytes(b"U-BOOT" * 512)
    (staging / "Image").write_bytes(b"IMAGE" * 256)
    dtb = staging / "microchip-mpfs-disco-kit.dtb"
    dtb.write_bytes(b"DTB-DISCO" * 32)
    (staging / "system.dtb").write_bytes(dtb.read_bytes())
    (staging / "boot.scr").write_bytes(b"BOOTSCR")
    rootfs = staging / "rootfs"
    for rel in ROOTFS_MARKERS:
        p = rootfs / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"overlay-marker:{rel}\n", encoding="utf-8")
    return staging


def _hss_env(tmp: Path) -> dict[str, str]:
    gen_dir = tmp / "hss" / "tools" / "hss-payload-generator"
    gen_dir.mkdir(parents=True)
    gen = gen_dir / "hss-payload-generator"
    gen.write_text(_STUB_GENERATOR, encoding="utf-8")
    gen.chmod(gen.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return {"PFSOC_HSS_DIR": str(tmp / "hss")}


def _assemble(tmp: Path) -> tuple[Path, Path, str]:
    staging = _good_staging(tmp)
    root = _slim_root(tmp)
    out = tmp / "sdcard.img"
    proc = _cli(
        "assemble", "--root", str(root), "--from", str(staging),
        "--out", str(out), extra_env=_hss_env(tmp),
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert out.is_file() and out.stat().st_size > 0, combined
    return out, root, combined


def _bootargs_root() -> str:
    import re
    linux = yaml.safe_load(TARGET.read_text(encoding="utf-8"))["linux"]
    m = re.search(r"root\s*=\s*(/dev/mmcblk\d+p\d+)", str(linux["bootargs"]))
    assert m, "no root= in bootargs"
    return m.group(1)


def test_image_is_gpt_not_mbr(tmp_path: Path) -> None:
    """RECEIPT IDENTITY. The on-disk table must be a GPT whose ORDERED
    partition TYPES are the deployed disco-kit boot medium — not merely a GPT
    header. A bare ``EFI PART`` check is too weak: a GPT that still carries
    ``EFI PART`` but puts the payload on FAT, or the ext4 rootfs on P2, is a
    non-booting medium the ``label: gpt``→``dos`` mutant does NOT exercise. So
    this asserts, via an INDEPENDENT table parse (not the tool's inspect):

      * a GPT primary header at LBA1;
      * P1 type == the HSS-payload GPT type (the HSS scans it), and its RAW
        bytes carry the 0xb007c0de magic — payload is its OWN partition, never
        a FAT file;
      * P2 type == the FAT/ESP boot type;
      * P3 type == the Linux-fs type — the ext4 rootfs is partition THREE
        (root=/dev/mmcblk0p3), never P2.
    """
    out, _root, _ = _assemble(tmp_path)
    blob = out.read_bytes()
    # GPT primary header sits at LBA1 (offset 512) and begins "EFI PART".
    assert blob[512:520] == b"EFI PART", (
        "the in-tree assembler must emit a GPT (found no EFI PART primary "
        "header at LBA1) — the deployed card is a >=3-partition GPT and an MBR "
        "'label: dos' image is not a bootable HSS medium"
    )
    parts = _gpt_partitions(blob)
    assert len(parts) >= 3, (
        f"deployed boot medium is a >=3-partition GPT; found {len(parts)}: {parts}"
    )
    (p1_type, p1_first, p1_last), (p2_type, _, _), (p3_type, _, _) = parts[:3]
    assert p1_type == HSS_PAYLOAD_GPT_TYPE, (
        f"P1 must be the raw HSS-payload GPT type {HSS_PAYLOAD_GPT_TYPE} (the "
        f"HSS scans this type, never a FAT file); P1 is {p1_type}"
    )
    assert p2_type == FAT_GPT_TYPE, (
        f"P2 must be the FAT/ESP boot GPT type {FAT_GPT_TYPE}; P2 is {p2_type}"
    )
    assert p3_type == LINUX_FS_GPT_TYPE, (
        f"P3 must be the Linux-fs GPT type {LINUX_FS_GPT_TYPE} — the ext4 rootfs "
        f"is partition 3 (root=/dev/mmcblk0p3), never P2; P3 is {p3_type}"
    )
    # No OTHER partition may carry the Linux-fs type (ext4 must not also/instead
    # sit on P2 — the exact root=p3->p2 divergence DS-P2.150 discarded).
    assert [t for t, _, _ in parts].count(LINUX_FS_GPT_TYPE) == 1, (
        f"exactly one Linux-fs (ext4) partition, and it must be P3: {parts}"
    )
    # P1 raw bytes are a real HSS payload (magic), proving payload.bin is the
    # raw partition, not a FAT file copied elsewhere.
    p1_bytes = blob[p1_first * _SECTOR:(p1_last + 1) * _SECTOR]
    assert p1_bytes[:4] == HSS_PAYLOAD_MAGIC_LE, (
        "P1 raw bytes must carry the HSS-payload magic 0xb007c0de — payload is "
        "its own raw GPT partition, not a FAT file"
    )


def test_assemble_emits_gpt_hss_payload_partition_layout(tmp_path: Path) -> None:
    """CLI surface: inspect must report P1 raw HSS payload / P2 FAT / P3 ext4.

    intended_assertion (RED at baseline): "GPT P3 ext4 rootfs".
    """
    out, root, _ = _assemble(tmp_path)
    insp = _cli("inspect", str(out), "--root", str(root))
    text = insp.stdout + insp.stderr
    assert insp.returncode == 0, text
    assert "P1 raw HSS payload" in text, (
        "the in-tree assembler must place payload.bin on its OWN raw GPT "
        "partition (HSS payload type), not as a FAT file: " + text
    )
    assert "P2 FAT boot" in text, text
    assert "GPT P3 ext4 rootfs" in text, (
        "the in-tree assembler must emit a GPT P3 ext4 rootfs (deployed-card "
        "layout: p1 raw payload / p2 FAT boot / p3 ext4); found: " + text
    )
    # payload.bin is a raw partition now, never a FAT file.
    assert "FAT payload.bin" not in text, (
        "payload.bin must be a raw GPT partition, not a FAT file (the HSS "
        "scans the partition type, never a FAT filename): " + text
    )
    assert HSS_PAYLOAD_GPT_TYPE in text, (
        "the raw payload partition must carry the yocto HSS-payload GPT type "
        f"{HSS_PAYLOAD_GPT_TYPE}: " + text
    )
    # the raw payload still carries the 0xb007c0de HSS-payload magic.
    assert "0xb007c0de" in text, text
    assert "magic=0xb007c0de" in text, text
    # rootfs markers land on the ext4 (p3).
    for marker in ROOTFS_MARKERS:
        assert f"ROOTFS {marker}" in text, text
    assert "hss-lim.elf" not in text
    assert "prv_m" not in text


def test_ext4_rootfs_is_partition_three_matching_bootargs(tmp_path: Path) -> None:
    """The ext4 rootfs must be partition 3 — where bootargs root= points."""
    out, root, _ = _assemble(tmp_path)
    assert _bootargs_root() == "/dev/mmcblk0p3", _bootargs_root()
    insp = _cli("inspect", str(out), "--root", str(root))
    text = insp.stdout + insp.stderr
    assert insp.returncode == 0, text
    # The report labels the ext4 as P3, so the deployed root=/dev/mmcblk0p3 is
    # the rootfs of the image THIS tool emits (not p2, the FAT boot partition).
    assert "GPT P3 ext4 rootfs" in text, text
    assert "P2 ext4 rootfs" not in text, (
        "ext4 rootfs must be partition 3 (root=p3), not partition 2: " + text
    )
