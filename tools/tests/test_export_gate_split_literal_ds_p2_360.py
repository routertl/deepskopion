# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.360: the export gate sees a private address or a cable serial split across adjacent string literals.

The public export of 2026-11-18 carried a bench board address in a test fixture written as two adjacent bytes
literals, which the gate's IPv4 pattern never saw whole. The gate now joins each run of adjacent literals (joined by
whitespace, ``+`` or VHDL ``&``) and checks the result. Every planted address here is built at run time, so this
file itself carries no address literal, whole or split.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import public_artifact_hygiene as hygiene  # noqa: E402

Q = '"'
SYNTH = (10, 255, 254, 253)            # RFC 1918, not a bench address
DOC = (192, 0, 2, 7)                   # RFC 5737 documentation address: legal in public artifacts


def _split(parts, joiner: str, prefix: str = "") -> str:
    a, b, c, d = (str(n) for n in parts)
    return f"{prefix}{Q}{a}.{b}{Q}{joiner}{prefix}{Q}.{c}.{d}{Q}"


def _scan(tmp_path: Path, rel: str, text: str):
    tree = tmp_path / "tree"
    target = tree / rel
    target.parent.mkdir(parents=True)
    target.write_text(text)
    return hygiene.scan([tree], include_index=False, export_gate=True, root=ROOT)


@pytest.mark.parametrize("rel,line", [
    ("sw/a.py", f"HOST = {_split(SYNTH, ' ')}\n"),
    ("sw/b.py", f"HOST = {_split(SYNTH, ' ', 'b')}\n"),
    ("sw/c.py", f"HOST = {_split(SYNTH, ' + ')}\n"),
    ("hw/d.vhd", f"constant C_HOST : string := {_split(SYNTH, ' & ')};\n"),
    ("sw/e.js", f"const host = {_split(SYNTH, '+')};\n"),
])
def test_a_split_private_address_is_refused(tmp_path, rel, line):
    findings, errors = _scan(tmp_path, rel, line)
    assert errors == []
    want = ".".join(str(n) for n in SYNTH)
    assert any("private-subnet assumption (split literal)" in f and want in f for f in findings), findings


def test_a_split_documentation_address_is_not_flagged(tmp_path):
    findings, errors = _scan(tmp_path, "sw/doc.py", f"EXAMPLE = {_split(DOC, ' ')}\n")
    assert errors == [] and findings == []


def test_a_whole_documentation_address_is_not_flagged(tmp_path):
    doc = ".".join(str(n) for n in DOC)
    findings, errors = _scan(tmp_path, "sw/doc2.py", f"EXAMPLE = {Q}{doc}{Q}\n")
    assert errors == [] and findings == []


def test_a_whole_private_literal_is_reported_once_not_twice(tmp_path):
    whole = ".".join(str(n) for n in SYNTH)
    findings, _ = _scan(tmp_path, "sw/w.py", f"HOST = {Q}{whole}{Q} {Q}{Q}\n")
    hits = [f for f in findings if whole in f]
    assert len(hits) == 1 and "split literal" not in hits[0]


def test_unrelated_adjacent_literals_are_not_flagged(tmp_path):
    findings, errors = _scan(tmp_path, "sw/v.py", f"V = {Q}version 1.2{Q} {Q}.3.4 build{Q}, {Q}10{Q}, {Q}1{Q}\n")
    assert errors == [] and findings == []


def test_a_split_cable_serial_is_refused(tmp_path):
    head, tail = "21", "F" * 10                      # synthetic serial of the bench cable shape
    findings, errors = _scan(tmp_path, "sw/s.py", f"SERIAL = {Q}{head}{tail[:3]}{Q} + {Q}{tail[3:]}{Q}\n")
    assert errors == []
    assert any("bench cable serial (split literal)" in f and head + tail in f for f in findings), findings
