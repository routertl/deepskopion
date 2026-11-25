# rr unknown-llm (AI-generated)
"""Structural guard: every *_datalogger_protocol.yml must be valid YAML (DS-P3.104).

Regression guard for the class of bug found while wiring CAN into the daemon
decoder: can_datalogger_protocol.yml had a malformed flow-mapping description
(unquoted `[`/`,` and a `>` block scalar inside `{ }`) that sat undetected
because nothing parsed that file until "can" joined protocol_codes_gen.PROTOCOLS.
A protocol YAML that doesn't parse silently breaks any tool that loads it; this
catches it for EVERY datalogger's contract, not just the ones in PROTOCOLS today.
"""
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_YMLS = sorted((_ROOT / "hw" / "rtl" / "datalogger").rglob("*_datalogger_protocol.yml"))


def test_discovery_nonempty():
    assert _YMLS, "no *_datalogger_protocol.yml found — moved tree?"


@pytest.mark.parametrize("yml", _YMLS, ids=lambda p: p.parent.name)
def test_protocol_yaml_parses(yml):
    doc = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{yml} did not parse to a mapping"
    # the two blocks every consumer relies on
    assert "constants" in doc, f"{yml} missing top-level 'constants'"
