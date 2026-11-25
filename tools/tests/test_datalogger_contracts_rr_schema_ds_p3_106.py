# rr unknown-llm (AI-generated)
"""DS-P3.106: every datalogger requirements.yml is in the rr functional_contract schema.

rr's loader (sdk.cli.requirements.load_requirements) is the reader that
``rr sim coverage-map`` and the contract tools use. These guards load each file
through it, so a file rr would refuse fails here first. The CAN file was the
last one on the old top-level ``requirements:`` key, and its spec links were
free-text strings, so a constant or clause could not be queried by field.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdk.cli import requirements as rr_requirements

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted((ROOT / "hw" / "rtl" / "datalogger").glob("**/requirements.yml"))

#: Requirements that cite a public standard, per file. A link that disappears
#: (an edit that drops a `spec:` block) makes the count fall below this floor.
SPEC_LINKED_FLOOR = {"can": 14, "i2c": 4, "jtag": 3}


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def test_the_family_is_found():
    assert len(FILES) == 15, [_rel(p) for p in FILES]


@pytest.mark.parametrize("path", FILES, ids=_rel)
def test_each_file_loads_through_rr_at_schema_1(path):
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "requirements" not in raw, "top-level requirements: is the old key; use functional_contract:"
    # The loader reads a missing schema_version as 1; `rr ip contract migrate
    # --check` reads the file's own key, so the key itself is asserted.
    assert raw.get("schema_version") == 1, "run the schema 0 -> 1 migration (schema_version: 1)"
    loaded = rr_requirements.load_requirements(path)
    assert loaded.functional_contract, "no requirement loaded"


@pytest.mark.parametrize("name,floor", sorted(SPEC_LINKED_FLOOR.items()))
def test_spec_links_are_structured_citations(name, floor):
    path = ROOT / "hw" / "rtl" / "datalogger" / name / "requirements.yml"
    loaded = rr_requirements.load_requirements(path)
    linked = [c for c in loaded.functional_contract if getattr(c, "spec", None)]
    assert len(linked) >= floor
    raw = {r["id"]: r for r in yaml.safe_load(path.read_text(encoding="utf-8"))["functional_contract"]}
    for c in linked:
        for i, entry in enumerate(raw[c.id]["spec"]):
            if isinstance(entry, str):
                # Only free-text research notes stay strings; a citation of a
                # standard or of the CRC catalogue is a mapping.
                assert not entry.startswith(("ISO ", "BOSCH ", "reveng ", "Internal:")), (
                    f"{c.id}.spec[{i}] is a citation written as a string: {entry[:60]!r}")
            else:
                assert entry.get("doc") or entry.get("non_spec"), f"{c.id}.spec[{i}]"


def test_can_crc_constants_stay_queryable():
    path = ROOT / "hw" / "rtl" / "datalogger" / "can" / "requirements.yml"
    loaded = rr_requirements.load_requirements(path)
    constants = {s.constant for c in loaded.functional_contract for s in (c.spec or ())
                 if getattr(s, "constant", None)}
    assert {"CAN-FD.CRC17=0x1685B", "CAN-FD.CRC21=0x102899"} <= constants
