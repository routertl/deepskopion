# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.66 — the generator owns the logical-pin -> board-pin map.

configurator.py numbers a mix's logical capture pins 0..N. Before DS-P1.66
deepskopion_core drove only the hard-wired capture_pin(12..15, 20..28, 36, 37,
42..45), so a configurator mix built, elaborated, and then read constant '0' on
every other pin. Now datalogger_gather_gen.py gives every configured signal a
core capture source (board_pins.yml) and emits the map as C_CAPTURE_BOARD_PIN;
the core routes capture_pin from it.

  - the core wires the pins a configurator mix numbers (sim, frozen package)
  - the default mix keeps its pre-DS-P1.66 wiring (sim, live generator)
  - the core reaches the internal taps (JTAG on REA, CAN bus) (sim, live)
  - the generated package maps every configured signal (generator CLI)
  - the configurator refuses a mix the core cannot wire (configurator CLI)
  - the generator refuses a config the core cannot wire (generator CLI)
"""

from __future__ import annotations

import re
import subprocess
import sys

import pytest
import yaml

import datalogger_mix_bench as bench

BUILDER = bench.ROOT / "tools" / "datalogger-builder"
GENERATOR = BUILDER / "datalogger_gather_gen.py"
sys.path.insert(0, str(BUILDER))
import configurator as cfg  # noqa: E402

# The ticket's mixes, plus one that takes the internal JTAG / CAN taps.
CONFIGURATOR_MIXES = {
    "readme_example": ["--i2c", "2", "--uart", "2", "--spi", "1", "--edge", "4"],
    "mix_5uart_2i2c": ["--uart", "5", "--i2c", "2"],
    "mix_0uart_5i2c_1spi": ["--i2c", "5", "--spi", "1"],
    "taps": ["--i2c", "2", "--uart", "2", "--spi", "1", "--jtag", "1", "--can", "1",
             "--edge", "4"],
}


@pytest.fixture(scope="module")
def synced_workspace():
    yield from bench.workspace_synced()


def _configured_pins(config: dict) -> dict[int, str]:
    used: dict[int, str] = {}
    for proto, instances in config["Dataloggers"]["Burst"].items():
        for datalogger_id, pin_map in instances.items():
            for signal, pin in pin_map.items():
                used.setdefault(int(pin), f"{proto.upper()} id {datalogger_id} {signal}")
    return used


def _declared_map(pkg_text: str) -> dict[int, str]:
    """{logical pin: board pin} from a generated package's C_CAPTURE_BOARD_PIN."""
    block = re.search(r"constant C_CAPTURE_BOARD_PIN\b.*?;", pkg_text, re.S)
    if not block:
        return {}
    return {int(pin): name for pin, name in
            re.findall(r"(\d+)\s*=>\s*C_BOARD_PIN_(\w+)", block.group(0))
            if name != "NONE"}


def _run_generator(config_path, out_dir) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GENERATOR), str(config_path),
         "--pkg-dir", str(out_dir / "pkg"), "--rtl-dir", str(out_dir / "rtl")],
        cwd=bench.ROOT, text=True, capture_output=True, timeout=60)


def test_core_wires_configurator_pins(synced_workspace):
    """deepskopion_core gen_capture_pin: every logical pin of the README's
    configurator mix (0..13) reaches exactly one core input, as the package
    declares. Frozen package: only the core's capture_pin assembly is tested."""
    failure = bench.bench_failure("pinmap_configurator")
    assert failure is None, failure


def test_default_mix_wiring_unchanged(synced_workspace):
    """The project's datalogger_config.yml, through the live generator, is wired
    exactly as the hard-wired core wired it (I2C JA0-3, SPI JA4-7, UART JB0-1,
    EDGE JB2-5, JTAG on the REA tap)."""
    failure = bench.bench_failure("pinmap_default")
    assert failure is None, failure


def test_core_wires_internal_taps(synced_workspace):
    """A configurator mix with JTAG and CAN: JTAG takes the REA tap and CAN the
    fabric CAN bus, every other signal a PMOD pin."""
    failure = bench.bench_failure("pinmap_taps")
    assert failure is None, failure


