"""DS-P2.126 — every PolarFire target declares the MEASURED device IDCODE.

pfsoc_datalogger.yml carried the PF-B0 placeholder 0x0F8271CF long after
DS-P2.132 read the live FlashPro5 chain (0x0F8181CF, MPFS095T) into
pfsoc_ka_a.yml. `rr program run` compares the declared IDCODE against the
cable and REFUSES on mismatch, so the placeholder blocked programming the
datalogger target on the kit (measured 2026-09-23). Pin every pfsoc target to
the measured value so a placeholder cannot silently return.
"""
from __future__ import annotations

from pathlib import Path

import sys
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
MEASURED_MPFS095T_IDCODE = 0x0F8181CF
PFSOC_TARGETS = [p for p in all_target_files(ROOT) if p.name.startswith("pfsoc_")]


def test_pfsoc_targets_exist():
    assert PFSOC_TARGETS, "no targets/pfsoc_*.yml found — glob is vacuous"


@pytest.mark.parametrize("target", PFSOC_TARGETS, ids=lambda p: p.name)
def test_declared_idcode_is_the_measured_one(target):
    doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    idcode = (doc.get("hardware") or {}).get("idcode") or doc.get("idcode")
    if idcode is None:
        # search one level down (the key lives under the tool/hardware block)
        for v in doc.values():
            if isinstance(v, dict) and "idcode" in v:
                idcode = v["idcode"]
                break
    assert idcode is not None, f"{target.name}: no idcode declared"
    assert int(str(idcode), 0) == MEASURED_MPFS095T_IDCODE, (
        f"{target.name}: idcode {idcode} != measured {MEASURED_MPFS095T_IDCODE:#010x}")
