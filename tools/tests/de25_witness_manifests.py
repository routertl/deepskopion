"""The registered DE25 silicon-witness manifests beside DS-P2.165's.

runtimes/hw.yml may declare a requirement only when a silicon receipt for it
is in some witness manifest. DS-P2.165's manifest
(tools/tests/data/ds_p2_165_witness/images.json) is the first; every later
witness registers its own manifest here, by explicit path. A registered
manifest that is missing, or names no receipt, is an error: registering is a
claim that the receipts exist.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: DS-P2.165's manifest: the eight receipts recorded before DS-T2.87.
P2165_MANIFEST = "tools/tests/data/ds_p2_165_witness/images.json"

#: Sibling witness manifests, in registration order (repo-relative paths).
SIBLING_MANIFESTS = (
    "tools/tests/data/ds_p2_267_witness/images.json",
)


def sibling_receipt_ids() -> list[str]:
    """Every receipt id the registered sibling manifests name."""
    ids: list[str] = []
    for rel in SIBLING_MANIFESTS:
        path = ROOT / rel
        assert path.is_file(), f"registered witness manifest {rel} is missing"
        receipts = json.loads(path.read_text(encoding="utf-8")).get("receipts") or {}
        assert receipts, f"registered witness manifest {rel} names no receipt"
        ids += list(receipts)
    return ids


def p2165_receipt_ids() -> list[str]:
    """The receipt ids of DS-P2.165's manifest."""
    path = ROOT / P2165_MANIFEST
    assert path.is_file(), f"DS-P2.165 witness manifest {P2165_MANIFEST} is missing"
    return sorted(json.loads(path.read_text(encoding="utf-8"))["receipts"])
