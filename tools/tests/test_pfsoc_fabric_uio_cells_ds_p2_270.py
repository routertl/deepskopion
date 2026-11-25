# rr unknown-llm (AI-generated)
"""DS-P2.270: the pfsoc CSR UIO node resolves to 0x60000000+0x10000 on the CPU bus.

``rr linux dtb`` (routertl e74ec1b33, RTL-P2.2279) refuses a ``linux.dt_nodes``
path that the base tree lacks unless the entry says ``create: true``, and it
creates one node per entry, never an intermediate. The PolarFire base tree
(linux4microchip mpfs-disco-kit.dtb) has a 2/2-cell root and no node at
``/fabric-uio@0``, so ``targets/pfsoc_datalogger.yml`` creates the container
``/fabric-uio@0`` (simple-bus, 1/1 cells) before its child
``ring-watermark@60000000``.

The container must make the child's ``reg = <0x60000000 0x10000>`` read as
written and translate 1:1 to the CPU bus. ``ranges`` is declared explicitly as
``<child-addr parent-addr-hi parent-addr-lo size>`` because rr cannot write a
zero-length property (RTL-P2.2314): it writes ``""`` and ``true`` as a one-byte
string, and a one-byte ``ranges`` is not the empty 1:1 form.

INPUT DOMAIN: the committed ``linux.dt_nodes`` of ``targets/pfsoc_datalogger.yml``
plus the base root's cell sizes (2/2, read from the decompiled base DTB on
2026-11-11). OUTPUT CONTRACT: the child's reg decodes under its parent's cells
to (0x60000000, 0x10000), and the parent's ranges map that window 1:1 into the
root's address space; a ``ranges`` that is a string, the wrong arity, or not
1:1 is refused.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"

#: Root #address-cells / #size-cells of the PolarFire base tree.
BASE_ROOT_CELLS = (2, 2)
CONTAINER = "/fabric-uio@0"
CHILD = "/fabric-uio@0/ring-watermark@60000000"
CSR_WINDOW = (0x60000000, 0x10000)


def _entries() -> dict[str, dict]:
    nodes = yaml.safe_load(TARGET.read_text(encoding="utf-8"))["linux"]["dt_nodes"]
    return {n["path"]: n for n in nodes}


def _cells(props: dict) -> tuple[int, int]:
    return props["#address-cells"], props["#size-cells"]


def _number(cells: list[int]) -> int:
    value = 0
    for c in cells:
        value = (value << 32) | c
    return value


def decode_reg(reg: list[int], addr_cells: int, size_cells: int) -> list[tuple[int, int]]:
    """(address, size) pairs of a reg property under the parent's cell sizes."""
    width = addr_cells + size_cells
    assert isinstance(reg, list) and reg and len(reg) % width == 0, (
        f"reg {reg!r} is not a whole number of {addr_cells}+{size_cells}-cell entries")
    return [(_number(reg[i:i + addr_cells]), _number(reg[i + addr_cells:i + width]))
            for i in range(0, len(reg), width)]


def translate(addr: int, size: int, ranges, child_cells: tuple[int, int],
              parent_addr_cells: int) -> int:
    """CPU-side address of a child window through one bus's ``ranges``."""
    assert isinstance(ranges, list) and all(
        isinstance(c, int) and not isinstance(c, bool) for c in ranges), (
        f"ranges {ranges!r} is not a cell list; rr writes a string or true as a "
        "one-byte property, which is not the empty 1:1 form (RTL-P2.2314)")
    c_addr, c_size = child_cells
    width = c_addr + parent_addr_cells + c_size
    assert ranges and len(ranges) % width == 0, (
        f"ranges has {len(ranges)} cells; each entry needs {width} "
        f"({c_addr} child, {parent_addr_cells} parent, {c_size} size)")
    for i in range(0, len(ranges), width):
        child = _number(ranges[i:i + c_addr])
        parent = _number(ranges[i + c_addr:i + c_addr + parent_addr_cells])
        span = _number(ranges[i + c_addr + parent_addr_cells:i + width])
        if child <= addr and addr + size <= child + span:
            return parent + (addr - child)
    raise AssertionError(f"no ranges entry covers {addr:#x}+{size:#x}")


def test_container_is_created_before_its_child():
    paths = list(_entries())
    assert CONTAINER in paths and CHILD in paths
    assert paths.index(CONTAINER) < paths.index(CHILD)
    for p in (CONTAINER, CHILD):
        assert _entries()[p].get("create") is True, p


def test_container_is_a_simple_bus_with_one_one_cells():
    props = _entries()[CONTAINER]["properties"]
    assert props["compatible"] == "simple-bus"
    assert _cells(props) == (1, 1)


def test_child_reg_decodes_to_the_csr_window_under_one_one_cells():
    parent = _entries()[CONTAINER]["properties"]
    reg = _entries()[CHILD]["properties"]["reg"]
    assert decode_reg(reg, *_cells(parent)) == [CSR_WINDOW]


def test_ranges_maps_the_csr_window_one_to_one():
    parent = _entries()[CONTAINER]["properties"]
    (addr, size), = decode_reg(_entries()[CHILD]["properties"]["reg"], *_cells(parent))
    cpu = translate(addr, size, parent["ranges"], _cells(parent), BASE_ROOT_CELLS[0])
    assert cpu == CSR_WINDOW[0]


def test_translate_refuses_the_one_byte_empty_form():
    """The value rr cannot write as zero-length must not pass as 1:1."""
    for bad in ("", True):
        try:
            translate(*CSR_WINDOW, bad, (1, 1), 2)
        except AssertionError:
            continue
        raise AssertionError(f"ranges {bad!r} was accepted")


def test_translate_refuses_wrong_arity_and_offset_maps():
    for bad in ([0x60000000, 0x60000000, 0x10000],          # parent addr as 1 cell
                [0x60000000, 0, 0x50000000, 0x10000]):      # not 1:1
        try:
            assert translate(*CSR_WINDOW, bad, (1, 1), 2) == CSR_WINDOW[0]
        except AssertionError:
            continue
        raise AssertionError(f"ranges {bad!r} was accepted")
