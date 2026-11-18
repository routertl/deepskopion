"""DS-P1.53 regression guard: deepskopion_de25_top.vhd's `component
hps_subsys` declaration must match the port SHAPE the REAL rr hps_preset
generator actually renders for this project's ip.yml — not a hand-authored
guess at what it "should" look like.

**Measured defect** (queued `rr synth run`, job 963d5342d885, against this
project's pre-fix top): Quartus VHDL Binding Indication **Error 13870 x4** —
"design entity hps_subsys does not contain port pll_refclk_clk /
pll_locked_export / fabric_clk_out_clk / f2h_irq0_irq specified in
associated component". None of those four strings has EVER appeared in
sdk.engine.hps_preset (grep confirms zero hits across the file) — the
top's `component hps_subsys` was authored against an imagined PLL-embedded /
IRQ-mapped wrapper shape that rr's hps_preset engine has never produced.
DS-P1.52 landed ip.yml's `hps_frozen`/`hps_emif`/`export_f2sdram`/
`export_lwh2f` declaration (proven correct — a queued `rr synth run`
against it renders `add_component` and the bridge re-exports exactly as
expected; DS-P2.196 moved it to `hps_regenerate`, see
test_de25_hps_regenerate_ds_p2_196.py), but that alone
was only HALF the mechanism: the top's own component declaration still
didn't match what that declaration actually causes rr to generate, so
elaboration black-boxed the whole instance the first time a real generated
wrapper (hps_subsys/synth/hps_subsys.v) reached the Quartus binder.

DS-P1.53's fix mirrors the proven, real-hardware rr-ddr-stream
de25_agilex5_ddr_validator top (top_de25_ddr_validator.vhd) verbatim: no
PLL, no IRQ export — `fabric_clk_clk`/`fabric_reset_reset` conduit pair
driven directly from the board oscillator.

**This guard** drives the SAME code path `rr synth run`'s pre-synth HPS
generator uses (`sdk.engine.hps_gen` / `hps_preset` / `soc_backend`) against
this project's real ip.yml, extracts the top-level interface NAMES the real
generator exports (`add_interface <name> ...` in the rendered qsys-script),
and checks every port in deepskopion_de25_top.vhd's `component hps_subsys`
declaration is prefixed by one of them. An orphan prefix (pll_refclk,
f2h_irq0, ...) is exactly the defect class job 963d5342d885 hit — the guard
also asserts every real interface has at least one matching top port, so a
whole interface silently dropped from the top would fail too.

**What this guard cannot see** (no Quartus Pro / qsys toolchain in this
sandbox): a port WIDTH/DIRECTION mismatch *within* a correctly-named
interface, or anything downstream of elaboration (fit, timing, the actual
.sof). The real end-to-end proof for those is a queued build — this ticket:
job 0eb53c2ad665 (`rr synth run`, PASS 0 errors/0 warnings) and job
933c504a9fe3 (`rr bitstream run`, PASS 0 errors/0 warnings, timing MET,
WNS +10.387ns @ 104.0 MHz fmax), followed by `rr linux sof --refresh`
producing sw/linux/de25/out/de25_datalogger/de25_datalogger_hps.sof (see this
revision's fresh pasted transcript for the exact run and byte count — the
sof_path/SOF_HPS_ARTIFACT basename mismatch that made the first attempt's
`deepskopion_hps.sof` claim wrong is fixed in
tools/de25_linux_image.py:_stage_target_stem_sof and pinned by
tools/tests/test_de25_hps_sof_artifact_name_p1_53.py).
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
TOP_VHD = ROOT / "hw/rtl/de25/deepskopion_de25_top.vhd"


def _rendered_qsys_script() -> str:
    """Render the qsys-script the REAL rr pre-synth HPS generator would emit
    for this project — the call chain of ``generate_hps_pre_synth``: a
    regenerated HPS (``hps_regenerate``, DS-P2.196) renders through
    ``hps_generation_config`` in the build-tree root ``hps_generation_root``."""
    from sdk.engine.hps_gen import (
        _build_hps_config,
        _hardware_platform,
        detect_hps_declaration,
        hps_generation_config,
        hps_generation_root,
    )
    from sdk.engine.hps_preset import render_hps_qsys_script
    from sdk.engine.soc_backend import generate_hps_platform

    decl = detect_hps_declaration(ROOT)
    assert decl is not None, f"{ROOT / 'ip.yml'} does not declare an HPS"
    board_id = _hardware_platform(ROOT)
    config = _build_hps_config(decl.config, project_root=ROOT, board_id=board_id)
    platform = generate_hps_platform(
        decl.vendor, decl.system_name, hps_generation_root(ROOT, config),
        config=hps_generation_config(config), fabric_ips=decl.fabric_ips,
        board_id=board_id,
    )
    return render_hps_qsys_script(platform)


def _generator_exported_interfaces() -> list[str]:
    """Top-level interface names the real generator exports at the
    hps_subsys boundary (``add_interface <name> ...`` lines)."""
    text = _rendered_qsys_script()
    return re.findall(r"^add_interface (\S+) ", text, re.M)


def _top_component_hps_subsys_ports() -> list[str]:
    """Port names declared on deepskopion_de25_top.vhd's `component
    hps_subsys` (the VHDL binding surface Quartus elaborates against)."""
    text = TOP_VHD.read_text()
    start = text.index("component hps_subsys is")
    end = text.index("end component hps_subsys;")
    block = text[start:end]
    ports = []
    for line in block.split("\n"):
        line = line.strip()
        m = re.match(r"(\w+)\s*:\s*(in|out|inout)\b", line)
        if m:
            ports.append(m.group(1))
    return ports


def test_top_component_has_no_orphan_ports_vs_real_generator():
    """Every port on the top's `component hps_subsys` must belong to an
    interface the REAL generator actually exports. A port whose name
    doesn't match any real interface prefix (pll_refclk_clk,
    pll_locked_export, f2h_irq0_irq, ...) is precisely the Error-13870-x4
    "design entity hps_subsys does not contain port ..." defect class
    measured on job 963d5342d885."""
    ifaces = _generator_exported_interfaces()
    assert ifaces, (
        "the real HPS generator exported zero top-level interfaces for "
        "this project's ip.yml — nothing to check the top against."
    )
    ports = _top_component_hps_subsys_ports()
    orphans = [
        p for p in ports
        if not any(p == iface or p.startswith(iface + "_") for iface in ifaces)
    ]
    assert not orphans, (
        f"deepskopion_de25_top.vhd's component hps_subsys declares "
        f"port(s) {orphans} that match NO interface the real rr hps_preset "
        f"generator exports ({sorted(ifaces)}) for this project's ip.yml — "
        "Quartus will fail VHDL Binding Indication Error 13870 "
        "('design entity hps_subsys does not contain port ...') at "
        "elaboration, exactly as measured on job 963d5342d885. Update the "
        "component declaration to the shape the real generator renders "
        "(mirror rr-ddr-stream's proven top_de25_ddr_validator.vhd), don't "
        "hand-author a guess."
    )


def test_top_component_covers_every_real_generator_interface():
    """The converse of the orphan check: every interface the real generator
    exports must have at least one matching port on the top's component
    declaration, so a whole bridge/conduit silently dropped from the top
    (as opposed to merely misnamed) is also caught."""
    ifaces = _generator_exported_interfaces()
    ports = _top_component_hps_subsys_ports()
    missing = [
        iface for iface in ifaces
        if not any(p == iface or p.startswith(iface + "_") for p in ports)
    ]
    assert not missing, (
        f"the real rr hps_preset generator exports interface(s) {missing} "
        "for this project's ip.yml that deepskopion_de25_top.vhd's "
        "component hps_subsys declares no matching port for — a real "
        "generated wrapper would carry ports this component declaration "
        "can never bind to."
    )


def test_top_component_carries_no_stale_pll_or_irq_ports():
    """Named-string regression pin (DS-P1.53's exact fix): the pre-fix top
    declared pll_refclk_clk / pll_locked_export / fabric_clk_out_clk /
    f2h_irq0_irq — a shape sdk.engine.hps_preset has never produced (grep
    confirms zero occurrences of any of these strings in that module, ever).
    A regression back to that shape must fail even if some future generator
    change happened to add a same-prefixed interface that would slip past
    the prefix-based orphan check above."""
    ports = set(_top_component_hps_subsys_ports())
    stale = {"pll_refclk_clk", "pll_locked_export", "fabric_clk_out_clk",
             "f2h_irq0_irq"} & ports
    assert not stale, (
        f"deepskopion_de25_top.vhd's component hps_subsys declares stale "
        f"port(s) {sorted(stale)} from the disproven PLL-embedded / "
        "IRQ-mapped wrapper shape (DS-P1.53) — rr's hps_preset generator "
        "has never produced these ports."
    )
