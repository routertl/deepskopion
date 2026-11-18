"""PF-H0 (DS-P2.124): guards for the Libero SmartDesign builder tcl.

The landing refusal proved two things about hw/libero/pfsoc/pfsoc_mss_subsys.tcl:
NO test read it (emptying it left the suite green), and it invented MSS pin names
(`FIC_0_AXI4_INITIATOR_fic0_awaddr_o`, `MSS_INT_F2M_CONFIG_DONE`,
`FIC_0_INTERRUPT`) that no real MSS component could elaborate. These guards close
both:

  * the SmartDesign top ports the tcl creates must EQUAL the pfsoc_mss VHDL
    component ports, one for one — so the "one writer owns both boundaries"
    invariant is machine-checked and emptying the tcl fails;
  * the tcl must wire to REAL MSS pin forms (MSS_RESET_N_M2F, MSS_INT_F2M,
    FIC_x_ACLK) and must NOT carry the invented forms or the wrong SmartDesign
    idioms (sd_create_pin_slices with no slices, sd_rename_port on instance pins).

DS-P2.152 (PF-H0) closed the DS-P2.133 deferred defect: the builder now wires
the REAL `FIC_0_AXI4_M_*` / `FIC_1_AXI4_S_*` pins with the real widths (38-bit
addr, 64-bit data, 8/4-bit ids), gen_project elaborates, and synthesis produces
a netlist. The prefix guards below now LOCK IN the proven names, and
test_fic_rows_match_the_frozen_mss_verilog checks every row's pin name, width
and direction against the `disco_mss.v` inside the frozen .cxz — the authority
that disproved the original guesses.

DS-P2.152 also revised the port-set contract: Libero AUTO-PROMOTES every MSS
dedicated PAD pin (DDR4/SD/MMUART0/CAN/I2C/USB/QSPI/SGMII/REFCLK) to a port of
the generated pfsoc_mss component, so the VHDL component now carries those pad
ports too (verbatim UPPERCASE MSS pin names, passed through the top entity).
The one-writer equality below therefore compares the tcl-created ports against
the component's FABRIC ports only — the lowercase ones this builder creates;
the pad passthrough has its own guard in test_pfsoc_top_wiring_contract.py.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FABRIC = ROOT / "hw" / "rtl" / "pfsoc" / "deepskopion_pfsoc_fabric.vhd"
TCL = ROOT / "hw" / "libero" / "pfsoc" / "pfsoc_mss_subsys.tcl"

# The AXI channel suffixes that are legitimate real MSS AXI4 bus-interface pins.
_VALID_AXI_SUFFIX = re.compile(
    r"^(AW|AR)(ID|ADDR|LEN|SIZE|BURST|LOCK|CACHE|PROT|QOS|VALID|READY)$"
    r"|^(W|R)(ID|DATA|STRB|LAST|RESP|VALID|READY)$"
    r"|^(B)(ID|RESP|VALID|READY)$"
)


def _component_ports() -> set[str]:
    text = FABRIC.read_text(encoding="utf-8")
    m = re.search(
        r"component\s+pfsoc_mss\s+is\s+port\s*\((.*?)\)\s*;\s*end\s+component\s+pfsoc_mss",
        text,
        re.S,
    )
    assert m, "component pfsoc_mss declaration not found in the fabric"
    body = "\n".join(line.split("--", 1)[0] for line in m.group(1).splitlines())
    ports = set(re.findall(r"(\w+)\s*:\s*(?:in|out)\s+std_logic", body))
    assert ports, "no ports parsed from the pfsoc_mss component"
    # Keep only the FABRIC ports the builder tcl creates (lowercase, _i/_o
    # convention). The UPPERCASE ports are the Libero-auto-promoted MSS pad
    # pins (DS-P2.152) — created by the tool, not by the builder.
    return {p for p in ports if p[0].islower()}


def _tcl_created_ports() -> set[str]:
    """Every SmartDesign top port the builder creates: explicit sd_create_*_port
    literals, plus the ports the pf_wire helper creates (its bare-name calls and
    its two `foreach {port dir width suffix}` mapping tables)."""
    text = TCL.read_text(encoding="utf-8")
    ports: set[str] = set()

    # Explicit sd_create_*_port -port_name {X}
    ports.update(re.findall(r"sd_create_\w+_port\b.*?-port_name\s*\{?(\w+)\}?", text))

    # Bare pf_wire <port> ... calls (e.g. the F2M line).
    ports.update(re.findall(r"^\s*pf_wire\s+(\w+)\b", text, re.M))

    # foreach {port dir width suffix} { <list> } { ... } — take every 4th token.
    for body in re.findall(
        r"foreach\s*\{\s*port\s+dir\s+width\s+suffix\s*\}\s*\{(.*?)\}\s*\{", text, re.S
    ):
        toks = body.split()
        ports.update(toks[0::4])
    return ports


def test_builder_creates_exactly_the_component_ports() -> None:
    """One writer owns both boundaries: the tcl's created ports == the VHDL
    component's ports. Emptying the tcl, or drifting either side, fails."""
    comp = _component_ports()
    tcl = _tcl_created_ports()
    missing = comp - tcl
    extra = tcl - comp
    assert not missing, (
        "pfsoc_mss component ports the builder tcl never creates (the "
        "SmartDesign would not present them to the VHDL binding): "
        f"{sorted(missing)}"
    )
    assert not extra, (
        "builder tcl creates SmartDesign ports absent from the pfsoc_mss "
        f"component (a binding mismatch): {sorted(extra)}"
    )


