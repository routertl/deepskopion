"""PF-H0 (DS-P2.124): mutation-resistant guards for deepskopion_pfsoc_top.

The first delivery's target-contract tests only checked that the top FILE
existed and that the YAML named it — so stubbing the top to an empty entity left
the whole suite green (the landing refusal's mutation proof). These guards bind
the MECHANISM the ticket asked to land, cross-checked against the live top RTL,
so emptying or breaking the top turns a caller-facing test red:

  * the top instantiates deepskopion_core at the EXACT PFSoC FIC product
    geometry (G_DDR_BACKEND=>"fic", 64-bit, 64-beat, base 0xA0000000, 100 MHz);
  * the CSR plane carries the AXI4->AXI-Lite thinning shim PF-P0 §3.2 requires
    (lwh2f_axilite_bridge) feeding axil_demux3 + 2x axil_apb_shim — NOT a bare
    "native AXI-Lite FIC0" that drops AWLEN/WLAST/IDs;
  * the ring rides the FIC1 AXI4 target and the interrupts drive MSS_INT_F2M;
  * the MSS fabric reset is the active-LOW MSS_RESET_N_M2F, inverted before the
    reset synchroniser (no DONE-named / high-when-ready polarity trap);
  * every top port is subscribed by targets/pfsoc_datalogger.yml hardware.pins
    (the DE-B0 unplaced-pin class, ported — no PF analog existed before).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
TOP = ROOT / "hw" / "rtl" / "pfsoc" / "deepskopion_pfsoc_top.vhd"
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"


def _top_text() -> str:
    return TOP.read_text(encoding="utf-8")


FABRIC = ROOT / "hw" / "rtl" / "pfsoc" / "deepskopion_pfsoc_fabric.vhd"


def _arch_text() -> str:
    """The product architecture: the thin top plus the fabric it wraps."""
    return _top_text() + "\n" + FABRIC.read_text(encoding="utf-8")


def test_top_instantiates_core_with_the_exact_fic_product_generics() -> None:
    """Stubbing the top to an empty entity removes all of these — the mutation
    the first delivery's guards missed."""
    text = _arch_text()
    assert "u_core : entity tich.deepskopion_core" in text, (
        "the top no longer instantiates the portable deepskopion_core"
    )
    # The PFSoC generics that make this the FIC leg and not a mis-copied DE25 —
    # each pinned per PF-P0 §3.3/§3.4 (a wrong one silently ships the wrong ring).
    for needle in (
        'G_DDR_BACKEND => "fic"',
        "G_DDR_DATA_WIDTH => 64",
        "G_AXI_MAX_BURST => 64",
        'G_DDR_BASE_ADDR => x"A0000000"',
        "G_DDR_WIN_WIDTH => 28",
        "G_SYS_CLK_HZ => 100_000_000",
    ):
        assert needle in text, (
            f"deepskopion_pfsoc_top must drive core generic `{needle}` "
            "(PF-P0 §3.3/§3.4) — a missing/changed one ships the wrong ring"
        )


def test_csr_plane_has_the_axi4_to_lite_thinning_shim() -> None:
    """PF-P0 §3.2: FIC0 is AXI4, so an AXI4->Lite bridge must precede the demux.

    The refused delivery declared FIC0 as native AXI-Lite straight into the
    demux, dropping AWLEN/WLAST/IDs. The fix reuses the DE25-proven
    lwh2f_axilite_bridge verbatim between the FIC0 AXI4 initiator and the demux
    (axil_demux4 since DS-P2.128).
    """
    text = _arch_text()
    assert "entity work.lwh2f_axilite_bridge" in text, (
        "the CSR plane has no AXI4->AXI-Lite thinning shim — FIC0 is AXI4 "
        "(PF-P0 §3.2); AWLEN/WLAST/IDs have nowhere to go without the bridge"
    )
    # DS-P2.128: the PolarFire demux is axil_demux4 — axil_demux3's FSM with
    # selector 3 decoded to the REA MMIO door (pfsoc_rea_door) instead of DECERR.
    assert "entity work.axil_demux4" in text, "missing the 1-in/4-out CSR demux"
    assert "entity work.axil_demux3" not in text, "demux3 would DECERR the REA door window"
    assert "entity work.pfsoc_rea_door" in text and "m_arvalid(3)" in text, (
        "demux selector 3 does not reach the REA door"
    )
    assert text.count("entity work.axil_apb_shim") == 2, (
        "the two CAN APB windows (axil_apb_shim x2) are not both present"
    )
    # FIC0 must be a FULL AXI4 initiator on the MSS boundary (carries the burst
    # attributes), not a native-Lite port. The component must declare the IDs.
    assert "fic0_awid_o" in text and "fic0_awlen_o" in text, (
        "the pfsoc_mss component no longer declares FIC0 as a full AXI4 "
        "initiator (AWID/AWLEN) — it regressed to the invented native-Lite port"
    )


