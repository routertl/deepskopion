"""DS-P2.147 — PolarFire boot-media assembly: payload.bin(U-Boot) + overlay rootfs.

The kit boots HSS (DS-P2.145) but had no next stage: no committed recipe that
assembles a flashable sdcard.img with (1) payload.bin wrapping S-mode U-Boot
via ``generate_payload`` (never the HSS ELF), (2) the mpfs-disco-kit kernel
DTB, (3) the riscv64 python_overlay on the ext4 rootfs — P3 of the DS-P2.149
GPT layout (root=/dev/mmcblk0p3), through the declarative ``rr linux`` /
in-tree assembler — never petalinux.

These guards assert at the CLI surface a caller actually reaches
(``python3 tools/pfsoc_boot_media.py assemble|inspect|stage``):

  * wrapping an HSS firmware ELF (by name OR by content) as the next stage is
    REFUSED (exit != 0), and inspect also refuses that image if one exists;
  * dropping the overlay from the rootfs is REFUSED;
  * a missing hss-payload-generator is REFUSED (no raw U-Boot copy into
    payload.bin);
  * a well-formed staging tree produces an image whose GPT/FAT/ext4 layout
    inspects offline, including the raw-partition payload BYTES (magic
    0xb007c0de, not a raw U-Boot copy). No board required (DS-P2.149: GPT).

MUTATION: emptying ``refuse_hss_next_stage`` (allow wrapping hss-lim.elf) →
the assemble CLI exits 0 and the HSS-refuse test goes RED.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "pfsoc_boot_media.py"
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"
LAYOUT = ROOT / "sw" / "linux" / "pfsoc" / "boot_media.yml"
STAGE = ROOT / "tools" / "pfsoc_hss_lim_stage.py"

ROOTFS_MARKERS = (
    "etc/init.d/S99deepskopion-stream",
    "usr/local/share/deepskopion/sw/stream/__main__.py",
    "usr/bin/python3.12",
)

# Minimal stand-in for hss-payload-generator: wrap next-stage bytes in the
# HSS payload magic so inspect can assert on payload.bin BYTES. Parses the
# same ``-c <yaml> <out>`` argv the committed generate_payload caller uses.
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
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _slim_root(tmp: Path) -> Path:
    """A --root tree with a small FAT so the suite stays cheap."""
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


def _write_overlay(rootfs: Path) -> None:
    for rel in ROOTFS_MARKERS:
        path = rootfs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"overlay-marker:{rel}\n", encoding="utf-8")


def _good_staging(tmp: Path) -> Path:
    staging = tmp / "stage"
    staging.mkdir()
    (staging / "u-boot.bin").write_bytes(b"U-BOOT" * 512)
    (staging / "Image").write_bytes(b"IMAGE" * 256)
    dtb = staging / "microchip-mpfs-disco-kit.dtb"
    dtb.write_bytes(b"DTB-DISCO" * 32)
    (staging / "system.dtb").write_bytes(dtb.read_bytes())
    (staging / "boot.scr").write_bytes(b"BOOTSCR")
    _write_overlay(staging / "rootfs")
    return staging


def _stub_hss_dir(tmp: Path) -> Path:
    gen_dir = tmp / "hss" / "tools" / "hss-payload-generator"
    gen_dir.mkdir(parents=True)
    gen = gen_dir / "hss-payload-generator"
    gen.write_text(_STUB_GENERATOR, encoding="utf-8")
    gen.chmod(gen.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tmp / "hss"


def _hss_env(tmp: Path) -> dict[str, str]:
    return {"PFSOC_HSS_DIR": str(_stub_hss_dir(tmp))}


def test_target_pins_disco_dtb_and_partition_rootfs() -> None:
    """Declarative rr linux knobs the assembler and `rr linux dtb` consume."""
    linux = yaml.safe_load(TARGET.read_text(encoding="utf-8"))["linux"]
    # kernel vs U-Boot DTB names DIFFER on this kit (measured 2026-09-12):
    # linux4microchip emits mpfs-disco-kit.dtb, U-Boot microchip-mpfs-….
    assert linux["kernel_dtb"] == "mpfs-disco-kit"
    assert linux["uboot_device_tree"] == "microchip-mpfs-disco-kit"
    assert linux["sdcard"]["rootfs"] == "partition"
    assert linux["rootfs"]["python_overlay"] == "sw/linux/pfsoc/python_overlay"
    stage_cmd = linux["rootfs"]["stage_command"]
    assert "./sw/linux/stage_daemon.sh" in stage_cmd
    assert "python3 tools/pfsoc_boot_media.py stage" in stage_cmd
    layout = yaml.safe_load(LAYOUT.read_text(encoding="utf-8"))
    assert layout["payload"]["never_wraps"] == "hss"
    assert layout["payload"]["exec_addr"] == "0x80200000"
    assert layout["payload"]["priv_mode"] == "prv_s"
    assert layout["payload"]["owner_hart"] == "u54_1"
    assert "generate_payload" in layout["payload"]["generator_entrypoint"]
    src = STAGE.read_text(encoding="utf-8")
    assert "def generate_payload" in src
    assert "refuse_hss_next_stage" in src
    media = TOOL.read_text(encoding="utf-8")
    assert "generate_payload" in media
    assert "exec_addr" in media
    assert "priv_mode" in media
    assert "never_wraps" in media
    assert "generator_entrypoint" in media
    assert "HSS_PAYLOAD_MAGIC" in media
    layout_text = LAYOUT.read_text(encoding="utf-8").lower()
    assert "never petalinux" in layout_text


def test_assemble_cli_refuses_hss_wrapped_payload(tmp_path: Path) -> None:
    """CLI surface: handing HSS firmware to assemble must exit non-zero.

    Covers the basename AND the content path (HSS bytes named u-boot.bin).
    If assemble accidentally emits an image, inspect must also refuse it.

    MUTATION: emptying ``refuse_hss_next_stage`` lets assemble wrap
    ``hss-lim.elf`` and this assertion fails with the intended substring.
    """
    staging = _good_staging(tmp_path)
    hss = staging / "hss-lim.elf"
    hss.write_bytes(b"HSS-FIRMWARE" * 64)
    root = _slim_root(tmp_path)
    hss_env = _hss_env(tmp_path)
    out = tmp_path / "sdcard.img"
    proc = _cli(
        "assemble", "--root", str(root), "--from", str(staging),
        "--out", str(out), "--next-stage", str(hss),
        extra_env=hss_env,
    )
    combined = proc.stdout + proc.stderr
    if out.is_file():
        insp = _cli("inspect", str(out), "--root", str(root))
        combined += insp.stdout + insp.stderr
        assert insp.returncode != 0, (
            "payload.bin must not wrap the HSS: inspect stayed green on the "
            "HSS-wrapped image"
        )
    assert proc.returncode != 0, (
        "payload.bin must not wrap the HSS: assemble exited 0 wrapping hss-lim.elf"
    )
    assert "payload.bin must not wrap the HSS" in combined, combined
    assert "assembled " not in proc.stdout, (
        "payload.bin must not wrap the HSS: assemble printed success wrapping hss-lim.elf"
    )

    # Same bytes, innocent name — the basename-only hole DS-P2.147 refused.
    (staging / "u-boot.bin").write_bytes(b"HSS-FIRMWARE" * 64)
    renamed = _cli(
        "assemble", "--root", str(root), "--from", str(staging),
        "--out", str(tmp_path / "sdcard-renamed.img"),
        extra_env=hss_env,
    )
    renamed_out = renamed.stdout + renamed.stderr
    assert renamed.returncode != 0, (
        "payload.bin must not wrap the HSS: assemble exited 0 wrapping "
        "HSS firmware bytes named u-boot.bin"
    )
    assert "payload.bin must not wrap the HSS" in renamed_out, renamed_out


def test_assemble_cli_fails_closed_without_payload_generator(tmp_path: Path) -> None:
    """Missing hss-payload-generator must not copy next-stage bytes into payload.bin."""
    staging = _good_staging(tmp_path)
    root = _slim_root(tmp_path)
    empty = tmp_path / "empty-hss"
    empty.mkdir()
    out = tmp_path / "sdcard.img"
    proc = _cli(
        "assemble", "--root", str(root), "--from", str(staging),
        "--out", str(out),
        extra_env={"PATH": "/bin:/usr/bin", "PFSOC_HSS_DIR": str(empty)},
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "hss-payload-generator not found" in combined, combined
    assert "assembled " not in proc.stdout
    work_payload = staging / ".boot_media_work" / "payload.bin"
    if work_payload.is_file():
        assert work_payload.read_bytes() != (staging / "u-boot.bin").read_bytes()


def test_assemble_cli_refuses_dropped_overlay(tmp_path: Path) -> None:
    staging = _good_staging(tmp_path)
    (staging / "rootfs" / "usr" / "bin" / "python3.12").unlink()
    root = _slim_root(tmp_path)
    proc = _cli(
        "assemble", "--root", str(root), "--from", str(staging),
        "--out", str(tmp_path / "sdcard.img"),
        extra_env=_hss_env(tmp_path),
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "dropped the overlay" in combined, combined


def test_cli_inspects_partition_and_payload_layout(tmp_path: Path) -> None:
    """Offline inspection of a real assembled image — no board required.

    Asserts payload.bin BYTES, not just uboot_payload.yaml (the YAML-only
    hole DS-P2.145 already refused).
    """
    staging = _good_staging(tmp_path)
    root = _slim_root(tmp_path)
    out = tmp_path / "sdcard.img"
    proc = _cli(
        "assemble", "--root", str(root), "--from", str(staging),
        "--out", str(out), extra_env=_hss_env(tmp_path),
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert out.is_file() and out.stat().st_size > 0
    insp = _cli("inspect", str(out), "--root", str(root))
    text = insp.stdout + insp.stderr
    assert insp.returncode == 0, text
    # DS-P2.149: the layout is now the deployed >=3-partition GPT (P1 raw HSS
    # payload / P2 FAT boot / P3 ext4 rootfs), not the old 2-partition MBR.
    assert "P1 raw HSS payload" in text
    assert "P2 FAT boot" in text
    assert "GPT P3 ext4 rootfs" in text
    # payload.bin is a RAW partition now, never a FAT file.
    assert "FAT payload.bin" not in text
    assert "FAT microchip-mpfs-disco-kit.dtb" in text
    assert "FAT Image" in text
    assert "exec-addr=0x80200000" in text
    assert "priv-mode=prv_s" in text
    assert "owner-hart=u54_1" in text
    assert "0xb007c0de" in text, (
        "payload.bin bytes must carry HSS magic 0xb007c0de"
    )
    for marker in ROOTFS_MARKERS:
        assert f"ROOTFS {marker}" in text, text
    assert "hss-lim.elf" not in text
    assert "prv_m" not in text
    assert "magic=0xb007c0de" in text
