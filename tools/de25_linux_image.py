#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Prepare and verify the declarative DE25 Linux image inputs (DE-L0).

``rr linux rootfs`` calls ``stage`` through
``targets/de25_datalogger.yml:linux.rootfs.stage_command``.  The operation builds
the DE25 overlay in a temporary directory and publishes it only after every
pinned input has been verified.  A corrupt cache therefore cannot leave a
fresh-looking mixture of old and new files behind.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import re
import tempfile
from typing import Mapping, Sequence
import urllib.request
import time

import yaml


ROOT = Path(__file__).resolve().parents[1]

# DS-P1.57: the aarch64 glibc dynamic loader + runtime .so files that the
# staged python-build-standalone CPython needs to execute at all come out of
# this ALREADY-pinned ARM GNU toolchain archive (tools/de25_toolchain_stage.py
# — the same trusted, checksummed source `rr linux build` cross-compiles
# with) instead of a second independent pinned download for the same
# architecture. Loaded by file path, not package import: tools/ has no
# __init__.py, matching sw/linux/de25/test_de25_linux_image.py's own
# convention for reaching this sibling module's constants.
_toolchain_stage_spec = importlib.util.spec_from_file_location(
    "de25_toolchain_stage", Path(__file__).with_name("de25_toolchain_stage.py")
)
de25_toolchain_stage = importlib.util.module_from_spec(_toolchain_stage_spec)
_toolchain_stage_spec.loader.exec_module(de25_toolchain_stage)
# DS-P3.341: the init-script scan reads each script's parse tree from the image's own BusyBox ash (built
# from the same source and options, tools/ash_dump.py). Loaded by file path on first use, like the sibling above,
# so a clone that stages only this file (the golden-path provider imports it by path) can still import it.
_ash_dump_module = None


