#!/usr/bin/env python3
"""Retarget the HSS Discovery-Kit E51 debug console from MMUART1 to MMUART4.

WHY THIS EXISTS (DS-P2.145 / DS-P3.160)
---------------------------------------
The upstream Hart Software Services board port for ``mpfs-disco-kit`` routes the
**E51 boot/debug console to MMUART1**:

  * ``boards/mpfs-disco-kit/uart_device_list.c`` returns ``pUartDeviceList[1]``
    (== ``&g_mss_uart1_lo``, i.e. MMUART1) for ``HSS_HART_E51`` in BOTH the
    pre-boot and (via ``CONFIG_UART_POST_BOOT=1``) post-boot paths.

In the authoritative Libero MSS design for this kit (``hw/libero/pfsoc/mss/disco_mss.cxz``,
rebased in DS-P1.60 on the reference MSS design), MMUART1 and MMUART0 are routed
to FABRIC, while the physical console on MSSIO_B2 is **MMUART_4**:

  * ``MMUART_4  ->  MSSIO_B2``     (the physical MSSIO pins the on-board embedded
    FlashPro5 UART bridge reaches — serial@20106000 = ttyS0)
  * ``MMUART_1  ->  FABRIC``       (routed into the fabric, NOT to a physical
    console pin)
  * ``MMUART_0  ->  FABRIC``       (routed into the fabric)

So stock HSS writes its banner to MMUART1 -> fabric -> nowhere reachable, and the
E51 console is SILENT on the bench even though HSS is running.

This overlay corrects the HSS disco-kit board port to match the reference design:
the E51 debug console uses **MMUART4** (``pUartDeviceList[4]``), and
``CONFIG_UART_POST_BOOT`` is set to ``4`` so the post-boot console is MMUART4 too.

Applied by the build helper against the (gitignored) HSS clone after checkout, so
it survives a fresh clone without patching the shared SDK or the upstream repo in
place. Idempotent.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path


def patch_uart_device_list(path: Path) -> bool:
    """Rewrite the E51 (and default) console selection to MMUART4. Returns True if changed."""
    text = path.read_text(encoding="utf-8")
    original = text

    # E51 pre-boot branch: `pUart = pUartDeviceList[1];` inside the E51 case's else.
    # default branch also selects [1]. Retarget both non-post-boot selections that
    # feed the E51/default console to index 4 (MMUART4). We do this narrowly by
    # replacing the two `pUartDeviceList[1]` selections that are the console
    # (default + E51 pre-boot), leaving the U54 mappings (indices 4/0/3) intact.
    # Also handle idempotent re-application if already patched from [0] to [4].
    target_repl = (
        "pUart = pUartDeviceList[4]; /* DS-P3.160: MMUART_4 is the physical console (MSSIO_B2) */"
    )
    text = text.replace("pUart = pUartDeviceList[1];", target_repl)
    text = text.replace(
        "pUart = pUartDeviceList[0]; /* DS-P2.145: MMUART0 is the physical console (MSSIO_B2_B) */",
        target_repl,
    )
    text = text.replace(
        "p = pUartDeviceList[1];",
        "p = pUartDeviceList[4]; /* DS-P3.160: MMUART_4 is the physical console (MSSIO_B2) */",
    )

    changed = text != original
    if changed:
        path.write_text(text, encoding="utf-8")
    return changed


def patch_post_boot_config(def_config: Path, dot_config: Path | None) -> bool:
    """Force CONFIG_UART_POST_BOOT=4 (MMUART4) in def_config and any generated .config."""
    changed = False
    for cfg in (def_config, dot_config):
        if cfg is None or not cfg.is_file():
            continue
        text = cfg.read_text(encoding="utf-8")
        new = re.sub(
            r"^CONFIG_UART_POST_BOOT=.*$",
            "CONFIG_UART_POST_BOOT=4",
            text,
            flags=re.MULTILINE,
        )
        if new != text:
            cfg.write_text(new, encoding="utf-8")
            changed = True
    return changed


def apply(hss_dir: Path) -> bool:
    board = hss_dir / "boards" / "mpfs-disco-kit"
    udl = board / "uart_device_list.c"
    if not udl.is_file():
        raise SystemExit(
            f"HSS disco-kit uart_device_list.c not found: {udl} (clone HSS first)."
        )
    c1 = patch_uart_device_list(udl)
    c2 = patch_post_boot_config(board / "def_config", hss_dir / ".config")
    return c1 or c2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hss-dir", required=True, type=Path)
    args = ap.parse_args(argv)
    changed = apply(args.hss_dir.resolve())
    print("OK: console retargeted to MMUART4" + ("" if changed else " (already applied)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
