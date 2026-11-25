# rr unknown-llm (AI-generated)
"""DE-B0 (DS-P2.91) regression guard.

The DE25 target used to subscribe an explicit `hardware.pins` list that had
gone stale: it named only LEDR/GPIO_D/CLOCK2_50 (the DE-P0-era plan), while
`deepskopion_de25_top.vhd` had since grown ~70 more real ports — the DDR4_*
fabric-DDR4 conduit and HPS_* dedicated-IO passthroughs DE-H0 added. Because
`hardware.pins` was set, rr's board-pinout resolver ran in strict-explicit
mode and bound ONLY the listed names; every unlisted top port got no board
pin at all. Quartus's Assembler refuses to produce a .sof while any user pin
lacks a location/IO-standard assignment (Critical Warning 25196 class) — the
queued DE25 bitstream build exited 1 at the Assembler stage for exactly this
reason. Separately, boards/de25-standard's shared `master.sdc` unconditionally
declares `create_clock` on all three board oscillators (CLOCK0_50/1_50/2_50);
with only CLOCK2_50 declared as a top port, the other two named a port that
did not exist (Critical Warning 25207 class).

CLOCK1_50 stays deliberately absent from the top even after this fix. DE-P0
§5.2 flagged its registry location (D8/LVCMOS18) as vendor-seed and not yet
schematic-cross-checked; DS-P3.143 ran that cross-check. The DE25-Standard
revD User Manual's Table 3-9 confirms CLOCK1_50 = PIN_D8, 1.8V verbatim —
D8/LVCMOS18 was never a wrong guess, it is the one fixed PCB trace for this
signal. That does NOT make it placeable here: re-queued after this repo's
own DE-F0 fitter cure (v13.0.0 agilex_hps.ip regen + f2sdram R-channel
keep-alive sink) to test whether that cure had freed up the conflict, job
a68149b2dab5 reproduced the exact same "Error (175020): cannot place logic
pin in region (108, 53) ... no valid locations ... that supports the 1.8-V
standard" as the original job 3d510c77d9bd — same region, same pin, same
error. This design's real HPS DDR4 EMIF permanently occupies that I/O
bank's 1.2-V VCCIO on this SKU; there is no location for a schematic-
confirmed 1.8-V pin in that bank while the EMIF is present. So this guard
only requires CLOCK0_50 and CLOCK2_50 to exist as top ports; a build with
CLOCK0_50 bound and CLOCK1_50 undeclared reaches Assembler 0 errors/0
warnings and "Timing requirements were met" (job 143337099b45).

This test keeps `targets/de25_datalogger.yml`'s `hardware.pins` list true
against the entity it subscribes for: every port of `deepskopion_de25_top`
must appear, and nothing that isn't a real port may appear (an over-broad
bare bus name — e.g. subscribing all of the board's LEDR[0..9] when the top
only exposes LEDR[3:0] — would hand the Assembler a location assignment for
a port that doesn't exist, the same failure class in reverse).

It does not reach into the sibling `routertl` checkout (DS-X1: no reach into
sibling checkouts) — it only checks that the two files *in this repo* (the
VHDL entity and the target yml) agree. The actual name -> location/IO
standard binding is the routertl board registry's job and is only provable
by a live `rr queue submit --board bitstream` run against it (see the DE-B0
ticket record for that proof); this guard is what keeps the *subscription*
honest between builds, cheaply and on every commit.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TOP = ROOT / "hw" / "rtl" / "de25" / "deepskopion_de25_top.vhd"
TARGET = ROOT / "targets" / "de25_datalogger.yml"

# Vector ports whose board-registry width is WIDER than the port this top
# declares (LEDR[0..9]/GPIO_D[0..35] on the board vs LEDR[3:0]/GPIO_D[15:0]
# here) — hardware.pins must list these per-bit; every other vector port's
# top width matches the board's exactly, so a bare bus name is safe there.
NARROWER_THAN_BOARD = {"LEDR", "GPIO_D"}


def _entity_ports() -> dict[str, int]:
    """port base name -> bit width (1 for scalar) from the top entity."""
    text = TOP.read_text(encoding="utf-8")
    entity = re.search(
        r"entity\s+deepskopion_de25_top\s+is(.*?)end\s+entity\s+deepskopion_de25_top",
        text,
        re.S,
    )
    assert entity, "deepskopion_de25_top entity not found"
    body = entity.group(1)
    # Strip line comments before parsing so `-- foo : bar` never masquerades
    # as a port declaration or corrupts one that shares its line.
    body = "\n".join(line.split("--", 1)[0] for line in body.splitlines())
    ports: dict[str, int] = {}
    for m in re.finditer(
        r"(\w+)\s*:\s*(?:in|out|inout)\s+std_logic(?:_vector\s*\(\s*(\d+)\s+downto\s+(\d+)\s*\))?",
        body,
    ):
        name, hi, lo = m.groups()
        width = 1 if hi is None else int(hi) - int(lo) + 1
        assert name not in ports, f"duplicate port parsed: {name}"
        ports[name] = width
    assert ports, "no ports parsed from deepskopion_de25_top entity"
    return ports


def _expand_entity_ports(ports: dict[str, int]) -> set[str]:
    expanded: set[str] = set()
    for name, width in ports.items():
        if width == 1:
            expanded.add(name)
        else:
            expanded.update(f"{name}[{i}]" for i in range(width))
    return expanded


def _target_pins() -> list[str]:
    config = yaml.safe_load(TARGET.read_text(encoding="utf-8"))
    pins = (config.get("hardware") or {}).get("pins")
    assert isinstance(pins, list) and pins, (
        "targets/de25_datalogger.yml hardware.pins must be a non-empty explicit "
        "list — see the DE-B0 guard's module docstring for why inferred mode "
        "is not used here."
    )
    return pins


def _expand_subscription(pins: list[str], entity_ports: dict[str, int]) -> set[str]:
    """Mirror routertl's bare-bus-name expansion, but against the entity's
    OWN width (this test's whole point: catch over- and under-subscription
    without reaching into the sibling board registry)."""
    expanded: set[str] = set()
    for entry in pins:
        m = re.match(r"^(\w+)\[(\d+)\]$", entry)
        if m:
            expanded.add(entry)
            continue
        assert entry in entity_ports, (
            f"hardware.pins entry {entry!r} is not a bare top-port name or an "
            f"indexed one — check targets/de25_datalogger.yml"
        )
        width = entity_ports[entry]
        if width == 1:
            expanded.add(entry)
        else:
            expanded.update(f"{entry}[{i}]" for i in range(width))
    return expanded


def test_every_top_port_is_subscribed():
    entity_ports = _entity_ports()
    entity_expanded = _expand_entity_ports(entity_ports)
    subscribed = _expand_subscription(_target_pins(), entity_ports)

    missing = entity_expanded - subscribed
    assert not missing, (
        f"{len(missing)} top port bit(s) of deepskopion_de25_top are not in "
        f"targets/de25_datalogger.yml's hardware.pins, so the Assembler would "
        f"get no location/IO-standard assignment for them (Critical Warning "
        f"25196 class): {sorted(missing)}"
    )

    extra = subscribed - entity_expanded
    assert not extra, (
        f"targets/de25_datalogger.yml's hardware.pins subscribes {sorted(extra)}, "
        "which is not a bit of any deepskopion_de25_top port — rr would hand "
        "the Assembler a location/IO-standard assignment for a port that "
        "doesn't exist (the CLOCK0_50/CLOCK1_50 failure class, in reverse)."
    )


def test_narrower_than_board_ports_are_listed_per_bit():
    """LEDR/GPIO_D must be per-bit in hardware.pins — a bare bus name for
    these two would over-subscribe against the board's wider registry
    entry (checked directly against the live registry by the DE-B0 build
    record; this half of the guard only needs the entity + target yml)."""
    entity_ports = _entity_ports()
    pins = _target_pins()
    bare_entries = {p for p in pins if re.match(r"^\w+$", p)}
    overlap = bare_entries & NARROWER_THAN_BOARD
    assert not overlap, (
        f"{sorted(overlap)} must be subscribed per-bit in hardware.pins, not "
        "as a bare bus name — the board registry has more bits than this "
        "top exposes, and a bare name expands to ALL of the board's bits."
    )
    for name in NARROWER_THAN_BOARD:
        width = entity_ports[name]
        for i in range(width):
            assert f"{name}[{i}]" in pins, (
                f"{name}[{i}] (declared width {width}) missing from "
                "hardware.pins"
            )


def test_clock_ports_exist_on_top():
    """boards/de25-standard's master.sdc create_clocks CLOCK0_50/1_50/2_50
    unconditionally; a name absent from the top has no port to bind (Critical
    Warning 25207 class). CLOCK1_50 is excluded here on purpose: DS-P3.143's
    schematic cross-check confirmed its D8/LVCMOS18 location is the real,
    correct pin (not a guess) AND independently reproduced the Fitter
    placement conflict with this design's HPS DDR4 EMIF on a fresh queued
    build (see the module docstring) — a real, permanent hardware conflict,
    not something this guard should paper over by demanding a port for it."""
    entity_ports = _entity_ports()
    for clk in ("CLOCK0_50", "CLOCK2_50"):
        assert clk in entity_ports, f"{clk} is not a top port of deepskopion_de25_top"
        assert entity_ports[clk] == 1, f"{clk} must be a scalar std_logic port"
    assert "CLOCK1_50" not in entity_ports, (
        "CLOCK1_50 was re-added to the top — DS-P3.143 schematic-cross-checked "
        "and confirmed D8/LVCMOS18 is genuinely Fitter-illegal with this "
        "design's HPS DDR4 EMIF in the fit (reproduced on jobs 3d510c77d9bd "
        "and a68149b2dab5); adding it back needs new hardware evidence that "
        "the conflict is gone (e.g. a passing queued fit+assembler run with "
        "it bound), not a bare port declaration."
    )