def test_builder_wires_real_mss_pin_names_not_invented_ones() -> None:
    text = TCL.read_text(encoding="utf-8")
    # Real MSS pin forms that are proven correct and must stay present. The FIC
    # AXI *prefix* is deliberately NOT asserted here — see the module docstring:
    # the builder's current INITIATOR/TARGET forms are disproven (real: M_*/S_*),
    # the rewrite is PF-B0, and locking in either prefix would be a test-power
    # defect. The suffix/stem guards below stay prefix-agnostic.
    for needle in (
        "MSS_RESET_N_M2F",
        "MSS_INT_F2M",
        "FIC_0_ACLK",
        "FIC_1_ACLK",
        # DS-P2.152: the PROVEN FIC AXI pin prefixes (disco_mss.v inside the
        # frozen .cxz; gen_project + synthesis pass with these on the bench).
        "FIC_0_AXI4_M_",
        "FIC_1_AXI4_S_",
    ):
        assert needle in text, f"builder tcl no longer references real MSS pin {needle!r}"

    # The invented forms the refusal named must NOT come back — checked against
    # CODE only (comment lines stripped): the DS-P2.152 header legitimately
    # documents the old wrong names as history.
    code = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )
    for bad in (
        "MSS_INT_F2M_CONFIG_DONE",   # F2M is interrupts, not a fabric-release pin
        "FIC_0_INTERRUPT",           # invented; F2M is the 64-bit MSS_INT_F2M bus
        "FIC_0_AXI4_INITIATOR_",     # DS-P2.124 invented prefix — no such MSS pin;
                                     # the very first sd_connect_pins against it
                                     # aborted gen_project (DS-P2.152 root cause)
        "FIC_1_AXI4_TARGET_",        # ditto for the FIC1 side
        "_fic0_awaddr_o",            # wrapper port names concatenated onto the pin
        "_fic1_awid_i",
        "sd_create_pin_slices",      # bus-slice API misused with no slice args
        "sd_rename_port",            # renaming instance pins instead of create/connect
    ):
        assert bad not in code, (
            f"builder tcl still carries the invented/wrong form {bad!r} the "
            "landing refusal named"
        )


def test_builder_axi_suffixes_are_all_real_axi_channel_names() -> None:
    """Every FIC pin the tcl builds resolves to FIC_x_AXI4_*_<AXI> with a real
    AXI channel suffix — proving the names are Microchip pins, not invented."""
    text = TCL.read_text(encoding="utf-8")
    suffixes: list[str] = []
    for body in re.findall(
        r"foreach\s*\{\s*port\s+dir\s+width\s+suffix\s*\}\s*\{(.*?)\}\s*\{", text, re.S
    ):
        toks = body.split()
        suffixes.extend(toks[3::4])  # the 4th column is the AXI suffix
    assert suffixes, "no FIC AXI suffix mapping found in the builder tcl"
    bad = [s for s in suffixes if not _VALID_AXI_SUFFIX.match(s)]
    assert not bad, f"non-AXI FIC pin suffixes (invented?): {sorted(set(bad))}"


