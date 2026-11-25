# rr unknown-llm (AI-generated)
"""DS-P3.203 / DS-P3.204: a JTAG datalogger reads the REA tap, not header pins.

board_pins.yml routes JTAG.TCK/TMS/TDI/TDO to the internal REA_* capture
sources (general: false) on every board, and deepskopion_core feeds those
from rea_tap_*_i. So the first JTAG datalogger takes no header pin; the pin
budget is checked on the generator's own allocation. Where a board's top ties
rea_tap_*_i to constants (DE25, PolarFire SoC), the configurator warns that a
JTAG datalogger there records no live traffic.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import configurator as cfg  # noqa: E402

ROOT = HERE.parents[2]
BUILT_PFSOC_MIX = {"uart": 2, "i2c": 2, "spi": 1, "jtag": 1, "edge": 4}


def test_a_jtag_datalogger_takes_no_header_pin_on_any_board():
    for board_id in ("zybo-z7-20", "de25-standard", "polarfire-soc-discovery"):
        header, unwired = cfg.header_pin_demand(cfg.BOARDS[board_id], {"jtag": 1})
        assert (header, unwired) == (0, []), board_id


def test_a_second_jtag_datalogger_takes_four_header_pins():
    header, unwired = cfg.header_pin_demand(cfg.BOARDS["zybo-z7-20"], {"jtag": 2})
    assert (header, unwired) == (4, [])


def test_the_mix_built_for_pfsoc_passes_its_own_pin_check():
    ok, errors, _warnings = cfg.validate_config(cfg.BOARDS["polarfire-soc-discovery"],
                                                BUILT_PFSOC_MIX)
    assert ok, errors
    assert cfg.header_pin_demand(cfg.BOARDS["polarfire-soc-discovery"], BUILT_PFSOC_MIX)[0] == 14


def test_the_report_counts_header_pins(tmp_path, capsys):
    assert cfg.main(["--board", "polarfire-soc-discovery", "--uart", "2", "--i2c", "2",
                     "--spi", "1", "--jtag", "1", "--edge", "4",
                     "-o", str(tmp_path / "pfsoc.yml")]) == 0
    assert "Pin Budget Usage          : 14 / 14 (100.0%)" in capsys.readouterr().out


@pytest.mark.parametrize("board_id,top_line", [
    ("de25-standard", "hw/rtl/de25/deepskopion_de25_top.vhd:823"),
    ("polarfire-soc-discovery", "hw/rtl/pfsoc/deepskopion_pfsoc_fabric.vhd:794"),
])
def test_a_jtag_datalogger_on_a_tied_tap_is_flagged(board_id, top_line):
    board = cfg.BOARDS[board_id]
    assert board.rea_tap_tied == top_line
    path, line = top_line.rsplit(":", 1)
    text = (ROOT / path).read_text(encoding="utf-8").splitlines()[int(line) - 1]
    assert "rea_tap_tck_i => '0'" in text, (top_line, text)
    ok, _errors, warnings = cfg.validate_config(board, {"jtag": 1})
    assert ok
    assert any(top_line in w and "no live JTAG traffic" in w for w in warnings), warnings


def test_a_live_tap_is_not_flagged():
    board = cfg.BOARDS["zybo-z7-20"]
    assert board.rea_tap_tied is None
    top = (ROOT / "hw/rtl/deepskopion_top.vhd").read_text(encoding="utf-8")
    assert "rea_tap_tck_i => rea_tap_tck" in top
    _ok, _errors, warnings = cfg.validate_config(board, {"jtag": 1})
    assert not any("REA tap" in w for w in warnings), warnings
