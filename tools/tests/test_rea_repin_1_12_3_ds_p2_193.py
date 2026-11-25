# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""DS-P2.193 — routertl/rea is the 1.12.3 release, with no local REA patch left.

rr-rea 1.12.3 (REA-P2.26, tag v1.12.3 = 6d9d218) carries the RR-CDC-002
quasi-static waiver on rr_rea_sync_word s1/s2 (REA-REQ-959) upstream. Before
it, deepskopion carried the waiver itself: first as an inline pragma in the
vendored rr_rea_cdc.vhd with a hand-overridden ip.lock hash (DS-T1.21,
31761198), which the re-vendor 30c53d04 dropped, then as a cdc_lint_waivers.yml
entry (DS-P2.189). One guard per independent part:

  1. the lock pin: ip.lock resolves routertl/rea to the 1.12.3 release at
     6d9d218ef384, and every target that declares it pins `==1.12.3` exactly;
  2. no local waiver: the DS-P2.189 gate, run as the pre-commit leg runs it,
     holds no waiver for a REA file and refuses none. Re-adding the old entry
     is refused as stale, because the upstream pragma already covers s1/s2;
  3. no hash override: ip.lock's sha256 for rr_rea_cdc.vhd is the upstream
     file's at the locked commit. A local patch that re-hashes the lock (the
     31761198 pattern) keeps disk and lock consistent, so `rr pkg verify`
     stays green; only a comparison with the upstream bytes catches it.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
sys.path.insert(0, str(ROOT / "tools"))
import cdc_lint_waivers  # noqa: E402

REA = "routertl/rea"
REA_CDC = "libs/routertl/rea/rtl/rr_rea_cdc.vhd"
# DS-P1.79: 1.12.4 (REA-P1.4) adds set_max_skew to rr_rea_scoped.sdc only; the
# RR-CDC-002 pragma this test guards is unchanged from 1.12.3.
WANT_VERSION = "1.12.4"
WANT_COMMIT_PREFIX = "82cfcc75b751"

# sha256 of the UPSTREAM rr_rea_cdc.vhd at each commit deepskopion has locked,
# taken from the rr-rea git objects (`git show <commit>:rtl/rr_rea_cdc.vhd |
# sha256sum`), hard-coded rather than derived from the tree under test. A repin
# adds the new commit's row.
UPSTREAM_REA_CDC_SHA256 = {
    # commit:d7b7d3bcdd2f (REA-P2.24, DS-P3.174): no s1/s2 pragma
    "d7b7d3bcdd2f3ae0d9f0f42ad75a93978fb5f702":
        "bd44649fd306c438dcf7f0538c33c02d9abd1ebc1335df03e18a7dac63a2e20b",
    # v1.12.3 (REA-P2.26): upstream RR-CDC-002 pragma on s1/s2
    "6d9d218ef384996c2f4d151abaa0ab335d63d6c6":
        "a6cbcba0fffe13f30188f890da1af72b8d6b0a095bf8edb01e6cd218ea207d8f",
    # v1.12.4 (REA-P1.4): constraints only, rr_rea_cdc.vhd unchanged
    "82cfcc75b75159cf66e1e8b07c0db12b20ccccd5":
        "a6cbcba0fffe13f30188f890da1af72b8d6b0a095bf8edb01e6cd218ea207d8f",
}


def _lock_entry() -> dict:
    return yaml.safe_load((ROOT / "ip.lock").read_text(encoding="utf-8"))[REA]


def test_ip_lock_pins_the_rea_release() -> None:
    entry = _lock_entry()
    assert str(entry["resolved_version"]) == WANT_VERSION \
        and entry["commit"].startswith(WANT_COMMIT_PREFIX), (
            f"routertl/rea not repinned to {WANT_VERSION} @ {WANT_COMMIT_PREFIX}: "
            f"ip.lock holds {entry['resolved_version']} @ {entry['commit']}"
        )
    pins: dict[str, str] = {}
    for target in all_target_files(ROOT):
        spec = ((yaml.safe_load(target.read_text(encoding="utf-8")) or {})
                .get("packages") or {}).get(REA)
        if spec is not None:
            pins[target.name] = str(spec["version"] if isinstance(spec, dict) else spec)
    assert pins, "no target declares routertl/rea"
    wrong = {t: s for t, s in pins.items() if s != f"=={WANT_VERSION}"}
    assert not wrong, (
        f"routertl/rea not repinned to {WANT_VERSION} @ {WANT_COMMIT_PREFIX}: "
        f"targets without the exact =={WANT_VERSION} pin: {wrong}"
    )


def test_no_local_rea_waiver_remains() -> None:
    waivers, refused = cdc_lint_waivers.load_waivers(
        ROOT, ROOT / cdc_lint_waivers.WAIVERS_FILE)
    local = [w.id for w in waivers if w.path.startswith("libs/routertl/rea/")]
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "cdc_lint_waivers.py"), "check"],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        check=False,
    ).stdout
    gate_refusals = [line for line in out.splitlines() if line.startswith("REFUSED ")]
    assert not local and not refused and not gate_refusals, (
        "stale local REA patch: cdc_lint_waivers.yml still waives a REA file "
        f"{local} that upstream 1.12.3 waives itself; gate refusals: "
        f"{refused + gate_refusals}"
    )


def test_locked_rea_cdc_hash_is_the_upstream_file() -> None:
    entry = _lock_entry()
    commit = entry["commit"]
    assert commit in UPSTREAM_REA_CDC_SHA256, (
        f"ip.lock pins routertl/rea @ {commit}, which has no recorded upstream "
        f"{REA_CDC} hash; add its row from `git show <commit>:rtl/rr_rea_cdc.vhd | sha256sum` in rr-rea"
    )
    locked = entry["file_hashes"][REA_CDC]
    assert locked == UPSTREAM_REA_CDC_SHA256[commit], (
        f"ip.lock hash override: {REA_CDC} is locked as {locked}, but upstream "
        f"@ {commit[:12]} is {UPSTREAM_REA_CDC_SHA256[commit]} (a local patch "
        "re-hashed into the lock)"
    )
    on_disk = hashlib.sha256((ROOT / REA_CDC).read_bytes()).hexdigest()
    assert on_disk == locked, (
        f"{REA_CDC} on disk ({on_disk}) differs from ip.lock ({locked}): a local patch"
    )