def _ash():
    global _ash_dump_module
    if _ash_dump_module is None:
        spec = importlib.util.spec_from_file_location("de25_ash_dump", Path(__file__).with_name("ash_dump.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules["de25_ash_dump"] = module      # the module's dataclass needs to find itself
        spec.loader.exec_module(module)
        _ash_dump_module = module
    return _ash_dump_module
MSGPACK_VERSION = "1.1.1"
MSGPACK_URL = (
    "https://files.pythonhosted.org/packages/45/b1/"
    "ea4f68038a18c77c9467400d166d74c4ffa536f34761f7983a104357e614/"
    "msgpack-1.1.1.tar.gz"
)
MSGPACK_SHA256 = "77b79ce34a2bdab2594f490c8e80dd62a02d650b91a75159a63ec413b8d104cd"
PYTHON_VERSION = "3.13.1+20241219"
PYTHON_ARCHIVE = "cpython-3.13.1+20241219-aarch64-unknown-linux-gnu-install_only.tar.gz"
PYTHON_URL = (
    "https://github.com/indygreg/python-build-standalone/releases/download/"
    "20241219/" + PYTHON_ARCHIVE
)
# Official release sidecar digest.  RouteRTL 4.8.1 carries a stale digest for
# this same asset; using it makes every honest download fail closed.
PYTHON_SHA256 = "c991fe7da0fdcdc35738169c02b966d23e69dbf3e33a6dd73d157fc9d14325ad"
# Newest commit timestamp in the GSRD-25.3 ATF/U-Boot/kernel source set.
# Exporting this for rr makes kernel, FIT and mkimage timestamps reproducible.
SOURCE_DATE_EPOCH = 1758780332
KBUILD_BUILD_USER = "routertl"
KBUILD_BUILD_HOST = "reproducible"
KBUILD_BUILD_VERSION = 1
KBUILD_BUILD_TIMESTAMP = "2025-09-25 06:05:32 +0000"
MTOOLS_DATE_STRING = "2025-09-25 06:05:32"
# DS-P2.135: the target yml input is resolved per active target — see
# lock_inputs(). The static trio is shared by every DE25 target.
# DS-P2.210: the daemon's native edge path (sw/stream/native), built from
# source for the HPS with the pinned ARM GNU toolchain stage already verifies,
# and installed in the Python overlay: the image's native AArch64 tree, where
# CPython and glibc sit. sw/stream/native/edge_frames.py looks for it at
# SYSTEM_LIB, which is "/" + NATIVE_EDGE_LIB_REL. Its sha256, its sources'
# sha256 and the toolchain archive's go into etc/deepskopion-inputs.
NATIVE_EDGE_LIB_REL = "usr/local/lib/deepskopion/libds_edge_drain.so"
NATIVE_EDGE_SOURCE = "sw/stream/native/edge_drain.c"
NATIVE_EDGE_SOURCES = (NATIVE_EDGE_SOURCE, "sw/stream/native/edge_drain.h")
NATIVE_EDGE_CFLAGS = (
    "-O2", "-std=c11", "-D_POSIX_C_SOURCE=199309L", "-mcpu=cortex-a55",
    "-fPIC", "-shared", "-Wall", "-Wextra", "-Werror",
)
# DS-P2.227: the SocketCAN user tools for the DE25 CTU CAN FD bench target's two
# CTU CAN FD controllers, cross-compiled from checksum-pinned upstream sources with the
# same pinned toolchain and installed in the Python overlay beside glibc, the
# only libraries they link (libc.so.6). iproute2's `ip` goes to /usr/sbin/ip;
# rr linux rootfs repoints busybox's /sbin/ip applet link at it (RouteRTL
# RTL-P3.2108 applet precedence), so `ip link set can0 type can ... fd on`
# reaches the full ip. The kernel side is CONFIG_CAN_RAW=y and CONFIG_CAN_BCM=y
# in sw/linux/de25/kernel_de25.config-fragment.
IPROUTE2_VERSION = "6.18.0"
IPROUTE2_ARCHIVE = f"iproute2-{IPROUTE2_VERSION}.tar.xz"
IPROUTE2_URL = ("https://mirrors.edge.kernel.org/pub/linux/utils/net/iproute2/"
                + IPROUTE2_ARCHIVE)
# kernel.org's signed sha256sums.asc for the iproute2 directory.
IPROUTE2_SHA256 = "6ba520e1975e4c50dc931eeae91ea37c198b8a173744885f8895b84325f9d456"
CAN_UTILS_VERSION = "2025.01"
CAN_UTILS_ARCHIVE = f"can-utils-v{CAN_UTILS_VERSION}.tar.gz"
# The release has no uploaded asset; this is GitHub's archive of the tag.
CAN_UTILS_URL = ("https://github.com/linux-can/can-utils/archive/refs/tags/"
                 f"v{CAN_UTILS_VERSION}.tar.gz")
CAN_UTILS_SHA256 = "88b30feace372c4a61e6adb2534791c0cfba6123ebcca7f59bbb76580d4a6915"
CAN_UTILS_PROGRAMS = ("cansend", "candump", "cangen")
# Image path -> (component, path of the build output inside its source tree).
CAN_TOOLS = {
    "usr/sbin/ip": ("iproute2", "ip/ip"),
    **{f"usr/bin/{prog}": ("can-utils", prog) for prog in CAN_UTILS_PROGRAMS},
}
HPS_ELF_INTERPRETER = "/lib/ld-linux-aarch64.so.1"
_EM_AARCH64 = 183
LOCK_INPUTS_STATIC = (
    "sw/linux/de25/system_memory_map.yml",
    "sw/linux/de25/kernel_de25.config-fragment",
    "tools/de25_linux_image.py",
)


def sbin_tool(tool: str) -> str:
    """Resolve a host tool that distributions install under sbin (debugfs,
    mke2fs, sfdisk), which an unprivileged user's PATH often lacks. Falls back
    to the bare name so a missing tool still fails with the usual message."""
    found = shutil.which(tool)
    if found:
        return found
    for d in ("/sbin", "/usr/sbin", "/usr/local/sbin"):
        cand = Path(d) / tool
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return tool


def lock_inputs(root: Path):
    """Provenance inputs for the ACTIVE DE25 target (target yml first)."""
    return (active_target_rel(root),) + LOCK_INPUTS_STATIC
# DS-P1.50 built this artifact on the theory that the fpga-first SDM needs an
# SPL-embedded, fabric-configuring .sof staged as a FAT member on the SD card
# itself (`rr linux sof` / `run_socfpga_sof_hps`: `quartus_pfg -c <bare>.sof
# <stem>_hps.sof -o hps_path=u-boot-spl-dtb.hex`, the GHRD's own
# `sof_with_hps.bat` recipe). DS-P3.147 found that theory wrong on two counts,
# both measured directly rather than inferred:
#
# 1. routertl's own `run_socfpga_sdcard` (sdk/cli/commands/linux_steps/
#    _socfpga.py, `fat_items` dict) NEVER stages a `.sof` — for a fpga-first
#    target (no `_CORE_RBF_NAME`) the FAT gets exactly u-boot.itb, Image, the
#    dtb, and boot.scr. `run_socfpga_sof_hps` writes `<stem>_hps.sof` into
#    `out/` and stops there; nothing downstream registers it as a FAT member
#    or copies it into the sdcard staging tree. So a build assembled through
#    the sanctioned `build()` route never had this file on the card to begin
#    with — the DS-P1.50 lock check was demanding a file no code path ever
#    produced on the SD.
# 2. Reading the mounted, silicon-proven boot17-era card directly (before
#    reflash) showed the same four-file FAT (Image, boot.scr,
#    socfpga_agilex5_socdk.dtb, u-boot.itb) and it booted; the DS-P3.146
#    boot21 witness rebuilt and reflashed the card fresh
#    from this module's own `build()` and it ALSO booted cleanly straight
#    from cold reset with the same four-file FAT and no .sof present. The
#    fabric config this fpga-first flow relies on reaches the SDM through
#    `rr program` (JTAG) / persistent config flash, not through the SD FAT —
#    matching targets/de25_datalogger.yml's own `sdcard:` comment ("the SD
#    payload carries no core.rbf and U-Boot must not try to reconfigure the
#    live fabric").
#
# SOF_HPS_ARTIFACT below is still real: `rr linux sof` still produces it, and
# it is still the correct JTAG-programmable object to hand to `rr program`
# for out-of-band fabric+SPL bring-up (its own GHRD docstring: "JTAG-program
# THAT... to boot the HPS"). It stays in BOOT_ARTIFACTS below so `lock` still
# pins/hashes it as a build output. It must NOT be in SDCARD_CONTENTS: no
# assembler stages it into the FAT, and no witnessed boot layout has ever
# carried it there.
SOF_HPS_ARTIFACT = "de25_datalogger_hps.sof"
DTB_ARTIFACT = "socfpga_agilex5_socdk.dtb"
UBOOT_ENV_ARTIFACT = "uboot.env"
# The DEFAULT target's identity — matches targets/de25_datalogger.yml's
# output_dir basename ("build/de25_datalogger") and the target file's own stem.
# `_stage_target_stem_sof` below stages the built .sof under this exact
# name, so `run_socfpga_sof_hps`'s `<stem>_hps.sof` output is exactly
# SOF_HPS_ARTIFACT above — see that function's docstring for why this
# staging step exists.
#
# DS-P2.135: this tool serves EVERY DE25 target, not just de25_datalogger —
# a DE25 bench target (e.g. the CAN opt-in build with real ctu-fee cores and
# enabled ctucanfd dt_nodes) shares the whole image pipeline. DS-P1.103: the
# bench targets it serves are listed in bench/de25_image_targets.txt, which only
# a checkout with bench/ has (the export drops bench/), so this file names none.
# TARGET_STEM / SOF_HPS_ARTIFACT stay as module constants because tests and
# the P1.53 pairing guard import them, but every entry point resolves the
# ACTIVE target via active_target_stem() and derives per-target paths from it
# (rr's own out/ is target-scoped since DS-P1.19, so outputs never collide).
TARGET_STEM = "de25_datalogger"
def _bench_de25_stems(root: Path) -> tuple[str, ...]:
    """The DE25 bench targets this tool serves, one stem per line in bench/de25_image_targets.txt ('#' starts
    a comment); empty when the checkout has no bench/ (the export)."""
    listing = root / "bench" / "de25_image_targets.txt"
    if not listing.is_file():
        return ()
    stems = (line.split("#", 1)[0].strip() for line in listing.read_text(encoding="utf-8").splitlines())
    return tuple(s for s in stems if s)


DE25_TARGET_STEMS = (TARGET_STEM,) + _bench_de25_stems(Path(__file__).resolve().parents[1])
BENCH_TARGET = "de25_bench"
CMDLINE_IP_KEY = "ds_ip"
BOOT_ARTIFACTS = (
    "bl31.bin", "u-boot.itb", "u-boot-spl-dtb.hex", "Image",
    "socfpga_agilex5_socdk.dtb", "boot.scr",
    SOF_HPS_ARTIFACT, "sdcard.img",
)


def _target_files_module():
    """tools/target_files.py, beside this file (DS-P1.103)."""
    spec = importlib.util.spec_from_file_location(
        "_ds_target_files", Path(__file__).resolve().parent / "target_files.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _active_target_module():
    """tools/active_target.py, beside this file (DS-P2.336)."""
    spec = importlib.util.spec_from_file_location(
        "_ds_active_target", Path(__file__).resolve().parent / "active_target.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def active_target_stem(root: Path) -> str:
    """The active project target's stem, refusing non-DE25 targets loudly.

    The whole tool keys its paths (target yml, ``<stem>_hps.sof``, the
    target-scoped ``out/`` dir) off this one resolution, so a non-DE25
    active target fails here with the fix spelled out instead of half-way
    into a build with a missing-file error.

    The target is resolved as rr resolves it (tools/active_target.py):
    ``$RR_TARGET`` (DS-P1.103: a DE25 bench target lives in bench/targets/ and
    is selected that way), then the target ``rr target set`` stored locally,
    then the project.yml ``target:`` pointer (DS-P2.336).
    """
    stem = _active_target_module().active_target_stem(root)
    if stem is None:
        raise RuntimeError(
            "no active target: set RR_TARGET, run rr target set, or add "
            "'target: targets/<stem>.yml' to project.yml")
    if stem not in DE25_TARGET_STEMS:
        raise RuntimeError(
            f"active target '{stem}' is not a DE25 target this image tool "
            "serves; run rr target set de25_datalogger, or select a DE25 bench "
            "target with RR_TARGET=bench/targets/<stem>.yml ("
            + " | ".join(DE25_TARGET_STEMS) + ")")
    return stem


def active_target_rel(root: Path) -> str:
    """Repo-relative path of the active DE25 target yml (targets/ or bench/targets/)."""
    path = _target_files_module().target_file(active_target_stem(root), root)
    return path.relative_to(root).as_posix()


def active_sof_hps_artifact(root: Path) -> str:
    """``<active-stem>_hps.sof`` — the JTAG-programmable SPL-embedded SOF."""
    return f"{active_target_stem(root)}_hps.sof"


def linux_out_dir(root: Path) -> Path:
    """The active target's ``rr linux`` output dir (target-scoped, DS-P1.19)."""
    return root / "sw/linux/de25/out" / active_target_stem(root)
SDCARD_CONTENTS = (
    "Image", "boot.scr",
    "socfpga_agilex5_socdk.dtb", "u-boot.itb",
)
# P2 ext4 rootfs markers, read back from the assembled image to prove the
# rootfs partition is real (not just present-but-empty). There is NO type-A2
# raw partition on this Agilex 5 / SDM chain to verify instead: the SDM boots
# from FAT files, not a BootROM+A2 raw loader area (routertl
# sdk.cli.commands.linux_steps._socfpga SocfpgaSdLayout docstring, RTL-P2.668;
# confirmed at build time by that module's own "A2: none (FAT-file boot)"
# console line). The two real partitions are P1 FAT32 boot and P2 ext4 rootfs,
# matching the rr-ddr-stream de25_agilex5_ddr_validator BENCH_RUNBOOK.md
# CHILD-1 step 4 recipe ("SPL/ATF/FIT in the boot partition + rootfs" — no A2
# mentioned there either).
SDCARD_ROOTFS_CONTENTS = (
    "etc/init.d/S99deepskopion-stream",
    "usr/local/share/deepskopion/sw/stream/__main__.py",
)
# Partition byte offsets, matching routertl's SocfpgaSdLayout defaults
# (align_mib=4, fat_mib=256) that targets/de25_datalogger.yml's linux.sdcard
# does not override. If those sizes are ever overridden there, these must
# move with them.
SDCARD_FAT_OFFSET_BYTES = 4 * 1024 * 1024
SDCARD_ROOTFS_OFFSET_BYTES = (4 + 256) * 1024 * 1024
SOURCE_REPOS = ("arm-trusted-firmware", "u-boot-socfpga", "linux-socfpga")

# DS-P2.94: U-Boot's own control DTB — binman-embedded into u-boot.itb at
# build time from this source file, NOT the kernel board DTB
# `rr linux dtb` patches post-build — ships the GSRD default MDIO child of
# gmac0 (== /soc/ethernet@10810000, the DE25's EMAC0) at reg=0:
#
#   &gmac0 { status = "okay"; phy-mode = "rgmii"; phy-handle = <&emac0_phy0>;
#     mdio0 { emac0_phy0: ethernet-phy@0 { reg = <0>; }; }; };
#
# (arch/arm/dts/socfpga_agilex5_socdk-u-boot.dtsi, QPDS25.3_REL_GSRD_PR,
# fetched and read directly to confirm this rather than guessed). The DE25
# KSZ9031 is strapped at MDIO address 1 — the SAME fact
# targets/de25_datalogger.yml's linux.dt_nodes override already encodes for the
# KERNEL board DTB (path /soc@0/ethernet@10810000/mdio0/ethernet-phy@0,
# reg: 1). routertl's u-boot control-DTB patch surface
# (_patch_uboot_control_dtb_disable_nodes / _patch_uboot_control_dtb_console,
# module sdk.cli.commands.linux_steps._socfpga) only covers dtb_disable_nodes
# and the console UART — there is no analogous additive/override hook for an
# arbitrary property, so the kernel-side dt_nodes fix never reaches U-Boot.
# routertl is a separate repo (out of scope for this worktree, same rule
# _stage_target_stem_sof's docstring already notes for the .sof-naming gap),
# so the fix lives entirely here: patch the checked-out control DTB source
# after `rr linux uboot` has cloned+built it once, then rebuild u-boot.itb
# directly with `make` so the FIT picks up the corrected phy address before
# it ships. Measured on the DE25 U-Boot prompt (boot5, 2026-08-23): with the
# unpatched GSRD default, tftpboot fails "Could not get PHY for
# ethernet@10810000: addr 0 / phy_connect() failed" — U-Boot never finds the
# KSZ9031 because it is probing the wrong MDIO address.
UBOOT_CONTROL_BOARD_DT = "socfpga_agilex5_socdk"
# The node's unit address is renamed alongside `reg` (see
# patch_uboot_control_dtb_emac0_phy below) so the compiled control DTB never
# carries a `ethernet-phy@0` node with `reg = <1>` — a unit-address/reg
# mismatch dtc's `unit_address_vs_reg`-family checks are written to catch.
UBOOT_EMAC0_PHY_NODE_PATH = "/soc/ethernet@10810000/mdio0/ethernet-phy@1"
UBOOT_ARCH = "arm64"
UBOOT_CROSS_COMPILE = "aarch64-linux-gnu-"
# A prior version of this patch appended a `&{path} { reg = <1>; };` override
# fragment delimited by these markers instead of renaming the base node in
# place. `patch_uboot_control_dtb_emac0_phy` still strips a leftover block
# (marker text unchanged for that one purpose) so a source tree patched by
# that earlier code stays idempotent under the new approach.
_UBOOT_EMAC0_PHY_BEGIN = "/* >>> deepskopion uboot EMAC0 PHY (DS-P2.94) >>> */"
_UBOOT_EMAC0_PHY_END = "/* <<< deepskopion uboot EMAC0 PHY <<< */"
# The exact GSRD-default node text (QPDS25.3_REL_GSRD_PR) this patch renames
# in place: unit address AND `reg` change together, from address 0 (the
# probe address that fails on real hardware — U-Boot never finds the DE25's
# KSZ9031) to 1 (its real MDIO strap, the same fact
# targets/de25_datalogger.yml's kernel-side linux.dt_nodes override already
# encodes for the board DTB).
_UBOOT_EMAC0_PHY_BASE_NODE = "emac0_phy0: ethernet-phy@0 {\n\t\t\treg = <0>;\n\t\t};"
_UBOOT_EMAC0_PHY_PATCHED_NODE = "emac0_phy0: ethernet-phy@1 {\n\t\t\treg = <1>;\n\t\t};"
# Mirrors routertl's own u-boot copy dict (run_socfpga_uboot) so a rebuild
# through this module ships the same artifact set `rr linux uboot` would.
UBOOT_REBUILD_COPIES = {
    "u-boot.itb": "u-boot.itb",
    "u-boot-dtb.bin": "u-boot-dtb.bin",
    "spl/u-boot-spl": "u-boot-spl",
    "spl/u-boot-spl.bin": "u-boot-spl.bin",
    "spl/u-boot-spl-dtb.bin": "u-boot-spl-dtb.bin",
    "spl/u-boot-spl-dtb.hex": "u-boot-spl-dtb.hex",
}


def patch_uboot_control_dtb_emac0_phy(
    uboot_dir: Path, board_dt: str = UBOOT_CONTROL_BOARD_DT,
) -> None:
    """DS-P2.94: rewrite the U-Boot control DTB's EMAC0 phy node in place to
    the DE25's real KSZ9031 MDIO strap address (1), source-patching
    ``<board_dt>-u-boot.dtsi``.

    An earlier version of this patch appended a ``&{path} { reg = <1>; };``
    override fragment instead: that left the node's own unit address at
    ``@0`` while ``reg`` became ``1`` — a unit-address/reg mismatch on the
    SHIPPED node (dtc's ``unit_address_vs_reg``/``simple_bus_reg`` family of
    checks exist to catch exactly this shape, even though this particular
    MDIO bus's ``snps,dwxgmac-mdio`` compatible — not ``simple-bus`` — means
    dtc does not currently flag it). Renaming the unit address alongside
    ``reg`` closes that mismatch by construction instead of relying on dtc
    never adding a check that would catch it.
    """
    import re
    dtsi = uboot_dir / "arch" / "arm" / "dts" / f"{board_dt}-u-boot.dtsi"
    if not dtsi.is_file():
        raise RuntimeError(
            f"DS-P2.94 U-Boot EMAC0 PHY fix: control DTB source missing: "
            f"{dtsi} — has `rr linux uboot` cloned u-boot-socfpga yet?"
        )
    text = dtsi.read_text(encoding="utf-8", errors="replace")
    # Idempotent against a worktree patched by the older override-fragment
    # shape: strip any leftover marker block before checking/applying the
    # in-place rename below.
    text = re.sub(
        re.escape(_UBOOT_EMAC0_PHY_BEGIN) + r".*?" + re.escape(_UBOOT_EMAC0_PHY_END)
        + r"\n?",
        "", text, flags=re.DOTALL,
    )
    if _UBOOT_EMAC0_PHY_PATCHED_NODE in text:
        dtsi.write_text(text, encoding="utf-8")
        return
    if _UBOOT_EMAC0_PHY_BASE_NODE not in text:
        raise RuntimeError(
            "DS-P2.94 U-Boot EMAC0 PHY fix: expected GSRD default node "
            f"{_UBOOT_EMAC0_PHY_BASE_NODE!r} not found in {dtsi} — upstream "
            "source shape changed, refusing to guess where to patch"
        )
    text = text.replace(_UBOOT_EMAC0_PHY_BASE_NODE, _UBOOT_EMAC0_PHY_PATCHED_NODE, 1)
    dtsi.write_text(text, encoding="utf-8")


_FDT_MAGIC = 0xD00DFEED
_FDT_BEGIN_NODE = 1
_FDT_END_NODE = 2
_FDT_PROP = 3
_FDT_NOP = 4
_FDT_END = 9


def _iter_fdt_props(blob: bytes):
    """Yield ``(node_path, prop_name, prop_value)`` for every property in a
    flattened-devicetree (FDT) blob, by walking its structure block directly
    — no ``dtc``/``libfdt`` dependency. Raises ``ValueError`` if ``blob``
    is not a well-formed FDT (bad magic, truncated, unknown tag) so a
    corrupt/placeholder artifact is unambiguously rejected rather than
    silently yielding nothing.

    Also yields a ``(node_path, None, None)`` marker each time a node
    begins (root excluded), so callers can confirm a node exists at a given
    path even when it carries no properties of its own (e.g. the FIT
    ``/images`` node).
    """
    if len(blob) < 40:
        raise ValueError(f"blob too short for an FDT header ({len(blob)} bytes)")
    (magic, totalsize, off_dt_struct, off_dt_strings, _off_mem_rsvmap,
     _version, _last_comp_version, _boot_cpuid_phys, _size_dt_strings,
     size_dt_struct) = struct.unpack_from(">10I", blob, 0)
    if magic != _FDT_MAGIC:
        raise ValueError(f"bad FDT magic: 0x{magic:08x}")
    if totalsize > len(blob):
        raise ValueError(
            f"FDT header totalsize {totalsize} exceeds blob length {len(blob)}"
        )
    off = off_dt_struct
    end = off_dt_struct + size_dt_struct
    path: list[str] = []
    while off < end:
        (tag,) = struct.unpack_from(">I", blob, off)
        off += 4
        if tag == _FDT_BEGIN_NODE:
            nul = blob.index(b"\x00", off)
            name = blob[off:nul].decode("utf-8", "replace")
            if name:
                path.append(name)
                yield "/".join(path), None, None
            off = (nul + 1 + 3) & ~3
        elif tag == _FDT_END_NODE:
            if path:
                path.pop()
        elif tag == _FDT_PROP:
            length, nameoff = struct.unpack_from(">II", blob, off)
            off += 8
            name_end = blob.index(b"\x00", off_dt_strings + nameoff)
            name = blob[off_dt_strings + nameoff:name_end].decode("ascii")
            value = blob[off:off + length]
            yield "/".join(path), name, value
            off = (off + length + 3) & ~3
        elif tag == _FDT_NOP:
            continue
        elif tag == _FDT_END:
            break
        else:
            raise ValueError(f"unrecognised FDT structure tag 0x{tag:08x}")


def _find_emac0_phy_reg_cells(blob: bytes) -> bytes | None:
    """Return the raw `reg` property of ``UBOOT_EMAC0_PHY_NODE_PATH`` inside
    a single FDT blob, or ``None`` if that blob does not carry it.
    """
    target = UBOOT_EMAC0_PHY_NODE_PATH.strip("/")
    for node_path, name, value in _iter_fdt_props(blob):
        if node_path == target and name == "reg":
            return value
    return None


def _fit_image_data_blobs(blob: bytes, top_level: list[tuple[str, str | None, bytes | None]]):
    """Yield the resolved raw bytes of every ``/images/<name>`` node in a
    FIT, whether the image data is embedded inline (a ``data`` property —
    ``mkimage``'s default) or lives in the FIT's external-data region (a
    ``data-offset``/``data-position`` + ``data-size`` triple — the shape a
    real binman-built ``u-boot.itb`` actually uses).

    A real FIT never inlines image data: binman builds ``u-boot.itb`` with
    ``mkimage -E``, which appends every image's bytes AFTER the FDT
    structure block and records only a reference in the tree — a
    ``937,888``-byte de25 FIT's own structure block was measured at 1,216
    bytes. A guard that only reads a top-level ``data`` property therefore
    never finds the real control DTB at all and fails closed on every
    genuine build (landing-refusal-reproduced: a real, fixed ``u-boot.itb``
    was rejected — "does not embed a control DTB" — even though its
    embedded ``fdt-0`` image carried ``reg = <1>`` at the right path).

    ``data-offset`` is relative to the FIT's own total size (the external
    region's base); ``data-position`` is an absolute file offset. Both are
    part of the documented FIT external-data format (``mkimage -E``/``-p``),
    not reverse-engineered from this one file.
    """
    (_magic, totalsize) = struct.unpack_from(">2I", blob, 0)
    nodes: dict[str, dict[str, bytes]] = {}
    for node_path, prop_name, value in top_level:
        if prop_name is None:
            nodes.setdefault(node_path, {})
            continue
        nodes.setdefault(node_path, {})[prop_name] = value
    for node_path, props in nodes.items():
        parts = node_path.split("/")
        if len(parts) != 2 or parts[0] != "images":
            continue
        if "data" in props:
            yield props["data"]
            continue
        size_bytes = props.get("data-size")
        if size_bytes is None:
            continue
        size = int.from_bytes(size_bytes, "big")
        if "data-position" in props:
            offset = int.from_bytes(props["data-position"], "big")
        elif "data-offset" in props:
            offset = totalsize + int.from_bytes(props["data-offset"], "big")
        else:
            continue
        if offset < 0 or size < 0 or offset + size > len(blob):
            continue
        yield blob[offset:offset + size]


def _assert_uboot_itb_carries_emac0_phy_fix(itb_path: Path) -> None:
    """DS-P2.94: validate the FIT ``rebuild_uboot_with_emac0_phy_fix`` is
    about to ship, not just that a file with the right name exists.

    A zero-exit ``make`` that never actually relinked anything can leave an
    arbitrary stale/placeholder file at ``u-boot.itb`` — the landing-refusal
    reproduced this with an 18-byte ASCII placeholder that
    ``rebuild_uboot_with_emac0_phy_fix``'s prior existence-only check
    happily shipped. Reject anything that is not a real FIT (a FIT is
    itself a flattened devicetree with a top-level ``/images`` node), and
    reject a real FIT whose embedded U-Boot control DTB does not carry the
    KSZ9031 fix at ``UBOOT_EMAC0_PHY_NODE_PATH`` with ``reg = <1>``.

    A landing refusal reproduced a second gap here: a bare 172-byte
    standalone FDT with no ``/images`` node at all, but with
    ``UBOOT_EMAC0_PHY_NODE_PATH`` planted directly at its top level, used
    to pass — because the old check looked for that node/property in the
    top-level blob itself before ever confirming the blob was a FIT (a
    real FIT's control DTB only ever appears nested inside an ``/images/*``
    node, never as the top-level tree). So a FIT structure check is
    mandatory before the phy fix is even searched for, and the phy fix is
    now searched for ONLY inside resolved ``/images/*`` blobs (via
    ``_fit_image_data_blobs`` — inline ``data`` or external
    ``data-offset``/``data-position`` + ``data-size``), never in the
    top-level tree.
    """
    blob = itb_path.read_bytes()
    try:
        top_level = list(_iter_fdt_props(blob))
    except ValueError as exc:
        raise RuntimeError(
            f"DS-P2.94 U-Boot EMAC0 PHY fix: {itb_path} is not a valid FIT "
            f"(flattened image tree) blob, refusing to ship it: {exc}"
        ) from exc

    has_images_node = any(
        prop_name is None and node_path == "images"
        for node_path, prop_name, _value in top_level
    )
    if not has_images_node:
        raise RuntimeError(
            f"DS-P2.94 U-Boot EMAC0 PHY fix: {itb_path} is not a valid FIT "
            "(flattened image tree) blob — it has no top-level /images "
            "node, refusing to ship it"
        )

    reg = None
    for value in _fit_image_data_blobs(blob, top_level):
        if len(value) < 40 or value[:4] != struct.pack(">I", _FDT_MAGIC):
            continue
        try:
            reg = _find_emac0_phy_reg_cells(value)
        except ValueError:
            continue
        if reg is not None:
            break

    if reg is None:
        raise RuntimeError(
            f"DS-P2.94 U-Boot EMAC0 PHY fix: {itb_path} does not embed a "
            f"control DTB with node {UBOOT_EMAC0_PHY_NODE_PATH!r} — cannot "
            "confirm the KSZ9031 PHY fix shipped, refusing to ship it"
        )
    if reg != struct.pack(">I", 1):
        observed = int.from_bytes(reg, "big") if len(reg) == 4 else reg.hex()
        raise RuntimeError(
            f"DS-P2.94 U-Boot EMAC0 PHY fix: {itb_path}'s control DTB still "
            f"has {UBOOT_EMAC0_PHY_NODE_PATH} reg={observed!r} (expected 1) "
            "— the KSZ9031 PHY fix did not ship"
        )


UBOOT_SRC_ENV = "DS_T3_36_UBOOT_SRC"
UBOOT_SRC_REL = Path("sw/linux/de25/src/u-boot-socfpga")
_UBOOT_SRC_REQUIRED_FILES = (
    "Makefile",
    "bl31.bin",
    "tools/binman/binman",
    f"arch/arm/dts/{UBOOT_CONTROL_BOARD_DT}-u-boot.dtsi",
)


_UBOOT_ENV_BOOTARGS_RE = re.compile(rb"bootargs=([ -~]*)\x00")


def assert_uboot_itb_default_bootargs(itb_path: Path, bootargs: str) -> None:
    """DS-P2.310: the shipped ``u-boot.itb``'s default environment carries
    exactly *bootargs*, once.

    U-Boot writes its environment ``bootargs`` into ``/chosen`` at ``booti``
    (boot/fdt_support.c), replacing the string ``rr linux dtb --bootargs`` put
    in the dtb, and the DE25 card carries no ``uboot.env``, so the compiled-in
    default IS the kernel command line of a hands-off boot. The default env is
    stored uncompressed in the FIT as ``bootargs=<value>`` ending in NUL.
    Refuse an image with no such entry, more than one, or a different value.
    """
    found = [m.group(1).decode("ascii")
             for m in _UBOOT_ENV_BOOTARGS_RE.finditer(itb_path.read_bytes())]
    if found != [bootargs]:
        raise RuntimeError(
            f"DS-P2.310: {itb_path} default env bootargs {found!r} is not exactly "
            f"the composed deployment bootargs {bootargs!r}; U-Boot would replace "
            "the dtb's /chosen/bootargs with it at booti. Rebuild with "
            "`rr linux uboot --refresh --bootargs <the same string>`."
        )


def _candidate_repo_roots(starts: Sequence[Path]) -> list[Path]:
    """Discover candidate repository worktrees reachable from ``starts`` via
    git worktree / common-dir / local-origin metadata (DS-T3.36).

    ``sw/linux/de25/src/u-boot-socfpga`` is gitignored and lives in the
    canonical bench checkout after ``rr linux uboot``, so a detached git
    worktree (``ds-python-suite``, ``.agent-wt/...``) or a local test-power
    clone reaches it through ``--git-common-dir`` / ``core.worktree`` /
    ``remote.origin.url`` without hardcoding any machine-local path.
    """
    queue: list[Path] = []
    seen: set[Path] = set()
    for start in starts:
        try:
            resolved = Path(start).resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            queue.append(resolved)
    idx = 0
    while idx < len(queue) and len(queue) < 16:
        cur = queue[idx]
        idx += 1
        if not cur.is_dir():
            continue
        proc = subprocess.run(
            ["git", "-C", str(cur), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            common = (cur / proc.stdout.strip()).resolve()
            if common.name == ".git" and common.parent not in seen:
                seen.add(common.parent)
                queue.append(common.parent)
            wt_proc = subprocess.run(
                ["git", "-C", str(common), "config", "--get", "core.worktree"],
                capture_output=True, text=True, check=False,
            )
            if wt_proc.returncode == 0 and wt_proc.stdout.strip():
                wt = (common / wt_proc.stdout.strip()).resolve()
                if wt not in seen:
                    seen.add(wt)
                    queue.append(wt)
        origin_proc = subprocess.run(
            ["git", "-C", str(cur), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=False,
        )
        if origin_proc.returncode == 0 and origin_proc.stdout.strip():
            raw_url = origin_proc.stdout.strip()
            cand_origin = Path(
                raw_url[7:] if raw_url.startswith("file://") else raw_url
            )
            if cand_origin.is_dir():
                resolved_origin = cand_origin.resolve()
                if resolved_origin not in seen:
                    seen.add(resolved_origin)
                    queue.append(resolved_origin)
    return queue


def resolve_uboot_src(
    root: Path = ROOT,
    *,
    env: Mapping[str, str] | None = None,
    search_roots: Sequence[Path] | None = None,
) -> Path:
    """Locate the built ``u-boot-socfpga`` source tree and verify its binman
    and control-DTB prerequisites (DS-T3.36).

    When ``DS_T3_36_UBOOT_SRC`` is set in ``env`` (default ``os.environ``),
    only that directory is considered; otherwise ``search_roots`` (or the git
    worktree/origin closure of ``root`` and ``ROOT``) are searched for
    ``sw/linux/de25/src/u-boot-socfpga``.
    """
    environ = os.environ if env is None else env
    override = str(environ.get(UBOOT_SRC_ENV) or "").strip()
    if override:
        candidates = [Path(override)]
    elif search_roots is not None:
        candidates = []
        for item in search_roots:
            base = Path(item)
            for cand in (base / UBOOT_SRC_REL, base):
                if cand not in candidates:
                    candidates.append(cand)
    else:
        candidates = [
            repo / UBOOT_SRC_REL for repo in _candidate_repo_roots((Path(root), ROOT))
        ]

    reasons: list[str] = []
    for cand in candidates:
        if not cand.is_dir():
            reasons.append(f"{cand}: not a directory")
            continue
        missing = [
            rel for rel in _UBOOT_SRC_REQUIRED_FILES if not (cand / rel).is_file()
        ]
        if missing:
            reasons.append(f"{cand}: missing {', '.join(missing)}")
            continue
        return cand.resolve()

    detail = "; ".join(reasons) if reasons else "no candidate roots configured"
    raise RuntimeError(
        f"DS-T3.36: cannot resolve DE25 u-boot-socfpga build tree "
        f"(set {UBOOT_SRC_ENV} or run 'rr linux uboot' once first): {detail}"
    )


def stage_uboot_build_tree(
    dest: Path,
    *,
    root: Path = ROOT,
    src: Path | None = None,
    env: Mapping[str, str] | None = None,
    search_roots: Sequence[Path] | None = None,
) -> Path:
    """Stage a writable copy of the verified ``u-boot-socfpga`` build tree at
    ``dest`` so an incremental ``make`` + binman rebuild can patch the control
    DTB and regenerate ``u-boot.itb`` without mutating the shared or read-only
    checkout (DS-T3.36).
    """
    resolved = (
        Path(src).resolve()
        if src is not None
        else resolve_uboot_src(root, env=env, search_roots=search_roots)
    )
    dest = Path(dest)
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "rsync",
            "-a",
            "--exclude=.git",
            "--exclude=/doc",
            f"{resolved}/",
            f"{dest}/",
        ],
        check=True,
    )
    return dest


def rebuild_uboot_with_emac0_phy_fix(root: Path) -> None:
    """DS-P2.94: patch the U-Boot control DTB (see
    ``patch_uboot_control_dtb_emac0_phy``) and rebuild ``u-boot.itb`` so the
    fix actually ships, then re-copy the rebuilt artifacts over the ones
    ``rr linux uboot`` just staged. Must run AFTER a completed ``rr linux
    uboot`` build (needs the cloned source tree and the staged ``bl31.bin``
    binman input it leaves behind).
    """
    uboot_dir = root / "sw/linux/de25/src/u-boot-socfpga"
    out_dir = linux_out_dir(root)
    bl31 = uboot_dir / "bl31.bin"
    if not bl31.is_file():
        raise RuntimeError(
            f"DS-P2.94 U-Boot EMAC0 PHY fix: {bl31} missing — run "
            "`rr linux uboot` before rebuilding with this fix."
        )
    patch_uboot_control_dtb_emac0_phy(uboot_dir)
    # A `make` that exits 0 without actually relinking (e.g. a broken/no-op
    # invocation) must not be trusted just because the old artifacts are
    # still sitting there from a PRIOR build. Remove every artifact we are
    # about to ship first, so the missing-output check below can only pass
    # if `make` genuinely regenerated them from the just-patched control DTB.
    for src_rel in UBOOT_REBUILD_COPIES:
        (uboot_dir / src_rel).unlink(missing_ok=True)
    jobs = str(os.cpu_count() or 1)
    # DS-P2.94 landing-refusal finding: this incremental `make` — unlike the
    # `rr linux uboot` step that first built this tree — never set `BL31`,
    # so U-Boot's own Makefile (`-a atf-bl31-path=${BL31}`, passed straight
    # to binman) got an empty path. binman's `--allow-missing
    # --fake-ext-blobs` then falls back to a bare `bl31.bin` lookup in cwd,
    # which happens to resolve here today only because a prior real build
    # left one sitting in ``uboot_dir`` — set it explicitly instead of
    # depending on that fallback. Also reuse the SAME toolchain-discovery
    # (DS-P2.92, `rr linux uboot`'s own path) rather than requiring this
    # rebuild's caller to have Vitis on PATH already: measured directly,
    # this `make` needs aarch64-linux-gnu-gcc, which lives under
    # ``/tools/Xilinx/Vitis/2024.1`` — OFF this process's ambient PATH.
    from sdk.cli.commands.linux_steps._cross_compile import make_env
    env = make_env({
        "ARCH": UBOOT_ARCH, "CROSS_COMPILE": UBOOT_CROSS_COMPILE,
        "BL31": str(bl31),
    })
    subprocess.run(["make", f"-j{jobs}"], cwd=uboot_dir, env=env, check=True)
    missing = [s for s in UBOOT_REBUILD_COPIES if not (uboot_dir / s).is_file()]
    if missing:
        raise RuntimeError(
            "DS-P2.94 U-Boot EMAC0 PHY fix: rebuild reported success but "
            f"missing output(s): {missing} in {uboot_dir} — `make` did not "
            "actually regenerate the FIT"
        )
    _assert_uboot_itb_carries_emac0_phy_fix(uboot_dir / "u-boot.itb")
    for src_rel, dst_name in UBOOT_REBUILD_COPIES.items():
        shutil.copy2(uboot_dir / src_rel, out_dir / dst_name)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_pinned(archive: Path, *, url: str, expected: str, label: str) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        actual = _sha256(archive)
        if actual != expected:
            raise RuntimeError(
                f"corrupt pinned input: {archive}\n"
                f"  expected sha256 {expected}\n"
                f"  observed sha256 {actual}\n"
                "Refusing to replace the previous DE25 overlay with stale output. "
                "Delete the corrupt cache file and rerun rr linux build."
            )
        return

    fd, tmp_name = tempfile.mkstemp(prefix="msgpack-", suffix=".download", dir=archive.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        urllib.request.urlretrieve(url, tmp)  # controlled, checksum-pinned URL
        actual = _sha256(tmp)
        if actual != expected:
            raise RuntimeError(
                f"downloaded {label} checksum mismatch: "
                f"expected {expected}, observed {actual}"
            )
        os.replace(tmp, archive)
    finally:
        tmp.unlink(missing_ok=True)


#: DS-P3.316: shared-overlay files the DE25 image leaves out. The DE25 image stages
#: no AArch64 dropbear, so S49dropbear would fail at every boot and the key tool
#: would provision a server that does not exist. The DE25 has no SSH server; its
#: console and the stream on port 7475 do not need one.
DE25_OVERLAY_EXCLUDE = frozenset({
    Path("etc/init.d/S49dropbear"),
    Path("usr/local/bin/deepskopion-authorize-key"),
})
#: What the built DE25 image may not hold: the two excluded files and the server itself.
DE25_IMAGE_FORBIDDEN = DE25_OVERLAY_EXCLUDE | {
    Path(d, "dropbear") for d in ("usr/local/bin", "usr/local/sbin", "usr/bin", "usr/sbin", "bin", "sbin")}
#: A script the DE25 image always ships; its absence means the image was not read.
DE25_SHIPPED_INIT_SCRIPT = Path("etc/init.d/S99deepskopion-stream")

#: Directories whose files an init script runs as programs.
_BIN_DIRS = ("bin/", "sbin/", "usr/bin/", "usr/sbin/", "usr/local/bin/", "usr/local/sbin/")
#: DS-P3.342: a program under one of these is created or mounted at boot, so the image cannot be expected to hold it.
_RUNTIME_DIRS = ("tmp/", "run/", "var/", "dev/", "proc/", "sys/", "mnt/", "media/")
_ROOT_PREFIX = re.compile(r"^\$\{?ROOT_PREFIX(?::-)?\}?(?=/)")
_SHELL_DECLARE = {"export", "local", "readonly"}
_ASSIGN = re.compile(r"^[A-Za-z_]\w*=(.*)$", re.S)


def _image_path(word: str) -> str | None:
    """*word* as an image path ("usr/sbin/x"), with a leading ${ROOT_PREFIX} removed, or None when it is not an
    absolute path. The word is the text the shell resolves (the parse has already removed quotes and escapes), so
    a quote left in it is a character of the path."""
    word = _ROOT_PREFIX.sub("", word)
    if not word.startswith("/"):
        return None
    rel = word[1:]
    # Under a bin directory a lone constant $((N)) is the number N (the shell computes nothing else), so the file
    # the shell runs is known. Anywhere else it stays computed: the path is a WARN, never a guessed refusal.
    return _ARITH_CONST.sub(r"\1", rel) if rel.startswith(_BIN_DIRS) else rel


# DS-P3.316: a bare command name is looked up in the PATH the images' profile exports
# (sw/linux/rootfs_overlay/etc/profile, Buildroot's BR2_SYSTEM_DEFAULT_PATH).
_INIT_PATH = ("usr/local/bin", "usr/local/sbin", "usr/bin", "usr/sbin", "bin", "sbin")
# BusyBox ash builtins: never looked up in PATH. Reserved words are not here: the parse never yields one as a
# command word, and a quoted one (`'if'`) is a command that PATH decides.
_SHELL_BUILTINS = frozenset({
    ":", ".", "[", "[[", "alias", "bg", "break", "cd", "chdir", "command", "continue", "echo", "eval", "exec",
    "exit", "export", "false", "fg", "getopts", "hash", "help", "jobs", "kill", "let", "local", "printf", "pwd",
    "read", "readonly", "return", "set", "shift", "source", "test", "times", "trap", "true", "type", "ulimit",
    "umask", "unalias", "unset", "wait",
})
_COMMAND_NAME = re.compile(r"^[A-Za-z0-9_][\w.+-]*$")                 # what PATH lookup is done for


#: DS-P3.316: the init-script scan's scope (worded identically in sw/linux/README.md and
#: tools/tests/test_de25_no_ssh_ds_p3_316.py). DS-P3.341 replaced the hand-written scanner by the image's own
#: BusyBox ash parse; judging more of the WARN classes is DS-P3.342.
INIT_SCRIPT_SCAN_SCOPE = (
    "Parsed: each script is parsed by the ash of BusyBox 1.36.1 (tools/ash_dump.py), built from the same "
    "source and with the same ash options as the image's busybox, not the same binary, in a sandbox with "
    "no network; nothing in a script is executed. Judged: every simple command the parse yields, wherever "
    "it sits: lists, pipelines and && / || chains, if, while, until, for and case (patterns included), "
    "subshells ( ), brace groups { }, redirected compound commands, function bodies, and the $( ) and "
    "backtick substitutions (compound commands inside them included) in any word, redirection target, for "
    "list, case pattern or here-document body that the shell expands; the literal command words, quotes, "
    "escapes and line continuations as the shell resolves them; literal NAME=value assignments, prefix "
    "assignments included; a literal PATH assignment at the top level of the file, unconditional only; "
    "function definitions, visible in their own subshell scope, to calls after the definition, and inside "
    "function bodies (command NAME bypasses a function); the -x operand of [ and test; the #! "
    "interpreter; and a command word that is exactly $NAME or ${NAME} for a variable the script assigns, "
    "judged by each literal value assigned. At each judged program position a path under a bin directory, "
    "or any other absolute command word whose every segment is a plain name of letters, digits and . _ + "
    "- (and not . or ..), except the runtime and computed paths named below, must be an executable file "
    "in the image, and a bare name must resolve through the PATH in effect. Not judged, and reported as "
    "one WARN line naming script:line and the construct, and refused by the image gate unless it is on the reviewed accept list: command -p; a PATH "
    "assignment that is conditional or inside a compound command, a subshell, a function body, a pipeline "
    "or the background (it is not applied); a PATH entry that is not a literal absolute directory; an "
    "absolute command path under tmp, run, var, dev, proc, sys, mnt or media (the file is created at "
    "boot), and an absolute command path that is computed (a variable, substitution, arithmetic "
    "expansion, unquoted glob or tilde, or a character outside the plain-name set in it) or has a . or .. "
    "segment; a command word that is not a literal name or absolute path, such as a relative path; a "
    "command word that joins a variable with other text; eval; alias; a here-document fed to a shell; "
    "process substitution; sh -c strings; . and source; a command word held in a variable the script does "
    "not assign or assigns a value that is not a literal; a command word produced by a substitution; and "
    "a program run through a wrapper (env, nice, nohup, setsid, timeout, xargs, chroot, su, flock, or "
    "start-stop-daemon with --exec or --startas), whose own name is judged. A script that ash refuses is "
    "an error, never skipped. What the scan cannot know is what a variable, a substitution or a sourced "
    "file holds when the script runs. An accepted WARN passes with its reason; a clean run is not a proof."
)
_WRAPPERS = frozenset({"env", "nice", "nohup", "setsid", "timeout", "xargs", "chroot", "su", "flock"})
_SSD_EXEC = frozenset({"--exec", "-x", "--startas", "-a"})
_SHELLS = frozenset({"sh", "ash", "bash", "dash"})


_SUBST = "${__ds_subst__}"          # a command word that is a substitution's output
_GLOBBED = "${__ds_glob__}"          # a command word with an unquoted glob or tilde: not the path the shell runs
_EXPANSION = "${__ds_expansion__}"   # ash_dump.Word.template's spelling of any expansion but a plain $NAME


def _parsed(script_text: str, name: str) -> list:
    """The parse trees of *script_text* from the image's own ash (tools/ash_dump.py), cached per text.
    A script ash refuses, or a parse that cannot be done safely, raises: it is never skipped."""
    return list(_parse_cached(script_text, name))


@functools.lru_cache(maxsize=64)
def _parse_cached(script_text: str, name: str) -> tuple:
    try:
        ash = _ash()
        return tuple(ash.dump(script_text))
    except _ash().AshDumpError as exc:
        raise RuntimeError(f"{name}: cannot inspect this script ({exc})") from exc


_ARITH_CONST = re.compile(r"\$\{__ds_const_(\d+)\}")


def _shown(text: str) -> str:
    """A command word as a WARN line quotes it: $NAME for a plain variable, $(...) for another expansion."""
    text = _ARITH_CONST.sub(r"$((\1))", text.replace(_GLOBBED, ""))
    return re.sub(r"\$\{(\w+)\}", r"$\1", text).replace(_EXPANSION, "$(...)")


def _word_text(word) -> str:
    """The text a word contributes to a judged position: the literal when it has no expansion, else its
    template (a plain $NAME written ${NAME}, any other expansion ${__ds_expansion__})."""
    return word.literal if word.literal is not None else word.template


def _scan_script(script_text: str, name: str = "script") -> tuple[list[tuple[str, str, int]], list[str]]:
    """DS-P3.316: the one walk over an init script. Returns its program positions as
    (kind, word, line): kind "head" (a command word), "value" (the value of a NAME=value)
    or "x-test" (the -x operand of [ / test); and its WARN lines for constructs outside
    INIT_SCRIPT_SCAN_SCOPE, as "<script>:<line>: <construct>". The script is parsed by the
    image's own ash (DS-P3.341), so quoting, escapes, line continuations, here-documents and
    substitutions are what the shell reads, not a guess."""
    positions, warnings, _lookups = _walk_script(script_text, name)
    return positions, warnings


def _walk_script(script_text: str, name: str):
    """_scan_script's walk, also returning each bare command word with the PATH in
    effect where it runs, as (word, dirs, line)."""
    state = {"path": (_INIT_PATH,), "functions": set(), "cond": False, "in_func": False}
    positions: list[tuple] = []
    assignments: dict[str, list[tuple[str, int]]] = {}
    for tree in _parsed(script_text, name):
        _walk_node(tree, state, positions, assignments, 0)
    warnings: list[str] = []
    kept: list[tuple[str, str, int]] = []
    lookups: list[tuple[str, tuple[str, ...], int]] = []
    for entry in positions:
        kind, word, number = entry[:3]
        if kind == "warn":
            warnings.append(f"{name}:{number}: {word}")
            continue
        if kind == "head" and word.startswith("$") and _image_path(word) is None:
            var = re.fullmatch(r"\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)\})", word)
            var_name = var and (var.group(1) or var.group(2))
            if word.startswith(_SUBST):
                warnings.append(f"{name}:{number}: a command word produced by a substitution")
            elif not var:
                warnings.append(f"{name}:{number}: a command word that joins a variable with other text "
                                f"({_shown(word)!r}, not judged)")
            elif var_name not in assignments:
                warnings.append(f"{name}:{number}: a command word held in {_shown(word)!r}, which the script does not assign")
            else:                               # judged by the literal value(s) the script assigns
                for value, at in assignments[var_name]:
                    vrel = _image_path(value)
                    if vrel is not None:
                        vcls = _outside_bin_class(vrel)
                        if vcls == "runtime":
                            warnings.append(f"{name}:{number}: an absolute command path under a directory the "
                                            f"system fills at boot ({_shown(value)!r} held in {_shown(word)!r}, not judged)")
                        elif vcls in ("computed", "lexical"):
                            warnings.append(f"{name}:{number}: an absolute command path that is computed or has a "
                                            f"./.. segment ({_shown(value)!r} held in {_shown(word)!r}, not judged)")
                        kept.append(("head", value, at))
                    elif _COMMAND_NAME.match(value):
                        kept.append(("head", value, at))
                        lookups.append((value, entry[3], number, entry[4], entry[5]))
                    else:
                        warnings.append(f"{name}:{number}: a command word held in {_shown(word)!r}, assigned a value "
                                        f"that is not a literal ({_shown(value)!r}, line {at})")
            continue
        kept.append((kind, word, number))
        if kind == "head":
            rel = _image_path(word)
            cls = _outside_bin_class(rel) if rel is not None else None
            if cls == "runtime":
                warnings.append(f"{name}:{number}: an absolute command path under a directory the system fills "
                                f"at boot ({_shown(word)!r}, not judged)")
            elif cls in ("computed", "lexical"):
                warnings.append(f"{name}:{number}: an absolute command path that is computed or has a ./.. segment "
                                f"({_shown(word)!r}, not judged)")
            elif rel is None and not _COMMAND_NAME.match(word) and word not in _SHELL_BUILTINS:
                warnings.append(f"{name}:{number}: a command word that is not a literal name or absolute "
                                f"path ({_shown(word)!r}, not judged)")
            lookups.append((word, entry[3], number, entry[4], entry[5]))
    # a name the script defines as a function in scope is not looked up, unless run as `command NAME`
    return kept, warnings, [(w, alts, n) for w, alts, n, fset, bypass in lookups if bypass or w not in fset]


def _walk_node(node, state: dict, positions: list, assignments: dict, line: int) -> None:
    """Walk one parse tree node of ash_dump, in the order the shell runs it. *state* carries the PATH in
    effect, the functions in scope and whether this node runs unconditionally ("cond" False)."""
    if node is None:
        return
    kind = node["t"]
    if kind == "cmd":
        _walk_cmd(node, state, positions, assignments)
    elif kind == "pipe":                        # every element runs detached: it changes no PATH for the rest
        for cmd in node["cmds"]:
            _walk_node(cmd, dict(state, cond=True), positions, assignments, line)
    elif kind in ("and", "or"):
        _walk_node(node["l"], state, positions, assignments, line)
        _walk_node(node["r"], dict(state, cond=True), positions, assignments, line)
    elif kind == "semi":
        _walk_node(node["l"], state, positions, assignments, line)
        _walk_node(node["r"], state, positions, assignments, line)
    elif kind in ("bg", "redir", "subshell"):
        inner = dict(state, cond=True)
        if kind == "subshell":                  # its own PATH and function scope
            inner["functions"] = set(state["functions"])
        _walk_node(node["body"], inner, positions, assignments, line)
        _walk_redirs(node.get("redir", []), state, positions, assignments, line)
    elif kind == "not":
        _walk_node(node["body"], state, positions, assignments, line)
    elif kind == "if":
        inner = dict(state, cond=True)
        for part in ("test", "then", "else"):
            _walk_node(node[part], inner, positions, assignments, line)
    elif kind in ("while", "until"):
        inner = dict(state, cond=True)
        _walk_node(node["test"], inner, positions, assignments, line)
        _walk_node(node["body"], inner, positions, assignments, line)
    elif kind == "for":
        for word in node["args"]:
            _walk_subs(word, state, positions, assignments, node["line"])
        assignments.setdefault(node["var"], []).append(("$" + node["var"], node["line"]))
        _walk_node(node["body"], dict(state, cond=True), positions, assignments, node["line"])
    elif kind == "case":
        _walk_subs(node["expr"], state, positions, assignments, node["line"])
        for clause in node["cases"]:
            for word in clause["pat"]:
                _walk_subs(word, state, positions, assignments, node["line"])
            _walk_node(clause["body"], dict(state, cond=True), positions, assignments, node["line"])
    elif kind == "defun":
        state["functions"].add(node["name"])    # defined from here on, also inside its own body
        _walk_node(node["body"], dict(state, cond=True, in_func=True), positions, assignments, node["line"])
    else:
        positions.append(("warn", f"a shell construct the scan does not know ({kind}, not judged)", line))


def _walk_subs(word, state: dict, positions: list, assignments: dict, line: int) -> None:
    """The command substitutions inside *word*, each its own shell context, judged where they run. A process
    substitution is run too, and is not judged."""
    if "proc" in word.expands:
        positions.append(("warn", "a process substitution (not judged)", line))
    for tree in word.subs:
        sub = {"path": state["path"], "functions": set(state["functions"]), "cond": False, "in_func": False}
        _walk_node(tree, sub, positions, assignments, line)


def _walk_redirs(redirs: list, state: dict, positions: list, assignments: dict, line: int) -> None:
    for redir in redirs:
        for key in ("to", "doc"):
            if key in redir:
                _walk_subs(redir[key], state, positions, assignments, line)


def _walk_cmd(node: dict, state: dict, positions: list, assignments: dict) -> None:
    number = node["line"]
    for word in node["assign"] + node["args"]:
        _walk_subs(word, state, positions, assignments, number)
    _walk_redirs(node["redir"], state, positions, assignments, number)
    heredoc = any(r["k"] in (24, 25) for r in node["redir"])
    conditional = state["cond"] or state["in_func"]

    def assign_path(new):
        if conditional:                   # outside the judged grammar: reported, not applied
            positions.append(("warn", "a PATH assignment that is conditional or inside a compound command "
                                      "(not applied)", number))
        elif new is not None:
            state["path"] = new

    prefix_path = None
    path_assigned = False
    for word in node["assign"]:
        var, value = _word_text(word).split("=", 1)
        _record_assignment(var, value, word, number, positions, assignments)
        if var == "PATH":
            path_assigned = True
            prefix_path = _set_path(value, number, positions, state["path"])
    args = list(node["args"])
    bypass = False
    while args:
        first = args[0].literal if args[0].is_plain else None
        if first == "command" and len(args) > 1:
            option = args[1].literal if args[1].is_plain else ""
            if option.startswith("-"):
                if any(c in option for c in "vV"):
                    return                # command -v / -V NAME looks NAME up; it runs nothing
                positions.append(("warn", f"command {option} (not judged)", number))
                return
            bypass = True                 # command NAME runs NAME, never a function of that name
            args.pop(0)
        elif first == "exec":
            args.pop(0)
        else:
            break
    if not args:                          # NAME=value alone sets it for the rest of the script
        if path_assigned:
            assign_path(prefix_path)
        return
    head, rest = args[0], args[1:]
    head_text = head.literal if head.is_plain else None
    if head_text in _SHELL_DECLARE:
        for word in rest:
            text = _word_text(word)
            if _ASSIGN.match(text):
                var, value = text.split("=", 1)
                _record_assignment(var, value, word, number, positions, assignments)
                if var == "PATH":
                    assign_path(_set_path(value, number, positions, state["path"]))
        return
    if head_text in ("[", "test"):
        for i, word in enumerate(rest[:-1]):
            if word.is_plain and word.literal == "-x" and _judgeable_text(rest[i + 1]) is not None:
                positions.append(("x-test", _judgeable_text(rest[i + 1]), number))
        return
    if head_text in ("read", "for") and rest:   # read NAME: the shell assigns NAME a runtime value
        for word in rest:
            if word.is_plain and re.fullmatch(r"[A-Za-z_]\w*", word.literal):
                assignments.setdefault(word.literal, []).append(("$" + word.literal, number))
    positions.append(("head", _head_text(head), number,
                      prefix_path if prefix_path is not None else state["path"],
                      state["functions"] if state["in_func"] else frozenset(state["functions"]), bypass))
    base = (_image_path(head_text) or head_text).rsplit("/", 1)[-1] if head_text else None
    literal_args = {w.literal for w in rest if w.is_plain}
    if head_text in ("eval", "alias"):
        positions.append(("warn", head_text, number))
    elif head_text in (".", "source"):
        positions.append(("warn", f"{head_text} (a sourced file is not judged)", number))
    elif base in _SHELLS and "-c" in literal_args:
        positions.append(("warn", f"{base} -c (the command string is not judged)", number))
    elif base in _SHELLS and heredoc:
        positions.append(("warn", f"a here-document run by {base} (its body is not judged)", number))
    elif base in _WRAPPERS or (base == "start-stop-daemon" and _SSD_EXEC & literal_args):
        positions.append(("warn", f"a program run through {base} (its arguments are not judged)", number))


def _judgeable_text(word) -> str | None:
    """The word's text when it is a literal, or a literal after a leading ${ROOT_PREFIX} (the hermetic-test
    prefix that is empty on the image); None when any other expansion is in it."""
    if word.literal is not None:
        return word.literal
    rest = _ROOT_PREFIX.sub("", word.template)
    return word.template if "${" not in rest else None


def _record_assignment(var: str, value: str, word, number: int, positions: list, assignments: dict) -> None:
    assignments.setdefault(var, []).append((value, number))
    if word.literal is not None or "${" not in _ROOT_PREFIX.sub("", value):   # only a literal is a program position
        positions.append(("value", value, number))


def _head_text(word) -> str:
    """A command word as the judge reads it: its literal; $NAME for a word that is exactly one plain variable; the
    substitution marker for one that starts with a command substitution; else its template (expansions marked)."""
    if word.is_plain:
        return word.literal
    if word.var is not None:
        return "$" + word.var
    if word.literal is not None:              # a glob or a tilde: not the path the shell runs
        return word.literal + _GLOBBED
    if not word.glob and not word.tilde and _judgeable_text(word) is not None:
        return word.template                  # ${ROOT_PREFIX}/usr/bin/x
    if word.expands and word.expands[0] == "cmd" and word.template.startswith(_EXPANSION):
        return _SUBST
    return word.template


def _set_path(value: str, number: int, positions: list, alts: tuple) -> tuple | None:
    """The PATH alternatives after assigning the literal *value* under each of *alts*
    (${PATH} stands for that alternative's list), or None (with a WARN) when an
    entry is not a literal absolute directory."""
    out = []
    for alt in alts:
        dirs: list[str] = []
        for entry in value.split(":"):
            if entry in ("$PATH", "${PATH}"):
                dirs += alt
            elif entry.startswith("/") and "$" not in entry and "`" not in entry:
                dirs.append(entry.strip("/"))
            elif entry:
                positions.append(("warn", f"a PATH entry that is not a literal absolute directory ({_shown(entry)!r})", number))
                return None
        out.append(tuple(dirs))
    return tuple(dict.fromkeys(out))


def _bare_lookups(script_text: str, name: str = "script") -> list[tuple[str, tuple, int]]:
    """DS-P3.316: each name *script_text* runs without a path, with the PATH alternatives
    that may be in effect where it runs: command words of the walk that have a command
    name's shape and are not a shell builtin, a reserved word or a function the walk saw
    the script define."""
    _positions, _warnings, lookups = _walk_script(script_text, name)
    return [(w, alts, n) for w, alts, n in lookups
            if _COMMAND_NAME.match(w) and w not in _SHELL_BUILTINS]


def _bare_commands(script_text: str, name: str = "script") -> set[str]:
    """DS-P3.316: the names *script_text* runs without a path (see _bare_lookups)."""
    return {w for w, _dirs, _n in _bare_lookups(script_text, name)}


_LITERAL_SEGMENT = re.compile(r"[A-Za-z0-9._+-]+")


def _outside_bin_class(rel: str, raw: str = "") -> str | None:
    """DS-P3.342: how the scan treats an absolute command path (leading "/" removed). An ALLOW-LIST: "judge"
    (it must be an executable file in the image) only when every segment is a plain literal name,
    [A-Za-z0-9._+-]+ and neither "." nor "..", and the path is not under _RUNTIME_DIRS. Everything else is a WARN,
    and a refusal needs this positive match: "runtime" (under _RUNTIME_DIRS), "lexical" (a ".", ".." or empty
    segment outside a bin directory: the directory walked decides) or "computed" (any other segment: an
    expansion, a glob, a quote or a space; the path the shell runs is not this text). None for a path under a
    bin directory whose segments are plain names; a bin path with an expansion or a glob in it is "computed"
    too, never judged as a file of that name. *raw* is unused (the parse resolves quoting)."""
    segments = rel.split("/")
    if rel.startswith(_BIN_DIRS):
        return None if all(seg in ("", ".", "..") or _LITERAL_SEGMENT.fullmatch(seg) for seg in segments) \
            else "computed"
    if any(seg in ("", ".", "..") for seg in segments):
        return "lexical"
    if not all(_LITERAL_SEGMENT.fullmatch(seg) for seg in segments):
        return "computed"
    return "runtime" if rel.startswith(_RUNTIME_DIRS) else "judge"


def _program_refs(script_text: str, name: str = "script") -> set[str]:
    """DS-P3.316: the image paths at any program position of _scan_script: under a bin
    directory (a command word, a NAME=value value or a -x operand), or an absolute command
    word anywhere else except under _RUNTIME_DIRS (DS-P3.342), with a leading ${ROOT_PREFIX}
    removed."""
    positions, _warnings = _scan_script(script_text, name)
    refs = set()
    for kind, word, _n in positions:
        rel = _image_path(word)
        if not rel:
            continue
        cls = _outside_bin_class(rel)
        if (rel.startswith(_BIN_DIRS) and cls is None) or (kind == "head" and cls == "judge"):
            refs.add(rel)
    return refs


def _executable_in(tree: Path, rel: str) -> bool:
    """Whether /*rel* in the image rooted at *tree* resolves, following symlinks inside
    the image, to an executable regular file."""
    parts, cur, hops = [p for p in rel.split("/") if p], [], 0
    while parts:
        part = parts.pop(0)
        if part in ("", "."):
            continue
        if part == "..":                       # the kernel walks into each component first
            if not tree.joinpath(*cur).is_dir():
                return False
            if cur:
                cur.pop()
            continue
        cand = tree.joinpath(*cur, part)
        if cand.is_symlink():
            hops += 1
            if hops > 40:
                return False
            target = os.readlink(cand)
            if target.startswith("/"):
                cur = []
            parts = [p for p in target.split("/") if p] + parts
            continue
        cur.append(part)
    final = tree.joinpath(*cur)
    return final.is_file() and not final.is_symlink() and bool(final.stat().st_mode & 0o111)


def init_script_missing_binaries(tree: Path, provided: frozenset[str] = frozenset(),
                                 *, bare: bool = True, warnings: list[str] | None = None) -> list[str]:
    """DS-P3.316: each init script in *tree* (etc/init.d/S*) whose #! interpreter, or a
    program at any program position of _scan_script, the image does not hold as an
    executable file: "<script>: /<path>" for a path under a bin directory, "<script>:
    <name> (not in PATH)" for a bare name (when *bare*), "<script>: /<path> (interpreter)".
    *provided* lists image paths (or bare names) another layer supplies. Constructs
    outside the scan's scope are appended to *warnings*, never refused.

    Scope: Parsed: each script is parsed by the ash of BusyBox 1.36.1 (tools/ash_dump.py), built from the same source
    and with the same ash options as the image's busybox, not the same binary, in a sandbox with no network;
    nothing in a script is executed. Judged: every simple command the parse yields, wherever it sits: lists,
    pipelines and && / || chains, if, while, until, for and case (patterns included), subshells ( ), brace
    groups { }, redirected compound commands, function bodies, and the $( ) and backtick substitutions (compound
    commands inside them included) in any word, redirection target, for list, case pattern or here-document body
    that the shell expands; the literal command words, quotes, escapes and line continuations as the shell
    resolves them; literal NAME=value assignments, prefix assignments included; a literal PATH assignment at the
    top level of the file, unconditional only; function definitions, visible in their own subshell scope, to
    calls after the definition, and inside function bodies (command NAME bypasses a function); the -x operand of
    [ and test; the #! interpreter; and a command word that is exactly $NAME or ${NAME} for a variable the
    script assigns, judged by each literal value assigned. At each judged program position a path under a bin
    directory, or any other absolute command word whose every segment is a plain name of letters, digits and . _
    + - (and not . or ..), except the runtime and computed paths named below, must be an executable file in the
    image, and a bare name must resolve through the PATH in effect. Not judged, and reported as one WARN line
    naming script:line and the construct, and refused by the image gate unless it is on the reviewed accept list: command -p; a PATH assignment that is conditional
    or inside a compound command, a subshell, a function body, a pipeline or the background (it is not applied);
    a PATH entry that is not a literal absolute directory; an absolute command path under tmp, run, var, dev,
    proc, sys, mnt or media (the file is created at boot), and an absolute command path that is computed (a
    variable, substitution, arithmetic expansion, unquoted glob or tilde, or a character outside the plain-name
    set in it) or has a . or .. segment; a command word that is not a literal name or absolute path, such as a
    relative path; a command word that joins a variable with other text; eval; alias; a here-document fed to a
    shell; process substitution; sh -c strings; . and source; a command word held in a variable the script does
    not assign or assigns a value that is not a literal; a command word produced by a substitution; and a
    program run through a wrapper (env, nice, nohup, setsid, timeout, xargs, chroot, su, flock, or
    start-stop-daemon with --exec or --startas), whose own name is judged. A script that ash refuses is an
    error, never skipped. What the scan cannot know is what a variable, a substitution or a sourced file holds
    when the script runs. An accepted WARN passes with its reason; a clean run is not a proof.
    """
    missing = []
    for script in sorted((tree / "etc/init.d").glob("S*")):
        text = script.read_text(encoding="utf-8", errors="replace")
        first = text.splitlines()[0] if text else ""
        if first.startswith("#!"):              # the interpreter runs first; a comment to the parser
            interp = first[2:].split()
            rel = _image_path(interp[0]) if interp else None
            if rel and not (f"/{rel}" in provided or _executable_in(tree, rel)):
                missing.append(f"{script.name}: /{rel} (interpreter)")
        _positions, found = _scan_script(text, script.name)
        if warnings is not None:
            warnings.extend(found)
        if bare:     # a command by name must resolve through the PATH in effect to an executable
            for cmd in sorted({c for c, alts, _n in _bare_lookups(text, script.name) if c not in provided
                               and not all(any(_executable_in(tree, f"{d}/{c}") for d in alt) for alt in alts)}):
                missing.append(f"{script.name}: {cmd} (not in PATH)")
        for rel in sorted(_program_refs(text, script.name)):
            if f"/{rel}" in provided or _executable_in(tree, rel):
                continue
            missing.append(f"{script.name}: /{rel}")
    return missing


#: DS-P3.348: the reviewed WARNs of the init-script scan, one exact WARN per entry with its reason.
ROOTFS_WARN_ACCEPT_REL = Path("sw/linux/de25/rootfs_warn_accept.json")


def rootfs_warn_accepts(path: Path | None = None) -> dict[str, dict[str, str]]:
    """The accept list as {"<script>:<line>: <construct>": {"text": <the line, stripped>, "reason": ...}}.
    Every entry must name a script, a positive line, a construct, the line's exact text and a
    reason; a malformed or duplicated entry is refused."""
    path = path or ROOT / ROOTFS_WARN_ACCEPT_REL
    data = json.loads(path.read_text(encoding="utf-8"))
    accepts: dict[str, dict[str, str]] = {}
    for entry in data["accept"]:
        script, line, construct, text, reason = (entry.get(k) for k in ("script", "line", "construct", "text", "reason"))
        if not (isinstance(script, str) and script and type(line) is int and line > 0   # a JSON true is no line
                and isinstance(construct, str) and construct and isinstance(text, str) and text.strip()
                and isinstance(reason, str) and reason.strip()):
            raise RuntimeError(f"{path}: an accept entry needs script, line, construct, text and reason: {entry}")
        key = f"{script}:{line}: {construct}"
        if key in accepts:
            raise RuntimeError(f"{path}: {key} is accepted twice")
        accepts[key] = {"text": text.strip(), "reason": reason}
    return accepts


def script_line(tree: Path, warning: str) -> str | None:
    """The stripped text of the init-script line a WARN names, read from the image tree."""
    script, number = warning.split(":", 2)[:2]
    path = tree / "etc/init.d" / script
    if path.is_symlink() or not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[int(number) - 1].strip() if 0 < int(number) <= len(lines) else None


def unaccepted_warnings(found: list[str], accepts: dict[str, dict[str, str]], *, tree: Path) -> list[str]:
    """DS-P3.348: a WARN is a pass only when it is on the reviewed accept list. (That the list
    holds exactly the WARNs of the shipped scripts, so a moved or removed line is reviewed again,
    is checked against the tracked overlay by the DS-P3.348 test.)"""
    problems = [f"WARN {w} is not on the reviewed accept list {ROOTFS_WARN_ACCEPT_REL}"
                for w in found if w not in accepts]
    # one entry accepts one occurrence: a second identical WARN on that line is not reviewed
    problems += [f"WARN {w} occurs {found.count(w)} times; the accept list reviews one occurrence"
                 for w in sorted(set(found)) if w in accepts and found.count(w) > 1]
    for w in sorted(set(found)):    # the reviewed operation, not only its line number and construct
        if w in accepts and script_line(tree, w) != accepts[w]["text"]:
            problems.append(f"WARN {w} is accepted for the line {accepts[w]['text']!r}, "
                            f"but the line reads {script_line(tree, w)!r}")
    return problems


def rootfs_image_problems(image: Path, offset: int = 0, *, warnings: list[str] | None = None,
                          accepts: dict[str, dict[str, str]] | None = None) -> list[str]:
    """DS-P3.316, checked on the artifact: an ext4 image (at byte *offset*, e.g. the SD
    card's P2) extracted with debugfs, which must yield the shipped init script; then
    DE25_IMAGE_FORBIDDEN must be absent and no init script may name a program the image
    does not hold. Each construct outside the init-script scan's scope is appended to
    *warnings*, or, when no list is given, printed to stderr as one "WARN <script>:<line>:
    <construct>" line. DS-P3.348: a WARN not on the reviewed accept list (*accepts*, by
    default ROOTFS_WARN_ACCEPT_REL) is a problem."""
    spec = f"{image}?offset={offset}" if offset else str(image)
    with tempfile.TemporaryDirectory(prefix=".de25-rootfs-check-") as tmp:
        tree = Path(tmp) / "root"
        tree.mkdir()
        # rdump also tries to restore ownership; as a normal user that part fails
        # and prints a warning, which does not affect the extracted content.
        proc = subprocess.run([sbin_tool("debugfs"), "-R", f"rdump / {tree}", spec],
                              capture_output=True, text=True, errors="replace", check=False)
        if not (tree / DE25_SHIPPED_INIT_SCRIPT).is_file():
            raise RuntimeError(f"cannot inspect the rootfs of {spec}: /{DE25_SHIPPED_INIT_SCRIPT} was not "
                               f"extracted ({proc.stderr.strip()[-300:]})")
        problems = [f"/{rel} is in the image" for rel in sorted(map(str, DE25_IMAGE_FORBIDDEN))
                    if (tree / rel).exists() or (tree / rel).is_symlink()]
        found: list[str] = []
        problems += init_script_missing_binaries(tree, warnings=found)
        accepts = rootfs_warn_accepts() if accepts is None else accepts
        if warnings is None:
            for line in found:
                note = f" (accepted: {accepts[line]['reason']})" if line in accepts else ""
                print(f"WARN {line}{note}", file=os.sys.stderr)
        else:
            warnings.extend(found)
        return problems + unaccepted_warnings(found, accepts, tree=tree)


def de25_overlay_files(root: Path) -> list[Path]:
    """The tracked shared-overlay files the DE25 image ships (DE25_OVERLAY_EXCLUDE left out)."""
    files = _tracked_overlay_files(root)
    absent = DE25_OVERLAY_EXCLUDE - set(files)
    if absent:  # a renamed file would silently ship again under its new name
        raise RuntimeError(f"DE25_OVERLAY_EXCLUDE names files the overlay does not track: {sorted(map(str, absent))}")
    return [rel for rel in files if rel not in DE25_OVERLAY_EXCLUDE]


def _tracked_overlay_files(root: Path) -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "-z", "sw/linux/rootfs_overlay"],
        cwd=root, check=True, capture_output=True,
    )
    prefix = Path("sw/linux/rootfs_overlay")
    result: list[Path] = []
    for raw in proc.stdout.split(b"\0"):
        if raw:
            result.append(Path(os.fsdecode(raw)).relative_to(prefix))
    if not result:
        raise RuntimeError("git index contains no tracked sw/linux/rootfs_overlay files")
    return result


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=True)


# What the rootfs does NOT ship from sw/stream and the wave_stream contract.
# Production code in sw/stream must never import sw.stream.testbench: this image
# strips it (DS-P2.126, DS-P2.136).
# A libds_edge_drain.so beside edge_frames.py is a developer's host build: the
# image's own AArch64 build goes to NATIVE_EDGE_LIB_REL instead (DS-P2.210).
STREAM_EXCLUDE = ("tests", "testbench", "__pycache__", ".pytest_cache",
                  "libds_edge_drain.so")
CONTRACT_EXCLUDE = ("tests", "fixtures", "__pycache__", ".pytest_cache")


def _copy_tree(source: Path, destination: Path, *, exclude: tuple[str, ...] = ()) -> None:
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        if any(part in exclude for part in rel.parts) or path.suffix in {".pyc", ".pyo"}:
            continue
        _copy_file(path, destination / rel)


def _stage_msgpack(archive: Path, destination: Path) -> None:
    prefix = f"msgpack-{MSGPACK_VERSION}/msgpack/"
    with tarfile.open(archive, "r:gz") as package:
        members = [
            member for member in package.getmembers()
            if member.isfile()
            and member.name.startswith(prefix)
            and member.name.endswith(".py")
        ]
        if not any(member.name == prefix + "fallback.py" for member in members):
            raise RuntimeError("verified msgpack source archive has no pure-Python fallback.py")
        for member in members:
            rel = Path(member.name.removeprefix(prefix))
            if rel.is_absolute() or ".." in rel.parts:
                raise RuntimeError(f"unsafe msgpack archive member: {member.name}")
            stream = package.extractfile(member)
            if stream is None:
                raise RuntimeError(f"could not read msgpack archive member: {member.name}")
            target = destination / "lib/python3.13/site-packages/msgpack" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stream.read())


# DS-P1.57 MEASURED on silicon (boot21): python-build-standalone's aarch64
# CPython is dynamically linked (ET_DYN) against glibc, but the overlay only
# ever staged bin/ + lib/python3.13/* + libpython3.13.so — no ELF interpreter
# and no libc. `find / -name "ld-linux*"` on the board came back EMPTY, so
# every dynamically linked binary failed exec with "not found" (the classic
# missing-interpreter message), and busybox only worked because IT is
# statically linked (ET_EXEC). These are exactly libpython3.13.so.1.0's and
# python3.13's own DT_NEEDED entries (readelf -d against the pinned
# PYTHON_SHA256 archive, verified 2026-08-24) plus the loader named by its
# PT_INTERP. Destination paths land in the SAME "lib/" the python overlay
# already stages python3's libpython3.13.so.1.0 into (python_output/lib/*),
# which is exactly rootfs "/lib/" on the board (ticket measured "ls
# /lib/libpython3.13.so*  -> PRESENT") — matching PT_INTERP's own
# "/lib/ld-linux-aarch64.so.1" with no extra merge/symlink step needed.
GLIBC_RUNTIME_MEMBERS = {
    "aarch64-none-linux-gnu/libc/lib/ld-linux-aarch64.so.1": "lib/ld-linux-aarch64.so.1",
    "aarch64-none-linux-gnu/libc/lib64/libc.so.6": "lib/libc.so.6",
    "aarch64-none-linux-gnu/libc/lib64/libm.so.6": "lib/libm.so.6",
    "aarch64-none-linux-gnu/libc/lib64/libpthread.so.0": "lib/libpthread.so.0",
    "aarch64-none-linux-gnu/libc/lib64/libdl.so.2": "lib/libdl.so.2",
    "aarch64-none-linux-gnu/libc/lib64/librt.so.1": "lib/librt.so.1",
    "aarch64-none-linux-gnu/libc/lib64/libutil.so.1": "lib/libutil.so.1",
}


def _stage_glibc_runtime(toolchain_archive: Path, destination: Path) -> None:
    """Copy the pinned aarch64 glibc runtime into ``destination`` (see
    GLIBC_RUNTIME_MEMBERS above for why these exact files, and why this
    source archive)."""
    prefix = de25_toolchain_stage.TOOLCHAIN_DIR_NAME + "/"
    found: set[str] = set()
    with tarfile.open(toolchain_archive, "r:xz") as package:
        for member in package.getmembers():
            if not member.name.startswith(prefix):
                continue
            rel = member.name[len(prefix):]
            dest_rel = GLIBC_RUNTIME_MEMBERS.get(rel)
            if dest_rel is None:
                continue
            if not member.isfile():
                raise RuntimeError(
                    f"expected a regular file for glibc runtime member: {member.name}"
                )
            stream = package.extractfile(member)
            if stream is None:
                raise RuntimeError(
                    f"could not read toolchain archive member: {member.name}"
                )
            target = destination / dest_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stream.read())
            target.chmod(member.mode & 0o777)
            found.add(rel)
    missing = sorted(set(GLIBC_RUNTIME_MEMBERS) - found)
    if missing:
        raise RuntimeError(
            "verified ARM GNU toolchain archive is missing expected glibc "
            f"runtime member(s): {missing}"
        )
    # DS-P2.102 MEASURED on silicon: PT_INTERP correctly names
    # /lib/ld-linux-aarch64.so.1 (the kernel loads that literal path, no
    # search involved — confirmed via `readelf -l python3.13`), but once
    # running, this SAME loader's DT_NEEDED search for a bare SONAME (e.g.
    # `libpthread.so.0`, `libc.so.6` — every runtime member here except the
    # loader itself) falls back to *its own compiled default path*, not
    # PT_INTERP's directory. With no /etc/ld.so.cache staged (nothing here
    # builds one; the board's busybox ships no ldconfig applet either),
    # `LD_DEBUG=libs python3 -c 1` on the physical board showed that default
    # is `/lib64:/usr/lib64` for this ARM GNU toolchain build — not `/lib`,
    # where GLIBC_RUNTIME_MEMBERS above stages every file. Every dynamically
    # linked python3 invocation on the board failed
    # "error while loading shared libraries: libpthread.so.0: cannot open
    # shared object file" even though `ls -la` / `stat` / `md5sum` on
    # /lib/libpthread.so.0 all showed a perfectly intact 73536-byte file —
    # not a staging or corruption defect, a search-path defect. Symlinking
    # each SONAME into lib64/ (skipping the loader itself, which the kernel
    # only ever opens via PT_INTERP's literal /lib path) closes the gap
    # without duplicating any binary content.
    for rel, dest_rel in GLIBC_RUNTIME_MEMBERS.items():
        if rel.endswith("/ld-linux-aarch64.so.1"):
            continue
        name = Path(dest_rel).name
        link = destination / "lib64" / name
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(Path("..") / dest_rel)


_ELF_MAGIC = b"\x7fELF"
_ELFCLASS64 = 2
_ET_DYN = 3
_PT_INTERP = 3


def _elf_dyn_interp(data: bytes) -> tuple[bool, str | None] | None:
    """Return ``(is_et_dyn, interp_or_None)`` for a 64-bit little-endian ELF,
    or None if ``data`` is not one (this rootfs is aarch64-only, so 32-bit /
    big-endian ELFs are out of scope)."""
    if data[:4] != _ELF_MAGIC or len(data) < 64:
        return None
    if data[4] != _ELFCLASS64 or data[5] != 1:  # EI_CLASS, EI_DATA (1=LE)
        return None
    e_type = int.from_bytes(data[16:18], "little")
    e_phoff = int.from_bytes(data[32:40], "little")
    e_phentsize = int.from_bytes(data[54:56], "little")
    e_phnum = int.from_bytes(data[56:58], "little")
    interp = None
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if off + 16 > len(data):
            break
        p_type = int.from_bytes(data[off:off + 4], "little")
        if p_type == _PT_INTERP:
            p_offset = int.from_bytes(data[off + 8:off + 16], "little")
            p_filesz = int.from_bytes(data[off + 32:off + 40], "little")
            raw = data[p_offset:p_offset + p_filesz]
            interp = raw.split(b"\x00", 1)[0].decode("ascii")
            break
    return e_type == _ET_DYN, interp


def assert_dynamic_executables_have_interpreters(root: Path) -> None:
    """DS-P1.57 regression guard.

    Assert the EFFECT, not the file list: every ELF64 staged under ``root``
    that has a PT_INTERP (PIE ET_DYN or non-PIE ET_EXEC) must have that loader actually
    present at that same path inside ``root`` — a file-presence check on the
    ELF headers, which is exactly the class of check that would have caught
    python-build-standalone's dynamically linked CPython shipping with no
    ld-linux-aarch64.so.1 / libc.so.6 in the committed overlay (the ticket's
    "not file-presence-of-python3, but PT_INTERP-resolves" bar). Statically
    linked ET_EXEC binaries (busybox) are untouched: they have no PT_INTERP
    segment and need none.
    """
    missing: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        head = path.open("rb").read(64)
        if head[:4] != _ELF_MAGIC:
            continue
        info = _elf_dyn_interp(path.read_bytes())
        if info is None:
            continue
        _is_dyn, interp = info
        # PT_INTERP, not e_type, says a loader is needed: a non-PIE program
        # (ET_EXEC) linked against libc has one too (iproute2's ip, can-utils;
        # DS-P2.227). Static busybox has none and is skipped.
        if interp is None:
            continue
        interp_path = root / interp.lstrip("/")
        if not interp_path.is_file():
            missing.append(
                f"{path.relative_to(root)} needs ELF interpreter "
                f"{interp!r}, but {interp_path} is not staged"
            )
    if missing:
        raise RuntimeError(
            "DS-P1.57 regression guard: dynamically linked executable(s) staged "
            "with no working ELF interpreter:\n  " + "\n  ".join(missing)
        )


def native_edge_source_sha256(root: Path) -> str:
    """sha256 over the native edge path's sources, each as path NUL bytes NUL."""
    digest = hashlib.sha256()
    for rel in NATIVE_EDGE_SOURCES:
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update((root / rel).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_native_edge_lib(root: Path, toolchain_dir: Path, output: Path) -> str:
    """Cross-compile libds_edge_drain.so for the HPS (DS-P2.210); its sha256.

    Refuses anything but an AArch64 ELF64 shared object without an ELF
    interpreter, so a host compiler found by mistake cannot slip one in."""
    gcc = toolchain_dir / "bin" / f"{de25_toolchain_stage.CROSS_PREFIX}gcc"
    if not gcc.is_file():
        raise RuntimeError(f"pinned aarch64 compiler not staged: {gcc}")
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(gcc), *NATIVE_EDGE_CFLAGS, f"-ffile-prefix-map={root}=.",
           "-o", str(output), NATIVE_EDGE_SOURCE, "-lm"]
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(SOURCE_DATE_EPOCH), LC_ALL="C")
    proc = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True,
                          errors="replace", check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"native edge library build failed ({' '.join(cmd)}):\n"
                           f"{proc.stdout}{proc.stderr}")
    data = output.read_bytes()
    info = _elf_dyn_interp(data)
    machine = int.from_bytes(data[18:20], "little") if len(data) >= 20 else None
    if info is None or machine != _EM_AARCH64 or info != (True, None):
        raise RuntimeError(f"{output} is not an AArch64 ELF64 shared object "
                           f"(e_machine={machine}, dyn/interp={info})")
    return hashlib.sha256(data).hexdigest()


