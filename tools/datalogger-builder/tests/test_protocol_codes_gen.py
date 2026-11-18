"""Drift-guard for the single-sourced decoder codes (DS-P3.59).

protocol_codes.py must stay generated-in-sync with the *_datalogger_protocol.yml
contracts, and the well-known codes must survive the round-trip.
"""
import sys
from pathlib import Path

_SB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SB))
import protocol_codes_gen as pcg  # noqa: E402

_ROOT = pcg._repo_root(_SB)


def test_generated_file_in_sync_with_yaml():
    # The committed sw/stream/protocol_codes.py must equal a fresh render.
    assert pcg.check(_ROOT) == 0


def test_render_matches_committed():
    rendered = pcg._render(_ROOT)
    committed = (_ROOT / pcg._GEN_PATH).read_text(encoding="utf-8")
    assert rendered == committed


def test_known_codes_round_trip_from_yaml():
    # Re-parse the YAMLs directly and assert the anchor codes the decoders rely
    # on — a typo in either the YAML or the generator fails here.
    i2c_t, i2c_e = pcg._load(_ROOT, "i2c")
    spi_t, spi_e = pcg._load(_ROOT, "qspi")
    jtag_t, jtag_e = pcg._load(_ROOT, "jtag")
    assert i2c_t["event"] == 0b100 and i2c_t["payload"] == 0b00
    assert {v for v in i2c_e.values()} >= {"start", "stop", "repeated_start"}
    assert 0x09 in i2c_e and i2c_e[0x09] == "start"
    assert spi_t["payload_a"] == 0b001 and spi_t["payload_b"] == 0b010
    assert spi_e[0x09] == "cs_down" and spi_e[0x0A] == "cs_up"
    assert jtag_t["event"] == 0b100
    assert jtag_e[0x0C] == "tap_reset" and jtag_e[0x10] == "ir_shift_begin"


def test_decoders_consume_the_generated_codes():
    # The decoders must reference the generated table, not a private copy.
    sys.path.insert(0, str(_ROOT))
    from sw.stream import decoders, protocol_codes
    assert decoders.I2C_T_EVENT == protocol_codes.I2C_TYPES["event"]
    assert decoders.SPI_T_MISO == protocol_codes.SPI_TYPES["payload_b"]
    assert decoders.JTAG_EV_TAP_RESET == 0x0C
