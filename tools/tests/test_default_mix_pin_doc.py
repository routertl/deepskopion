# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""docs/hardware/DEFAULT_MIX_PINS.md stays equal to the build it describes.

Every row of the doc's tables is compared, column by column, with the source that decides it:

- Inputs table: the shared mix through the generator that builds it
  (datalogger_gather_gen.configured_signals + allocate_board_pins).
- Per-board tables: the port is the board pin's package-pin name in the product top
  (the same JA/JB -> port map test_default_mix_ds_p1_104 checks against the tops), the port is in
  the target's hardware.pins, and the package pin, schematic net (Zybo) and RPi header pin (PolarFire)
  are the RouteRTL board registry's, from which RouteRTL emits the location constraints.
- DE25 JP1 pin: the standard DE 2x20 numbering; pins 11/29 (power) and 12/30 (ground) carry no GPIO.
- Every port in a board table carries a pull-up in that board's pull-up file.

A doc row that drifts from any of these sources fails here.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "datalogger-builder"))
import datalogger_gather_gen as gen  # noqa: E402

DOC = ROOT / "docs" / "hardware" / "DEFAULT_MIX_PINS.md"
MIX = ROOT / "tools" / "datalogger-builder" / "config" / "datalogger_config.yml"

#: doc section heading -> (target, registry board id, pull-up file, pull-up line pattern for a port)
BOARDS = {
    "Zybo Z7-20 (`zybo_datalogger`)": (
        "zybo_datalogger", "zybo-z7-20", "hw/xdc/zybo_header_pullups.xdc",
        lambda port: rf"set_property\s+PULLUP\s+true\s+\[get_ports\s+\{{{re.escape(port)}\}}\]"),
    "DE25-Standard (`de25_datalogger`)": (
        "de25_datalogger", "de25-standard", "hw/quartus/de25/de25_project_options.tcl",
        lambda port: rf"WEAK_PULL_UP_RESISTOR\s+ON\s+-to\s+\{{{re.escape(port)}\}}"),
    "PolarFire SoC Discovery Kit (`pfsoc_datalogger`)": (
        "pfsoc_datalogger", "polarfire-soc-discovery", "hw/pdc/pfsoc_header_pullups.pdc",
        lambda port: rf"set_io\s+-port_name\s+\{{{re.escape(port)}\}}\s+-RES_PULL\s+Up"),
}

#: board pin -> port of each product top (the wiring test_default_mix_ds_p1_104 holds against the tops)
PORT = {
    "zybo_datalogger": {**{f"JA{i}": f"ja[{i}]" for i in range(8)}, **{f"JB{i}": f"jb[{i}]" for i in range(8)}},
    "de25_datalogger": {**{f"JA{i}": f"GPIO_D[{i}]" for i in range(8)},
                        **{f"JB{i}": f"GPIO_D[{8 + i}]" for i in range(8)}},
    "pfsoc_datalogger": dict(zip(
        [f"JA{i}" for i in range(8)] + [f"JB{i}" for i in range(6)],
        ["rpi_gpio4", "rpi_gpio17", "rpi_gpio18", "rpi_gpio27", "rpi_gpio22", "rpi_gpio23",
         "rpi_gpio24", "rpi_gpio25", "rpi_gpio5", "rpi_gpio6", "rpi_gpio12", "rpi_gpio13",
         "rpi_gpio19", "rpi_gpio16"])),
}

#: DE 2x20 header: pins 11, 29 (power) and 12, 30 (ground) carry no GPIO.
JP1_GPIO_PINS = [p for p in range(1, 41) if p not in (11, 12, 29, 30)]


def _tables(text: str) -> dict[str, list[list[str]]]:
    """Section heading -> the data rows of the first table under it (cells stripped of backticks)."""
    out: dict[str, list[list[str]]] = {}
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if section is None or not line.startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r"-+", c) for c in cells):
            continue
        rows = out.setdefault(section, [])
        rows.append(cells)
    # drop the header row of every table
    return {k: v[1:] for k, v in out.items()}


