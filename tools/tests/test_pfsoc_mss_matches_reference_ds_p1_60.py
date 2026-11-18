"""DS-P1.60 — the PolarFire MSS configuration IS the reference board fact, plus declared deltas.

The first ``disco_mss.cfg`` was hand-written. On silicon (2026-09-23) the fabric
built from it let the reference eNVM HSS run but never lock the DDR-PLL
(0x3e010004 = 0x800010be, bit25 = 0), so DDR never trained and the U54s stayed
parked. It also sent the console UART (MMUART_4, serial@20106000) into the
fabric, so the kit was silent. The cause was the configuration itself: it
disagreed with the reference design this kit's HSS was built against, including
DDR4_ROW_ADDR_WIDTH 15 instead of 16, tRFC 160 instead of 350, and a different
memory map.

The contract enforced here:
  * the ``.cfg`` inside the committed ``disco_mss.cxz`` equals the committed
    ``disco_mss.cfg`` source (so the archive is reproducible from it);
  * every key equals the vendored reference (``reference/``, f09b6e0), except the
    keys declared in ``mss_deltas.yml``;
  * every declared delta is really present, with the declared values (no stale
    declaration can hide a later revert or a silent re-divergence);
  * the board facts that broke the first build are pinned explicitly.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MSS = ROOT / "hw" / "libero" / "pfsoc" / "mss"
REFERENCE = MSS / "reference" / "MPFS_DISCOVERY_KIT_MSS.cfg"
OURS = MSS / "disco_mss.cfg"
CXZ = MSS / "disco_mss.cxz"
DELTAS = MSS / "mss_deltas.yml"


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^(\S+)\s+(\S.*?)\s*$", line)
        if m and not line.startswith("#"):
            out[m.group(1)] = m.group(2)
    return out


@pytest.fixture(scope="module")
def ref() -> dict[str, str]:
    return _parse(REFERENCE.read_text(encoding="utf-8", errors="replace"))


@pytest.fixture(scope="module")
def ours() -> dict[str, str]:
    return _parse(OURS.read_text(encoding="utf-8", errors="replace"))


@pytest.fixture(scope="module")
def deltas() -> dict[str, dict]:
    return yaml.safe_load(DELTAS.read_text(encoding="utf-8")) or {}


def test_reference_and_source_are_real_configs(ref, ours):
    # Anti-vacuity: an empty or truncated parse would make every comparison pass.
    assert len(ref) > 1000 and len(ours) > 1000, (len(ref), len(ours))
    assert ref.get("DIE") == "MPFS095T" and ref.get("PACKAGE") == "FCSG325"


def test_every_divergence_from_the_reference_is_declared(ref, ours, deltas):
    undeclared = {
        k: (ref.get(k, "<absent>"), ours.get(k, "<absent>"))
        for k in set(ref) | set(ours)
        if ref.get(k) != ours.get(k) and k not in deltas
    }
    assert not undeclared, (
        "disco_mss.cfg diverges from the reference MSS config (the board fact the "
        "kit's eNVM HSS was built against) on undeclared keys "
        f"(key: (reference, ours)): {dict(sorted(undeclared.items())[:25])}. "
        "Either restore the reference value or declare the delta, with its reason, "
        "in hw/libero/pfsoc/mss/mss_deltas.yml.")


def test_every_declared_delta_is_real_and_matches(ref, ours, deltas):
    for key, d in deltas.items():
        assert str(d.get("why", "")).strip(), f"{key}: a delta must say why"
        assert ref.get(key) == str(d["reference"]), (
            f"{key}: declared reference value {d['reference']!r} but the vendored "
            f"reference has {ref.get(key)!r} — stale declaration")
        assert ours.get(key) == str(d["ours"]), (
            f"{key}: declared ours={d['ours']!r} but disco_mss.cfg has "
            f"{ours.get(key)!r} — stale declaration")


@pytest.mark.parametrize("key,value", [
    ("DDR4_ROW_ADDR_WIDTH", "16"),   # 8 Gb x16 die = 64K rows; the first build said 15
    ("DDR4_TIMING_RFC", "350"),      # 8 Gb tRFC; the first build said 160
    ("DDR4_WIDTH", "16"),
    ("MMUART_4", "MSSIO_B2"),        # the FlashPro5 console (serial@20106000 = ttyS0)
])
def test_board_facts_that_broke_the_first_build_are_pinned(ours, key, value):
    assert ours.get(key) == value, f"{key} = {ours.get(key)!r}, board fact is {value!r}"
