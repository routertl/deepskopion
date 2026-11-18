"""DS-P2.309: every header input of the three products has a weak pull-up.

An undriven board input with no pull resistor reads low. DE25 C3 run 1
(2026-11-15) measured the result: with the I2C logger enabled and its EMIO mux
bit clear, the logger read the GPIO_D header with nothing attached, saw SCL low
and emitted one STRETCH_ERROR per 25 ms (40.5/s), and the switch to the internal
lines was reported as a STOP_ERROR. UART RX, SPI CS and I2C all idle HIGH, so
every header input gets a pull-UP (MC ruling, DS-P2.309).

rr reads a pin's pull only from the shared board pinout (RTL-P2.2402 adds a
per-target key). Until then each product carries the pull-ups in a file it
already owns, through an rr extension point:

- DE25:      WEAK_PULL_UP_RESISTOR assignments in the tcl_hooks project-options
             script hw/quartus/de25/de25_project_options.tcl.
- Zybo:      PULLUP properties in a user XDC listed under sources.xdc.
- PolarFire: set_io -RES_PULL Up in a user I/O PDC imported by a gen_project
             hook.

The header input set is read from each product's top entity, so a header
input added later without a pull-up fails here by name.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

_PORT_RE = re.compile(
    r"^\s*(?P<names>[A-Za-z_][\w\s,]*?)\s*:\s*in\s+std_logic"
    r"(?:_vector\s*\(\s*(?P<hi>\d+)\s+downto\s+(?P<lo>\d+)\s*\))?",
    re.IGNORECASE | re.MULTILINE,
)


def _entity_text(path: Path, entity: str) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^entity\s+{entity}\s+is(.*?)^end\b", text, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    assert m, f"entity {entity} not found in {path}"
    # drop VHDL comments so a commented-out port is not counted
    return "\n".join(line.split("--", 1)[0] for line in m.group(1).splitlines())


def _header_inputs(path: Path, entity: str, is_header) -> list[str]:
    bits: list[str] = []
    for m in _PORT_RE.finditer(_entity_text(path, entity)):
        for name in (n.strip() for n in m.group("names").split(",")):
            if not name or not is_header(name):
                continue
            if m.group("hi") is None:
                bits.append(name)
            else:
                hi, lo = int(m.group("hi")), int(m.group("lo"))
                bits.extend(f"{name}[{i}]" for i in range(lo, hi + 1))
    return bits


def _target(name: str) -> dict:
    return yaml.safe_load((ROOT / "targets" / f"{name}.yml").read_text(encoding="utf-8"))


# product -> (top file, entity, header-port predicate)
PRODUCTS = {
    "de25_datalogger": ("hw/rtl/de25/deepskopion_de25_top.vhd", "deepskopion_de25_top",
                        lambda n: n == "GPIO_D"),
    "zybo_datalogger": ("hw/rtl/deepskopion_top.vhd", "deepskopion_top",
                        lambda n: n in ("ja", "jb")),
    "pfsoc_datalogger": ("hw/rtl/pfsoc/deepskopion_pfsoc_top.vhd", "deepskopion_pfsoc_top",
                         lambda n: n.startswith("rpi_gpio")),
}

# Expected header input counts, hard-coded so a parser that finds nothing
# cannot pass the per-bit checks vacuously.
EXPECTED_COUNT = {"de25_datalogger": 16, "zybo_datalogger": 16, "pfsoc_datalogger": 14}

DE25_HOOK = "hw/quartus/de25/de25_project_options.tcl"
ZYBO_XDC = "hw/xdc/zybo_header_pullups.xdc"
PFSOC_PDC = "hw/pdc/pfsoc_header_pullups.pdc"
PFSOC_HOOK = "hw/libero/pfsoc/import_header_pullups_pdc.tcl"


@pytest.mark.parametrize("product", sorted(PRODUCTS))
def test_header_input_set_is_read_from_the_top(product):
    top, entity, is_header = PRODUCTS[product]
    bits = _header_inputs(ROOT / top, entity, is_header)
    assert len(bits) == EXPECTED_COUNT[product], (product, bits)


def _pulled(text: str, pattern: str, bits: list[str]) -> list[str]:
    missing = []
    for bit in bits:
        if not re.search(pattern.format(re.escape(bit)), text, re.MULTILINE):
            missing.append(bit)
    return missing


def _code(path: Path) -> str:
    """The file without comment lines (Tcl/XDC/PDC comments start with #)."""
    return "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))


def test_de25_every_gpio_d_input_has_a_weak_pull_up():
    top, entity, is_header = PRODUCTS["de25_datalogger"]
    bits = _header_inputs(ROOT / top, entity, is_header)
    hooks = _target("de25_datalogger").get("tcl_hooks", {}).get("gen_project", [])
    assert DE25_HOOK in hooks, f"{DE25_HOOK} is not a gen_project hook of de25_datalogger"
    text = _code(ROOT / DE25_HOOK)
    missing = _pulled(
        text, r"^\s*set_instance_assignment\s+-name\s+WEAK_PULL_UP_RESISTOR\s+ON\s+-to\s+\{{?{}\}}?\s*$", bits)
    assert not missing, f"DE25 header inputs with no WEAK_PULL_UP_RESISTOR ON: {missing}"


def test_zybo_every_pmod_input_has_a_pullup():
    top, entity, is_header = PRODUCTS["zybo_datalogger"]
    bits = _header_inputs(ROOT / top, entity, is_header)
    xdc = _target("zybo_datalogger").get("sources", {}).get("xdc", [])
    listed = [e["path"] if isinstance(e, dict) else e for e in xdc]
    assert ZYBO_XDC in listed, f"{ZYBO_XDC} is not listed under sources.xdc of zybo_datalogger"
    text = _code(ROOT / ZYBO_XDC)
    missing = _pulled(
        text, r"^\s*set_property\s+PULLUP\s+(?:true|TRUE)\s+\[get_ports\s+\{{{}\}}\]\s*$", bits)
    assert not missing, f"Zybo header inputs with no PULLUP: {missing}"


def test_pfsoc_every_header_input_has_a_pull_up():
    top, entity, is_header = PRODUCTS["pfsoc_datalogger"]
    bits = _header_inputs(ROOT / top, entity, is_header)
    hooks = _target("pfsoc_datalogger").get("tcl_hooks", {}).get("gen_project", [])
    assert PFSOC_HOOK in hooks, f"{PFSOC_HOOK} is not a gen_project hook of pfsoc_datalogger"
    assert PFSOC_PDC in (ROOT / PFSOC_HOOK).read_text(encoding="utf-8").replace("${g_root_dir}/", ""), (
        f"{PFSOC_HOOK} does not import {PFSOC_PDC}")
    text = _code(ROOT / PFSOC_PDC)
    missing = _pulled(
        text, r"^\s*set_io\s+-port_name\s+\{{?{}\}}?\b.*-RES_PULL\s+Up\b", bits)
    assert not missing, f"PolarFire header inputs with no -RES_PULL Up: {missing}"
