"""DS-P2.271: the pfsoc device tree describes only the deepskopion fabric.

The PolarFire base tree that ``rr linux dtb`` patches is the linux4microchip
``mpfs-disco-kit.dtb`` (tag linux4microchip+fpga-2025.03.1, sha256
e6aa2c4463241713cef8045adf082002a0609c9b669caf6aa03026d0b1a2ffbf, nodes from
``mpfs-disco-kit-fabric.dtsi``). Under ``/fabric-bus@40000000`` (2/2 cells,
ranges 1:1) it declares the Microchip reference design's fabric peripherals,
all ``status = "okay"``. The deepskopion PolarFire fabric carries none of them:
FIC0 serves only the regbank CSR at 0x6000_0000
(``sw/linux/pfsoc/system_memory_map.yml`` system_regs) and FIC3's APB initiator
has only its clock connected (``hw/libero/pfsoc/pfsoc_mss_subsys.tcl``).

INPUT DOMAIN: the committed ``linux.dt_nodes`` of ``targets/pfsoc_datalogger.yml``
over the pinned base nodes below (read from the decompiled base DTB on
2026-11-11; a scan of that DTB found exactly one node overlapping the CSR
window, ``uio@60000000``). OUTPUT CONTRACT: every reference-fabric node is
disabled at its base path, without ``create``; and the only enabled node whose
reg overlaps the CSR window 0x6000_0000..0x6000_FFFF is
``/fabric-uio@0/ring-watermark@60000000``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"
MEMMAP = ROOT / "sw" / "linux" / "pfsoc" / "system_memory_map.yml"

_cells_spec = importlib.util.spec_from_file_location(
    "pfsoc_fabric_uio_cells",
    Path(__file__).with_name("test_pfsoc_fabric_uio_cells_ds_p2_270.py"))
_cells = importlib.util.module_from_spec(_cells_spec)
_cells_spec.loader.exec_module(_cells)

#: Base-tree reference-fabric nodes: path -> (CPU base, size, base status).
#: /fabric-bus@40000000 ranges map 0x4000_0000 and 0x6000_0000 1:1.
REFERENCE_FABRIC = {
    "/fabric-bus@40000000/pwm@40000000": (0x40000000, 0xF0, "okay"),
    "/fabric-bus@40000000/i2c@40000200": (0x40000200, 0x100, "okay"),
    "/fabric-bus@40000000/dma-controller@60010000": (0x60010000, 0x1000, "okay"),
    "/fabric-bus@40000000/uio@60000000": (0x60000000, 0x1000, "okay"),
}
CSR_NODE = "/fabric-uio@0/ring-watermark@60000000"


def _dt_nodes() -> list[dict]:
    return yaml.safe_load(TARGET.read_text(encoding="utf-8"))["linux"]["dt_nodes"]


def _csr_window() -> tuple[int, int]:
    memmap = yaml.safe_load(MEMMAP.read_text(encoding="utf-8"))
    csr = next(d for d in memmap["devices"] if d["name"] == "system_regs")
    return int(str(csr["base_addr"]), 0), int(str(csr["span"]), 0)


def _created_nodes(entries: list[dict]) -> dict[str, tuple[int, int, str]]:
    """CPU window of each created node with a reg, resolved through its parent."""
    by_path = {e["path"]: e for e in entries}
    out = {}
    for e in entries:
        reg = e.get("properties", {}).get("reg")
        if not e.get("create") or reg is None:
            continue
        parent_path = e["path"].rsplit("/", 1)[0]
        parent = by_path.get(parent_path, {}).get("properties", {})
        if parent_path and "ranges" in parent:
            pcells = _cells._cells(parent)
            (addr, size), = _cells.decode_reg(reg, *pcells)
            cpu = _cells.translate(addr, size, parent["ranges"], pcells,
                                   _cells.BASE_ROOT_CELLS[0])
        else:
            (cpu, size), = _cells.decode_reg(reg, *_cells.BASE_ROOT_CELLS)
        out[e["path"]] = (cpu, size, str(e["properties"].get("status", "okay")))
    return out


def enabled_overlapping(entries: list[dict], window: tuple[int, int]) -> set[str]:
    """Nodes left enabled whose CPU window overlaps ``window``."""
    lo, span = window
    overrides = {e["path"]: e.get("properties", {}) for e in entries if not e.get("create")}
    nodes = dict(REFERENCE_FABRIC)
    for path, (base, size, status) in REFERENCE_FABRIC.items():
        nodes[path] = (base, size, str(overrides.get(path, {}).get("status", status)))
    nodes.update(_created_nodes(entries))
    return {path for path, (base, size, status) in nodes.items()
            if status in ("okay", "ok") and base < lo + span and lo < base + size}


def test_each_reference_node_is_disabled_at_its_base_path_without_create():
    entries = {e["path"]: e for e in _dt_nodes()}
    for path in REFERENCE_FABRIC:
        assert path in entries, f"{path}: not overridden in linux.dt_nodes"
        entry = entries[path]
        assert not entry.get("create"), f"{path}: names a base node, must not create"
        assert entry.get("properties") == {"status": "disabled"}, (path, entry)


def test_only_the_csr_node_is_enabled_inside_the_csr_window():
    assert enabled_overlapping(_dt_nodes(), _csr_window()) == {CSR_NODE}


def test_the_overlap_check_sees_the_reference_lsram_without_the_overrides():
    """Positive control: the unpatched base leaves uio@60000000 enabled in the window."""
    kept = [e for e in _dt_nodes() if e["path"] not in REFERENCE_FABRIC]
    assert enabled_overlapping(kept, _csr_window()) == {
        CSR_NODE, "/fabric-bus@40000000/uio@60000000"}