def test_generator_maps_every_configured_pin(tmp_path):
    """configurator.py -> datalogger_gather_gen.py, both as a user runs them:
    the generated package wires every configured signal to its own board pin."""
    for mix, flags in CONFIGURATOR_MIXES.items():
        config_path = tmp_path / f"{mix}.yml"
        assert cfg.main(["--board", "zybo-z7-20", "--quiet", *flags,
                         "-o", str(config_path)]) == 0, f"configurator refused {mix}"
        gen = _run_generator(config_path, tmp_path / mix)
        assert gen.returncode == 0, f"generator failed on {mix}:\n{gen.stdout}\n{gen.stderr}"
        declared = _declared_map(
            (tmp_path / mix / "pkg" / "datalogger_config_pkg.vhd").read_text(encoding="utf-8"))
        configured = sorted(_configured_pins(yaml.safe_load(config_path.read_text())).items())
        for pin, who in configured:
            assert pin in declared, (
                f"{mix}: logical capture pin {pin} ({who}) is wired to no board pin "
                f"in the generated package (declared map: {declared})")
        # board_pins.yml is read only once every pin is mapped, so a missing map
        # fails on the map, not on reading the pin catalogue.
        board_pins = {bp["name"] for bp in yaml.safe_load(
            (BUILDER / "board_pins.yml").read_text(encoding="utf-8"))["board_pins"]}
        for pin, _who in configured:
            assert declared[pin] in board_pins, (
                f"{mix}: logical pin {pin} names unknown board pin {declared[pin]}")
        assert len(set(declared.values())) == len(declared), (
            f"{mix}: two logical pins share a board pin: {declared}")


def test_configurator_refuses_a_mix_the_core_cannot_wire(tmp_path, capsys):
    """20 EDGE lines fit the Zybo Z7-20 pin budget (40) and the ID budget (31),
    but deepskopion_core has 16 general capture sources: the configurator must
    refuse rather than emit a config whose extra pins read '0'."""
    out_file = tmp_path / "edge20.yml"
    code = cfg.main(["--board", "zybo-z7-20", "--quiet", "--edge", "20", "-o", str(out_file)])
    captured = capsys.readouterr()
    assert code != 0 and not out_file.exists(), (
        f"configurator accepted a mix deepskopion_core cannot wire (exit {code}, "
        f"wrote {out_file.exists()}): 20 EDGE lines for 16 general capture sources")
    assert "capture source" in captured.err.lower(), captured.err


def test_generator_refuses_unwired_signals(tmp_path):
    """A hand-written config with 17 EDGE lines: the generator refuses it and
    writes nothing, unless Constraints.AllowUnwiredPins opts in."""
    config = {
        "Design": "over_capacity",
        "Constraints": {"NumDataloggerPins": 64, "MaxDataloggerID": 31},
        "Dataloggers": {"Burst": {"Edge": {i + 1: {"LINE": i} for i in range(17)}}},
    }
    config_path = tmp_path / "edge17.yml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    gen = _run_generator(config_path, tmp_path / "refused")
    pkg = tmp_path / "refused" / "pkg" / "datalogger_config_pkg.vhd"
    assert gen.returncode != 0 and not pkg.exists(), (
        f"generator wrote a package with unwired capture pins (exit {gen.returncode}):\n"
        f"{gen.stdout}\n{gen.stderr}")
    assert "EDGE id 17 LINE (logical pin 16)" in gen.stderr, gen.stderr

    config["Constraints"]["AllowUnwiredPins"] = True
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    gen = _run_generator(config_path, tmp_path / "allowed")
    assert gen.returncode == 0, gen.stderr
    pkg_text = (tmp_path / "allowed" / "pkg" / "datalogger_config_pkg.vhd").read_text()
    assert "UNWIRED (AllowUnwiredPins): EDGE id 17 LINE, logical pin 16" in pkg_text