def _elf_needed(data: bytes) -> list[str]:
    """DT_NEEDED names of a 64-bit little-endian ELF file; empty without PT_DYNAMIC."""
    phoff = int.from_bytes(data[32:40], "little")
    phentsize = int.from_bytes(data[54:56], "little")
    phnum = int.from_bytes(data[56:58], "little")
    loads, dynamic = [], None
    for i in range(phnum):
        ph = data[phoff + i * phentsize: phoff + (i + 1) * phentsize]
        p_type = int.from_bytes(ph[0:4], "little")
        p_offset = int.from_bytes(ph[8:16], "little")
        p_vaddr = int.from_bytes(ph[16:24], "little")
        p_filesz = int.from_bytes(ph[32:40], "little")
        if p_type == 1:      # PT_LOAD
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == 2:    # PT_DYNAMIC
            dynamic = (p_offset, p_filesz)
    if dynamic is None:
        return []
    entries = []
    for off in range(dynamic[0], dynamic[0] + dynamic[1], 16):
        tag = int.from_bytes(data[off:off + 8], "little")
        val = int.from_bytes(data[off + 8:off + 16], "little")
        if tag == 0:
            break
        entries.append((tag, val))
    strtab_vaddr = next(val for tag, val in entries if tag == 5)   # DT_STRTAB
    strtab = next(off + strtab_vaddr - vaddr for vaddr, off, size in loads
                  if vaddr <= strtab_vaddr < vaddr + size)
    return [data[strtab + val:].split(b"\0", 1)[0].decode("ascii")
            for tag, val in entries if tag == 1]                    # DT_NEEDED