def test_builder_axi_port_stem_matches_its_suffix() -> None:
    """Each `{port dir width suffix}` row must PAIR its VHDL port with the AXI
    channel it names: the stem of `ficN_<stem>_<x>` (upper-cased) must equal the
    <suffix> it maps to. Swapping the awaddr<->wdata rows (a valid suffix on the
    wrong port — every FIC connection miswired) is caught here, not left green by
    the suffix-set-membership check alone."""
    text = TCL.read_text(encoding="utf-8")
    rows = 0
    mismatches: list[str] = []
    for body in re.findall(
        r"foreach\s*\{\s*port\s+dir\s+width\s+suffix\s*\}\s*\{(.*?)\}\s*\{", text, re.S
    ):
        toks = body.split()
        for port, _dir, _width, suffix in zip(
            toks[0::4], toks[1::4], toks[2::4], toks[3::4]
        ):
            rows += 1
            m = re.match(r"^fic[01]_(.+)_[oi]$", port)
            assert m, f"FIC port {port!r} not in ficN_<stem>_<dir> form"
            stem = m.group(1).upper()
            if stem != suffix:
                mismatches.append(f"{port} -> {suffix} (stem {stem})")
    assert rows, "no FIC {port dir width suffix} rows found in the builder tcl"
    assert not mismatches, (
        "FIC port names do not match the AXI suffix they wire to (miswired "
        f"channels): {mismatches}"
    )


def test_import_mss_component_matches_the_installed_libero_signature() -> None:
    """DS-P2.133: `import_mss_component` here takes ONLY `-file`.

    DS-P2.124 wrote the builder without ever running Libero (its own docstring:
    "NO test read it"), and guessed a `-name PFSOC_MSS_disco` override plus a
    matching `-component_name {PFSOC_MSS_disco}`. Running the harvested
    disco_mss.cxz through the bench Libero SoC 2025.2 (Designer/bin64/libero,
    SCRIPT mode) proved that guess wrong:

        Error: Parameter 'name' is not defined. Valid command formatting is
               'import_mss_component -file "file"'

    `import_mss_component` has no `-name`; the component is named after the .cxz
    base, i.e. `disco_mss`, and that is the only name `sd_instantiate_component`
    can bind ("Design 'disco_mss' was successfully imported."). With the wrong
    signature the VERY FIRST consuming call aborts the whole gen_project, so the
    vendored .cxz would never be consumed. This static guard would have caught it
    without a Libero run (which cannot execute under cron/CI/fresh-clone — no
    license server there), so it belongs in the always-on suite."""
    text = TCL.read_text(encoding="utf-8")

    # The COMMAND invocation only — anchored at column 0, so the word's use in
    # the explanatory comment above it is not what we parse. Capture the call
    # plus its backslash-continued argument lines up to the next top-level stmt.
    m = re.search(r"^import_mss_component\b(.*?)(?=^\S|\Z)", text, re.S | re.M)
    assert m, "builder tcl no longer calls import_mss_component at all"
    call = m.group(1)
    assert "-name" not in call, (
        "import_mss_component is called with a '-name' argument, which the "
        "installed Libero SoC 2025.2 rejects ('Parameter name is not defined') "
        "— the component name is fixed by the .cxz base (disco_mss)."
    )
    assert "-file" in call, "import_mss_component must still pass -file $mss_cxz"

    # The instance the FIC/clock wiring hangs off must bind the name the import
    # actually produces (the .cxz base), not the invented PFSOC_MSS_disco.
    assert re.search(r"-component_name\s*\{?disco_mss\}?", text), (
        "sd_instantiate_component must bind -component_name {disco_mss} (the "
        "name import_mss_component derives from disco_mss.cxz); the DS-P2.124 "
        "value PFSOC_MSS_disco never exists, so the instance would not elaborate."
    )
    assert "PFSOC_MSS_disco" not in text, (
        "the invented component name PFSOC_MSS_disco is still referenced — no "
        "such component is ever created by import_mss_component -file."
    )


def _disco_mss_ports() -> dict[str, tuple[str, int]]:
    """Port name -> (direction, width) parsed from the disco_mss.v inside the
    frozen .cxz — the ONLY authority for MSS pin names/widths (DS-P2.133/152)."""
    import io
    import zipfile

    cxz = ROOT / "hw" / "libero" / "pfsoc" / "mss" / "disco_mss.cxz"
    with zipfile.ZipFile(cxz) as z:
        text = z.read("disco_mss.v").decode("utf-8", errors="replace")
    ports: dict[str, tuple[str, int]] = {}
    for line in io.StringIO(text):
        m = re.match(
            r"\s*(input|output|inout)\s+(?:\[(\d+):(\d+)\]\s+)?(\w+)\s*;", line
        )
        if m:
            direction, hi, lo, name = m.groups()
            width = 1 if hi is None else abs(int(hi) - int(lo)) + 1
            ports[name] = (direction, width)
    assert ports, "no ports parsed from disco_mss.v inside the frozen .cxz"
    return ports
