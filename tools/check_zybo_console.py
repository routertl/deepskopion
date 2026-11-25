#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-P1.105: refuse a Zybo boot image whose console is not the board's USB UART.

The Zybo-Z7 USB console is the PS UART1 at 0xE0001000 (MIO, through the
FTDI). deepskopion also enables PS UART0 at 0xE0000000 on EMIO, into the
fabric. Which of the two a boot stage prints on is decided by the device
tree, through the ``serialN`` aliases, and the alias order is decided by the
device-tree generator, not by this project.

Measured 2026-11-14: the BOOT.bin built at 3d925cc6 carries a generated
device tree with ``serial0 = UART1`` and ``serial1 = UART0``, the reverse of
the order the old generator wrote. The target bootargs said
``console=ttyPS1``, so the kernel console and U-Boot's ``stdout-path``
(reconciled to the same alias) both resolved to the EMIO UART0. The FSBL
printed on the USB UART, handed off, and the console went silent.

This check reads the two device trees the boot chain uses, as built:

* ``system.dtb``: the kernel device tree (``/chosen/bootargs``,
  ``/chosen/stdout-path``, ``/aliases``);
* ``uboot_control.dtb``: the device tree embedded in U-Boot
  (``CONFIG_OF_EMBED``), whose ``stdout-path`` is U-Boot's console.

Judged where each stage reads its console: the kernel's ``console=ttyPS<N>``
(through ``serial<N>``) and ``earlycon`` MMIO address (``stdout-path`` only
when ``console=`` is absent), and U-Boot's control-dt ``stdout-path``. Each
must resolve to the expected UART base. Anything else is refused with every
reason listed. The booting 5a6a42a8 chain is accepted and the 3d925cc6 chain
refused. It reads the blobs
directly (no ``dtc``) with the same FDT walker the DE25 image builder uses.

Run it after ``rr linux sdcard`` and before the BOOT.bin leaves the host:

    python3 tools/check_zybo_console.py --target zybo_datalogger

Exit codes: 0 accepted, 1 refused, 2 an input is missing or not an FDT.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "de25_linux_image", Path(__file__).with_name("de25_linux_image.py")
)
de25_linux_image = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(de25_linux_image)

#: Zynq-7000 PS UART1, the Zybo-Z7 USB console (MIO 48/49 through the FTDI).
ZYBO_USB_UART_BASE = 0xE0001000

#: Where ``rr linux`` writes the built device trees of a target.
DEFAULT_OUT = "sw/linux/out/{target}"


def _cstr(value: bytes) -> str:
    return value.split(b"\x00", 1)[0].decode("utf-8", "replace")


def read_tree(blob: bytes) -> dict[str, dict[str, bytes]]:
    """``{node_path: {prop: value}}`` for every node; paths without the
    leading slash, as the FDT walker yields them."""
    tree: dict[str, dict[str, bytes]] = {}
    for node, name, value in de25_linux_image._iter_fdt_props(blob):
        props = tree.setdefault(node, {})
        if name is not None:
            props[name] = value
    return tree


def _node_base(tree: dict[str, dict[str, bytes]], path: str) -> int | None:
    """MMIO base of the node at ``path``: its first ``reg`` cell, else the
    unit address in its name."""
    node = path.strip("/")
    reg = tree.get(node, {}).get("reg")
    if reg is not None and len(reg) >= 4:
        return int.from_bytes(reg[:4], "big")
    unit = re.search(r"@([0-9a-fA-F]+)$", node)
    return int(unit.group(1), 16) if unit else None


def _alias_base(tree: dict[str, dict[str, bytes]], alias: str) -> tuple[int | None, str]:
    """Resolve ``serialN`` (or an absolute node path) to a base address."""
    if alias.startswith("/"):
        return _node_base(tree, alias), alias
    target = tree.get("aliases", {}).get(alias)
    if target is None:
        return None, f"{alias} (no such alias)"
    path = _cstr(target)
    return _node_base(tree, path), f"{alias} -> {path}"