def check_hps_executable(data: bytes, label: str) -> None:
    """Refuse anything but an AArch64 ELF64 program that loads with the image's
    glibc: PT_INTERP is HPS_ELF_INTERPRETER and every DT_NEEDED library is one
    _stage_glibc_runtime installs. A host binary, or one linked against a
    library the image does not carry, would fail on the board with "not
    found"; this fails the stage instead (DS-P2.227)."""
    machine = int.from_bytes(data[18:20], "little") if len(data) >= 20 else None
    if data[:4] != _ELF_MAGIC or data[4:5] != b"\x02" or machine != _EM_AARCH64:
        raise RuntimeError(f"{label} is not an AArch64 ELF64 file (e_machine={machine})")
    info = _elf_dyn_interp(data)
    interp = info[1] if info else None
    if interp != HPS_ELF_INTERPRETER:
        raise RuntimeError(f"{label} has ELF interpreter {interp!r}, "
                           f"expected {HPS_ELF_INTERPRETER!r}")
    shipped = {Path(dest).name for dest in GLIBC_RUNTIME_MEMBERS.values()}
    extra = sorted(set(_elf_needed(data)) - shipped)
    if extra:
        raise RuntimeError(f"{label} needs {extra}, which the DE25 image does not "
                           f"ship (it ships {sorted(shipped)})")


