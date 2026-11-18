"""Build a flattened-devicetree blob that REALISES a target's ``dt_nodes``.

Shared by every test that drives ``tools/de25_linux_image.py``'s build with
``subprocess.run`` mocked out.  In such a test ``rr linux dtb`` never runs, so
nothing produces the board DTB — but DS-P3.154's guard reads that artifact back
and refuses a build whose dtb does not carry the declared nodes.  A placeholder
text file is not a dtb, and weakening the guard to accept one would give back
exactly the blind spot it exists to close.

So the fixtures stage a REAL blob instead, generated from the same
``targets/de25_datalogger.yml`` the guard reads.  It models the two properties the
guard checks (``status`` and ``reg``); everything else in a node is irrelevant
to it and is not emitted.
"""
from __future__ import annotations

from pathlib import Path
import struct
from typing import Mapping

import yaml

_FDT_MAGIC = 0xD00DFEED


def encode_cells(value) -> bytes | None:
    """u32-cell encoding of a ``reg`` declaration, or None if unmodelled."""
    cells = [value] if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(cells, list) or not cells:
        return None
    if not all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c < 2**32
               for c in cells):
        return None
    return b"".join(struct.pack(">I", c) for c in cells)


def build_fdt(nodes: Mapping[str, Mapping[str, bytes]]) -> bytes:
    """A well-formed FDT carrying ``{"/soc@0/can@20010000": {"reg": b"..."}}``."""
    strings = bytearray()
    seen: dict[str, int] = {}

    def stroff(name: str) -> int:
        if name not in seen:
            seen[name] = len(strings)
            strings.extend(name.encode("ascii") + b"\x00")
        return seen[name]

    def pad(blob: bytes) -> bytes:
        return blob + b"\x00" * (-len(blob) % 4)

    tree: dict = {}
    for path, props in nodes.items():
        cursor = tree
        parts = [p for p in path.strip("/").split("/") if p]
        for i, part in enumerate(parts):
            node = cursor.setdefault(part, {"props": {}, "kids": {}})
            if i == len(parts) - 1:
                node["props"].update(props)
            cursor = node["kids"]

    body = bytearray()

    def emit(name: str, node: dict) -> None:
        body.extend(struct.pack(">I", 1) + pad(name.encode("ascii") + b"\x00"))
        for prop, value in node["props"].items():
            body.extend(struct.pack(">III", 3, len(value), stroff(prop)))
            body.extend(pad(value))
        for kid, sub in node["kids"].items():
            emit(kid, sub)
        body.extend(struct.pack(">I", 2))

    body.extend(struct.pack(">I", 1) + pad(b"\x00"))       # root
    for kid, sub in tree.items():
        emit(kid, sub)
    body.extend(struct.pack(">I", 2))                       # end root
    body.extend(struct.pack(">I", 9))                       # FDT_END

    off_rsvmap = 40
    off_struct = off_rsvmap + 16
    off_strings = off_struct + len(body)
    header = struct.pack(">10I", _FDT_MAGIC, off_strings + len(strings),
                         off_struct, off_strings, off_rsvmap,
                         17, 16, 0, len(strings), len(body))
    return header + b"\x00" * 16 + bytes(body) + bytes(strings)


def dtb_realising(target_yml: Path) -> bytes:
    """An FDT that satisfies every ``linux.dt_nodes`` entry of ``target_yml``."""
    target = yaml.safe_load(target_yml.read_text(encoding="utf-8")) or {}
    nodes: dict[str, dict[str, bytes]] = {}
    for node in ((target.get("linux") or {}).get("dt_nodes") or []):
        if not isinstance(node, dict) or not node.get("path"):
            continue
        props = node.get("properties") or {}
        emitted: dict[str, bytes] = {}
        if "status" in props:
            emitted["status"] = str(props["status"]).encode("ascii") + b"\x00"
        if "reg" in props:
            cells = encode_cells(props["reg"])
            if cells is not None:
                emitted["reg"] = cells
        nodes[str(node["path"])] = emitted
    return build_fdt(nodes)
