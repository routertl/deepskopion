"""DS-P2.322: the test-only JTAG gather fixture stays equal to the generator and to the product mix.

The DS-P1.104 default mix has no JTAG logger, so test_datalogger_gather_jtag (DS-P3.99) compiles a test-only
configuration: tools/sim/fixtures/gather_jtag/datalogger_config.yml (the default mix plus JTAG0, device id 16,
logical pins 24-27) with its generated datalogger_config_pkg.vhd and datalogger_gather.vhd frozen beside it. The
fixture lives outside the hw/ source walk, so its datalogger_gather entity never collides with the product one.

This test holds three things:
- the frozen pair equals the generator's output for the fixture yml (a stale pair, or a yml edited without
  regenerating, is red);
- the fixture's mix is the product mix plus exactly JTAG0 (a product mix change without a fixture refresh is red);
- JTAG0 is id 16 on pins 24-27, the values test_datalogger_gather_jtag hard-codes.
Refresh: python3 tools/datalogger-builder/datalogger_gather_gen.py tools/sim/fixtures/gather_jtag/datalogger_config.yml
  --pkg-dir tools/sim/fixtures/gather_jtag --rtl-dir tools/sim/fixtures/gather_jtag
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tools" / "sim" / "fixtures" / "gather_jtag"
FIXTURE_YML = FIXTURE / "datalogger_config.yml"
PRODUCT_YML = ROOT / "tools" / "datalogger-builder" / "config" / "datalogger_config.yml"
BUILDER = ROOT / "tools" / "datalogger-builder"
sys.path.insert(0, str(BUILDER))
import datalogger_gather_gen as dgg  # noqa: E402

JTAG0 = {16: {"TCK": 24, "TMS": 25, "TDI": 26, "TDO": 27}}


def _generate(config: dict) -> tuple[str, str]:
    protocols = dgg.load_yaml(BUILDER / "protocols.yml")
    pkg = dgg.generate_datalogger_config_pkg(config, protocols, dgg.load_board_pins(None))
    gather = dgg.generate_datalogger_gather(config, protocols, BUILDER / "templates")
    return pkg, gather


def test_frozen_pair_equals_the_generator_output() -> None:
    pkg, gather = _generate(dgg.load_yaml(FIXTURE_YML))
    assert (FIXTURE / "datalogger_config_pkg.vhd").read_text(encoding="utf-8") == pkg, (
        "fixture datalogger_config_pkg.vhd is stale: regenerate it (see this module's docstring)")
    assert (FIXTURE / "datalogger_gather.vhd").read_text(encoding="utf-8") == gather, (
        "fixture datalogger_gather.vhd is stale: regenerate it (see this module's docstring)")


def test_a_fixture_yml_edit_changes_the_generated_pair() -> None:
    """Red-proof for the freshness check: moving JTAG0 TDO changes the package the check compares."""
    config = dgg.load_yaml(FIXTURE_YML)
    moved = copy.deepcopy(config)
    moved["Dataloggers"]["Burst"]["Jtag"][16]["TDO"] = 29
    assert _generate(moved)[0] != _generate(config)[0]


def test_fixture_mix_is_the_product_mix_plus_jtag0() -> None:
    fixture = yaml.safe_load(FIXTURE_YML.read_text(encoding="utf-8"))
    product = yaml.safe_load(PRODUCT_YML.read_text(encoding="utf-8"))
    burst = dict(fixture["Dataloggers"]["Burst"])
    assert burst.pop("Jtag") == JTAG0
    assert burst == product["Dataloggers"]["Burst"], "the product mix changed: refresh the gather_jtag fixture"
    assert "Jtag" not in product["Dataloggers"]["Burst"], "the product mix has a JTAG logger again: drop the fixture"
    constraints = dict(fixture["Constraints"])
    assert constraints.pop("AllowUnwiredPins") is True
    assert constraints == product["Constraints"]


def test_fixture_package_places_jtag0_where_the_test_expects() -> None:
    pkg = (FIXTURE / "datalogger_config_pkg.vhd").read_text(encoding="utf-8")
    got = dict(re.findall(r"constant (C_JTAG0_(?:ID|TCK|TMS|TDI|TDO))\s*:\s*\w+\s*:=\s*(\d+);", pkg))
    assert got == {"C_JTAG0_ID": "16", "C_JTAG0_TCK": "24", "C_JTAG0_TMS": "25",
                   "C_JTAG0_TDI": "26", "C_JTAG0_TDO": "27"}