def _extract_source(archive: Path, dest: Path) -> Path:
    """Unpack a pinned source archive under *dest*; the single top directory.
    Refuses members that are absolute, climb out with '..', or are devices."""
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        tops = set()
        for m in members:
            parts = Path(m.name).parts
            if (not parts or Path(m.name).is_absolute() or ".." in parts
                    or m.isdev() or (m.islnk() and ".." in Path(m.linkname).parts)):
                raise RuntimeError(f"unsafe member {m.name!r} in {archive.name}")
            tops.add(parts[0])
        if len(tops) != 1:
            raise RuntimeError(f"{archive.name} has {len(tops)} top directories, expected 1")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:
            tar.extractall(dest)  # members checked above
    return dest / tops.pop()


def _run_build(cmd: list[str], cwd: Path, env: dict, label: str) -> None:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                          errors="replace", check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{label} failed ({' '.join(cmd)}), last output:\n"
                           + (proc.stdout + proc.stderr)[-4000:])


def build_can_tools(cache: Path, toolchain_dir: Path, workdir: Path) -> dict[str, Path]:
    """Cross-compile iproute2's ip and can-utils' cansend, candump and cangen
    for the HPS (DS-P2.227) from the pinned archives in *cache*, with the
    pinned toolchain. Returns image path -> stripped binary under *workdir*,
    each one checked by check_hps_executable."""
    bindir = toolchain_dir / "bin"
    prefix = de25_toolchain_stage.CROSS_PREFIX
    gcc, strip = bindir / f"{prefix}gcc", bindir / f"{prefix}strip"
    if not gcc.is_file() or not strip.is_file():
        raise RuntimeError(f"pinned aarch64 compiler not staged: {gcc}")
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
               SOURCE_DATE_EPOCH=str(SOURCE_DATE_EPOCH), LC_ALL="C",
               # no host library may be detected into a target build
               PKG_CONFIG="false", CC=f"{prefix}gcc", AR=f"{prefix}ar")
    jobs = f"-j{min(8, os.cpu_count() or 1)}"
    workdir.mkdir(parents=True, exist_ok=True)
    trees = {
        "iproute2": _extract_source(cache / IPROUTE2_ARCHIVE, workdir),
        "can-utils": _extract_source(cache / CAN_UTILS_ARCHIVE, workdir),
    }
    _run_build(["./configure"], trees["iproute2"], env, "iproute2 configure")
    _run_build(["make", jobs, f"CC={prefix}gcc", f"AR={prefix}ar", "SUBDIRS=lib ip"],
               trees["iproute2"], env, "iproute2 build")
    _run_build(["make", jobs, f"CC={prefix}gcc", *CAN_UTILS_PROGRAMS],
               trees["can-utils"], env, "can-utils build")
    out_dir = workdir / "out"
    out_dir.mkdir(exist_ok=True)
    built: dict[str, Path] = {}
    for rel, (component, product) in CAN_TOOLS.items():
        target = out_dir / Path(rel).name
        _run_build([str(strip), "-o", str(target), str(trees[component] / product)],
                   workdir, env, f"strip {rel}")
        check_hps_executable(target.read_bytes(), f"/{rel} ({component})")
        built[rel] = target
    return built


