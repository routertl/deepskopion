# rr unknown-llm (AI-generated)
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT

"""DS-P4.3 — structural guard for the datalogger capture-time timestamp contract.

The bug class (DS-T3.10 qspi, DS-T3.11 level): a datalogger builds its output
message from a COMBINATIONAL `TIMESTAMP` and holds it at an `*_MSG_RDY`-gated
boundary, so a backpressure stall slides the embedded timestamp forward to
DELIVERY time — unbounded error, silently wrong timing. See
docs/standards/datalogger_compliance_checklist.md §3 (T5).

A datalogger that emits a timestamped message through an `*_MSG_RDY` boundary MUST
freeze the timestamp at capture, by one of two strategies:

  * ``fifo``  — write the (timestamp, payload) tuple into the message FIFO
                together, at the capture instant (uart / jtag / edge).
  * ``latch`` — register the timestamp into a dedicated reg at the capture/decode
                cycle and build the message from the latch (i2c / qspi / level).

This guard does NOT try to prove the strategy from the RTL (too brittle — e.g.
i2c legitimately concatenates a combinational TIMESTAMP slice that is then
*registered* at the FSM capture cycle). Instead it enforces the **forcing
function**: every `*_datalogger.vhd` with an `*_MSG_RDY` port must be CLASSIFIED
here, so a new datalogger cannot ship without its author consciously choosing
(and documenting) a capture-time strategy. The two dataloggers that actually had
the bug additionally carry a backpressure-discriminator cocotb test, whose
existence is checked mechanically.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DS_ROOT = Path(__file__).resolve().parents[3]
DATALOGGER_RTL = DS_ROOT / "hw" / "rtl" / "datalogger"

VALID_STRATEGIES = {"fifo", "latch"}

# Every *_datalogger.vhd with an *_MSG_RDY boundary → (strategy, evidence).
# Adding a new datalogger (or removing one) breaks test_every_rdy_datalogger_is_classified
# until this manifest is updated — the forcing function.
DATALOGGER_TIMESTAMP_STRATEGY: dict[str, tuple[str, str]] = {
    "qspi_datalogger":  ("latch", "tsPayload_r@bufferPayload_r / tsEvent_r@newEvent_r (DS-T3.10)"),
    "level_datalogger": ("latch", "tsCaptured_r/pinsCaptured_r latched at the change instant (DS-T3.11)"),
    "i2c_datalogger":   ("latch", "msgPayload_r/msgEvent_r registered at the FSM capture cycle; fire-and-forget, no RDY wait"),
    "uart_datalogger":  ("fifo",  "(ts,data) written into fifoUartDin together at capture"),
    "jtag_datalogger":  ("fifo",  "Parts staged then pushed into the MsgRam FIFO at the decode cycle"),
    "edge_datalogger":  ("fifo",  "timeStamp_r captured into fifoEdDin at the edge instant"),
    "can_datalogger":   ("fifo",  "can_frame_fsm stamps each msg via MkMsg(TIMESTAMP) at the field-completion cycle, stages it, then pushes into the MsgRam FIFO — the ts is embedded in the stored word, so an *_MSG_RDY stall cannot slide it (mirrors jtag_datalogger)"),
}

# Dataloggers whose capture-time stamping is pinned by an explicit backpressure
# discriminator (V-during-capture / W-under-backpressure). Mandatory for any
# datalogger that HAD the bug; recommended for the rest.
DISCRIMINATOR_TESTS: dict[str, tuple[str, str]] = {
    "qspi_datalogger":  ("hw/sim/cocotb/tests/datalogger/qspi/test_qspi_spi2pin.py",
                      "test_timestamp_is_capture_not_send_time"),
    "level_datalogger": ("hw/sim/cocotb/tests/datalogger/nonstd/level_datalogger/test_level_datalogger.py",
                      "test_timestamp_is_change_time_not_delivery"),
}


def _discover_rdy_dataloggers() -> dict[str, Path]:
    """Map datalogger entity name → path for every *_datalogger.vhd with an *_MSG_RDY port."""
    found: dict[str, Path] = {}
    rdy = re.compile(r"_MSG_RDY\s*:", re.IGNORECASE)
    for vhd in DATALOGGER_RTL.rglob("*_datalogger.vhd"):
        text = vhd.read_text(encoding="utf-8", errors="replace")
        if rdy.search(text):
            found[vhd.stem] = vhd
    return found


def test_discovery_is_nonempty():
    """Sanity: the discovery actually finds the datalogger RTL (guards against a
    moved tree silently passing every other assertion vacuously)."""
    found = _discover_rdy_dataloggers()
    assert len(found) >= 4, f"suspiciously few RDY dataloggers discovered: {sorted(found)}"


def test_every_rdy_datalogger_is_classified():
    """Every *_MSG_RDY datalogger must be classified in the manifest, and the
    manifest must not carry stale entries — so a NEW datalogger forces its author to
    declare a capture-time strategy (DS-P4.3 / checklist T5)."""
    discovered = set(_discover_rdy_dataloggers())
    classified = set(DATALOGGER_TIMESTAMP_STRATEGY)
    unclassified = discovered - classified
    stale = classified - discovered
    assert not unclassified, (
        f"{len(unclassified)} datalogger(s) with an *_MSG_RDY boundary are NOT classified "
        f"for capture-time stamping: {sorted(unclassified)}. Add each to "
        "DATALOGGER_TIMESTAMP_STRATEGY with a 'fifo' or 'latch' strategy + evidence, "
        "and freeze its timestamp at capture (checklist §3 T5)."
    )
    assert not stale, (
        f"stale DATALOGGER_TIMESTAMP_STRATEGY entries (no matching RTL): {sorted(stale)}"
    )


@pytest.mark.parametrize("name", sorted(DATALOGGER_TIMESTAMP_STRATEGY))
def test_strategy_value_is_valid(name):
    strategy, evidence = DATALOGGER_TIMESTAMP_STRATEGY[name]
    assert strategy in VALID_STRATEGIES, (
        f"{name}: strategy {strategy!r} not in {sorted(VALID_STRATEGIES)}"
    )
    assert evidence.strip(), f"{name}: classification needs evidence (how the ts is frozen)"


def test_discriminator_test_targets_are_classified():
    """Every datalogger with a discriminator test must also be in the strategy
    manifest (no orphan test references)."""
    assert set(DISCRIMINATOR_TESTS) <= set(DATALOGGER_TIMESTAMP_STRATEGY)


@pytest.mark.parametrize("name", sorted(DISCRIMINATOR_TESTS))
def test_discriminator_test_exists(name):
    rel, func = DISCRIMINATOR_TESTS[name]
    path = DS_ROOT / rel
    assert path.is_file(), f"{name}: discriminator test file missing: {rel}"
    text = path.read_text(encoding="utf-8", errors="replace")
    assert f"def {func}(" in text, (
        f"{name}: discriminator test '{func}' not found in {rel} — the "
        "backpressure capture-time regression must not be removed/renamed (DS-P4.3)."
    )