def doc_problems(text: str) -> list[str]:
    """Every way the doc's tables differ from the mix, the targets, the registry and the pull-up files."""
    from sdk.engine.board_pinout import load_board_pinout

    tables = _tables(text)
    problems: list[str] = []

    config = gen.load_yaml(MIX)
    mapping, unwired = gen.allocate_board_pins(config)
    if unwired:
        problems.append(f"the mix leaves inputs unwired: {unwired}")
    mix_rows = sorted(
        (f"{p} {i}", s, mapping.get(pin)) for p, i, s, pin in gen.configured_signals(config))
    doc_rows = sorted(tuple(r) for r in tables.get("Inputs", []))
    if doc_rows != mix_rows:
        problems.append(f"Inputs table {sorted(set(doc_rows) ^ set(mix_rows))} differs from the mix")
    used = sorted(r[2] for r in mix_rows)

    for heading, (target, board_id, pull_file, pull_re) in BOARDS.items():
        rows = tables.get(heading)
        if rows is None:
            problems.append(f"no table under '## {heading}'")
            continue
        board = load_board_pinout(board_id)
        doc_t = yaml.safe_load((ROOT / "targets" / f"{target}.yml").read_text(encoding="utf-8"))
        target_pins = set((doc_t.get("hardware") or {}).get("pins") or [])
        pull_text = (ROOT / pull_file).read_text(encoding="utf-8")
        if sorted(r[0] for r in rows) != used:
            problems.append(f"{target}: board pins {sorted(r[0] for r in rows)} are not the mix's {used}")
        for n, row in enumerate(rows):
            board_pin, port, package_pin, extra = row
            if PORT[target].get(board_pin) != port:
                problems.append(f"{target}: {board_pin} is wired to {PORT[target].get(board_pin)}, doc says {port}")
            if port not in target_pins:
                problems.append(f"{target}: {port} is not in hardware.pins")
            reg = board.find(port)
            if reg is None:
                problems.append(f"{target}: {port} is not in the {board_id} registry")
                continue
            if reg.package_pin != package_pin:
                problems.append(f"{target}: {port} is at {reg.package_pin}, doc says {package_pin}")
            if target == "zybo_datalogger":
                net = (reg.notes or "").split(" / ")[0]
                if net != extra:
                    problems.append(f"{target}: {port} net is {net}, doc says {extra}")
            elif target == "pfsoc_datalogger":
                m = re.match(r"RPi Header Pin (\d+)\b", reg.notes or "")
                if not m or m.group(1) != extra:
                    problems.append(f"{target}: {port} header pin is {m and m.group(1)}, doc says {extra}")
            else:
                index = int(re.fullmatch(r"GPIO_D\[(\d+)\]", port).group(1)) if re.fullmatch(
                    r"GPIO_D\[(\d+)\]", port) else None
                want = str(JP1_GPIO_PINS[index]) if index is not None else None
                if want != extra:
                    problems.append(f"{target}: {port} is JP1 pin {want}, doc says {extra}")
            if not re.search(pull_re(port), pull_text):
                problems.append(f"{target}: {port} has no pull-up in {pull_file}")
    return problems


def test_the_pin_doc_equals_the_build():
    assert doc_problems(DOC.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("old, new", [
    ("| UART 13 | TX | JB1 |", "| UART 13 | TX | JB6 |"),          # inputs table vs the mix
    ("| JA0 | `ja[0]` | N15 | JA1_P |", "| JA0 | `ja[0]` | N16 | JA1_P |"),     # zybo package pin
    ("| JB0 | `jb[0]` | V8 | jb_p[1] |", "| JB0 | `jb[0]` | V8 | jb_n[1] |"),   # zybo net
    ("| JB2 | `GPIO_D[10]` | BU31 | 13 |", "| JB2 | `GPIO_D[10]` | BU31 | 11 |"),  # DE25 JP1 pin
    ("| JA7 | `GPIO_D[7]` | BP31 | 8 |", "| JA7 | `GPIO_D[6]` | BP31 | 8 |"),    # DE25 port
    ("| JB5 | `rpi_gpio16` | B15 | 36 |", "| JB5 | `rpi_gpio16` | B15 | 37 |"),  # PolarFire header pin
])
def test_a_drifted_row_is_caught(old, new):
    text = DOC.read_text(encoding="utf-8")
    assert old in text, f"fixture row no longer in the doc: {old}"
    assert doc_problems(text.replace(old, new)), f"the drift {old} -> {new} passed"


def test_a_missing_pull_up_is_caught(monkeypatch):
    real = Path.read_text

    def fake(self, *a, **k):
        text = real(self, *a, **k)
        if self.name == "pfsoc_header_pullups.pdc":
            text = re.sub(r"set_io -port_name \{rpi_gpio13\}[^\n]*\n", "", text)
        return text

    monkeypatch.setattr(Path, "read_text", fake)
    problems = doc_problems(DOC.read_text(encoding="utf-8"))
    assert any("rpi_gpio13 has no pull-up" in p for p in problems), problems
