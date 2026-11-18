"""Unit tests for sw.stream.regbank (FakeRegbank + Regs offset map)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sw.stream.regbank import FakeRegbank, Regs

# tests/ -> stream/ -> sw/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_REGBANK_YAML = _REPO_ROOT / "project_regbank.yml"


def _yaml_name_to_addr() -> dict[str, int]:
    """{register name -> byte address} parsed from project_regbank.yml."""
    doc = yaml.safe_load(_REGBANK_YAML.read_text())
    return {r["name"]: int(r["address"]) for r in doc["registers"]}


def _regs_name_to_addr() -> dict[str, int]:
    """{constant name -> address} from the Regs offset class (ints only)."""
    return {
        name: getattr(Regs, name)
        for name in vars(Regs)
        if not name.startswith("_") and isinstance(getattr(Regs, name), int)
    }


def test_fake_read_default_is_zero():
    rb = FakeRegbank()
    assert rb.read_u32(Regs.HEARTBEAT_CNT) == 0
    assert rb.read_u32(Regs.PKT_CNT_UART) == 0


def test_fake_read_preset_initial():
    rb = FakeRegbank({Regs.HEARTBEAT_CNT: 0xDEAD_BEEF})
    assert rb.read_u32(Regs.HEARTBEAT_CNT) == 0xDEAD_BEEF


def test_fake_write_then_read_roundtrip():
    rb = FakeRegbank()
    rb.write_u32(Regs.DATALOGGER_CONFIG, 0x1234)
    assert rb.read_u32(Regs.DATALOGGER_CONFIG) == 0x1234


def test_fake_write_masks_to_32_bits():
    rb = FakeRegbank()
    rb.write_u32(Regs.DATALOGGER_CONFIG, 0x1_DEAD_BEEF)
    assert rb.read_u32(Regs.DATALOGGER_CONFIG) == 0xDEAD_BEEF


def test_fake_writes_logged():
    rb = FakeRegbank()
    rb.write_u32(Regs.TELEMETRY_SNAPSHOT_TRIGGER, 1)
    rb.write_u32(Regs.TELEMETRY_CTRL, 1)
    assert rb.writes == [
        (Regs.TELEMETRY_SNAPSHOT_TRIGGER, 1),
        (Regs.TELEMETRY_CTRL, 1),
    ]


def test_fake_set_bypasses_writes_log():
    """set() is a test helper — should not contaminate the writes log."""
    rb = FakeRegbank()
    rb.set(Regs.HEARTBEAT_CNT, 0xABCD)
    assert rb.read_u32(Regs.HEARTBEAT_CNT) == 0xABCD
    assert rb.writes == []


def test_alignment_check_read():
    rb = FakeRegbank()
    with pytest.raises(ValueError, match="aligned"):
        rb.read_u32(0x01)


def test_alignment_check_write():
    rb = FakeRegbank()
    with pytest.raises(ValueError, match="aligned"):
        rb.write_u32(0x02, 0xDEAD)


def test_close_sets_flag():
    rb = FakeRegbank()
    assert rb.closed is False
    rb.close()
    assert rb.closed is True


def test_regs_constants_match_yaml_layout():
    """Spot-check a few constants against project_regbank.yml."""
    assert Regs.HEARTBEAT_CNT == 0x00C0
    assert Regs.FIFO_OVERFLOW_CNT == 0x00C4
    assert Regs.FIFO_OVERFLOW_CNT_RSVD == 0x00C8
    assert Regs.PKT_CNT_I2C == 0x00D0
    assert Regs.PKT_CNT_UART == 0x00D8
    assert Regs.ERR_CNT_I2C == 0x00F0
    assert Regs.TELEMETRY_SNAPSHOT_TRIGGER == 0x0144
    assert Regs.DATALOGGER_CONFIG == 0x0020


def test_regs_class_is_exhaustive_and_aligned_with_yaml():
    """DS-P3.117 drift gate: the SW Regs map and project_regbank.yml must agree.

    Every register declared in the YAML must have a Regs constant at the SAME
    byte address, and every Regs constant must name a real YAML register. This
    is the cross-language RTL(spec)<->SW guard that catches a slot added on one
    side but not the other — the exact failure mode the regression-guard rule
    targets. Strengthened from the old spot-check
    (test_regs_constants_match_yaml_layout) which only sampled 8.

    (The DS-P3.117 max-frequency datum is deliberately NOT a regbank slot — it
    travels in-band as EVT_EDGE_MAXFREQ — so it correctly has no Regs constant
    and no YAML register, and this exhaustive gate stays green.)
    """
    yaml_map = _yaml_name_to_addr()
    regs_map = _regs_name_to_addr()

    missing_in_regs = sorted(set(yaml_map) - set(regs_map))
    missing_in_yaml = sorted(set(regs_map) - set(yaml_map))
    mismatched = sorted(
        f"{n}: yaml=0x{yaml_map[n]:04X} regs=0x{regs_map[n]:04X}"
        for n in (set(yaml_map) & set(regs_map))
        if yaml_map[n] != regs_map[n]
    )

    problems = []
    if missing_in_regs:
        problems.append("in YAML but not Regs: " + ", ".join(missing_in_regs))
    if missing_in_yaml:
        problems.append("in Regs but not YAML: " + ", ".join(missing_in_yaml))
    if mismatched:
        problems.append("address mismatch: " + "; ".join(mismatched))

    assert not problems, (
        "sw/stream/regbank.py Regs has drifted from project_regbank.yml "
        "(RTL spec <-> SW). Re-sync the offsets.\n  " + "\n  ".join(problems)
    )


def test_stimulus_generator_registers_defined_and_match_contract():
    """DS-T2.78: Regs contains all 10 DS-P2.100 stimulus generator registers,
    matching both project_regbank.yml and de_c0_stimulus_contract."""
    from sw.stream.testbench import de_c0_stimulus_contract as K

    expected_stim = {
        "STIM_UART_CONTROL": (0x0220, K.STIM_UART_CONTROL),
        "STIM_UART_BAUD_DIV": (0x0224, K.STIM_UART_BAUD_DIV),
        "STIM_UART_DATA": (0x0228, K.STIM_UART_DATA),
        "STIM_I2C_CONTROL": (0x022C, K.STIM_I2C_CONTROL),
        "STIM_I2C_CLK_DIV": (0x0230, K.STIM_I2C_CLK_DIV),
        "STIM_I2C_DATA": (0x0234, K.STIM_I2C_DATA),
        "STIM_SPI_CONTROL": (0x0238, K.STIM_SPI_CONTROL),
        "STIM_SPI_CLK_DIV": (0x023C, K.STIM_SPI_CLK_DIV),
        "STIM_SPI_DATA": (0x0240, K.STIM_SPI_DATA),
        "STIM_EDGE_CONTROL": (0x0244, K.STIM_EDGE_CONTROL),
    }
    rb = FakeRegbank()
    for name, (yaml_addr, contract_addr) in expected_stim.items():
        assert hasattr(Regs, name), f"Regs lacks {name}"
        val = getattr(Regs, name)
        assert val == yaml_addr == contract_addr
        rb.write_u32(val, 0x1234_5678)
        assert rb.read_u32(val) == 0x1234_5678