def stdout_findings(tree: dict[str, dict[str, bytes]], expected: int,
                    label: str) -> list[str]:
    """``/chosen/stdout-path`` must resolve to ``expected``."""
    stdout = tree.get("chosen", {}).get("stdout-path")
    if stdout is None:
        return [f"{label}: no /chosen/stdout-path"]
    ref = _cstr(stdout).split(":", 1)[0]
    base, how = _alias_base(tree, ref)
    if base != expected:
        return [f"{label}: stdout-path {_cstr(stdout)!r} resolves to "
                f"{_fmt(base)} via {how}, not {_fmt(expected)}"]
    return []


def kernel_findings(tree: dict[str, dict[str, bytes]], expected: int,
                    label: str) -> list[str]:
    """The kernel console: ``console=ttyPS<N>`` through ``serial<N>`` and the
    ``earlycon`` address must both be ``expected``. The kernel falls back to
    ``stdout-path`` only without ``console=`` (and bare ``earlycon`` only
    without an address), so ``stdout-path`` is judged only in that case."""
    raw = tree.get("chosen", {}).get("bootargs")
    if raw is None:
        return [f"{label}: no /chosen/bootargs"]
    bootargs = _cstr(raw)
    problems: list[str] = []

    tty = re.search(r"\bconsole=ttyPS(\d+)", bootargs)
    if tty is None:
        problems.append(f"{label}: bootargs name no console=ttyPS<N>")
        problems += stdout_findings(tree, expected, label)
    else:
        base, how = _alias_base(tree, f"serial{tty.group(1)}")
        if base != expected:
            problems.append(
                f"{label}: console=ttyPS{tty.group(1)} resolves to {_fmt(base)} "
                f"via {how}, not {_fmt(expected)}")

    early = re.search(r"\bearlycon=[^\s,]+,mmio32,(0x[0-9a-fA-F]+)", bootargs)
    if early is None:
        problems.append(f"{label}: bootargs carry no earlycon=<drv>,mmio32,<base>")
    elif int(early.group(1), 16) != expected:
        problems.append(
            f"{label}: earlycon base {early.group(1)} is not {_fmt(expected)}")
    return problems


def _fmt(base: int | None) -> str:
    return "nothing" if base is None else f"0x{base:08X}"


def check(system_dtb: Path, uboot_dtb: Path,
          expected: int = ZYBO_USB_UART_BASE) -> list[str]:
    """All console problems across both device trees (empty = accepted).

    Raises ``FileNotFoundError`` / ``ValueError`` for a missing or malformed
    input, so an absent artifact is never read as a clean one."""
    kernel = read_tree(system_dtb.read_bytes())
    uboot = read_tree(uboot_dtb.read_bytes())
    # U-Boot (CONFIG_OF_EMBED) prints on its control dt's stdout-path; it does
    # not boot with that dt's /chosen/bootargs, so only stdout-path is judged.
    return (kernel_findings(kernel, expected, "system.dtb")
            + stdout_findings(uboot, expected, "uboot_control.dtb"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--target", default="zybo_datalogger",
                        help="rr target whose built device trees to read")
    parser.add_argument("--system-dtb", type=Path, default=None)
    parser.add_argument("--uboot-dtb", type=Path, default=None)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    out = args.root / DEFAULT_OUT.format(target=args.target)
    system_dtb = args.system_dtb or out / "system.dtb"
    uboot_dtb = args.uboot_dtb or out / "uboot_control.dtb"
    try:
        problems = check(system_dtb, uboot_dtb)
    except (FileNotFoundError, ValueError) as exc:
        print(f"console check: cannot read the device trees: {exc}", file=sys.stderr)
        return 2
    if problems:
        print(f"console check: REFUSED, the console is not the USB UART "
              f"{_fmt(ZYBO_USB_UART_BASE)}:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"console check: accepted, every stage of {system_dtb.name} and "
          f"{uboot_dtb.name} resolves to {_fmt(ZYBO_USB_UART_BASE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
