#!/usr/bin/env python3
"""Compose the PolarFire SoC deployment DTB — bake ds_ip into the image.

PF-L0 (DS-P2.125). The land refusal named a real gap: ``tools/check_bench_ip.py
--target pfsoc`` checks ``build/pfsoc_datalogger/linux/system.dtb`` for the
declared ``ds_ip=`` token, but NOTHING in-tree composed that token into a
PolarFire artifact — a detector nobody could call.

This tool is the caller. It is the PolarFire analogue of the DE25 builder's
``rr linux dtb --bootargs`` step (``tools/de25_linux_image.py``), done in-tree
because rr's Microchip Linux backend is still a stub (port plan §8.2), so the
DE25 path's ``rr linux dtb`` writer is not available for PolarFire yet. It:

  1. resolves the deployment bootargs through the SAME resolver the DE25 builder
     and the checker use (``de25_linux_image.deployment_bootargs`` with the
     pfsoc target/bench pair) — so the token baked here is exactly the token the
     checker demands, from one owner of "what is a deployable descriptor";
  2. patches ``/chosen/bootargs`` of the MSS/HSS-produced base DTB with it and
     writes the result to the checker's default path.

The base DTB is the ``system.dtb`` the Microchip HSS/U-Boot flow emits at PF-B1
(``--base-dtb``); until that backend materialises the base is not producible in
this worktree, and this tool fails closed with that exact diagnosis rather than
writing a DTB with no board nodes. The ``/chosen/bootargs`` writer itself is
self-contained (no ``dtc``/``libfdt``) and round-trip tested against the FDT
parser in ``de25_linux_image`` (``tools/tests/test_pfsoc_linux_image_p2_125.py``).

    tools/pfsoc_linux_image.py compose --base-dtb <hss_system.dtb>
    tools/pfsoc_linux_image.py compose --base-dtb b.dtb --output /tmp/out.dtb
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import struct
import sys


ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "de25_linux_image", Path(__file__).with_name("de25_linux_image.py")
)
de25_linux_image = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(de25_linux_image)

PFSOC_TARGET_REL = "targets/pfsoc_datalogger.yml"
PFSOC_BENCH_TARGET = "pfsoc_bench"
#: Where the composed DTB lands — kept identical to
#: tools/check_bench_ip.py BOARDS["pfsoc"]["default_dtb"], so the producer and
#: the detector name the same file.
DEFAULT_OUTPUT_REL = "build/pfsoc_datalogger/linux/system.dtb"

_FDT_MAGIC = 0xD00DFEED
_FDT_BEGIN_NODE = 0x1
_FDT_END_NODE = 0x2
_FDT_PROP = 0x3
_FDT_NOP = 0x4
_FDT_END = 0x9


def _pad4(b: bytes) -> bytes:
    return b + b"\x00" * (-len(b) % 4)


def set_chosen_bootargs(blob: bytes, bootargs: str) -> bytes:
    """Return ``blob`` with ``/chosen/bootargs`` set to ``bootargs``.

    Replaces the property if present, adds it (as the first property of an
    existing ``/chosen``) if the node exists without it, and creates
    ``/chosen`` under the root if it is absent. Pure FDT surgery — no external
    tools. Raises ``ValueError`` on a malformed blob.
    """
    if len(blob) < 40:
        raise ValueError(f"blob too short for an FDT header ({len(blob)} bytes)")
    (magic, totalsize, off_struct, off_strings, off_rsvmap, version,
     _last_comp, _boot_cpuid, size_strings, size_struct) = struct.unpack_from(
        ">10I", blob, 0)
    if magic != _FDT_MAGIC:
        raise ValueError(f"bad FDT magic: 0x{magic:08x}")
    if totalsize > len(blob):
        raise ValueError(f"FDT totalsize {totalsize} exceeds blob {len(blob)}")

    strings = bytearray(blob[off_strings:off_strings + size_strings])

    def stroff(name: str) -> int:
        needle = name.encode("ascii") + b"\x00"
        idx = bytes(strings).find(needle)
        # Must be a whole entry, i.e. preceded by NUL or at start.
        while idx != -1 and idx != 0 and strings[idx - 1] != 0:
            idx = bytes(strings).find(needle, idx + 1)
        if idx != -1:
            return idx
        idx = len(strings)
        strings.extend(needle)
        return idx

    value = _pad4(bootargs.encode("utf-8") + b"\x00")

    def prop_bootargs() -> bytes:
        return struct.pack(">III", _FDT_PROP, len(bootargs.encode("utf-8")) + 1,
                           stroff("bootargs")) + value

    out = bytearray()
    off = off_struct
    end = off_struct + size_struct
    path: list[str] = []           # non-empty node names, root excluded
    depth = 0                       # includes the (empty-named) root
    in_chosen = False
    chosen_seen = False
    written = False

    while off < end:
        (tag,) = struct.unpack_from(">I", blob, off)
        tok = off
        off += 4
        if tag == _FDT_BEGIN_NODE:
            nul = blob.index(b"\x00", off)
            name = blob[off:nul].decode("utf-8", "replace")
            new_off = (nul + 1 + 3) & ~3
            out += blob[tok:new_off]           # BEGIN_NODE + padded name
            off = new_off
            depth += 1
            if name:
                path.append(name)
            if path == ["chosen"]:
                in_chosen = True
                chosen_seen = True
                out += prop_bootargs()          # bootargs as first property
                written = True
        elif tag == _FDT_PROP:
            length, _nameoff = struct.unpack_from(">II", blob, off)
            name_end = blob.index(b"\x00", off_strings + _nameoff)
            name = blob[off_strings + _nameoff:name_end].decode("ascii")
            prop_end = (off + 8 + length + 3) & ~3
            if in_chosen and name == "bootargs":
                pass                            # drop the old value
            else:
                out += blob[tok:prop_end]
            off = prop_end
        elif tag == _FDT_END_NODE:
            if depth == 1 and not chosen_seen:
                # closing the root and no /chosen exists — create it
                out += struct.pack(">I", _FDT_BEGIN_NODE) + _pad4(b"chosen\x00")
                out += prop_bootargs()
                out += struct.pack(">I", _FDT_END_NODE)
                chosen_seen = written = True
            out += blob[tok:off]
            if path:
                # this END closes the deepest named node
                if path == ["chosen"]:
                    in_chosen = False
                path.pop()
            depth -= 1
        elif tag == _FDT_NOP:
            out += blob[tok:off]
        elif tag == _FDT_END:
            out += blob[tok:off]
            break
        else:
            raise ValueError(f"unrecognised FDT tag 0x{tag:08x}")

    if not written:
        raise ValueError("could not place /chosen/bootargs")

    new_struct = bytes(out)
    rsv = blob[off_rsvmap:off_struct]
    new_off_struct = 40 + len(rsv)
    new_off_strings = new_off_struct + len(new_struct)
    total = new_off_strings + len(strings)
    header = struct.pack(
        ">10I", _FDT_MAGIC, total, new_off_struct, new_off_strings, off_rsvmap,
        version, 16, _boot_cpuid, len(strings), len(new_struct),
    )
    return header + rsv + new_struct + bytes(strings)


def compose(root: Path, base_dtb: Path, output: Path) -> str:
    """Bake the pfsoc deployment ds_ip into ``base_dtb`` -> ``output``.

    Returns the composed bootargs. Raises ``RuntimeError`` if the base DTB is
    absent (the Microchip backend has not produced it yet) or malformed.
    """
    if not base_dtb.is_file():
        raise RuntimeError(
            f"base PolarFire DTB missing: {base_dtb}. It is the system.dtb the "
            "Microchip HSS/U-Boot flow emits at PF-B1; rr's Microchip Linux "
            "backend is still a stub (port plan §8.2), so it is not producible "
            "in this worktree. Pass --base-dtb once the backend materialises."
        )
    bootargs = de25_linux_image.deployment_bootargs(
        root, target_rel=PFSOC_TARGET_REL, bench_target=PFSOC_BENCH_TARGET)
    try:
        patched = set_chosen_bootargs(base_dtb.read_bytes(), bootargs)
    except ValueError as exc:
        raise RuntimeError(f"{base_dtb}: {exc}") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.write_bytes(patched)
    tmp.replace(output)
    return bootargs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("compose",
                       help="bake the deployment ds_ip into the pfsoc DTB")
    c.add_argument("--root", type=Path, default=ROOT)
    c.add_argument("--base-dtb", type=Path, required=True,
                   help="the MSS/HSS-produced base system.dtb")
    c.add_argument("--output", type=Path, default=None,
                   help=f"output DTB (default: {DEFAULT_OUTPUT_REL})")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    output = args.output if args.output is not None else root / DEFAULT_OUTPUT_REL
    try:
        bootargs = compose(root, args.base_dtb, output)
    except RuntimeError as exc:
        print(f"REFUSED: {exc}")
        return 1
    print(f"composed {output} with bootargs: {bootargs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