def _stage_python(archive: Path, toolchain_archive: Path, destination: Path,
                  native_lib: Path | None = None,
                  executables: dict[str, Path] | None = None) -> None:
    """Extract checksum-verified ``python/`` + its glibc runtime (DS-P1.57)
    as an atomic rootfs overlay, with the native edge library (DS-P2.210)
    at NATIVE_EDGE_LIB_REL when *native_lib* is given."""
    temp = Path(tempfile.mkdtemp(prefix=".de25-python-", dir=destination.parent))
    try:
        with tarfile.open(archive, "r:gz") as package:
            members = [m for m in package.getmembers() if m.name.startswith("python/")]
            if not any(m.name == "python/bin/python3" for m in members):
                raise RuntimeError("verified Python archive has no python/bin/python3")
            for member in members:
                rel = Path(member.name).relative_to("python")
                if rel == Path(".") or rel.is_absolute() or ".." in rel.parts:
                    continue
                # Runtime-only image: compilers, package managers, GUI stacks,
                # headers and their example/certificate corpora are neither
                # needed by the daemon nor appropriate public-image payload.
                top = rel.parts[0] if rel.parts else ""
                runtime_excludes = (
                    "lib/python3.13/ensurepip", "lib/python3.13/site-packages",
                    "lib/tcl", "lib/Tix", "lib/tk", "lib/thread", "lib/itcl",
                )
                if top in {"include", "share"} or any(
                    str(rel).startswith(prefix) for prefix in runtime_excludes
                ) or (top == "bin" and any(
                    rel.name.startswith(prefix)
                    for prefix in ("pip", "idle", "pydoc")
                )) or (top == "bin" and rel.name.endswith("-config")):
                    continue
                target = temp / rel
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.issym():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(member.linkname)
                elif member.isfile():
                    stream = package.extractfile(member)
                    if stream is None:
                        raise RuntimeError(f"could not read Python archive member: {member.name}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(stream.read())
                    target.chmod(member.mode & 0o777)
        _stage_glibc_runtime(toolchain_archive, temp)
        if native_lib is not None:
            _copy_file(native_lib, temp / NATIVE_EDGE_LIB_REL)
            (temp / NATIVE_EDGE_LIB_REL).chmod(0o644)
        for rel, built in (executables or {}).items():
            _copy_file(built, temp / rel)
            (temp / rel).chmod(0o755)
        assert_dynamic_executables_have_interpreters(temp)
        old = destination.with_name(destination.name + ".previous")
        if old.exists():
            shutil.rmtree(old)
        if destination.exists():
            os.replace(destination, old)
        os.replace(temp, destination)
        if old.exists():
            shutil.rmtree(old)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def stage(root: Path, output: Path, python_output: Path, cache: Path,
          toolchain_dir: Path | None = None) -> None:
    """Build the overlay atomically from tracked/canonical inputs.

    *toolchain_dir* is where the pinned aarch64 toolchain is unpacked to build
    the native edge library; default sw/linux/de25/toolchain/<release>, the
    directory tools/de25_toolchain_stage.py stages."""
    if toolchain_dir is None:
        toolchain_dir = (root / "sw/linux/de25/toolchain"
                         / de25_toolchain_stage.TOOLCHAIN_DIR_NAME)
    archive = cache / f"msgpack-{MSGPACK_VERSION}.tar.gz"
    python_archive = cache / PYTHON_ARCHIVE
    toolchain_archive = cache / de25_toolchain_stage.TOOLCHAIN_ARCHIVE
    _fetch_pinned(archive, url=MSGPACK_URL, expected=MSGPACK_SHA256,
                  label=f"msgpack-{MSGPACK_VERSION}")
    _fetch_pinned(python_archive, url=PYTHON_URL, expected=PYTHON_SHA256,
                  label=f"CPython {PYTHON_VERSION} AArch64")
    # DS-P1.57: same pinned ARM GNU toolchain tools/de25_toolchain_stage.py
    # already trusts for cross-compiling — reused here as the source of the
    # glibc runtime the staged CPython above needs to execute at all.
    for name, url, sha, label in (
            (IPROUTE2_ARCHIVE, IPROUTE2_URL, IPROUTE2_SHA256, f"iproute2-{IPROUTE2_VERSION}"),
            (CAN_UTILS_ARCHIVE, CAN_UTILS_URL, CAN_UTILS_SHA256, f"can-utils-{CAN_UTILS_VERSION}")):
        _fetch_pinned(cache / name, url=url, expected=sha, label=label)
    _fetch_pinned(toolchain_archive, url=de25_toolchain_stage.TOOLCHAIN_URL,
                  expected=de25_toolchain_stage.TOOLCHAIN_SHA256,
                  label="ARM GNU aarch64-none-linux-gnu toolchain (glibc runtime)")

    de25_toolchain_stage.stage(cache, toolchain_dir)

    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".de25-rootfs-", dir=parent))
    native_dir = Path(tempfile.mkdtemp(prefix=".de25-native-", dir=parent))
    try:
        native_lib = native_dir / Path(NATIVE_EDGE_LIB_REL).name
        native_sha = build_native_edge_lib(root, toolchain_dir, native_lib)
        can_tools = build_can_tools(cache, toolchain_dir, native_dir / "can-tools")
        shared = root / "sw/linux/rootfs_overlay"
        # DS-P3.316: the SSH files stay out; build() checks the merged card rootfs.
        for rel in de25_overlay_files(root):
            _copy_file(shared / rel, temp / rel)

        share = temp / "usr/local/share/deepskopion"
        _copy_tree(root / "sw/stream", share / "sw/stream", exclude=STREAM_EXCLUDE)
        _copy_file(root / "sw/__init__.py", share / "sw/__init__.py")
        _copy_tree(
            root / "contracts/wave_stream_v1",
            share / "contracts/wave_stream_v1",
            exclude=CONTRACT_EXCLUDE,
        )
        _stage_msgpack(archive, temp)

        # The deployed scanner consumes this exact provenance input.  It is
        # architecture-neutral data and contains no host path or identity.
        stamp = temp / "etc/deepskopion-inputs"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(
            f"msgpack_version={MSGPACK_VERSION}\nmsgpack_sha256={MSGPACK_SHA256}\n"
            f"native_edge_lib=/{NATIVE_EDGE_LIB_REL}\n"
            f"native_edge_lib_sha256={native_sha}\n"
            f"native_edge_source_sha256={native_edge_source_sha256(root)}\n"
            f"native_edge_toolchain={de25_toolchain_stage.TOOLCHAIN_ARCHIVE}\n"
            f"native_edge_toolchain_sha256={de25_toolchain_stage.TOOLCHAIN_SHA256}\n"
            f"iproute2_version={IPROUTE2_VERSION}\niproute2_sha256={IPROUTE2_SHA256}\n"
            f"can_utils_version={CAN_UTILS_VERSION}\ncan_utils_sha256={CAN_UTILS_SHA256}\n"
            + "".join(f"can_tool=/{rel} {_sha256(path)}\n"
                      for rel, path in can_tools.items()),
            encoding="utf-8",
        )

        # Publish Python only after both downloads and the rootfs construction
        # succeeded.  Any corrupt input above leaves both old overlays intact.
        _stage_python(python_archive, toolchain_archive, python_output, native_lib,
                      can_tools)
        old = output.with_name(output.name + ".previous")
        if old.exists():
            shutil.rmtree(old)
        if output.exists():
            os.replace(output, old)
        os.replace(temp, output)
        if old.exists():
            shutil.rmtree(old)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(native_dir, ignore_errors=True)


REDISTRIBUTION_DOC = "docs/REDISTRIBUTION.md"
OVERLAY_INVENTORY_SECTION = "## Contents of the DE25 image's Python runtime"
OVERLAY_INVENTORY_HEADER = "| Image path | Component |"


def documented_overlay_elfs(doc: Path) -> set[str]:
    """Image paths of the ELF files docs/REDISTRIBUTION.md lists for the DE25
    Python overlay: the first column of the "| Image path | Component |"
    table in OVERLAY_INVENTORY_SECTION."""
    lines = doc.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(OVERLAY_INVENTORY_SECTION)
    except ValueError:
        raise RuntimeError(f"{doc} has no section {OVERLAY_INVENTORY_SECTION!r}") from None
    paths: set[str] = set()
    in_table = False
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if line.strip() == OVERLAY_INVENTORY_HEADER:
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cell = line.split("|")[1].strip()
            if cell.startswith("`") and cell.endswith("`"):
                paths.add(cell.strip("`"))
    if not paths:
        raise RuntimeError(f"{doc}: no {OVERLAY_INVENTORY_HEADER!r} rows under "
                           f"{OVERLAY_INVENTORY_SECTION!r}")
    return paths


def overlay_elf_paths(overlay: Path) -> set[str]:
    """Image paths ("/" + path in the overlay) of every ELF file in *overlay*;
    symbolic links are not files of their own."""
    found: set[str] = set()
    for path in overlay.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(4) == _ELF_MAGIC:
                    found.add("/" + path.relative_to(overlay).as_posix())
    return found


def overlay_inventory_mismatch(python_overlay: Path, script_overlay: Path,
                               doc: Path) -> list[str]:
    """Why the staged DE25 overlays and docs/REDISTRIBUTION.md disagree on the
    image's ELF files; empty when they agree. The script overlay must hold none."""
    listed = documented_overlay_elfs(doc)
    staged = overlay_elf_paths(python_overlay)
    problems = [f"{p} is in the Python overlay but not listed in {doc.name}"
                for p in sorted(staged - listed)]
    problems += [f"{p} is listed in {doc.name} but not in the Python overlay"
                 for p in sorted(listed - staged)]
    problems += [f"{p} is an ELF file in the script overlay, which holds none"
                 for p in sorted(overlay_elf_paths(script_overlay))]
    return problems


def _git_commit(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    )
    return proc.stdout.strip()


def _payload_sha(rootfs: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in rootfs.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(rootfs).as_posix()
        if rel == "etc/rr-sw-provenance":
            continue
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_ENV_STRIP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")


def _source_git_rev(root: Path) -> str:
    """Resolve the full 40-char git commit SHA of *root*, or refuse.

    DS-P3.264: ``RR_SW_GIT_HASH`` / ``RR_SW_GIT_REV`` must name the real source
    revision the payload was built from — never a fabricated hash of
    ``lock_inputs()`` that looks like a git revision and stays identical across
    daemon commits.
    """
    if not (root / ".git").exists():
        raise RuntimeError(
            f"cannot stamp provenance: {root} is not a git checkout "
            "(missing .git); refusing to fabricate RR_SW_GIT_HASH"
        )
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_STRIP}
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    rev = proc.stdout.strip().lower()
    if proc.returncode != 0 or not _GIT_SHA_RE.fullmatch(rev):
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(
            f"cannot stamp provenance: unable to resolve git HEAD in {root}: {detail}"
        )
    return rev


def normalize_provenance(root: Path, rootfs: Path) -> None:
    """Replace RouteRTL's wall-clock stamp with deterministic source + payload provenance.

    DS-P3.264: stamps the real git revision of ``root`` (or refuses when it
    cannot be determined) instead of hashing only ``lock_inputs()``, while
    keeping ``SOURCE_DATE_EPOCH`` and ``RR_SW_PAYLOAD_SHA`` deterministic.
    """
    rev = _source_git_rev(root)
    date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(SOURCE_DATE_EPOCH))
    stamp = rootfs / "etc/rr-sw-provenance"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(
        "# DE-L0 deterministic rr linux PS-software provenance\n"
        f"RR_SW_GIT_HASH={rev[:8]}\n"
        f"RR_SW_GIT_REV={rev}\n"
        "RR_SW_GIT_DIRTY=0\n"
        f"RR_SW_BUILD_DATE={date}\n"
        f"RR_SW_BUILD_EPOCH={SOURCE_DATE_EPOCH}\n"
        f"RR_SW_PAYLOAD_SHA={_payload_sha(rootfs)}\n",
        encoding="utf-8",
    )


def boot_artifacts_for(stem: str) -> tuple[str, ...]:
    """``BOOT_ARTIFACTS`` with the per-target ``<stem>_hps.sof`` name
    (DS-P2.135 — every DE25 target's SPL-embedded SOF carries its own stem)."""
    return tuple(f"{stem}_hps.sof" if name == SOF_HPS_ARTIFACT else name
                 for name in BOOT_ARTIFACTS)


def normalize_artifact_mtimes(out_dir: Path) -> None:
    """Give mcopy stable FAT directory-entry timestamps.

    ``out_dir`` is the target-scoped ``out/<stem>`` dir, so its basename IS
    the target stem the artifact names derive from.
    """
    for name in boot_artifacts_for(out_dir.name):
        path = out_dir / name
        if path.is_file():
            os.utime(path, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH))


def _sdcard_content_sha(path: Path, out_dir: Path) -> str:
    """Hash files read back from the assembled image's P1 FAT and P2 ext4
    rootfs partitions, excluding random carrier metadata (FAT volume serial,
    ext4 UUID). Each readback is also byte-compared against the matching
    build output (``out_dir/<name>`` for FAT, ``out_dir/rootfs/<name>`` for
    the rootfs) — a wrong or stale partition (e.g. embedded-initramfs mode
    silently reverting the rootfs partition to empty) fails loudly here
    instead of only showing up as an opaque hash on the next ``lock --check``.
    """
    errors: list[str] = []
    for fat_offset in (SDCARD_FAT_OFFSET_BYTES, 0):
        digest = hashlib.sha256()
        ok = True
        forbidden = subprocess.run(
            ["mtype", "-i", f"{path}@@{fat_offset}",
             f"::/{UBOOT_ENV_ARTIFACT}"],
            capture_output=True, check=False,
        )
        if forbidden.returncode == 0:
            errors.append(
                f"forbidden /{UBOOT_ENV_ARTIFACT} is present on {path} P1; "
                "a persistent environment overrides the silicon-proven "
                "default distro boot and can strand the board"
            )
            continue
        for name in SDCARD_CONTENTS:
            proc = subprocess.run(
                ["mtype", "-i", f"{path}@@{fat_offset}", f"::/{name}"],
                capture_output=True, check=False,
            )
            if proc.returncode != 0:
                errors.append(proc.stderr.decode("utf-8", "replace"))
                ok = False
                break
            expected = (out_dir / name).read_bytes()
            if proc.stdout != expected:
                errors.append(f"FAT readback of {name} does not match {out_dir / name}")
                ok = False
                break
            digest.update(name.encode("ascii") + b"\0" + proc.stdout + b"\0")
        if not ok:
            continue
        for name in SDCARD_ROOTFS_CONTENTS:
            proc = subprocess.run(
                [sbin_tool("debugfs"), "-R", f"cat /{name}",
                 f"{path}?offset={SDCARD_ROOTFS_OFFSET_BYTES}"],
                capture_output=True, check=False,
            )
            stderr = proc.stderr.decode("utf-8", "replace")
            if proc.returncode != 0 or not proc.stdout or "not found" in stderr.lower():
                errors.append(stderr or f"empty ext4 rootfs readback for {name}")
                ok = False
                break
            source = out_dir / "rootfs" / name
            expected = source.read_bytes()
            if proc.stdout != expected:
                errors.append(f"ext4 rootfs readback of {name} does not match {source}")
                ok = False
                break
            digest.update(name.encode("ascii") + b"\0" + proc.stdout + b"\0")
        if ok:
            return digest.hexdigest()
    raise RuntimeError(
        "cannot verify DE25 FAT boot / ext4 rootfs partition contents: "
        + " | ".join(errors)
    )


# routertl's socfpga sdcard dependency preamble calls the generic
# `rr linux rootfs` step with NO --size-mb override, so it always packages
# its own (unused-by-the-final-image) out/rootfs.ext4 at the 32 MB CLI
# default — measured directly: mke2fs -d fails "Could not allocate block"
# against this project's ~1150-file staged rootfs (busybox + the AArch64
# Python/msgpack overlay). Building rootfs explicitly first, with a real
# --size-mb, then assembling with `sdcard --no-deps` (skipping that broken
# preamble entirely) works around it without touching routertl (another
# repo, out of scope for this worktree). The P2 partition rootfs.ext4 that
# actually ships on the SD card is a SEPARATE build inside `sdcard`'s own
# assembly step, sized from linux.sdcard.rootfs_mib (0 = autosize) — this
# constant only has to cover routertl's own redundant packaging step.
ROOTFS_EXT4_STAGE_SIZE_MB = 512


#: DS-P2.161: the gitignored per-deployment bench descriptor (DS-C1). The
#: tracked ``runtimes/hw.yml`` is the hardware contract, never this file.
DEPLOYMENT_HW_REL = "runtimes/hw.local.yml"


def _deployment_hw_path(root: Path) -> Path:
    """Return the deployment descriptor used by this image build.

    ``RR_BENCH_CONFIG`` preserves the existing hands-off bench invocation,
    while a normal project-local deployment uses the ignored
    ``runtimes/hw.local.yml``.  Neither path is copied into the image or
    lockfile.  DS-P2.161: ``runtimes/hw.yml`` is the TRACKED rr.hw.contract/v1
    hardware contract and carries no deployment state; it points rr's own
    bench resolver at ``hw.local.yml`` through ``extends:``.
    """
    override = os.environ.get("RR_BENCH_CONFIG")
    return Path(override).expanduser() if override else root / DEPLOYMENT_HW_REL


def deployment_bootargs(
    root: Path,
    *,
    target_rel: str | None = None,
    bench_target: str = BENCH_TARGET,
) -> str:
    """Compose a target's bootargs with the address declared in ``hw.yml``.

    DS-D.39 makes the public target address-free.  The deployed address and
    prefix are required inputs at the image boundary; malformed, missing, or
    ambiguous declarations abort instead of silently producing an image that
    falls through to unavailable DHCP.

    ``target_rel`` defaults to the ACTIVE DE25 target (DS-P2.135) so the DE25
    image build (its only in-tree caller) follows whichever DE25 target is
    set; PF-L0 (DS-P2.125) passes the PolarFire pair explicitly so
    ``tools/check_bench_ip.py`` resolves the pfsoc image's declared token
    through the SAME resolver the DE25 builder uses — one owner of "what is a
    deployable descriptor", across both boards.
    """
    if target_rel is None:
        target_rel = active_target_rel(root)
    target_path = root / target_rel
    try:
        target = yaml.safe_load(target_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"cannot load public target {target_path}: {exc}") from exc
    linux = target.get("linux") or {}
    base = str(linux.get("bootargs") or "").strip()
    tokens = base.split()
    if any(token.startswith((f"{CMDLINE_IP_KEY}=", "bench_ip=")) for token in tokens):
        raise RuntimeError(
            f"DS-D.39: public {target_rel} bootargs contain deployment network "
            "identity; remove ds_ip=/bench_ip= and declare it in runtimes/hw.local.yml"
        )

    hw_path = _deployment_hw_path(root)
    try:
        doc = yaml.safe_load(hw_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"DS-D.39: deployment descriptor missing: {hw_path}; copy "
            "runtimes/hw.example.yml to runtimes/hw.local.yml and declare "
            f"{bench_target}'s bench_ip resource"
        ) from exc
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"cannot load deployment descriptor {hw_path}: {exc}") from exc

    bench = doc.get("bench") or {}
    targets = bench.get("targets") or {}
    resources = bench.get("resources") or {}
    tdef = targets.get(bench_target)
    if not isinstance(tdef, dict):
        raise RuntimeError(f"{hw_path}: missing bench.targets.{bench_target}")
    key = tdef.get("cmdline_ip_key")
    if key != CMDLINE_IP_KEY:
        raise RuntimeError(
            f"{hw_path}: bench.targets.{bench_target}.cmdline_ip_key must be "
            f"{CMDLINE_IP_KEY!r}, got {key!r}"
        )
    resource_name = tdef.get("bench_ip")
    resource = resources.get(resource_name) if isinstance(resource_name, str) else None
    if not isinstance(resource, dict) or resource.get("kind") != "bench_ip":
        raise RuntimeError(
            f"{hw_path}: {bench_target}.bench_ip must name a kind: bench_ip resource"
        )
    address = str(resource.get("addr") or "")
    # The proven DE25 point-to-point deployment is /24. Deployments with a
    # different network can override this beside addr; older descriptors that
    # already declare the one authoritative address remain valid.
    prefix = resource.get("prefix", 24)
    try:
        parsed = ipaddress.ip_address(address)
        prefix_number = int(prefix)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{hw_path}: {resource_name} needs a valid addr and integer prefix"
        ) from exc
    if parsed.version != 4 or not 0 <= prefix_number <= 32:
        raise RuntimeError(
            f"{hw_path}: {resource_name} must declare an IPv4 addr and prefix 0..32"
        )
    return f"{base} {CMDLINE_IP_KEY}={parsed}/{prefix_number}".strip()


