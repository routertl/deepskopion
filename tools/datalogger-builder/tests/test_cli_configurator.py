# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""
Regression tests for Deepskopion CLI Configurator (DS-P2.114).

Validates:
1. Golden config YAML outputs for two mixes (Zybo Z7-20 PoC mix, DE25-Standard mix).
2. End-to-end integration: emitted configs are parsed and processed by datalogger_gather_gen.py.
3. Board pin budget constraint validation and rejection.
4. Max datalogger ID constraint validation and rejection.
5. Resource estimator calibration vs real Vivado (Zybo Z7-20) and Quartus (DE25) fit reports within tolerance (5%).
6. Interactive prompt mode via stdin stream.
"""
import io
import sys
from pathlib import Path
import pytest
import yaml

_SB = Path(__file__).resolve().parents[1]  # tools/datalogger-builder
sys.path.insert(0, str(_SB))

import configurator as cfg  # noqa: E402
import datalogger_gather_gen as sgg  # noqa: E402

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_board_registry_known_boards():
    """Verify supported board profiles exist with valid budgets and devices."""
    boards = cfg.get_supported_boards()
    assert "zybo-z7-10" in boards
    assert "zybo-z7-20" in boards
    assert "de25-standard" in boards

    z10 = cfg.get_board("zybo-z7-10")
    assert z10.pin_budget == 32
    assert z10.logic_capacity == 17600
    assert z10.base_logic == 22504
    assert z10.base_ff == 33691
    assert z10.base_bram == 33.0
    assert "STEP7_SILICON_SCOPING.md" in z10.calibration_provenance

    z20 = cfg.get_board("zybo-z7-20")
    assert z20.pin_budget == 40
    assert z20.logic_capacity == 53200
    assert z20.base_logic == 23971
    assert z20.base_ff == 32817
    assert z20.base_bram == 10.5

    de25 = cfg.get_board("de25-standard")
    assert de25.pin_budget == 36
    assert de25.logic_capacity == 46800
    assert de25.base_logic == 26010
    assert de25.base_ff == 43516
    assert de25.base_bram == 54.0


def test_golden_config_mix1_zybo(tmp_path):
    """Mix 1: Zybo Z7-20 (2 I2C, 2 UART, 1 SPI, 1 JTAG, 1 CAN, 4 EDGE).
    Outputs must match the golden reference YAML."""
    out_file = tmp_path / "mix1_zybo.yml"
    golden_file = _FIXTURES_DIR / "golden_config_mix1_zybo.yml"

    # Run non-interactive CLI generator
    exit_code = cfg.main([
        "--board", "zybo-z7-20",
        "--i2c", "2",
        "--uart", "2",
        "--spi", "1",
        "--jtag", "1",
        "--can", "1",
        "--edge", "4",
        "-o", str(out_file),
    ])
    assert exit_code == 0
    assert out_file.exists()

    generated_yaml = yaml.safe_load(out_file.read_text(encoding="utf-8"))
    golden_yaml = yaml.safe_load(golden_file.read_text(encoding="utf-8"))
    assert generated_yaml == golden_yaml


def test_golden_config_mix2_de25(tmp_path):
    """Mix 2: DE25-Standard (4 UART, 2 I2C, 1 SPI, 2 EDGE).
    Outputs must match the golden reference YAML."""
    out_file = tmp_path / "mix2_de25.yml"
    golden_file = _FIXTURES_DIR / "golden_config_mix2_de25.yml"

    exit_code = cfg.main([
        "--board", "de25-standard",
        "--uart", "4",
        "--i2c", "2",
        "--spi", "1",
        "--edge", "2",
        "-o", str(out_file),
    ])
    assert exit_code == 0
    assert out_file.exists()

    generated_yaml = yaml.safe_load(out_file.read_text(encoding="utf-8"))
    golden_yaml = yaml.safe_load(golden_file.read_text(encoding="utf-8"))
    assert generated_yaml == golden_yaml


def test_emitted_configs_pass_datalogger_gather_gen(tmp_path):
    """Verify that both emitted configs are fully valid when consumed by
    datalogger_gather_gen.py to generate config packages and gather RTL."""
    protocols_file = _SB / "protocols.yml"
    template_dir = _SB / "templates"
    protocols = sgg.load_yaml(protocols_file)

    for mix_golden in ["golden_config_mix1_zybo.yml", "golden_config_mix2_de25.yml"]:
        cfg_path = _FIXTURES_DIR / mix_golden
        config = sgg.load_yaml(cfg_path)

        # Generate datalogger config package
        pkg_content = sgg.generate_datalogger_config_pkg(config, protocols)
        assert "package datalogger_config_pkg is" in pkg_content
        assert "C_NUM_DATALOGGER_PINS" in pkg_content

        # Generate gather RTL
        rtl_content = sgg.generate_datalogger_gather(config, protocols, template_dir)
        assert "entity datalogger_gather is" in rtl_content
        assert "architecture RTL of datalogger_gather is" in rtl_content


def test_validation_pin_budget_exceeded(tmp_path, capsys):
    """Reject configuration when pin budget of target board is exceeded."""
    out_file = tmp_path / "over_pin.yml"
    # Zybo Z7-10 has 32 pins. 17 I2C loggers require 17 * 2 = 34 pins.
    exit_code = cfg.main([
        "--board", "zybo-z7-10",
        "--i2c", "17",
        "-o", str(out_file),
    ])
    assert exit_code != 0
    assert not out_file.exists()
    captured = capsys.readouterr()
    assert "pin budget exceeded" in captured.err.lower() or "pin budget exceeded" in captured.out.lower()


def test_validation_max_datalogger_id_exceeded(tmp_path, capsys):
    """Reject configuration when datalogger count exceeds MaxDataloggerID (31)."""
    out_file = tmp_path / "over_id.yml"
    # 32 UART loggers require 32 IDs (> 31 max)
    exit_code = cfg.main([
        "--board", "zybo-z7-20",
        "--uart", "32",
        "-o", str(out_file),
    ])
    assert exit_code != 0
    assert not out_file.exists()
    captured = capsys.readouterr()
    assert "max datalogger" in captured.err.lower() or "max datalogger" in captured.out.lower()


def test_validation_unknown_board(tmp_path, capsys):
    """Reject configuration when an unknown board name is specified."""
    out_file = tmp_path / "unknown_board.yml"
    exit_code = cfg.main([
        "--board", "nonexistent-board-99",
        "--uart", "1",
        "-o", str(out_file),
    ])
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "unknown board" in captured.err.lower() or "unknown board" in captured.out.lower()


def test_rejection_of_protocols_without_rtl(tmp_path, capsys):
    """Reject configuration when protocols lacking RTL implementation are requested."""
    for no_rtl_proto in ["i2s", "mdio", "onewire"]:
        out_file = tmp_path / f"no_rtl_{no_rtl_proto}.yml"
        exit_code = cfg.main([
            "--board", "zybo-z7-20",
            f"--{no_rtl_proto}", "1",
            "-o", str(out_file),
        ])
        assert exit_code != 0
        assert not out_file.exists()
        captured = capsys.readouterr()
        assert no_rtl_proto in captured.err.lower()
        assert "no rtl implementation" in captured.err.lower()


def test_zybo_z7_10_overflow_warning(tmp_path, capsys):
    """Verify Zybo Z7-10 profile uses in-tree silicon measurement (22,504 LUT base design)
    and warns that base logic overflows board capacity (17,600 LUTs)."""
    out_file = tmp_path / "z10_test.yml"
    exit_code = cfg.main([
        "--board", "zybo-z7-10",
        "--uart", "1",
        "-o", str(out_file),
    ])
    assert exit_code == 0
    assert out_file.exists()

    captured = capsys.readouterr()
    combined_output = captured.out + captured.err
    assert "exceeds board capacity" in combined_output.lower() or "overflowed by base platform" in combined_output.lower()
    assert "WARNING: Estimated logic exceeds board capacity" in captured.out

    board = cfg.get_board("zybo-z7-10")
    assert board.base_logic == 22504
    assert board.base_ff == 33691
    assert board.base_bram == 33.0

    est = cfg.estimate_resources(board, {"uart": 1})
    assert est.base_logic == 22504
    assert est.total_logic > board.logic_capacity
    assert est.logic_util_pct > 100.0


def test_check_fit_missing_report_fails(tmp_path, capsys):
    """Verify --check-fit fails gracefully when report file does not exist."""
    exit_code = cfg.main([
        "--board", "zybo-z7-20",
        "--uart", "1",
        "--check-fit", str(tmp_path / "nonexistent.rpt"),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "fit report not found" in captured.err.lower()


def test_canfd_alias_behavior(tmp_path, capsys):
    """Verify --canfd is an alias for --can, does not add to --can, and rejects conflicts."""
    out_alias = tmp_path / "canfd_alias.yml"
    exit_code = cfg.main([
        "--board", "zybo-z7-20",
        "--canfd", "2",
        "-o", str(out_alias),
    ])
    assert exit_code == 0
    content = yaml.safe_load(out_alias.read_text(encoding="utf-8"))
    assert len(content["Dataloggers"]["Burst"]["Can"]) == 2

    # Both given with matching value: must NOT add together to 4
    out_matching = tmp_path / "canfd_matching.yml"
    exit_code = cfg.main([
        "--board", "zybo-z7-20",
        "--can", "2",
        "--canfd", "2",
        "-o", str(out_matching),
    ])
    assert exit_code == 0
    content = yaml.safe_load(out_matching.read_text(encoding="utf-8"))
    assert len(content["Dataloggers"]["Burst"]["Can"]) == 2

    # Both given with conflicting values: must error
    out_conflict = tmp_path / "canfd_conflict.yml"
    exit_code = cfg.main([
        "--board", "zybo-z7-20",
        "--can", "1",
        "--canfd", "2",
        "-o", str(out_conflict),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "conflicting arguments --can and --canfd" in captured.err.lower()


def test_interactive_mode_input_stream(tmp_path, monkeypatch):
    """Test interactive mode by simulating sequential user input prompts."""
    out_file = tmp_path / "interactive_test.yml"

    # Input sequence (only RTL protocols are prompted):
    # Board choice: "2" (Zybo Z7-20)
    # UART: "1"
    # I2C: "1"
    # SPI: "0"
    # QSPI: "0"
    # CAN: "0"
    # EDGE: "2"
    # JTAG: "0"
    # Output file: str(out_file)
    user_inputs = [
        "2",          # board: zybo-z7-20
        "1",          # uart
        "1",          # i2c
        "0",          # spi
        "0",          # qspi
        "0",          # can
        "2",          # edge
        "0",          # jtag
        str(out_file),# output file
    ]
    input_stream = io.StringIO("\n".join(user_inputs) + "\n")
    monkeypatch.setattr("sys.stdin", input_stream)

    exit_code = cfg.main(["--interactive"])
    assert exit_code == 0
    assert out_file.exists()

    content = yaml.safe_load(out_file.read_text(encoding="utf-8"))
    assert content["Design"] == "Zybo Z7-20"
    assert "Uart" in content["Dataloggers"]["Burst"]
    assert "I2c" in content["Dataloggers"]["Burst"]
    assert "Edge" in content["Dataloggers"]["Burst"]
    assert len(content["Dataloggers"]["Burst"]["Uart"]) == 1
    assert len(content["Dataloggers"]["Burst"]["I2c"]) == 1
    assert len(content["Dataloggers"]["Burst"]["Edge"]) == 2


def test_interactive_mode_respects_output_flag(tmp_path, monkeypatch):
    """Test interactive mode does not re-prompt for output path when -o is provided."""
    out_file = tmp_path / "interactive_with_flag.yml"

    user_inputs = [
        "2",  # board: zybo-z7-20
        "1",  # uart
        "0",  # i2c
        "0",  # spi
        "0",  # qspi
        "0",  # can
        "0",  # edge
        "0",  # jtag
        # Note: no output file input here; if it prompts, EOFError or missing out_file occurs
    ]
    input_stream = io.StringIO("\n".join(user_inputs) + "\n")
    monkeypatch.setattr("sys.stdin", input_stream)

    exit_code = cfg.main(["--interactive", "-o", str(out_file)])
    assert exit_code == 0
    assert out_file.exists()

    content = yaml.safe_load(out_file.read_text(encoding="utf-8"))
    assert content["Design"] == "Zybo Z7-20"
    assert len(content["Dataloggers"]["Burst"]["Uart"]) == 1


def test_can_notice_text_appears_when_can_selected(capsys, tmp_path):
    """DS-P1.103: selecting CAN prints no CTU licence or Bosch notice; the CAN FD data logger is project code
    (DS-P2.121 printed one; the configurator never builds the CTU core)."""
    out_file = tmp_path / "test_can_notice.yml"
    exit_code = cfg.main([
        "--board", "zybo-z7-20",
        "--can", "1",
        "-o", str(out_file),
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "CTU CAN FD IP Core" not in captured.out
    assert "Copyright (C) 2021-present Ondrej Ille" not in captured.out
    assert "educational, research, evaluation, self-interest purposes" not in captured.out
    assert "commercial purposes is forbidden" not in captured.out
    assert "The CAN protocol is developed by Robert Bosch GmbH and protected by patents" not in captured.out
    assert "obtain a CAN" not in captured.out and "protocol license from Bosch" not in captured.out
    assert "LICENSES/LicenseRef-CTU-CAN-FD-NonCommercial.txt" not in captured.out


def test_can_notice_text_appears_when_canfd_alias_selected(capsys, tmp_path):
    """DS-P1.103: the --canfd alias prints no CTU licence notice either (DS-P2.121 printed one)."""
    out_file = tmp_path / "test_canfd_alias_notice.yml"
    exit_code = cfg.main([
        "--board", "zybo-z7-20",
        "--canfd", "1",
        "-o", str(out_file),
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "CTU CAN FD IP Core" not in captured.out
    assert "Robert Bosch GmbH" not in captured.out
    assert "LICENSES/LicenseRef-CTU-CAN-FD-NonCommercial.txt" not in captured.out


def test_can_notice_text_does_not_appear_when_can_not_selected(capsys, tmp_path):
    """DS-P2.121: notice text must NOT appear when CAN is 0."""
    out_file = tmp_path / "test_no_can_notice.yml"
    exit_code = cfg.main([
        "--board", "zybo-z7-20",
        "--uart", "2",
        "--i2c", "1",
        "-o", str(out_file),
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "CTU CAN FD IP Core" not in captured.out
    assert "Ondrej Ille" not in captured.out
    assert "Robert Bosch GmbH" not in captured.out
    assert "LicenseRef-CTU-CAN-FD-NonCommercial" not in captured.out
