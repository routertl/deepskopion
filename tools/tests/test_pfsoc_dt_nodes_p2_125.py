"""PF-L0 (DS-P2.125): the pfsoc DT deltas must REALISE the bootargs wiring.

The land refusal was that ``targets/pfsoc_datalogger.yml`` shipped
``dt_nodes: []`` in the SAME commit that added
``uio_pdrv_genirq.of_id=generic-uio`` to the bootargs and a ``mem=512M`` DDR
split — a bootargs that declares a UIO ring against a devicetree with no
``generic-uio`` node and no ring reservation. The daemon would boot, find no
``/dev/uio*`` and no mappable ring, and SKIP — the failure DS-X5 forbids.

This guard closes that gap by cross-checking the THREE surfaces that must
agree, so any one of them drifting (or dt_nodes reverting to empty) fails:

  * bootargs ``ds_mmio_base=`` / ``ds_clk_hz=`` / the UIO of_id (and NO
    ``mem=`` — DS-P2.157),
  * the DT nodes in ``linux.dt_nodes``,
  * ``sw/linux/pfsoc/system_memory_map.yml`` (the daemon's runtime map).

INPUT DOMAIN: the committed pfsoc target + memory map, as the image build reads
them. OUTPUT CONTRACT: a generic-uio node at ds_mmio_base, a no-map ring
reservation at the memory-map ring base that overlaps no declared kernel RAM,
and a fixed-clock at ds_clk_hz.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"
MEMMAP = ROOT / "sw" / "linux" / "pfsoc" / "system_memory_map.yml"

# The Discovery Kit reference kernel DTB (linux4microchip mpfs-disco-kit.dtb,
# as deployed and decompiled 2026-09-23) declares kernel RAM as these memory
# nodes (path: (base, size)). This check compares CPU addresses only; the same
# DDR aliased through other MSS windows is guarded by
# test_pfsoc_ring_ddr_alias_ds_p1_62.py (DS-P1.62).
REFERENCE_RAM = {
    "/memory@80000000": (0x80000000, 0x04000000),
    "/memory@8a000000": (0x8A000000, 0x08000000),
    "/memory@c4000000": (0xC4000000, 0x06000000),
    "/memory@1022000000": (0x1022000000, 0x1E000000),
    "/memory@1412000000": (0x1412000000, 0x10000000),
}


def _target() -> dict:
    return yaml.safe_load(TARGET.read_text(encoding="utf-8"))


def _bootargs_tokens() -> dict[str, str]:
    linux = _target()["linux"]
    tokens = {}
    for tok in str(linux["bootargs"]).split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            tokens[k] = v
    return tokens


def _dt_nodes() -> list[dict]:
    return _target()["linux"]["dt_nodes"]


def _node(compatible: str | None = None, path_contains: str | None = None) -> dict:
    for node in _dt_nodes():
        props = node.get("properties", {})
        if compatible is not None and props.get("compatible") == compatible:
            return node
        if path_contains is not None and path_contains in node.get("path", ""):
            return node
    raise AssertionError(
        f"no dt_node with compatible={compatible!r}/path~{path_contains!r} "
        "in targets/pfsoc_datalogger.yml linux.dt_nodes"
    )


def test_dt_nodes_is_not_the_empty_stub():
    """The exact regression: dt_nodes shipped empty beside a UIO bootargs."""
    assert _dt_nodes(), (
        "linux.dt_nodes is empty — the bootargs declare a generic-uio ring and "
        "a mem= split with no devicetree to realise them (DS-P2.125 land refusal)"
    )


def test_generic_uio_node_sits_at_ds_mmio_base():
    """uio_pdrv_genirq.of_id=generic-uio binds a `compatible = generic-uio`
    node; its reg base must equal ds_mmio_base (the FIC0 CSR the daemon reads),
    or /dev/uioN would map the wrong window."""
    toks = _bootargs_tokens()
    assert toks.get("uio_pdrv_genirq.of_id") == "generic-uio", toks
    base = int(toks["ds_mmio_base"], 0)
    node = _node(compatible="generic-uio")
    reg = node["properties"]["reg"]
    assert reg[0] == base, (
        f"generic-uio node reg base {reg[0]:#x} != bootargs ds_mmio_base "
        f"{base:#x} — the UIO device would map the wrong aperture"
    )


def test_generic_uio_base_matches_the_memory_map_csr():
    """The DT node and the daemon's runtime map must name the SAME CSR base."""
    memmap = yaml.safe_load(MEMMAP.read_text(encoding="utf-8"))
    csr = next(d for d in memmap["devices"] if d["name"] == "system_regs")
    node = _node(compatible="generic-uio")
    assert node["properties"]["reg"][0] == int(str(csr["base_addr"]), 0), (
        "generic-uio DT node and system_memory_map system_regs.base_addr "
        "disagree on the CSR base"
    )


def test_ring_reservation_matches_memory_map_and_clears_kernel_ram():
    """A no-map reserved-memory carve at the ring base that overlaps NO RAM the
    kernel is given — otherwise Linux and the ring DMA fight over DDR.

    DS-P2.157 (owner-approved 2026-09-23): this used to model kernel RAM as one
    linear window 0x8000_0000 + mem=, the DE25 layout. The Discovery Kit's
    reference DTB declares RAM as five fragments, and that false model is what
    let mem=512M ship and starve the linux,dma-default pool (eth0 -12). The
    check is now against the declared fragments, after this target's dt_nodes
    overrides (the /memory@1022000000 cap)."""
    memmap = yaml.safe_load(MEMMAP.read_text(encoding="utf-8"))
    ring = next(d for d in memmap["devices"] if d["name"] == "ddr_ring")
    ring_base = int(str(ring["base_addr"]), 0)
    ring_span = int(str(ring["span"]), 0)

    node = _node(path_contains="reserved-memory")
    props = node["properties"]
    assert "no-map" in props, "the ring reservation must be no-map"
    reg = props["reg"]
    # reg = [base_hi, base_lo, size_hi, size_lo] (#address/#size-cells = 2)
    assert reg[1] == ring_base and reg[3] == ring_span, (
        f"reserved-memory reg {reg} does not match system_memory_map ddr_ring "
        f"base {ring_base:#x} span {ring_span:#x}"
    )

    assert "mem" not in _bootargs_tokens(), (
        "mem= is back in the pfsoc bootargs: on this kit it truncates the five "
        "RAM fragments in address order and drops the dma-default pool (DS-P2.157)"
    )
    ram = dict(REFERENCE_RAM)
    for n in _dt_nodes():
        if n["path"] in ram and "reg" in n.get("properties", {}):
            r = n["properties"]["reg"]
            ram[n["path"]] = ((r[0] << 32) | r[1], (r[2] << 32) | r[3])
    for path, (base, size) in ram.items():
        assert ring_base + ring_span <= base or base + size <= ring_base, (
            f"ring {ring_base:#x}+{ring_span:#x} overlaps kernel RAM {path} "
            f"({base:#x}+{size:#x}); the ring DMA would corrupt kernel memory"
        )


def test_fixed_clock_matches_ds_clk_hz():
    node = _node(compatible="fixed-clock")
    freq = int(str(node["properties"]["clock-frequency"]), 0)
    assert freq == int(_bootargs_tokens()["ds_clk_hz"], 0), (
        "fixed-clock clock-frequency disagrees with bootargs ds_clk_hz"
    )