def _stage_target_stem_sof(root: Path) -> Path:
    """Stage a same-content copy of the built bitstream under the ACTIVE
    TARGET's identity stem, so rr's own name-based .sof lookup (and thus
    ``run_socfpga_sof_hps``'s ``<stem>_hps.sof`` output name) resolves to
    ``SOF_HPS_ARTIFACT`` above.

    rr's ``_find_project_sof`` (sdk.cli.commands.linux_steps._socfpga)
    matches a ``.sof`` by TARGET identity (``output_dir`` basename / target
    file stem — e.g. ``de25_datalogger``). Quartus instead names the built
    bitstream after project.yml's ``platform.name`` (``deepskopion``),
    since this project defines several targets under one Quartus project
    name — so no ``.sof`` in ``bitstream/`` naturally carries the
    target stem for rr to match, or for ``run_socfpga_sof_hps`` to
    derive ``<stem>_hps.sof`` from.

    A first attempt worked around the miss with an explicit
    ``linux.sof_path`` pointed straight at the Quartus-named file — that
    fixed the lookup but left the *output* name as ``deepskopion_hps.sof``,
    which nothing else in the boot chain (SOF_HPS_ARTIFACT, the ticket's
    close artifact) consumes. Staging a renamed copy first, and pointing
    ``linux.sof_path`` at THAT, fixes both ends with one name.
    """
    stem = active_target_stem(root)
    bitstream_dir = root / "build" / stem / "bitstream"
    target_sof = bitstream_dir / f"{stem}.sof"
    built = [p for p in bitstream_dir.glob("*.sof") if p != target_sof]
    if not built:
        raise RuntimeError(
            f"no built .sof in {bitstream_dir} — run `rr bitstream run` "
            "before `rr linux sof`."
        )
    source = max(built, key=lambda p: p.stat().st_mtime)
    if (not target_sof.is_file()
            or target_sof.stat().st_mtime < source.stat().st_mtime):
        shutil.copy2(source, target_sof)
    return target_sof


# ── DS-T2.71: the lwh2f decode crosscheck ────────────────────────────────
#
# Boot7/boot8 (2026-08-23) is the scar. Two CAN nodes were declared ENABLED
# in ``linux.dt_nodes`` at 0x20010000/0x20020000, ``rr linux dtb`` finally
# started applying dt_nodes, and the kernel wedged the NoC byte-identically
# right after "CAN device driver interface" — console dead, board recoverable
# only by a cold power cycle. Nothing in the build refused that configuration:
# a dt_node is free text until the kernel dereferences it on silicon, and the
# feedback loop for "this endpoint is not really there" was a wedged board.
#
# So the build now refuses it up front. Two directions, both mechanical:
#
#   A. Every ENABLED dt_node whose reg window lands on the lwh2f aperture
#      must be covered by a device DECLARED in system_memory_map.yml. A node
#      the design never declared is exactly the boot7/boot8 configuration.
#   B. Every device declared in system_memory_map.yml on that aperture must
#      land in a window the FABRIC actually decodes — read out of the RTL
#      (u_lwh2f's ``lite_addr_g`` and axil_demux3's selector slice + its
#      DECERR selector), not restated by hand. Without B, A would be
#      satisfiable by adding any address at all to the map, which is
#      rubber-stamping, not a crosscheck.
#
# ``status: disabled`` exempts a node from A — that is the declared,
# reviewable way to keep an endpoint in the tree while its fabric side is
# still missing, and it is what the interim DS-P3.144 fix used.
LWH2F_APERTURE_BASE = 0x20000000
LWH2F_BRIDGE_NAME = "lwh2f"
LWH2F_TOP_RTL = "hw/rtl/de25/deepskopion_de25_top.vhd"
LWH2F_DEMUX_RTL = "hw/rtl/de25/axil_demux3.vhd"
MEMORY_MAP_REL = "sw/linux/de25/system_memory_map.yml"
# DS-P2.135: kept as the datalogger default for importers; the check
# functions below resolve the ACTIVE target via active_target_rel(root).
TARGET_REL = "targets/de25_datalogger.yml"


