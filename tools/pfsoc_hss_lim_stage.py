#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""DS-P2.145 (PF-L0e) — the in-repo caller that wires the HSS L2-LIM firmware leg.

WHY THIS EXISTS
---------------
The DS-P2.145 landing refusal found that the two HSS fixes
(``sw/linux/pfsoc/hss/link_hss_l2lim.py`` — relink for the backed L2-LIM base
``0x08000000``; ``sw/linux/pfsoc/hss/disco_uart0_console.py`` — retarget the E51
console to MMUART4, the reference console on MSSIO_B2) were **sideband helpers with no production caller**: nothing
in the repo, the ``Makefile``, or the ``rr linux hss`` / ``hss-load`` path
invoked them, so ``hss-lim.elf`` was a file the next ``rr program pfsoc hss-load``
would never pick up (that search stages ``build/hss-l2scratch.elf``, entry
``0x0A000000``).

This module is that caller. Run against the (gitignored) HSS clone AFTER
``rr linux hss`` has built it, it:

  1. applies the MMUART4 console overlay to the clone
     (``disco_uart0_console.apply``),
  2. relinks the built objects for the backed L2-LIM base
     (``link_hss_l2lim.relink`` -> ``hss-lim.elf``, entry ``0x08000000``),
  3. **stages** the LIM ELF over the FIRST path the SDK's ``transient_candidates``
     search reaches (``build/hss-l2scratch.elf``), so the very next
     ``rr program pfsoc hss-load`` loads the BACKED image
     (``link_hss_l2lim.stage_lim_as_transient``),
  4. optionally generates the HSS ``payload.bin`` with the built
     ``hss-payload-generator`` (libyaml-dev 0.2.2 is installed on this bench;
     the old "missing" blocker was stale). ``payload.bin`` wraps the U54
     NEXT-STAGE image (U-Boot), NOT the HSS firmware: HSS is the E51 first stage
     and is never wrapped in payload.bin (DS-P2.145 round-3 finding #2).

It performs NO hardware access and NO shared-SDK / upstream in-place edit; the
overlay + relink touch only the gitignored clone, and the staging writes into the
clone's own ``build/`` dir. It is deterministic and idempotent.

TWO-PHASE FLOW (the console/pad fix has a pre-build half)
---------------------------------------------------------
Two of the fixes must be applied BEFORE the build compiles them in — the MMUART4
console source and the authoritative MSS design XML (so the config-generator
emits the ``MMUART_4 -> MSSIO_B2`` pad mux; DS-P3.160). So the flow is:

    # 0. SDK checks out the HSS clone (no build yet), e.g. `rr linux hss --checkout`
    #    or the first part of `rr linux hss`.
    # 1. PRE-BUILD overlays (MMUART4 console source + authoritative MSS XML):
    tools/pfsoc_hss_lim_stage.py --prebuild \
        --hss-dir sw/linux/pfsoc/src/hart-software-services
    # 2. SDK builds/refreshes the HSS clone: `rr linux hss` (or `--refresh`).
    # 3. POST-BUILD: LIM relink + stage onto the SDK-searched transient path:
    tools/pfsoc_hss_lim_stage.py \
        --hss-dir sw/linux/pfsoc/src/hart-software-services \
        --out     sw/linux/pfsoc/out/pfsoc_datalogger/hss-lim.elf

USAGE
-----
    tools/pfsoc_hss_lim_stage.py \
        --hss-dir sw/linux/pfsoc/src/hart-software-services \
        --out     sw/linux/pfsoc/out/pfsoc_datalogger/hss-lim.elf \
        [--payload-out sw/linux/pfsoc/out/pfsoc_datalogger/payload.bin \
         --next-stage  sw/linux/pfsoc/out/pfsoc_datalogger/u-boot.bin] \
        [--cross riscv64-unknown-elf-]

    (``--payload-out`` wraps the U54 NEXT-STAGE ``--next-stage`` image, e.g. U-Boot,
    NOT the HSS firmware.)
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import struct
import subprocess
import sys
from pathlib import Path

_HSS_TOOLS = Path(__file__).resolve().parents[1] / "sw" / "linux" / "pfsoc" / "hss"


def _load(mod_path: Path, name: str):
    if not mod_path.is_file():
        raise SystemExit(f"required HSS helper missing: {mod_path}")
    spec = importlib.util.spec_from_file_location(name, mod_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def find_payload_generator(hss_dir: Path) -> Path | None:
    """Locate a built ``hss-payload-generator`` (in the clone, or on PATH)."""
    import shutil

    built = hss_dir / "tools" / "hss-payload-generator" / "hss-payload-generator"
    if built.is_file():
        return built
    on_path = shutil.which("hss-payload-generator")
    return Path(on_path) if on_path else None


# The U54 application entry the HSS payload jumps to. This is the standard
# PolarFire SoC U-Boot SPL/proper load address for the DDR-resident next stage
# (matches the SDK's own ``generate_pfsoc_payload``: exec-addr 0x80200000,
# owner-hart u54_1, priv-mode prv_s). It is a NEXT-STAGE (U54) address, NOT the
# E51 firmware's LIM base 0x08000000.
_U54_NEXT_STAGE_EXEC_ADDR = "0x80200000"
_U54_NEXT_STAGE_PRIV_MODE = "prv_s"
_U54_NEXT_STAGE_OWNER_HART = "u54_1"
# HSS payload.bin magic (little-endian 0xb007c0de). A raw U-Boot copy does
# not start with this; HSS will not boot that blob as a payload.
HSS_PAYLOAD_MAGIC = struct.pack("<I", 0xB007C0DE)
_HSS_FIRMWARE_CONTENT = (
    b"HSS-FIRMWARE",
    b"Hart Software Service",
    b"Hart Software Services",
)


def build_payload_config(
    next_stage_image: Path,
    *,
    exec_addr: str | None = None,
    priv_mode: str | None = None,
    owner_hart: str | None = None,
) -> str:
    """Return the hss-payload-generator YAML for wrapping the U54 NEXT-STAGE image.

    CRITICAL SEMANTICS (DS-P2.145 round-3 finding #2): HSS is the E51 first-stage
    firmware — it boots from eNVM (or, transiently, from L2-LIM). **HSS is NOT
    wrapped in payload.bin.** ``payload.bin`` is the image HSS *loads and hands to
    the U54 application harts* — i.e. the next stage (U-Boot / a bare-metal U54
    app), owner-hart ``u54_*``, executing at the U54 DDR address, in supervisor
    mode. Wrapping the HSS LIM ELF here (as the prior revision did, with
    ``owner-hart: u54_1`` + ``exec-addr 0x08000000``) is a category error: it makes
    the U54s jump into the E51's firmware image in the LIM region, which is neither
    what HSS boots nor what the U54s run.
    """
    exec_addr = exec_addr or _U54_NEXT_STAGE_EXEC_ADDR
    priv_mode = priv_mode or _U54_NEXT_STAGE_PRIV_MODE
    owner_hart = owner_hart or _U54_NEXT_STAGE_OWNER_HART
    return (
        "set-name: 'PolarFire-SoC-HSS::next-stage'\n"
        "hart-entry-points: {u54_1: '%s', u54_2: '%s', u54_3: '%s', u54_4: '%s'}\n"
        "payloads:\n"
        f"  {next_stage_image}:\n"
        f"    exec-addr: '{exec_addr}'\n"
        f"    owner-hart: {owner_hart}\n"
        "    secondary-hart: u54_2\n"
        "    secondary-hart: u54_3\n"
        "    secondary-hart: u54_4\n"
        f"    priv-mode: {priv_mode}\n"
        % (exec_addr, exec_addr, exec_addr, exec_addr)
    )


_HSS_FIRMWARE_NAME = re.compile(r"(?:^|[._-])hss(?:[-_.]|$)", re.IGNORECASE)


def refuse_hss_next_stage(next_stage_image: Path) -> None:
    """Refuse wrapping the E51 HSS firmware in ``payload.bin`` (DS-P2.145/147).

    ``payload.bin`` is the U54 NEXT STAGE HSS loads (S-mode U-Boot at
    ``0x80200000`` / ``prv_s`` / ``u54_1``). Wrapping ``hss-lim.elf`` (or any
    other HSS firmware image) is the category error that costs a round.

    The name regex is not enough: renaming the blob to ``u-boot.bin`` must
    still refuse when the bytes are HSS firmware (DS-P2.147 landing refusal).
    """
    name = next_stage_image.name
    blob = next_stage_image.read_bytes() if next_stage_image.is_file() else b""
    named_hss = bool(_HSS_FIRMWARE_NAME.search(name))
    content_hss = any(marker in blob for marker in _HSS_FIRMWARE_CONTENT)
    if named_hss or content_hss:
        raise SystemExit(
            "payload.bin must not wrap the HSS firmware ELF "
            f"({name}); wrap S-mode U-Boot (exec-addr 0x80200000, "
            "priv-mode prv_s, owner-hart u54_1)."
        )


def generate_payload(
    generator: Path,
    next_stage_image: Path,
    payload_out: Path,
    *,
    exec_addr: str | None = None,
    priv_mode: str | None = None,
    owner_hart: str | None = None,
) -> None:
    """Emit an HSS ``payload.bin`` that HSS loads for the U54s from ``next_stage_image``.

    ``next_stage_image`` is the U54 next stage (U-Boot / U54 app), NOT the HSS
    firmware itself (see :func:`build_payload_config`).
    """
    if not next_stage_image.is_file():
        raise SystemExit(
            f"next-stage payload image not found: {next_stage_image}. payload.bin wraps the "
            "U54 next stage (U-Boot), not the HSS firmware — build/point at the U-Boot image."
        )
    refuse_hss_next_stage(next_stage_image)
    cfg = payload_out.parent / "ds_p2_145_payload.yaml"
    cfg.write_text(
        build_payload_config(
            next_stage_image,
            exec_addr=exec_addr,
            priv_mode=priv_mode,
            owner_hart=owner_hart,
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [str(generator), "-c", str(cfg), str(payload_out)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not payload_out.is_file():
        sys.stderr.write(proc.stdout + "\n" + proc.stderr + "\n")
        raise SystemExit(f"hss-payload-generator failed (exit {proc.returncode}).")
    produced = payload_out.read_bytes()
    source = next_stage_image.read_bytes()
    if produced == source:
        raise SystemExit(
            "payload.bin must not wrap the HSS: generator emitted a raw copy of "
            "the next-stage bytes (missing HSS payload magic 0xb007c0de)."
        )
    if not produced.startswith(HSS_PAYLOAD_MAGIC):
        raise SystemExit(
            "payload.bin is not an HSS payload (missing magic 0xb007c0de)."
        )


def apply_prebuild(hss_dir: Path) -> dict:
    """Apply the overlays that MUST land BEFORE `rr linux hss` compiles the clone.

    Two of the firmware-leg fixes have to be in place before the build, because
    the build BAKES them into objects/headers that later steps cannot patch:

      * the MMUART4 console overlay (``disco_uart0_console.apply``) edits
        ``uart_device_list.c`` / ``def_config`` — the console selection is
        compiled into ``uart_device_list.o`` (retargeted to MMUART_4 /
        pUartDeviceList[4] / CONFIG_UART_POST_BOOT=4, the reference console on MSSIO_B2);
      * the authoritative-MSS-XML overlay (``disco_iomux_config.apply``) repoints
        ``CONFIG_SOC_FPGA_DESIGN_XML`` at ``disco_mss_mss_cfg.xml`` so the
        configuration-generator emits an ``hw_mssio_mux.h`` that routes
        ``MMUART_4 -> MSSIO_B2`` (the reference console pads, DS-P3.160).

    DS-P3.160: The factory IOMUX register overlay is NOT applied (it overrides the
    reference XML with legacy values that send MMUART0 to fabric / tristate lines).
    Instead, any prior factory override block in ``mss_sw_config.h`` is reverted.

    Run this against the (gitignored) HSS clone right after ``rr linux hss``
    checks it out but BEFORE it builds (or before a ``--refresh`` rebuild).
    Idempotent.
    """
    hss_dir = hss_dir.resolve()
    uart_mod = _load(_HSS_TOOLS / "disco_uart0_console.py", "ds_p2_145_uart")
    iomux_mod = _load(_HSS_TOOLS / "disco_iomux_config.py", "ds_p2_145_iomux")
    factory_mod = _load(
        _HSS_TOOLS / "disco_console_register_overlay.py", "ds_p2_145_factory_iomux"
    )
    console_changed = uart_mod.apply(hss_dir)
    iomux_changed = iomux_mod.apply(hss_dir)
    factory_reverted = factory_mod.revert(hss_dir)
    return {
        "console_overlay_changed": console_changed,
        "iomux_xml_overlay_changed": iomux_changed,
        "factory_iomux_overlay_changed": False,
        "factory_iomux_overlay_reverted": factory_reverted,
    }


def stage(
    hss_dir: Path,
    out_elf: Path,
    cross: str = "riscv64-unknown-elf-",
    payload_out: Path | None = None,
    next_stage_image: Path | None = None,
) -> dict:
    """Run the full firmware-leg wiring; return a summary dict of what was done.

    ``out_elf`` is the L2-LIM-linked HSS (E51 firmware). ``payload_out`` (optional)
    is the HSS ``payload.bin`` — the U54 NEXT-STAGE image HSS loads, which requires
    a ``next_stage_image`` (U-Boot); it is NOT the HSS ELF (round-3 finding #2).

    This is the POST-build step (console overlay is re-applied idempotently for
    safety, but the IOMUX/console source fixes must already have been applied via
    :func:`apply_prebuild` BEFORE the build — see that function's docstring).
    """
    hss_dir = hss_dir.resolve()
    out_elf = out_elf.resolve()
    link_mod = _load(_HSS_TOOLS / "link_hss_l2lim.py", "ds_p2_145_link")
    uart_mod = _load(_HSS_TOOLS / "disco_uart0_console.py", "ds_p2_145_uart")

    console_changed = uart_mod.apply(hss_dir)
    out_elf.parent.mkdir(parents=True, exist_ok=True)
    link_mod.relink(hss_dir, cross, out_elf)
    staged = link_mod.stage_lim_as_transient(hss_dir, out_elf)

    summary = {
        "console_overlay_changed": console_changed,
        "lim_elf": str(out_elf),
        "staged_transient": str(staged),
        "payload": None,
    }
    if payload_out is not None:
        if next_stage_image is None:
            raise SystemExit(
                "--payload-out requires --next-stage <u-boot.bin>: payload.bin wraps the "
                "U54 NEXT-STAGE image HSS loads, NOT the HSS firmware itself (DS-P2.145 "
                "round-3 finding #2). Point --next-stage at the built U-Boot image."
            )
        generator = find_payload_generator(hss_dir)
        if generator is None:
            raise SystemExit(
                "hss-payload-generator not found (build it: "
                "make -C <hss_dir>/tools/hss-payload-generator). "
                "libyaml-dev is required and is installed (0.2.2)."
            )
        generate_payload(generator, next_stage_image.resolve(), payload_out.resolve())
        summary["payload"] = str(payload_out.resolve())
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hss-dir", required=True, type=Path)
    ap.add_argument(
        "--prebuild",
        action="store_true",
        help="Apply ONLY the pre-build overlays (MMUART4 console source + authoritative "
        "MSS design XML so the config-generator emits the reference console pad mux, "
        "MMUART_4 -> MSSIO_B2). "
        "Run this against the freshly-checked-out clone BEFORE `rr linux hss` builds it. "
        "In this mode --out is ignored.",
    )
    ap.add_argument("--out", required=False, type=Path, help="LIM-linked ELF output path (not needed with --prebuild).")
    ap.add_argument("--cross", default="riscv64-unknown-elf-")
    ap.add_argument(
        "--payload-out",
        type=Path,
        default=None,
        help="Also emit the HSS payload.bin here (requires --next-stage).",
    )
    ap.add_argument(
        "--next-stage",
        type=Path,
        default=None,
        help="U54 next-stage image (U-Boot) that HSS loads into payload.bin. "
        "This is NOT the HSS firmware — HSS is the E51 first stage and is not "
        "wrapped in payload.bin.",
    )
    args = ap.parse_args(argv)
    if args.prebuild:
        summary = apply_prebuild(args.hss_dir)
        for k, v in summary.items():
            print(f"{k}: {v}")
        return 0
    if args.out is None:
        ap.error("--out is required unless --prebuild is given")
    summary = stage(args.hss_dir, args.out, args.cross, args.payload_out, args.next_stage)
    for k, v in summary.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
