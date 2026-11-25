# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.103: the CTU CAN FD core is a bench-only package; no product reaches it.

The CTU targets (canfd_demo, de25_canfd) moved to bench/targets/. routertl's rule that leaves a package out of
a target's lock when only OTHER targets declare it (RTL-P2.2271) scans targets/*.yml only, so with ctu-fee still
in the shared ip.lock every product lock, lint list and sim list took it back in, and a vendored libs/ctu-fee
bound the real can_top_apb over the products' stub. The CTU targets therefore keep their packages in their own
lock (`packages_lock: target`, targets/<t>.ip.lock), and the shared ip.lock carries no CTU row.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CTU = "ctu-fee/ctucanfd_ip_core"
BENCH_CTU_TARGETS = ("canfd_demo", "de25_canfd")


def _yaml(rel: str) -> dict:
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8")) or {}


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", pattern],
                         capture_output=True, text=True, check=True).stdout
    return sorted(p for p in out.split("\0") if p)


def test_the_shared_ip_lock_has_no_ctu_row():
    assert CTU not in _yaml("ip.lock"), f"ip.lock still pins {CTU}: every product target reaches it"


def test_no_product_target_declares_the_ctu_package():
    for rel in _tracked("targets/*.yml"):
        assert CTU not in (_yaml(rel).get("packages") or {}), rel
