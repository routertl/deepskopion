# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.332: a configurator-written mix puts CAN RX on an external header pin, never on the internal CAN_BUS tap.

CAN_BUS is deepskopion_core's fabric wired-AND of the on-chip CAN controllers' TX
(deepskopion_core.vhd: can_bus_w <= can0_phy_tx_w and can1_phy_tx_w and ...), a self-test
source. The shipped default mix (DS-P1.104, config/datalogger_config.yml) wires CAN#20 RX to
the external header pin JB5, like every other input. The configurator used to leave CAN RX to
the generator's preference, which is CAN_BUS, so a user-generated mix captured the internal
bus instead of the user's CAN transceiver, and counted 13 header pins for the 14-input default.

Each check runs the written file through the build's own allocator
(datalogger_gather_gen.allocate_board_pins with board_pins.yml), which is what the
pre_build hook does with the mix.
"""
import pytest
import yaml

from test_cli_configurator import _FIXTURES_DIR  # noqa: F401  (puts tools/datalogger-builder on sys.path)

import configurator as cfg  # noqa: E402
import datalogger_gather_gen as dgg  # noqa: E402

DEFAULT_MIX = ["--uart", "2", "--i2c", "2", "--spi", "1", "--canfd", "1", "--edge", "3"]
BOARDS = ["zybo-z7-20", "de25-standard", "polarfire-soc-discovery"]


def _written(tmp_path, board_id, args):
    out_file = tmp_path / f"{board_id}.yml"
    assert cfg.main(["--board", board_id, *args, "-o", str(out_file)]) == 0
    return yaml.safe_load(out_file.read_text(encoding="utf-8"))


@pytest.mark.parametrize("board_id", BOARDS)
def test_can_rx_of_a_written_mix_lands_on_an_external_header_pin(tmp_path, board_id):
    config = _written(tmp_path, board_id, DEFAULT_MIX)
    pins = {bp["name"]: bp for bp in dgg.load_board_pins()}
    mapping, unwired = dgg.allocate_board_pins(config)
    assert unwired == []
    can = [mapping[pin] for proto, _id, _sig, pin in dgg.configured_signals(config) if proto == "CAN"]
    assert len(can) == 1
    assert can[0] != "CAN_BUS"
    assert pins[can[0]].get("general") is True
    assert can[0] not in cfg.get_board(board_id).unwired_core_pins
    on_taps = [mapping[pin] for _p, _i, _s, pin in dgg.configured_signals(config)
               if not pins[mapping[pin]].get("general")]
    assert on_taps == []


def test_the_pfsoc_default_mix_takes_all_fourteen_header_pins(tmp_path, capsys):
    _written(tmp_path, "polarfire-soc-discovery", DEFAULT_MIX)
    out = capsys.readouterr().out
    assert "Pin Budget Usage          : 14 / 14 (100.0%)" in out
    assert "-> CAN_BUS" not in out
    assert cfg.header_pin_demand(cfg.get_board("polarfire-soc-discovery"),
                                 {"uart": 2, "i2c": 2, "spi": 1, "can": 1, "edge": 3})[0] == 14


def test_a_fifteenth_input_with_can_is_refused_on_pfsoc(tmp_path, capsys):
    out_file = tmp_path / "pfsoc_15.yml"
    assert cfg.main(["--board", "polarfire-soc-discovery", "--uart", "2", "--i2c", "2", "--spi", "1",
                     "--canfd", "1", "--edge", "4", "-o", str(out_file)]) != 0
    assert not out_file.exists()
    assert "requested 15 pins, but board only has 14 pins available" in capsys.readouterr().err