def test_ring_rides_fic1_axi4_target_and_irqs_drive_f2m() -> None:
    text = _arch_text()
    # The core's DDR write records land on the FIC1 AXI4 target. DS-P2.152:
    # the real FIC_1_AXI4_S_AWADDR pin is 38-bit (disco_mss.v), so the core's
    # 32-bit record address is zero-extended through an intermediate signal
    # rather than mapped verbatim — assert the resize AND the port wiring so
    # dropping either leg (or reverting to a direct 32-bit map) fails.
    assert re.search(
        r"fic1_awaddr\s*<=\s*std_logic_vector\(resize\(unsigned\(axi_h2c\.AWADDR\),\s*38\)\)",
        text,
    ), "the ring write address is not resized from axi_h2c.AWADDR to the 38-bit FIC1 pin"
    assert re.search(r"fic1_awaddr_i\s*=>\s*fic1_awaddr", text), (
        "the ring write path is not wired onto the FIC1 AXI4 target"
    )
    # The three fabric interrupts drive the low MSS_INT_F2M lines, rest inactive.
    assert re.search(r"mss_f2m\(2 downto 0\)\s*<=\s*irq_core", text), (
        "the {CTU B, CTU A, ring watermark} irqs are not driven onto "
        "MSS_INT_F2M[2:0]"
    )
    assert re.search(r"mss_f2m\(63 downto 3\)\s*<=\s*\(others\s*=>\s*'0'\)", text), (
        "the unused MSS_INT_F2M lines (63:3) are not tied inactive"
    )


def test_mss_fabric_reset_is_active_low_m2f_not_a_done_polarity_trap() -> None:
    """The reset must invert the active-LOW MSS_RESET_N_M2F before the sync.

    The refused delivery wired the reset to a DONE-named pin documented as
    'HIGH while releasing' and fed it straight into the shift register — a
    self-contradiction that holds the fabric in reset forever under one reading.
    """
    text = _arch_text()
    assert "mss_reset_n_o" in text and "mss_init_done" not in text, (
        "the MSS reset export must be the active-low mss_reset_n_o "
        "(MSS_RESET_N_M2F), not the mss_init_done DONE-name trap"
    )
    assert re.search(r"mss_release_busy\s*<=\s*not\s+mss_reset_n", text), (
        "MSS_RESET_N_M2F (active-low) must be inverted to an active-high "
        "release-busy before the reset synchroniser"
    )
    assert re.search(
        r"rst_sync_r\s*<=\s*rst_sync_r\(1 downto 0\)\s*&\s*mss_release_busy", text
    ), "the reset synchroniser must shift in the inverted release-busy"


# ── every_top_port_is_subscribed (DE-B0 unplaced-pin class, PF analog) ────────

def _entity_ports() -> dict[str, int]:
    text = _top_text()
    m = re.search(
        r"entity\s+deepskopion_pfsoc_top\s+is(.*?)end\s+entity\s+deepskopion_pfsoc_top",
        text,
        re.S,
    )
    assert m, "deepskopion_pfsoc_top entity not found"
    body = "\n".join(line.split("--", 1)[0] for line in m.group(1).splitlines())
    ports: dict[str, int] = {}
    for pm in re.finditer(
        r"(\w+)\s*:\s*(?:in|out|inout)\s+std_logic(?:_vector\s*\(\s*(\d+)\s+downto\s+(\d+)\s*\))?",
        body,
    ):
        name, hi, lo = pm.groups()
        width = 1 if hi is None else int(hi) - int(lo) + 1
        ports[name] = width
    assert ports, "no ports parsed from the pfsoc top entity"
    return ports


def _expand(ports: dict[str, int]) -> set[str]:
    out: set[str] = set()
    for name, width in ports.items():
        if width == 1:
            out.add(name)
        else:
            out.update(f"{name}[{i}]" for i in range(width))
    return out


def test_every_top_port_is_subscribed() -> None:
    """Every port of the top must appear in hardware.pins, and nothing that is
    not a port may appear — the DE-B0 failure (a .sof with unplaced pins) ported
    to the PFSoC leg, which had no such guard before."""
    ports = _entity_ports()
    # DS-P2.152: the UPPERCASE ports are the Libero-auto-promoted MSS dedicated
    # pads (DDR4/SD/MMUART0/CAN/I2C/USB/QSPI/SGMII/REFCLK). They are located by
    # the frozen MSS configuration on dedicated balls, never from hardware.pins
    # (a hardware.pins entry would ask the board registry to place a ball it
    # does not own). The unplaced-fabric-pin guard applies to the fabric ports.
    ports = {n: w for n, w in ports.items() if n[0].islower()}
    pins = (yaml.safe_load(TARGET.read_text(encoding="utf-8"))["hardware"] or {}).get("pins")
    assert isinstance(pins, list) and pins, "hardware.pins must be a non-empty list"
    want = _expand(ports)
    have = set(pins)
    missing = want - have
    extra = have - want
    assert not missing, f"top ports with no board subscription: {sorted(missing)}"
    assert not extra, f"hardware.pins names non-existent top ports: {sorted(extra)}"


def test_thin_top_passes_every_port_straight_through() -> None:
    """DS-P2.326: the top is a thin wrapper around deepskopion_pfsoc_fabric. The body
    checks above read the fabric, so this guards the hop in between: exactly one
    fabric instance, and every top port mapped by its own name (a swapped or tied
    port here would bypass every wiring check on the fabric)."""
    text = _top_text()
    assert len(re.findall(r"\bentity\s+work\.deepskopion_pfsoc_fabric\b", text)) == 1, (
        "the pfsoc top must instantiate deepskopion_pfsoc_fabric exactly once")
    unmapped = sorted(p for p in _entity_ports() if not re.search(rf"\b{p}\s*=>\s*{p}\b", text))
    assert not unmapped, f"top ports not passed straight through to the fabric: {unmapped}"