def _rtl_lwh2f_aperture_bits(root: Path) -> int:
    """Address bits the lwh2f bridge presents to the fabric decoder.

    Read from the ``u_lwh2f`` generic map rather than restated: the bridge's
    ``lite_addr_g`` IS the aperture width the HPS sees, so a fabric that grows
    or shrinks its window moves this guard with it.
    """
    text = (root / LWH2F_TOP_RTL).read_text(encoding="utf-8")
    match = re.search(
        r"u_lwh2f\s*:\s*entity\s+work\.lwh2f_axilite_bridge\s*"
        r"generic\s+map\s*\((?P<generics>[^)]*)\)",
        text, re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise RuntimeError(
            f"{LWH2F_TOP_RTL}: no u_lwh2f generic map found — the lwh2f "
            "decode guard cannot read the aperture width out of the design"
        )
    bits = re.search(r"lite_addr_g\s*=>\s*(\d+)", match.group("generics"))
    if bits is None:
        raise RuntimeError(
            f"{LWH2F_TOP_RTL}: u_lwh2f declares no lite_addr_g — the lwh2f "
            "decode guard cannot read the aperture width out of the design"
        )
    return int(bits.group(1))


def _rtl_lwh2f_decoded_windows(root: Path) -> list[tuple[int, int]]:
    """CPU-absolute ``(base, span)`` windows the fabric decoder answers on.

    Derived from axil_demux3's own selector slice (``awaddr_r(H downto L)``)
    and the DECERR literal it compares that slice against, so a selector the
    RTL routes to DECERR is NOT reported as decoded.
    """
    aperture_span = 1 << _rtl_lwh2f_aperture_bits(root)
    text = (root / LWH2F_DEMUX_RTL).read_text(encoding="utf-8")
    slice_match = re.search(
        r"wr_sel_r\s*<=\s*to_integer\(unsigned\(awaddr_r\("
        r"(?P<high>\d+)\s+downto\s+(?P<low>\d+)\)\)\)",
        text, re.IGNORECASE,
    )
    if slice_match is None:
        raise RuntimeError(
            f"{LWH2F_DEMUX_RTL}: no wr_sel_r selector slice found — the lwh2f "
            "decode guard cannot read the decode map out of the design"
        )
    high = int(slice_match.group("high"))
    low = int(slice_match.group("low"))
    window_span = 1 << low
    selectors = 1 << (high - low + 1)
    decerr = {
        int(literal, 2)
        for literal in re.findall(
            rf'awaddr_r\({high} downto {low}\)\s*=\s*"([01]+)"'
            rf'|s_araddr_i\({high} downto {low}\)\s*=\s*"([01]+)"',
            text,
        )
        for literal in (literal[0] or literal[1],)
        if literal
    }
    if not decerr:
        raise RuntimeError(
            f"{LWH2F_DEMUX_RTL}: no DECERR selector comparison found — the "
            "lwh2f decode guard cannot tell a decoded window from an "
            "undecoded one"
        )
    windows = [
        (LWH2F_APERTURE_BASE + sel * window_span, window_span)
        for sel in range(selectors)
        if sel not in decerr
    ]
    # A selector window can never reach past the bridge's own aperture.
    return [w for w in windows
            if w[0] + w[1] <= LWH2F_APERTURE_BASE + aperture_span]


def _covers(outer: tuple[int, int], inner: tuple[int, int]) -> bool:
    return outer[0] <= inner[0] and inner[0] + inner[1] <= outer[0] + outer[1]


def _overlaps_aperture(window: tuple[int, int], root: Path) -> bool:
    aperture_span = 1 << _rtl_lwh2f_aperture_bits(root)
    end = LWH2F_APERTURE_BASE + aperture_span
    return window[0] < end and window[0] + window[1] > LWH2F_APERTURE_BASE


def _declared_lwh2f_devices(root: Path) -> list[tuple[str, int, int]]:
    """``(name, base, span)`` for every system_memory_map device on the lwh2f
    aperture — either by declared ``requires_bridge`` or by address."""
    data = yaml.safe_load(
        (root / MEMORY_MAP_REL).read_text(encoding="utf-8")) or {}
    devices = []
    for dev in data.get("devices") or []:
        if not isinstance(dev, dict) or "base_addr" not in dev or "span" not in dev:
            continue
        base = int(str(dev["base_addr"]), 0) if isinstance(dev["base_addr"], str) \
            else int(dev["base_addr"])
        span = int(str(dev["span"]), 0) if isinstance(dev["span"], str) \
            else int(dev["span"])
        bridge = str(dev.get("requires_bridge") or "").lower()
        if bridge == LWH2F_BRIDGE_NAME or _overlaps_aperture((base, span), root):
            devices.append((str(dev.get("name", "<unnamed>")), base, span))
    return devices


def _dt_node_reg_window(node: dict) -> tuple[int, int] | None:
    """The ``(base, span)`` a dt_node claims, or None when it declares none.

    Handles the two cell shapes this project's dt_nodes actually use: the
    2-cell ``[addr, size]`` of /soc@0 children and the 4-cell
    ``[addr_hi, addr_lo, size_hi, size_lo]`` of /reserved-memory.
    """
    reg = (node.get("properties") or {}).get("reg")
    if isinstance(reg, list) and len(reg) == 2 and all(
            isinstance(c, int) for c in reg):
        return int(reg[0]), int(reg[1])
    if isinstance(reg, list) and len(reg) == 4 and all(
            isinstance(c, int) for c in reg):
        return (int(reg[0]) << 32) | int(reg[1]), (int(reg[2]) << 32) | int(reg[3])
    return None


def _dt_node_unit_address(node: dict) -> int | None:
    leaf = str(node.get("path", "")).rsplit("/", 1)[-1]
    if "@" not in leaf:
        return None
    try:
        return int(leaf.rsplit("@", 1)[1], 16)
    except ValueError:
        return None


def _dt_node_is_enabled(node: dict) -> bool:
    status = (node.get("properties") or {}).get("status")
    return str(status).strip().lower() != "disabled"


def check_lwh2f_dt_nodes_are_decoded(root: Path) -> list[str]:
    """Return every reason this tree's dt_nodes must not reach a live dtb.

    Empty list == the crosscheck passes.  See the block comment above for
    what directions A and B mean and why boot7/boot8 needs both.
    """
    decoded = _rtl_lwh2f_decoded_windows(root)
    declared = _declared_lwh2f_devices(root)
    problems: list[str] = []

    # B — a declared window the fabric does not decode is a map that lies.
    for name, base, span in declared:
        if not any(_covers(w, (base, span)) for w in decoded):
            problems.append(
                f"{MEMORY_MAP_REL}: device '{name}' declares "
                f"0x{base:08X}+0x{span:X} on the lwh2f aperture, but "
                f"{LWH2F_DEMUX_RTL} decodes only "
                + ", ".join(f"0x{b:08X}+0x{s:X}" for b, s in decoded)
                + " — a read into the rest of the aperture is answered by "
                "nothing and wedges the NoC"
            )

    # A — an enabled dt_node the map never declared is boot7/boot8.
    target_rel = active_target_rel(root)
    target = yaml.safe_load(
        (root / target_rel).read_text(encoding="utf-8")) or {}
    for node in ((target.get("linux") or {}).get("dt_nodes") or []):
        if not isinstance(node, dict):
            continue
        window = _dt_node_reg_window(node)
        unit = _dt_node_unit_address(node)
        on_aperture = (window is not None and _overlaps_aperture(window, root)) or (
            unit is not None and _overlaps_aperture((unit, 1), root))
        if not on_aperture:
            continue
        if not _dt_node_is_enabled(node):
            continue
        path = node.get("path")
        if window is None:
            problems.append(
                f"{target_rel}: dt_node {path} sits on the lwh2f aperture but "
                "declares no parseable `reg` window, so the build cannot "
                "prove the fabric decodes it — give it `reg: [base, span]` "
                "or `status: disabled`"
            )
            continue
        if not any(_covers((b, s), window) for _n, b, s in declared):
            problems.append(
                f"{target_rel}: dt_node {path} is ENABLED at "
                f"0x{window[0]:08X}+0x{window[1]:X}, which no device in "
                f"{MEMORY_MAP_REL} declares"
                + (" (declared: "
                   + ", ".join(f"{n} 0x{b:08X}+0x{s:X}" for n, b, s in declared)
                   + ")" if declared else " (that map declares nothing there)")
                + " — the kernel will probe an endpoint the design never "
                "claimed. Land the fabric slave and declare its window, or "
                "carry `status: disabled` (boot7/boot8, DS-T2.71)"
            )
    return problems


def assert_lwh2f_dt_nodes_are_decoded(root: Path) -> None:
    """Refuse to build a boot set whose dt_nodes point at undecoded fabric."""
    problems = check_lwh2f_dt_nodes_are_decoded(root)
    if problems:
        raise RuntimeError(
            "lwh2f decode crosscheck refused this configuration (DS-T2.71):\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


# ── DS-P3.154: the dtb must REALISE the dt_nodes it was built from ───────
#
# The DS-T2.71 crosscheck above proves the CONFIGURATION is legal.  It cannot
# prove the shipped dtb ended up carrying it, and those are different claims
# with a measured gap between them.
#
# `rr linux dtb` applies linux.dt_nodes as a dtc ROOT OVERLAY appended to the
# decompiled board dtb, and dtc merges an overlay by "later property wins".
# A property the overlay does not mention therefore SURVIVES from the base.
# So re-patching a dtb that was already patched by an EARLIER revision of the
# target keeps that revision's properties: re-enabling a node by deleting its
# `status: disabled` line from targets/de25_datalogger.yml leaves the shipped dtb
# with `status = "disabled"` still on the node, because nothing in the new
# overlay overwrites it. The build reports "applied dt_nodes: … can@20010000",
# the tree says ENABLED, the DS-T2.71 crosscheck passes — and the kernel skips
# the node. That is exactly how DS-T2.71's silicon half could be witnessed
# GREEN while proving nothing: a CAN probe that never runs cannot wedge.
#
# So after the dtb exists, read the ARTIFACT back and require it to agree with
# the declaration on the three facts that decide whether the kernel touches
# the endpoint at all: the node exists, its status is what the target says,
# and its `reg` window is the declared one.
def _dt_cells_to_bytes(value) -> bytes | None:
    """Encode a dt_nodes ``reg`` declaration the way dtc emits it (u32 cells).

    Returns ``None`` for a shape this guard does not model, so an unfamiliar
    declaration is reported as unverifiable rather than silently passed.
    """
    cells = [value] if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(cells, list) or not cells:
        return None
    if not all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c < 2**32
               for c in cells):
        return None
    return b"".join(struct.pack(">I", c) for c in cells)


def check_dtb_realises_dt_nodes(root: Path, dtb: Path) -> list[str]:
    """Return every disagreement between the built dtb and linux.dt_nodes."""
    blob = dtb.read_bytes()
    nodes: dict[str, dict[str, bytes]] = {}
    for node_path, name, value in _iter_fdt_props(blob):
        entry = nodes.setdefault("/" + node_path, {})
        if name is not None:
            entry[name] = value

    target_rel = active_target_rel(root)
    target = yaml.safe_load(
        (root / target_rel).read_text(encoding="utf-8")) or {}
    problems: list[str] = []
    for node in ((target.get("linux") or {}).get("dt_nodes") or []):
        if not isinstance(node, dict):
            continue
        path = str(node.get("path") or "")
        props = node.get("properties") or {}
        built = nodes.get(path)
        if built is None:
            problems.append(
                f"{dtb.name}: dt_node {path} is declared in {target_rel} but "
                "no such node exists in the built dtb — the overlay was not "
                "applied, so the kernel never sees it"
            )
            continue

        want_status = props.get("status")
        got_status = built.get("status")
        got_status_text = (got_status.rstrip(b"\x00").decode("ascii", "replace")
                           if got_status is not None else None)
        if want_status is not None:
            if got_status_text != str(want_status):
                problems.append(
                    f"{dtb.name}: dt_node {path} is declared "
                    f"status: {want_status!r} but the built dtb carries "
                    f"{got_status_text!r}"
                )
        elif got_status_text == "disabled":
            problems.append(
                f"{dtb.name}: dt_node {path} declares no status (ENABLED) but "
                "the built dtb still carries status = \"disabled\" — a stale "
                "property survived the dtc overlay merge, so the kernel will "
                "skip this endpoint and any silicon witness of it is vacuous "
                "(DS-P3.154). Rebuild the dtb from the RAW kernel dtb, not "
                "from an already-patched one"
            )

        if "reg" in props:
            want_reg = _dt_cells_to_bytes(props["reg"])
            got_reg = built.get("reg")
            if want_reg is None:
                problems.append(
                    f"{dtb.name}: dt_node {path} declares a `reg` this guard "
                    f"cannot encode ({props['reg']!r}) — it cannot be checked "
                    "against the built dtb"
                )
            elif got_reg != want_reg:
                problems.append(
                    f"{dtb.name}: dt_node {path} declares reg {want_reg.hex()} "
                    f"but the built dtb carries "
                    f"{got_reg.hex() if got_reg is not None else 'no reg'}"
                )
    return problems


def assert_dtb_realises_dt_nodes(root: Path, dtb: Path) -> None:
    """Refuse a boot set whose dtb does not carry its declared dt_nodes."""
    problems = check_dtb_realises_dt_nodes(root, dtb)
    if problems:
        raise RuntimeError(
            f"the built board dtb does not realise {active_target_rel(root)}'s "
            "dt_nodes (DS-P3.154):\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


# ── DS-T1.15: an enabled ctucanfd node is allowed ONLY when the fabric BOUNDS a
#    wedged CTU response (the axil_demux3 completion watchdog) ──
#
# History.  MEASURED 2026-09-03 on the DE25 (DS-T1.11): a from-head de25 build
# with the two ctu,ctucanfd nodes ENABLED hung the kernel DETERMINISTICALLY at
# `[0.443] CAN device driver interface` — the mainline ctucanfd probe's MODE.RST
# soft-reset + poll of the CTU aperture through the lwh2f `0x2000_0000` window
# never returned, wedging the HPS master (cold-recover only).  The INTERIM
# (this guard's first form) refused ANY enabled ctucanfd node — time bought, not
# a fix, and the ticket's PROHIBITED end state.
#
# The lasting bound is DS-T1.15's axil_demux3 completion watchdog (wd_cycles_g):
# the demux sits directly below the lwh2f master and force-completes a selected
# slave's response that does not arrive within wd_cycles_g cycles (OKAY / reads
# all-ones), so a wedged CTU read RETURNS instead of hanging the CPU — the probe
# then -ETIMEDOUTs and boot continues to the daemon.  With that bound armed in
# the fabric, an enabled ctucanfd node can no longer HANG the board, so it is
# allowed.
#
# So the invariant flips from "no enabled ctucanfd node" to "an enabled ctucanfd
# node REQUIRES the demux watchdog armed in deepskopion_de25_top"
# (``wd_cycles_g /= 0`` on the ``axil_demux3`` instance).  A disabled node is
# still trivially allowed (never probed).  Deleting the watchdog / setting
# ``wd_cycles_g => 0`` re-exposes the unbounded DS-T1.11 hang, so the guard
# refuses an enabled node again — the mutation that proves it is not vacuous.
#
# On-silicon note (DS-T1.15 witness 2026-09-07, holder d-aa181a3b): a fresh-P&R
# from-head build with this watchdog + the REAL CTU cores boots the DE25 past
# the DS-T1.11 hang to userspace and both can0/can1 register.  The MODE.RST
# wedge did NOT reproduce on that build (bare-metal read returns 0207cafd), so
# the watchdog was not triggered — it stands as the sim-proven defensive bound
# (tb_axil_demux3_wedge_guard) that keeps a placement-marginal recurrence from
# hanging the board.
_CTUCANFD_COMPATIBLE = b"ctu,ctucanfd"
# ``u_demux : entity work.axil_demux3 generic map (wd_cycles_g => 4096)``
_DEMUX_INST_RE = re.compile(r"entity\s+work\.axil_demux3(.*?)port\s+map",
                            re.IGNORECASE | re.DOTALL)
_WD_CYCLES_RE = re.compile(r"wd_cycles_g\s*=>\s*(\d+)", re.IGNORECASE)


def demux_watchdog_cycles(root: Path) -> int:
    """The axil_demux3 completion-watchdog bound (clk cycles) armed on the
    ``u_demux`` instance in deepskopion_de25_top, or 0 if the watchdog is
    disabled/absent.  0 == a wedged CTU response is UNBOUNDED → the DS-T1.11
    hang is live and an enabled ctucanfd node is unsafe."""
    text = (root / LWH2F_TOP_RTL).read_text(encoding="utf-8", errors="replace")
    inst = _DEMUX_INST_RE.search(text)
    if inst is None:
        return 0
    m = _WD_CYCLES_RE.search(inst.group(1))
    return int(m.group(1)) if m else 0


def check_ctucanfd_probe_is_bounded(dtb: Path, root: Path) -> list[str]:
    """Return a reason for every enabled ctucanfd node that the fabric does NOT
    bound.  Empty list == every enabled ctucanfd node in the dtb is safe because
    the demux watchdog is armed (or there is no enabled node).  A ``disabled``
    node is never probed, so it is always allowed."""
    blob = dtb.read_bytes()
    nodes: dict[str, dict[str, bytes]] = {}
    for node_path, name, value in _iter_fdt_props(blob):
        entry = nodes.setdefault("/" + node_path, {})
        if name is not None:
            entry[name] = value

    wd = demux_watchdog_cycles(root)
    problems: list[str] = []
    for path, props in nodes.items():
        compatible = props.get("compatible")
        if compatible is None or _CTUCANFD_COMPATIBLE not in compatible:
            continue
        status = props.get("status")
        status_text = (status.rstrip(b"\x00").decode("ascii", "replace")
                       if status is not None else None)
        if status_text == "disabled":
            continue
        if wd > 0:
            continue  # the demux watchdog bounds a wedged probe → safe
        problems.append(
            f"{dtb.name}: node {path} is compatible \"ctu,ctucanfd\" and "
            f"{'ENABLED' if status_text is None else f'status {status_text!r}'}"
            " but the axil_demux3 completion watchdog is DISABLED "
            f"(wd_cycles_g={wd} in {LWH2F_TOP_RTL}). The mainline ctucanfd probe"
            " MODE.RST-poll of the CTU aperture can wedge the lwh2f master with"
            " no bound, hanging the kernel at \"CAN device driver interface\""
            " (DS-T1.11, 2026-09-03). Arm the watchdog (wd_cycles_g /= 0) before"
            " enabling a ctucanfd node, or carry `status: disabled`."
        )
    return problems


def assert_ctucanfd_probe_is_bounded(dtb: Path, root: Path) -> None:
    """Refuse a boot set with an enabled ctucanfd node the fabric cannot bound."""
    problems = check_ctucanfd_probe_is_bounded(dtb, root)
    if problems:
        raise RuntimeError(
            "the built board dtb enables a ctucanfd node the fabric does not "
            "bound (DS-T1.15): the demux watchdog is off, so its probe can wedge "
            "the lwh2f bridge unbounded (DS-T1.11):\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


def build(root: Path) -> None:
    """Build via sanctioned rr linux surfaces with deterministic provenance."""
    # DS-P2.135: any DE25 target builds through this pipeline; the resolver
    # refuses a non-DE25 active target with the `rr target set` fix spelled out.
    active_target_stem(root)
    # DS-T2.71: before ANY rr linux step runs — `rr linux dtb` is what turns
    # linux.dt_nodes into a live board DTB, and a node pointing at fabric the
    # design does not decode is the boot7/boot8 NoC wedge. Refuse here, where
    # the cost is an error message, instead of on silicon, where the cost is a
    # dead console and a cold power cycle.
    assert_lwh2f_dt_nodes_are_decoded(root)
    env = os.environ.copy()
    env.update({
        "SOURCE_DATE_EPOCH": str(SOURCE_DATE_EPOCH),
        "KBUILD_BUILD_TIMESTAMP": KBUILD_BUILD_TIMESTAMP,
        "KBUILD_BUILD_USER": KBUILD_BUILD_USER,
        "KBUILD_BUILD_HOST": KBUILD_BUILD_HOST,
        "KBUILD_BUILD_VERSION": str(KBUILD_BUILD_VERSION),
        "MTOOLS_DATE_STRING": MTOOLS_DATE_STRING,
    })
    bootargs = deployment_bootargs(root)
    # Build each boot-chain dependency explicitly (rootfs sized for real,
    # then ATF+SPL+U-Boot, then kernel, then boot.scr) instead of letting a
    # bare `rr linux sdcard --refresh` run routertl's own dependency preamble
    # — see ROOTFS_EXT4_STAGE_SIZE_MB above for why.
    subprocess.run(["rr", "linux", "rootfs", "--mode", "partition", "--size-mb",
                    str(ROOTFS_EXT4_STAGE_SIZE_MB), "--refresh"], cwd=root,
                   env=env, check=True)
    # DS-P2.310: U-Boot's default env `bootargs` (CONFIG_BOOTARGS) replaces the
    # dtb's /chosen/bootargs at `booti`, so it must be the SAME composed string
    # `rr linux dtb --bootargs` gets below (rr linux uboot --help says so).
    # Without it U-Boot carried the bare target string: no ds_ip on a fresh
    # build, and on the C3 card (2026-11-15) an older string with no
    # ds_ring_word_bytes, so the daemon reported every PL zero-pad as zs_resync.
    subprocess.run(["rr", "linux", "uboot", "--refresh", "--bootargs", bootargs],
                   cwd=root, env=env, check=True)
    # DS-P2.94: `rr linux uboot` above builds U-Boot from the unpatched GSRD
    # control DTB (EMAC0 phy reg=0) — see UBOOT_CONTROL_BOARD_DT above for
    # why routertl itself cannot be taught this fix from this worktree.
    # Rebuild u-boot.itb with the KSZ9031's real MDIO address before it ships.
    rebuild_uboot_with_emac0_phy_fix(root)
    # DS-P2.310: prove the u-boot.itb that ships carries that exact string.
    assert_uboot_itb_default_bootargs(linux_out_dir(root) / "u-boot.itb", bootargs)
    # fpga-first (see SOF_HPS_ARTIFACT above): embed the SPL just built into
    # the project .sof so the SDM has a fabric-configuring, HPS-bootable
    # object to load — needs u-boot-spl-dtb.hex, just staged by the uboot
    # step above, and an already-synthesized <output_dir>/bitstream/*.sof
    # (built separately via `rr bitstream run`; this is a Linux-image step,
    # not a synthesis one, so it fails closed with a clear message if that
    # prerequisite .sof is missing rather than silently skipping the file).
    _stage_target_stem_sof(root)
    subprocess.run(["rr", "linux", "sof", "--refresh"], cwd=root,
                   env=env, check=True)
    out = linux_out_dir(root)
    normalize_provenance(root, out / "rootfs")
    subprocess.run(["rr", "linux", "kernel", "--refresh"], cwd=root,
                   env=env, check=True)
    # DS-P1.55 measured defect: the kernel step stages the RAW SoCDK dtb; the
    # console retarget (serial0 alias / stdout-path → UART1), the
    # dtb_disable_nodes, and every linux.dt_nodes declaration (uio
    # ring-watermark, CAN nodes, the EMAC0/KSZ9031 PHY fix, the DS-P1.55 SD
    # vqmmc regulator override) are applied only by `rr linux dtb`
    # (run_socfpga_dtb), which this sequence never ran. Decompiling the
    # shipped card's dtb proved it: chosen/bootargs still the SoCDK default,
    # serial0 → UART0, spi@108d2000 still "okay", zero deepskopion nodes.
    # Deliberately NO --refresh here: the kernel step above just rebuilt the
    # dtb, and `rr linux dtb --refresh` would trigger a second full kernel
    # distclean+rebuild; without it the step finds the fresh dtb and still
    # applies the patch pipeline unconditionally.
    subprocess.run(["rr", "linux", "dtb", "--bootargs", bootargs], cwd=root,
                   env=env, check=True)
    # DS-P3.154: the dtb now EXISTS; prove it carries what it was built
    # from before anything downstream (boot.scr, the SD image, a bench
    # witness) treats the declaration as realised. See
    # check_dtb_realises_dt_nodes above for the merge defect this catches.
    assert_dtb_realises_dt_nodes(root, out / DTB_ARTIFACT)
    # DS-T1.15: the dtb exists and realises its declaration; now prove any
    # enabled ctucanfd node is BOUNDED by the fabric — the axil_demux3 completion
    # watchdog must be armed so the probe's MODE.RST-poll cannot wedge the lwh2f
    # bridge unbounded and hang the kernel at "CAN device driver interface"
    # (DS-T1.11). Refuse here, before the SD image is assembled, not on a dead
    # console. (Re-aimed from the DS-T1.11 interim "no enabled ctucanfd node".)
    assert_ctucanfd_probe_is_bounded(out / DTB_ARTIFACT, root)
    subprocess.run(["rr", "linux", "bootscr", "--refresh"], cwd=root,
                   env=env, check=True)
    # DS-T2.72 (boots 17/18/20): hands-off boot uses U-Boot's default distro
    # flow and the bootargs patched into the DTB /chosen node above.  Any
    # persistent environment can replace that working default bootcmd.  Drop
    # stale output from the reverted emitter before assembling the card.
    (out / UBOOT_ENV_ARTIFACT).unlink(missing_ok=True)
    (out / "env.txt").unlink(missing_ok=True)
    normalize_artifact_mtimes(out)
    subprocess.run(["rr", "linux", "sdcard", "--no-deps", "--refresh"],
                   cwd=root, env=env, check=True)
    # DS-P3.316: judge the card the board boots, after every layer is merged.
    assert_card_rootfs(out)


def assert_card_rootfs(out: Path) -> None:
    """The image gate (DS-P3.316, DS-P3.348): refuse the built card when its rootfs has a
    problem, an unaccepted WARN of the init-script scan included, naming each one."""
    problems = rootfs_image_problems(out / "sdcard.img", SDCARD_ROOTFS_OFFSET_BYTES)
    if problems:
        raise RuntimeError("the DE25 card's rootfs is not bootable as built: " + "; ".join(problems))


def build_lock(root: Path, out_dir: Path, source_root: Path) -> dict[str, object]:
    """Describe the complete reproducible DE25 input→boot-set relationship."""
    inputs: dict[str, str] = {}
    for name in lock_inputs(root):
        path = root / name
        if not path.is_file():
            raise RuntimeError(f"provenance input is missing: {path}")
        inputs[name] = _sha256(path)
    inputs["msgpack-1.1.1.tar.gz"] = MSGPACK_SHA256
    inputs[PYTHON_ARCHIVE] = PYTHON_SHA256
    inputs[de25_toolchain_stage.TOOLCHAIN_ARCHIVE] = de25_toolchain_stage.TOOLCHAIN_SHA256

    sources: dict[str, str] = {}
    for name in SOURCE_REPOS:
        repo = source_root / name
        if not repo.is_dir():
            raise RuntimeError(f"provenance source checkout is missing: {repo}")
        sources[name] = _git_commit(repo)

    artifacts: dict[str, dict[str, int | str]] = {}
    for name in boot_artifacts_for(active_target_stem(root)):
        path = out_dir / name
        if not path.is_file():
            raise RuntimeError(f"boot artifact is missing: {path}")
        if name == "sdcard.img":
            artifacts[name] = {
                "content_sha256": _sdcard_content_sha(path, out_dir),
                "size": path.stat().st_size,
            }
        else:
            artifacts[name] = {"sha256": _sha256(path), "size": path.stat().st_size}
    return {
        "version": 1,
        "target": active_target_stem(root),
        "architecture": "aarch64",
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "mtools_date_string": MTOOLS_DATE_STRING,
        "kernel_build_identity": {
            "user": KBUILD_BUILD_USER,
            "host": KBUILD_BUILD_HOST,
            "timestamp": KBUILD_BUILD_TIMESTAMP,
            "version": KBUILD_BUILD_VERSION,
        },
        "inputs": inputs,
        "sources": sources,
        "artifacts": artifacts,
    }


def lock(root: Path, out_dir: Path, source_root: Path, output: Path,
         check: Path | None) -> None:
    manifest = build_lock(root, out_dir, source_root)
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if check is not None:
        if not check.is_file():
            raise RuntimeError(f"frozen DE25 lock is missing: {check}")
        if check.read_text(encoding="utf-8") != encoded:
            raise RuntimeError(
                f"DE25 provenance drift: live boot set does not match {check}"
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".tmp")
    temp.write_text(encoded, encoding="utf-8")
    os.replace(temp, output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("--root", type=Path, default=ROOT)
    stage_parser.add_argument("--output", type=Path)
    stage_parser.add_argument("--python-output", type=Path)
    stage_parser.add_argument("--cache", type=Path)
    stage_parser.add_argument("--toolchain-dir", type=Path)
    lock_parser = sub.add_parser("lock")
    lock_parser.add_argument("--root", type=Path, default=ROOT)
    lock_parser.add_argument("--out-dir", type=Path)
    lock_parser.add_argument("--source-root", type=Path)
    lock_parser.add_argument("--output", type=Path)
    lock_parser.add_argument("--check", type=Path)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--root", type=Path, default=ROOT)
    # DS-T2.71: the same crosscheck `build` runs, available on its own so a
    # reviewer (or a test) can exercise it without a full boot-set build.
    decode_parser = sub.add_parser("check-dt-decode")
    decode_parser.add_argument("--root", type=Path, default=ROOT)
    dtb_parser = sub.add_parser("check-dtb-nodes")
    dtb_parser.add_argument("--root", type=Path, default=ROOT)
    dtb_parser.add_argument("--dtb", type=Path)
    ctucanfd_parser = sub.add_parser("check-ctucanfd")
    ctucanfd_parser.add_argument("--root", type=Path, default=ROOT)
    ctucanfd_parser.add_argument("--dtb", type=Path)
    normalize_parser = sub.add_parser("normalize-provenance")
    normalize_parser.add_argument("--root", type=Path, default=ROOT)
    normalize_parser.add_argument("--rootfs", type=Path)
    rootfs_parser = sub.add_parser("check-rootfs")
    rootfs_parser.add_argument("--root", type=Path, default=ROOT)
    rootfs_parser.add_argument("image", type=Path)
    rootfs_parser.add_argument("--offset", type=int, default=0,
                               help=f"byte offset of the ext4 (the DE25 card's P2: {SDCARD_ROOTFS_OFFSET_BYTES})")
    mtimes_parser = sub.add_parser("normalize-artifact-mtimes")
    mtimes_parser.add_argument("--root", type=Path, default=ROOT)
    mtimes_parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    try:
        if args.command == "stage":
            output = (args.output or root / "sw/linux/de25/rootfs_overlay").resolve()
            python_output = (
                args.python_output or root / "sw/linux/de25/python_overlay"
            ).resolve()
            cache = (args.cache or root / "sw/linux/de25/.cache").resolve()
            toolchain_dir = args.toolchain_dir.resolve() if args.toolchain_dir else None
            stage(root, output, python_output, cache, toolchain_dir)
        elif args.command == "lock":
            out_dir = (args.out_dir or linux_out_dir(root)).resolve()
            source_root = (args.source_root or root / "sw/linux/de25/src").resolve()
            output = (args.output or out_dir / "de25-linux.lock").resolve()
            check = args.check.resolve() if args.check else None
            lock(root, out_dir, source_root, output, check)
        elif args.command == "build":
            build(root)
            output = linux_out_dir(root)
        elif args.command == "check-dt-decode":
            assert_lwh2f_dt_nodes_are_decoded(root)
            output = root / active_target_rel(root)
        elif args.command == "check-dtb-nodes":
            output = (args.dtb or linux_out_dir(root) / DTB_ARTIFACT)
            assert_dtb_realises_dt_nodes(root, output)
        elif args.command == "check-ctucanfd":
            output = (args.dtb or linux_out_dir(root) / DTB_ARTIFACT)
            assert_ctucanfd_probe_is_bounded(output, root)
        elif args.command == "check-rootfs":
            output = args.image.resolve()
            problems = rootfs_image_problems(output, args.offset)
            if problems:
                raise RuntimeError("; ".join(problems))
        elif args.command == "normalize-provenance":
            rootfs = (args.rootfs or linux_out_dir(root) / "rootfs").resolve()
            normalize_provenance(root, rootfs)
            output = rootfs / "etc/rr-sw-provenance"
        else:
            out_dir = (args.out_dir or linux_out_dir(root)).resolve()
            normalize_artifact_mtimes(out_dir)
            output = out_dir
    except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"DE-L0 {args.command.upper()} FAILED: {exc}", file=os.sys.stderr)
        return 1
    if args.command == "stage":
        print(f"DE-L0 staged deterministic DE25 overlay: {output}")
        print(f"  CPython {PYTHON_VERSION} AArch64 sha256={PYTHON_SHA256}")
        print(f"  msgpack {MSGPACK_VERSION} sha256={MSGPACK_SHA256}")
    elif args.command == "lock":
        print(f"DE-L0 provenance lock verified: {output}")
    elif args.command == "build":
        print(f"DE-L0 deterministic rr linux boot set built: {output}")
    elif args.command == "check-dtb-nodes":
        print(f"DS-P3.154 dtb realisation check passed: {output} carries "
              f"every dt_node {active_target_rel(root)} declares, with the declared "
              "status and reg")
    elif args.command == "check-ctucanfd":
        print(f"DS-T1.15 ctucanfd probe-bound check passed: {output} — every "
              "enabled \"ctu,ctucanfd\" node is bounded by the axil_demux3 "
              f"watchdog (wd_cycles_g={demux_watchdog_cycles(root)}), so a "
              "wedged probe cannot hang the lwh2f bridge")
    elif args.command == "check-dt-decode":
        print("DS-T2.71 lwh2f decode crosscheck passed: every enabled dt_node "
              f"window in {output} is declared in {MEMORY_MAP_REL} and decoded "
              f"by {LWH2F_DEMUX_RTL}")
    elif args.command == "check-rootfs":
        print(f"DS-P3.316 rootfs check passed: {output} holds no dropbear init script, key "
              "tool or dropbear binary, and every program its init scripts name")
    elif args.command == "normalize-provenance":
        print(f"DE-L0 normalized rootfs provenance: {output}")
    else:
        print(f"DE-L0 normalized boot artifact mtimes: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
