# rr unknown-llm (AI-generated)
"""LT-P3.250 / DS-P3.120: the STATS per-protocol map is single-sourced.

The wave_stream STATS frame's `proto_stats` map MUST be keyed by exactly the
canonical ACTIVE datalogger set — the same set `datalogger_gather_gen.py` derives from
`tools/datalogger-builder/config/datalogger_config.yml` — with NO second hardcoded
list in the daemon. This is the producer-side half of the cross-repo coherence
gate (the super-repo `tests/test_contract_alignment.py` asserts the RouteWave
consumer agrees too).

The drift this prevents: the daemon previously hardcoded
{i2c, spi, uart, qspi, edge, i2s} → it showed a stale `i2s` row and dropped the
active `can` (DS-P3.100) / `jtag` (DS-P3.56) dataloggers.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from sw.stream import datalogger_registry
from sw.stream.data_source import build_proto_stats
from sw.stream.regbank import Regs

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIG = _REPO_ROOT / "tools" / "datalogger-builder" / "config" / "datalogger_config.yml"
_CORE_VHD = _REPO_ROOT / "hw" / "rtl" / "deepskopion_core.vhd"

# The regbank counter slots that exist TODAY, hardcoded on purpose (ROUTERTL-002:
# deriving this from datalogger_registry would make the assertion tautological).
# DS-T2.56 (commit 0006583) added the CAN + JTAG PKT/ERR slots and their
# deepskopion_top fan-in, mirroring EDGE; DROP is still exposed only for the
# three framed byte protocols. When a new slot lands in project_regbank.yml this
# table is the one place a stats test needs updating.
EXPECTED_SLOTS = {
    "can":  {"pkts": True, "errs": True, "drops": False},
    "edge": {"pkts": True, "errs": True, "drops": False},
    "i2c":  {"pkts": True, "errs": True, "drops": True},
    "spi":  {"pkts": True, "errs": True, "drops": True},
    "uart": {"pkts": True, "errs": True, "drops": True},
}

_SLOT_PREFIX = {"pkts": "PKT_CNT", "errs": "ERR_CNT", "drops": "DROP_CNT"}

# Mirror datalogger_gather_gen.sanitize_type for the active-set derivation.
_SANITIZE = {
    "i2c": "I2C", "spi": "SPI", "uart": "UART", "i2s": "I2S", "qspi": "QSPI",
    "quadspi": "QSPI", "edge": "EDGE", "edgedetection": "EDGE", "jtag": "JTAG",
    "mdio": "MDIO", "onewire": "ONEWIRE", "can": "CAN", "timestamp": "TIMESTAMP",
}


def _active_from_yaml() -> list[str]:
    cfg = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    burst = cfg.get("Dataloggers", {}).get("Burst", {})
    return sorted({_SANITIZE.get(n.lower(), n.upper()) for n in burst})


def test_registry_matches_datalogger_config_yaml():
    """The generated datalogger_registry == the live datalogger_config.yml active set
    (catches a stale generated file even if the drift gate wasn't run)."""
    want = [p.lower() for p in _active_from_yaml()]
    assert datalogger_registry.ACTIVE_PROTOCOLS == want, (
        "datalogger_registry.py is stale vs datalogger_config.yml — run "
        "`python3 tools/datalogger-builder/datalogger_registry_gen.py gen`"
    )


def test_emitted_proto_set_equals_registry_active_set():
    """build_proto_stats emits one entry per ACTIVE protocol — no more, no less.
    This is the producer-side coherence assertion: the wire's key set IS the
    registry's active set (no stale i2s, no missing can)."""
    stats = build_proto_stats(lambda _off: 0)  # values irrelevant, set is the point
    assert list(stats.keys()) == datalogger_registry.ACTIVE_PROTOCOLS
    # The exact canonical set today (a hard-coded expectation per ROUTERTL-002 —
    # a value derived from the same source would tautologically pass).
    assert list(stats.keys()) == ["can", "edge", "i2c", "spi", "uart"]
    assert "i2s" not in stats  # the stale row that motivated the ticket


def test_null_where_no_regbank_slot():
    """A field with no regbank counter slot reports null (n/a), never a
    fabricated 0; a field WITH a slot carries the real read. EDGE/CAN have
    no DROP slot today (EXPECTED_SLOTS), so exactly those two drop fields are
    null and every other field is the read value."""
    # Read 0xABCD for any slot so a real read is distinguishable from a sentinel.
    stats = build_proto_stats(lambda _off: 0xABCD)
    for proto, slots in EXPECTED_SLOTS.items():
        want = {f: (0xABCD if present else None) for f, present in slots.items()}
        assert stats[proto] == want, f"{proto} slot presence drifted from EXPECTED_SLOTS"
    # Spelled out, so the truth table above cannot silently become vacuous.
    assert stats["can"] == {"pkts": 0xABCD, "errs": 0xABCD, "drops": None}
    assert stats["edge"] == {"pkts": 0xABCD, "errs": 0xABCD, "drops": None}
    for p in ("i2c", "spi", "uart"):
        assert stats[p] == {"pkts": 0xABCD, "errs": 0xABCD, "drops": 0xABCD}


def test_expected_slots_match_registry():
    """The hardcoded truth table == the generated registry. DS-T2.68: DS-T2.56
    added the CAN/JTAG PKT/ERR slots and regenerated SLOTS but left the stats
    tests asserting the old null-CAN/JTAG world, so trunk went red. Whoever adds
    the next slot gets ONE failure, here, naming the file to update."""
    assert datalogger_registry.SLOTS == EXPECTED_SLOTS, (
        "regbank counter slots changed — update EXPECTED_SLOTS in this file AND "
        "the per-protocol expectations in test_stats_heartbeat.py"
    )


def _driven_telemetry_slots() -> set[str]:
    """Regbank slot names that deepskopion_core.vhd actually fans into telemetry_in."""
    text = _CORE_VHD.read_text(encoding="utf-8")
    return set(re.findall(r"telemetry_in\(\s*C_INDEX_(\w+?)\s*/\s*4\s*\)\.data\s*<=", text))


def test_declared_counter_slots_are_driven_in_rtl():
    """Every slot the registry marks 'present' must be DRIVEN in the RTL — a
    declared-but-undriven slot reads 0 forever and the daemon would report that
    false 0 as a real counter. This is precisely the ARB_GRANT_CNT trap
    (DS-T2.33), which the SW has to special-case to null; the guard stops the
    next one from reaching a consumer. It would also have caught a half-landed
    DS-T2.56 (regbank slot + SLOTS, no deepskopion_top fan-in)."""
    driven = _driven_telemetry_slots()
    assert "PKT_CNT_I2C" in driven, "fan-in regex matched nothing — the guard is dead"
    missing = [
        f"{_SLOT_PREFIX[field]}_{datalogger_registry.REGBANK_SUFFIX[proto]}"
        for proto, slots in datalogger_registry.SLOTS.items()
        for field, present in slots.items()
        if present and f"{_SLOT_PREFIX[field]}_{datalogger_registry.REGBANK_SUFFIX[proto]}" not in driven
    ]
    assert not missing, (
        f"regbank slots declared but never driven in deepskopion_core.vhd: {missing} — "
        "they read 0 forever; either drive them or drop the slot"
    )


def test_registry_slots_match_actual_regbank():
    """datalogger_registry.SLOTS agrees with the real Regs.* slot set — so a protocol
    marked 'has slot' truly has one (and vice versa). Guards a hand-edited registry."""
    reg_names = set(dir(Regs))
    for proto, slots in datalogger_registry.SLOTS.items():
        suffix = datalogger_registry.REGBANK_SUFFIX[proto]
        assert slots["pkts"] == (f"PKT_CNT_{suffix}" in reg_names)
        assert slots["errs"] == (f"ERR_CNT_{suffix}" in reg_names)
        assert slots["drops"] == (f"DROP_CNT_{suffix}" in reg_names)
