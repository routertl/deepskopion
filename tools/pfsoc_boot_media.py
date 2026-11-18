#!/usr/bin/env python3
"""Assemble and inspect PolarFire SoC Discovery Kit boot media (DS-P2.147).

WHY THIS EXISTS
---------------
Since RTL-P2.1271 the sanctioned ``rr linux sdcard`` path
(``sdk.cli.commands.linux_steps._pfsoc`` ``_assemble_raw_image``) emits a
GPT (``label: gpt`` — never MBR); for a target whose ``sdcard.rootfs:
partition`` (this kit — see ``targets/pfsoc_datalogger.yml``) it also lays a
P3 ext4 rootfs (``ext4_img`` is passed, not ``None``). This in-tree assembler
is the OFFLINE sibling of that path: it produces + inspects the same GPT
layout without a full board-side ``rr linux`` run, and it must stay
byte-for-byte reconciled with it. This kit needs:

  1. ``payload.bin`` wrapping S-mode U-Boot (exec-addr 0x80200000, prv_s,
     owner-hart u54_1) via the committed ``generate_payload`` caller — never the
     HSS ELF — on its OWN raw GPT partition (P1), the HSS-payload type the disco
     HSS scans (CONFIG_SERVICE_BOOT_MMC_USE_GPT);
  2. mainline kernel ``Image`` + ``microchip-mpfs-disco-kit`` DTB on the FAT (P2);
  3. P3 ext4 carrying the riscv64 ``python_overlay`` consumed through the
     deployment contract (``stage_daemon.sh``) — bootargs root=/dev/mmcblk0p3.

DS-P2.149 reconciled this in-tree assembler from a 2-partition MBR onto the
>=3-partition GPT the deployed card and RTL-P2.1271 ``rr linux sdcard`` use.

This tool is the in-tree assembler + offline inspector. It does not touch the
board. A full ``rr linux`` chain still produces the real kernel/U-Boot; tests
drive it with stand-in blobs so the layout/guards stay alive under cron.

    python3 tools/pfsoc_boot_media.py assemble --from <staging> --out sdcard.img
    python3 tools/pfsoc_boot_media.py inspect <sdcard.img>
    python3 tools/pfsoc_boot_media.py stage   # rr linux rootfs entry
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
LAYOUT_REL = "sw/linux/pfsoc/boot_media.yml"
STAGE_MOD = ROOT / "tools" / "pfsoc_hss_lim_stage.py"
DEFAULT_STAGING = ROOT / "sw" / "linux" / "pfsoc" / "out" / "pfsoc_datalogger"
DEFAULT_HSS_DIR = ROOT / "sw" / "linux" / "pfsoc" / "src" / "hart-software-services"
SECTOR = 512
# GPT partition type GUIDs — MUST match the deployed card / the RTL-P2.1271
# ``rr linux sdcard`` path (sdk.cli.commands.linux_steps._pfsoc). The disco
# kit HSS finds its payload by SCANNING the HSS-payload GPT type
# (CONFIG_SERVICE_BOOT_MMC_USE_GPT), never a FAT filename, so payload.bin is its
# OWN raw partition (P1), the FAT boot partition is P2, and the ext4 rootfs is
# P3 — which is why the target's live bootargs pin ``root=/dev/mmcblk0p3``
# (DS-P2.148/150). The pre-DS-P2.149 in-tree assembler laid a 2-partition MBR
# (p1 FAT + p2 ext4); that divergent layout is what seeded the discarded
# DS-P2.150 ``root=p3->p2`` regression. Reconciled onto GPT here.
GPT_HSS_PAYLOAD_TYPE = "21686148-6449-6E6F-744E-656564454649"
GPT_FAT_TYPE = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
GPT_LINUX_FS_TYPE = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
REFUSED_HSS = "payload.bin must not wrap the HSS"
REFUSED_OVERLAY = "dropped the overlay"
HSS_PAYLOAD_MAGIC = struct.pack("<I", 0xB007C0DE)
REQUIRED_PAYLOAD = {
    "exec_addr": "0x80200000",
    "priv_mode": "prv_s",
    "owner_hart": "u54_1",
    "never_wraps": "hss",
}


def _load_stage():
    spec = importlib.util.spec_from_file_location("pfsoc_hss_lim_stage", STAGE_MOD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if path is not None:
        return path
    for d in ("/sbin", "/usr/sbin", "/usr/local/sbin"):
        cand = Path(d) / tool
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    raise RuntimeError(f"required root-free SD tool not found: {tool}")


def load_layout(root: Path) -> dict:
    path = root / LAYOUT_REL
    if not path.is_file():
        raise RuntimeError(f"missing boot-media layout: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} is not a mapping")
    return data


def _mib_bytes(mib: int) -> int:
    return int(mib) * 1024 * 1024


def _du_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_symlink():
            total += 128
        elif p.is_file():
            total += p.stat().st_size
    return total


def _rootfs_size_bytes(layout: dict, rootfs_dir: Path) -> int:
    declared = int((layout.get("layout") or {}).get("rootfs_mib") or 0)
    if declared > 0:
        return _mib_bytes(declared)
    headroom = int((layout.get("layout") or {}).get("rootfs_headroom_mib") or 16)
    needed = _du_bytes(rootfs_dir) + _mib_bytes(headroom)
    return max(_mib_bytes(8), (needed + 1024 * 1024 - 1) // (1024 * 1024) * 1024 * 1024)


def require_payload_spec(layout: dict) -> dict:
    """Read ``boot_media.yml`` ``payload:`` keys and refuse a drifted recipe.

    assemble() used to ignore these identifiers and bake hardcoded YAML, so a
    layout that claimed ``never_wraps: hss`` could still wrap whatever blob it
    was handed. The keys are load-bearing now.
    """
    spec = layout.get("payload")
    if not isinstance(spec, dict):
        raise SystemExit(f"{REFUSED_HSS}: boot_media.yml is missing payload: keys")
    entry = str(spec.get("generator_entrypoint") or "")
    if "generate_payload" not in entry:
        raise SystemExit(
            f"{REFUSED_HSS}: payload.generator_entrypoint must be the committed "
            "generate_payload caller (tools/pfsoc_hss_lim_stage.py:generate_payload)"
        )
    for key, expected in REQUIRED_PAYLOAD.items():
        got = str(spec.get(key) or "")
        if got != expected:
            raise SystemExit(
                f"{REFUSED_HSS}: payload.{key}={got!r} (need {expected})"
            )
    return spec


def require_overlay(rootfs_dir: Path, markers: list[str]) -> None:
    missing = [m for m in markers if not (rootfs_dir / m).is_file()]
    if missing:
        raise SystemExit(
            f"{REFUSED_OVERLAY}: rootfs is missing {', '.join(missing)} "
            "(riscv64 python_overlay / daemon must be staged through the "
            "deployment contract before assembly)."
        )


def _build_fat(img: Path, size_bytes: int, items: dict[str, Path], label: str) -> None:
    mformat = _require("mformat")
    mcopy = _require("mcopy")
    with open(img, "wb") as fh:
        fh.truncate(size_bytes)
    env = {**os.environ, "MTOOLS_SKIP_CHECK": "1"}
    rc = subprocess.run(
        [mformat, "-i", str(img), "-v", label, "::"],
        env=env, check=False, capture_output=True,
    ).returncode
    if rc != 0:
        raise RuntimeError(f"mformat failed (exit {rc})")
    for name, src in items.items():
        rc = subprocess.run(
            [mcopy, "-i", str(img), str(src), f"::/{name}"],
            env=env, check=False, capture_output=True,
        ).returncode
        if rc != 0:
            raise RuntimeError(f"mcopy failed staging {name} (exit {rc})")


def _build_ext4(img: Path, size_bytes: int, rootdir: Path) -> None:
    mke2fs = _require("mke2fs")
    blocks = max(1, size_bytes // 1024)
    rc = subprocess.run(
        [mke2fs, "-F", "-t", "ext4", "-b", "1024", "-L", "rootfs",
         "-d", str(rootdir), str(img), str(blocks)],
        check=False, capture_output=True,
    ).returncode
    if rc != 0:
        raise RuntimeError(f"mke2fs failed (exit {rc})")


def _sectors_for_bytes(nbytes: int) -> int:
    return (int(nbytes) + SECTOR - 1) // SECTOR


def _align_up(sectors: int, align: int) -> int:
    return ((sectors + align - 1) // align) * align


def _assemble_gpt(out_img: Path, payload_img: Path, fat_img: Path, fat_mib: int,
                  ext4_img: Path, ext4_mib: int, align_mib: int) -> tuple[int, int, int]:
    """Lay a >=3-partition GPT boot medium the disco-kit HSS boots from.

    P1 = raw HSS payload (yocto payload type GUID; HSS MMC_USE_GPT scans it),
    P2 = FAT boot (bootable), P3 = ext4 rootfs (== bootargs root=/dev/mmcblk0p3).
    Mirrors RTL-P2.1271 ``rr linux sdcard`` so the in-tree assembler and the
    deployed card agree. Returns (payload_off, fat_off, ext4_off) in bytes.
    """
    sfdisk = _require("sfdisk")
    dd = _require("dd")
    spm = (1024 * 1024) // SECTOR
    align = max(1, align_mib) * spm
    cursor = align

    parts: list[tuple[Path, int]] = []
    lines: list[str] = []

    payload_size = _sectors_for_bytes(payload_img.stat().st_size)
    payload_start = cursor
    lines.append(
        f"start={cursor}, size={payload_size}, "
        f"type={GPT_HSS_PAYLOAD_TYPE}, name=payload"
    )
    parts.append((payload_img, cursor))
    cursor = _align_up(cursor + payload_size, align)

    fat_size = fat_mib * spm
    fat_start = cursor
    lines.append(
        f"start={cursor}, size={fat_size}, type={GPT_FAT_TYPE}, name=boot, bootable"
    )
    parts.append((fat_img, cursor))
    cursor = cursor + fat_size

    cursor = _align_up(cursor, align)
    ext4_size = ext4_mib * spm
    ext4_start = cursor
    lines.append(
        f"start={cursor}, size={ext4_size}, type={GPT_LINUX_FS_TYPE}, name=root"
    )
    parts.append((ext4_img, cursor))
    cursor = cursor + ext4_size

    # GPT backup header lives in the last 33 sectors; keep a full alignment
    # unit of tail so sfdisk can write it.
    total = cursor + max(align, 34)
    with open(out_img, "wb") as fh:
        fh.truncate(total * SECTOR)

    script = "label: gpt\n" + "\n".join(lines) + "\n"
    proc = subprocess.run(
        [sfdisk, str(out_img)], input=script, text=True,
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sfdisk failed writing GPT: {proc.stderr}")
    for src, start in parts:
        rc = subprocess.run(
            [dd, f"if={src}", f"of={out_img}", f"bs={SECTOR}",
             f"seek={start}", "conv=notrunc", "status=none"],
            check=False, capture_output=True,
        ).returncode
        if rc != 0:
            raise RuntimeError(f"dd failed writing {src.name}")
    return payload_start * SECTOR, fat_start * SECTOR, ext4_start * SECTOR


def _emit_payload(
    hss_dir: Path,
    next_stage: Path,
    payload_out: Path,
    spec: dict,
) -> Path:
    stage = _load_stage()
    stage.refuse_hss_next_stage(next_stage)
    yaml_text = stage.build_payload_config(
        next_stage,
        exec_addr=str(spec["exec_addr"]),
        priv_mode=str(spec["priv_mode"]),
        owner_hart=str(spec["owner_hart"]),
    )
    if "0x08000000" in yaml_text or "prv_m" in yaml_text:
        raise SystemExit(
            f"{REFUSED_HSS}: generate_payload YAML would wrap E51 firmware "
            "(LIM 0x08000000 / prv_m)."
        )
    payload_out.parent.mkdir(parents=True, exist_ok=True)
    yaml_path = payload_out.with_name("uboot_payload.yaml")
    yaml_path.write_text(yaml_text, encoding="utf-8")
    generator = stage.find_payload_generator(hss_dir)
    if generator is None:
        raise SystemExit(
            "hss-payload-generator not found (build it: "
            "make -C <hss_dir>/tools/hss-payload-generator). "
            "libyaml-dev is required and is installed (0.2.2). "
            "assemble() will not copy next-stage bytes into payload.bin."
        )
    stage.generate_payload(
        generator,
        next_stage,
        payload_out,
        exec_addr=str(spec["exec_addr"]),
        priv_mode=str(spec["priv_mode"]),
        owner_hart=str(spec["owner_hart"]),
    )
    return yaml_path


def _resolve_hss_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    env = (os.environ.get("PFSOC_HSS_DIR") or "").strip()
    if env:
        return Path(env)
    return DEFAULT_HSS_DIR


def assemble(root: Path, staging: Path, out_img: Path,
             next_stage: Path | None = None,
             rootfs_dir: Path | None = None,
             hss_dir: Path | None = None) -> Path:
    layout = load_layout(root)
    spec = require_payload_spec(layout)
    next_stage = next_stage or (staging / "u-boot.bin")
    if not next_stage.is_file():
        raise SystemExit(f"next-stage U-Boot image missing: {next_stage}")
    # HSS-wrap refuse BEFORE overlay packing so a missing overlay cannot
    # mask the category error (DS-P2.147).
    _load_stage().refuse_hss_next_stage(next_stage)
    rootfs_dir = rootfs_dir or (staging / "rootfs")
    markers = list(layout.get("rootfs_markers") or [])
    if not rootfs_dir.is_dir():
        raise SystemExit(f"{REFUSED_OVERLAY}: rootfs directory missing: {rootfs_dir}")
    require_overlay(rootfs_dir, markers)

    work = staging / ".boot_media_work"
    work.mkdir(parents=True, exist_ok=True)
    payload_out = work / "payload.bin"
    yaml_path = _emit_payload(
        _resolve_hss_dir(hss_dir), next_stage, payload_out, spec,
    )

    dtb_name = str(layout.get("kernel_dtb") or "microchip-mpfs-disco-kit") + ".dtb"
    fat_items: dict[str, Path] = {}
    mapping = {
        "payload.bin": payload_out,
        "u-boot.bin": next_stage,
        "uboot_payload.yaml": yaml_path,
        "Image": staging / "Image",
        "boot.scr": staging / "boot.scr",
        "system.dtb": staging / "system.dtb" if (staging / "system.dtb").is_file()
        else staging / dtb_name,
        dtb_name: staging / dtb_name if (staging / dtb_name).is_file()
        else staging / "system.dtb",
    }
    required_fat = list(layout.get("fat_files") or [])
    for name in required_fat:
        src = mapping.get(name)
        if src is None or not src.is_file():
            raise SystemExit(f"FAT member missing from staging: {name}")
        fat_items[name] = src

    loc = layout.get("layout") or {}
    fat_mib = int(loc.get("fat_mib") or 64)
    align_mib = int(loc.get("align_mib") or 1)
    label = str(loc.get("fat_label") or "BOOT")
    # Tests use a tiny FAT so the image stays small; never smaller than 4 MiB
    # (mformat needs room) and never smaller than the staged files + slack.
    fat_bytes = max(_mib_bytes(fat_mib), _mib_bytes(4),
                    sum(p.stat().st_size for p in fat_items.values()) + 1024 * 1024)
    fat_mib = (fat_bytes + 1024 * 1024 - 1) // (1024 * 1024)
    ext4_bytes = _rootfs_size_bytes(layout, rootfs_dir)
    ext4_mib = (ext4_bytes + 1024 * 1024 - 1) // (1024 * 1024)

    fat_img = work / "boot.fat"
    ext4_img = work / "rootfs.ext4"
    _build_fat(fat_img, _mib_bytes(fat_mib), fat_items, label)
    _build_ext4(ext4_img, _mib_bytes(ext4_mib), rootfs_dir)
    out_img.parent.mkdir(parents=True, exist_ok=True)
    # payload.bin is written as its OWN raw GPT partition (P1) — NOT a FAT file:
    # the disco-kit HSS scans the HSS-payload GPT type, never a FAT filename.
    _assemble_gpt(out_img, payload_out, fat_img, fat_mib, ext4_img, ext4_mib, align_mib)
    return out_img


def _guid_to_str(b16: bytes) -> str:
    """Canonical UPPERCASE GUID string from the on-disk mixed-endian 16 bytes."""
    d1 = struct.unpack_from("<I", b16, 0)[0]
    d2 = struct.unpack_from("<H", b16, 4)[0]
    d3 = struct.unpack_from("<H", b16, 6)[0]
    d4 = b16[8:10]
    d5 = b16[10:16]
    return f"{d1:08X}-{d2:04X}-{d3:04X}-{d4.hex().upper()}-{d5.hex().upper()}"


def parse_gpt(blob: bytes) -> list[tuple[str, int, int, str]]:
    """Return (type_guid, start_lba, size_lba, name) per GPT partition, in order."""
    if len(blob) < 2 * SECTOR or blob[SECTOR:SECTOR + 8] != b"EFI PART":
        raise RuntimeError("image has no GPT primary header (EFI PART) at LBA1")
    hdr = blob[SECTOR:SECTOR + 92]
    part_lba = struct.unpack_from("<Q", hdr, 72)[0]
    num = struct.unpack_from("<I", hdr, 80)[0]
    esize = struct.unpack_from("<I", hdr, 84)[0]
    base = part_lba * SECTOR
    zero = b"\x00" * 16
    parts: list[tuple[str, int, int, str]] = []
    for i in range(num):
        ent = blob[base + i * esize:base + (i + 1) * esize]
        if len(ent) < 56:
            break
        tguid = ent[0:16]
        if tguid == zero:
            continue
        first = struct.unpack_from("<Q", ent, 32)[0]
        last = struct.unpack_from("<Q", ent, 40)[0]
        name = ent[56:128].decode("utf-16-le", "replace").rstrip("\x00")
        parts.append((_guid_to_str(tguid), first, last - first + 1, name))
    return parts


def _mtype(img: Path, fat_off: int, name: str) -> bytes:
    mtype = _require("mtype")
    env = {**os.environ, "MTOOLS_SKIP_CHECK": "1"}
    proc = subprocess.run(
        [mtype, "-i", f"{img}@@{fat_off}", f"::/{name}"],
        env=env, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"FAT readback of {name} failed")
    return proc.stdout


def _debugfs_cat(img: Path, ext4_off: int, name: str) -> bytes:
    debugfs = _require("debugfs")
    proc = subprocess.run(
        [debugfs, "-R", f"cat /{name}", f"{img}?offset={ext4_off}"],
        capture_output=True, check=False,
    )
    stderr = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0 or not proc.stdout or "not found" in stderr.lower():
        raise RuntimeError(f"ext4 readback of {name} failed: {stderr}")
    return proc.stdout


def inspect(img: Path, layout: dict | None = None) -> str:
    if not img.is_file():
        raise SystemExit(f"boot media image missing: {img}")
    layout = layout or load_layout(ROOT)
    blob = img.read_bytes()
    parts = parse_gpt(blob)
    # The disco-kit boot medium is a >=3-partition GPT, in order: P1 raw HSS
    # payload, P2 FAT boot, P3 ext4 rootfs (== bootargs root=/dev/mmcblk0p3).
    if len(parts) < 3:
        raise SystemExit(
            f"{REFUSED_OVERLAY}: image has {len(parts)} GPT partitions "
            "(need P1 raw HSS payload / P2 FAT boot / P3 ext4 rootfs)"
        )
    payload_part, fat_part, ext4_part = parts[0], parts[1], parts[2]
    if payload_part[0].upper() != GPT_HSS_PAYLOAD_TYPE.upper():
        raise SystemExit(
            f"{REFUSED_HSS}: P1 is not the raw HSS-payload GPT partition "
            f"(type {payload_part[0]}, need {GPT_HSS_PAYLOAD_TYPE})"
        )
    if fat_part[0].upper() != GPT_FAT_TYPE.upper():
        raise SystemExit(f"image P2 is not a FAT boot GPT partition (type {fat_part[0]})")
    if ext4_part[0].upper() != GPT_LINUX_FS_TYPE.upper():
        raise SystemExit(
            f"{REFUSED_OVERLAY}: P3 is not a Linux-fs GPT partition "
            f"(type {ext4_part[0]}); the ext4 rootfs must be partition 3 "
            "(bootargs root=/dev/mmcblk0p3)"
        )
    payload_off = payload_part[1] * SECTOR
    fat_off = fat_part[1] * SECTOR
    ext4_off = ext4_part[1] * SECTOR
    payload_nbytes = payload_part[2] * SECTOR

    lines = [
        f"P1 raw HSS payload @ {payload_off} type={payload_part[0]} "
        f"size_mib={payload_part[2] * SECTOR // (1024 * 1024)}",
    ]
    # payload.bin is the RAW P1 partition, read straight from the image bytes —
    # never a FAT file (the HSS scans the GPT type, not a filename).
    raw_payload = blob[payload_off:payload_off + payload_nbytes]
    yaml_text = ""
    uboot_bytes = b""
    for name in layout.get("fat_files") or []:
        data = _mtype(img, fat_off, name)
        lines.append(f"  FAT {name} ({len(data)} bytes)")
        if name.endswith(".yaml") or name.endswith(".yml"):
            yaml_text = data.decode("utf-8", "replace")
        if name == "u-boot.bin":
            uboot_bytes = data
    if not yaml_text:
        raise SystemExit("FAT is missing uboot_payload.yaml (payload wrapping recipe)")
    spec = require_payload_spec(layout)
    if (
        spec["exec_addr"] not in yaml_text
        or spec["priv_mode"] not in yaml_text
        or spec["owner_hart"] not in yaml_text
    ):
        raise SystemExit(
            f"{REFUSED_HSS}: FAT payload recipe is not S-mode U-Boot "
            "(need exec-addr 0x80200000, priv-mode prv_s, owner-hart u54_1)"
        )
    if "0x08000000" in yaml_text or "hss-lim.elf" in yaml_text or "prv_m" in yaml_text:
        raise SystemExit(
            f"{REFUSED_HSS}: FAT payload recipe wraps E51 firmware"
        )
    if not raw_payload.startswith(HSS_PAYLOAD_MAGIC):
        raise SystemExit(
            f"{REFUSED_HSS}: P1 raw payload bytes must carry HSS magic "
            "0xb007c0de (raw U-Boot is not a payload HSS will load)"
        )
    if uboot_bytes and raw_payload[:len(uboot_bytes)] == uboot_bytes:
        raise SystemExit(
            f"{REFUSED_HSS}: P1 raw payload is a raw copy of u-boot.bin "
            "(not an HSS payload; missing magic 0xb007c0de)"
        )
    if uboot_bytes and any(
        marker in uboot_bytes
        for marker in (b"HSS-FIRMWARE", b"Hart Software Service", b"Hart Software Services")
    ):
        raise SystemExit(
            f"{REFUSED_HSS}: FAT u-boot.bin bytes are HSS firmware"
        )
    lines.append(
        "payload wraps U54 next-stage exec-addr=0x80200000 priv-mode=prv_s "
        "owner-hart=u54_1 magic=0xb007c0de"
    )
    lines.append(
        f"P2 FAT boot @ {fat_off} type={fat_part[0]} "
        f"size_mib={fat_part[2] * SECTOR // (1024 * 1024)}"
    )
    lines.append(
        f"GPT P3 ext4 rootfs @ {ext4_off} type={ext4_part[0]} "
        f"size_mib={ext4_part[2] * SECTOR // (1024 * 1024)}"
    )
    for name in layout.get("rootfs_markers") or []:
        data = _debugfs_cat(img, ext4_off, name)
        lines.append(f"  ROOTFS {name} ({len(data)} bytes)")
    report = "\n".join(lines) + "\n"
    return report


def stage(root: Path, hss_dir: Path | None = None) -> Path | None:
    """``rr linux rootfs`` entry: assemble when the chain is in the out dir.

    ``rr linux sdcard`` runs rootfs BEFORE U-Boot/kernel, so a hard fail on
    a missing Image would deadlock the sanctioned chain. When the artefacts
    are present this calls assemble() (fail-closed on a missing generator).
    """
    layout = load_layout(root)
    require_payload_spec(layout)
    staging = DEFAULT_STAGING
    out_img = staging / "sdcard.img"
    required = list(layout.get("fat_files") or [])
    # payload.bin and uboot_payload.yaml are produced by assemble itself.
    chain = [n for n in required if n not in ("payload.bin", "uboot_payload.yaml")]
    missing = [n for n in chain if not (staging / n).is_file()]
    rootfs_dir = staging / "rootfs"
    if not rootfs_dir.is_dir():
        missing.append("rootfs/")
    if missing:
        print(
            "PFSoC boot-media assemble deferred: missing "
            + ", ".join(missing)
            + f" under {staging} (rr linux sdcard builds those after rootfs)."
        )
        return None
    return assemble(
        root, staging, out_img, hss_dir=_resolve_hss_dir(hss_dir),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("assemble", help="assemble sdcard.img from a staging dir")
    a.add_argument("--root", type=Path, default=ROOT)
    a.add_argument("--from", dest="staging", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--next-stage", type=Path, default=None)
    a.add_argument("--rootfs", type=Path, default=None)
    a.add_argument("--hss-dir", type=Path, default=None)
    st = sub.add_parser("stage", help="rr linux rootfs entry (assemble when ready)")
    st.add_argument("--root", type=Path, default=ROOT)
    st.add_argument("--hss-dir", type=Path, default=None)
    i = sub.add_parser("inspect", help="offline partition/payload layout check")
    i.add_argument("image", type=Path)
    i.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        if args.command == "assemble":
            img = assemble(
                args.root.resolve(), args.staging.resolve(), args.out.resolve(),
                next_stage=args.next_stage.resolve() if args.next_stage else None,
                rootfs_dir=args.rootfs.resolve() if args.rootfs else None,
                hss_dir=args.hss_dir.resolve() if args.hss_dir else None,
            )
            print(f"assembled {img}")
            return 0
        if args.command == "stage":
            img = stage(
                args.root.resolve(),
                hss_dir=args.hss_dir.resolve() if args.hss_dir else None,
            )
            if img is not None:
                print(f"assembled {img}")
            return 0
        report = inspect(args.image.resolve(), load_layout(args.root.resolve()))
        print(report, end="")
        return 0
    except SystemExit as exc:
        msg = str(exc)
        if msg and msg != "0":
            print(f"REFUSED: {msg}")
            return 1
        raise
    except RuntimeError as exc:
        print(f"REFUSED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
