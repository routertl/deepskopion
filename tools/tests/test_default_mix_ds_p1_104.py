# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.104: the three products ship one default data logger mix, every input on an external header pin.

Owner ruling 2026-11-13 and its addenda: the default is 2 UART, 2 I2C, 1 SPI, 1 CAN FD and 3 EDGE data
loggers (14 inputs), no JTAG, one shared mix file read by zybo_datalogger, de25_datalogger and
pfsoc_datalogger, with the final pin table

    I2C#4 JA0/JA1, I2C#5 JA2/JA3, SPI#8 JA4-JA7, UART#12 JB0, UART#13 JB1,
    EDGE#1 JB2, EDGE#2 JB3, EDGE#3 JB4, CAN#20 RX JB5.

JA0..JB7 are deepskopion_core's board pins (tools/datalogger-builder/board_pins.yml). Each product top
wires them to package pins of its own header: the Zybo PMOD ja/jb ports, the DE25 GPIO_D bus (JP1), the
PolarFire Raspberry Pi GPIOs. These tests read the mix through the generator that builds it
(datalogger_gather_gen.allocate_board_pins), hard-code the expected table, and check every board's wiring.

The CAN FD data logger is project code; the CTU CAN FD controller is not in any product build. The
products bind only the project's own can_top_apb stub into library ctu_can_fd_rtl (deepskopion_core names
that library; removing it is DS-P2.288).
"""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "datalogger-builder"))
sys.path.insert(0, str(ROOT / "tools"))
import datalogger_gather_gen as gen  # noqa: E402
from target_files import product_target_files  # noqa: E402

MIX = ROOT / "tools" / "datalogger-builder" / "config" / "datalogger_config.yml"
STUB = ROOT / "tools" / "sim" / "vendor_stubs" / "ctu_can_fd" / "can_top_apb_stub.vhd"

#: (PROTO, data logger id, SIGNAL) -> board pin: the owner's final table.
EXPECTED = {
    ("I2C", 4, "SCL"): "JA0", ("I2C", 4, "SDA"): "JA1",
    ("I2C", 5, "SCL"): "JA2", ("I2C", 5, "SDA"): "JA3",
    ("SPI", 8, "CS"): "JA4", ("SPI", 8, "MOSI"): "JA5", ("SPI", 8, "MISO"): "JA6", ("SPI", 8, "SCLK"): "JA7",
    ("UART", 12, "TX"): "JB0", ("UART", 13, "TX"): "JB1",
    ("EDGE", 1, "LINE"): "JB2", ("EDGE", 2, "LINE"): "JB3", ("EDGE", 3, "LINE"): "JB4",
    ("CAN", 20, "RX"): "JB5",
}

#: Board pin -> the package pin each product top wires it to (hardware.pins names).
PACKAGE_PIN = {
    "zybo_datalogger": {**{f"JA{i}": f"ja[{i}]" for i in range(8)},
                        **{f"JB{i}": f"jb[{i}]" for i in range(8)}},
    "de25_datalogger": {**{f"JA{i}": f"GPIO_D[{i}]" for i in range(8)},
                        **{f"JB{i}": f"GPIO_D[{8 + i}]" for i in range(8)}},
    "pfsoc_datalogger": dict(zip(
        [f"JA{i}" for i in range(8)] + [f"JB{i}" for i in range(6)],
        ["rpi_gpio4", "rpi_gpio17", "rpi_gpio18", "rpi_gpio27", "rpi_gpio22", "rpi_gpio23",
         "rpi_gpio24", "rpi_gpio25", "rpi_gpio5", "rpi_gpio6", "rpi_gpio12", "rpi_gpio13",
         "rpi_gpio19", "rpi_gpio16"])),
}

#: The text in each product top that wires the core's ja/jb to those package pins.
TOP_WIRING = {
    "zybo_datalogger": ("hw/rtl/deepskopion_top.vhd", ["ja => ja, jb => jb"]),
    "de25_datalogger": ("hw/rtl/de25/deepskopion_de25_top.vhd",
                        ["ja => GPIO_D(7 downto 0), jb => GPIO_D(15 downto 8)"]),
    "pfsoc_datalogger": ("hw/rtl/pfsoc/deepskopion_pfsoc_fabric.vhd", [
        "ja(0) => rpi_gpio4, ja(1) => rpi_gpio17, ja(2) => rpi_gpio18, ja(3) => rpi_gpio27,",
        "ja(4) => rpi_gpio22, ja(5) => rpi_gpio23, ja(6) => rpi_gpio24, ja(7) => rpi_gpio25,",
        "jb(0) => rpi_gpio5, jb(1) => rpi_gpio6, jb(2) => rpi_gpio12, jb(3) => rpi_gpio13,",
        "jb(4) => rpi_gpio19, jb(5) => rpi_gpio16,",
    ]),
}

PROJECT_LICENCES = {"MIT", "CERN-OHL-W-2.0", "MPL-2.0", "LicenseRef-RouteRTL-1.0"}


def _norm(text: str) -> str:
    return " ".join(text.split())


def _mix() -> dict:
    return gen.load_yaml(MIX)


def mix_problems(config: dict) -> list[str]:
    """Why ``config`` is not the shipped default with every input on its own external pin."""
    board_pins = {bp["name"]: bp for bp in gen.load_board_pins()}
    try:
        mapping, unwired = gen.allocate_board_pins(config)
    except ValueError as exc:
        return [f"the generator refuses the mix: {exc}"]
    problems = [f"unwired: {p} id {i} {s}" for p, i, s, _ in unwired]
    signals = {(p, i, s): mapping.get(pin) for p, i, s, pin in gen.configured_signals(config)}
    if set(signals) != set(EXPECTED):
        problems.append(f"inputs {sorted(set(signals) ^ set(EXPECTED))} differ from the default")
    for key, pin in signals.items():
        if pin is not None and not board_pins[pin].get("general"):
            problems.append(f"{key} sits on {pin}, an internal tap, not an external pin")
        if key in EXPECTED and pin != EXPECTED[key]:
            problems.append(f"{key} is on {pin}, the table says {EXPECTED[key]}")
    used = [p for p in signals.values() if p is not None]
    for pin in sorted({p for p in used if used.count(p) > 1}):
        problems.append(f"{pin} carries more than one input")
    return problems


def test_the_three_products_read_one_shared_mix():
    products = {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in product_target_files(ROOT)}
    assert set(products) == set(PACKAGE_PIN)
    for name, doc in products.items():
        assert doc.get("system_config") == "tools/datalogger-builder/config/datalogger_config.yml", name


def test_the_shared_mix_is_the_default_on_its_table():
    assert mix_problems(_mix()) == []


@pytest.mark.parametrize("target", sorted(PACKAGE_PIN))
def test_every_default_input_reaches_its_own_package_pin(target):
    doc = yaml.safe_load((ROOT / "targets" / f"{target}.yml").read_text(encoding="utf-8"))
    package_pins = set((doc.get("hardware") or {}).get("pins") or [])
    wanted = [PACKAGE_PIN[target][pin] for pin in EXPECTED.values()]
    assert len(set(wanted)) == len(wanted), f"{target}: two inputs share a package pin"
    missing = [p for p in wanted if p not in package_pins]
    assert not missing, f"{target}: {missing} are not package pins of the target"
    top_file, lines = TOP_WIRING[target]
    top = _norm((ROOT / top_file).read_text(encoding="utf-8"))
    for line in lines:
        assert _norm(line) in top, f"{top_file} no longer wires: {line}"


def test_an_input_moved_to_an_internal_tap_is_refused():
    cfg = copy.deepcopy(_mix())
    can_pin = next(pin for p, i, s, pin in gen.configured_signals(cfg) if (p, i, s) == ("CAN", 20, "RX"))
    cfg.setdefault("BoardPins", {})[can_pin] = "CAN_BUS"
    problems = mix_problems(cfg)
    assert any("internal tap" in p for p in problems), problems


def test_two_inputs_on_one_pin_are_refused():
    cfg = copy.deepcopy(_mix())
    pins = {(p, i, s): pin for p, i, s, pin in gen.configured_signals(cfg)}
    board = cfg.setdefault("BoardPins", {})
    board[pins[("UART", 12, "TX")]] = "JB0"
    board[pins[("UART", 13, "TX")]] = "JB0"
    problems = mix_problems(cfg)
    assert problems and any("JB0" in p for p in problems), problems


@pytest.mark.parametrize("target", sorted(PACKAGE_PIN))
def test_no_ctu_package_in_the_product_build(target):
    lock = (ROOT / "targets" / f"{target}.routertl.lock").read_text(encoding="utf-8")
    assert "ctu-fee" not in lock and "ctucanfd_ip_core" not in lock, target
    doc = (ROOT / "targets" / f"{target}.yml").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in doc.splitlines())
    assert not re.search(r"ctucanfd|ctu,can", code, re.I), f"{target} carries a ctucanfd device-tree piece"


def test_the_ctu_named_stub_is_project_code():
    text = STUB.read_text(encoding="utf-8")
    m = re.search(r"SPDX-License-Identifier:\s*(\S+)", text)
    assert m and m.group(1) in PROJECT_LICENCES, m and m.group(1)
    assert not re.search(r"Czech Technical|Ondrej Ille|Jerabek|Copyright|NonCommercial", text, re.I)
    reuse = (ROOT / "REUSE.toml").read_text(encoding="utf-8")
    assert "vendor_stubs" not in reuse, "a REUSE annotation relicenses the stub"
