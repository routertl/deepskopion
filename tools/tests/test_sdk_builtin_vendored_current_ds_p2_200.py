# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.200: a vendored SDK-builtin unit is the unit the SDK ships.

The vendored edge_detector kept a reset defect the SDK had fixed. Sims that also
listed the SDK copy were refused as a duplicate design unit, and a design binding
the vendored copy carried the defect. A later SDK fix makes this test red on the
commit-gate run after the upgrade, not at some unrelated sim.

To fix a red here: rr pkg add <package> --yes refreshes that package alone. A
plain rr pkg update or install leaves the old copy, and rr pkg install --force
re-resolves every other package as well.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SEARCHED = ("hw", "sw", "tools")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _builtin_entries() -> dict[str, dict]:
    lock = yaml.safe_load((ROOT / "ip.lock").read_text(encoding="utf-8"))
    packages = lock.get("packages", lock)
    return {
        name: entry for name, entry in packages.items()
        if isinstance(entry, dict) and entry.get("commit") == "sdk-builtin"
    }


def _sdk_root() -> Path:
    rr = shutil.which("rr")
    assert rr, "PRECONDITION: rr is not on PATH; the SDK copy cannot be located"
    out = subprocess.run([rr, "sdk-path"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return Path(out.strip().splitlines()[-1])


def test_the_lock_has_sdk_builtin_packages():
    assert "routertl/edge_detector" in _builtin_entries()


def test_the_vendored_file_has_the_hash_the_lock_records():
    stale = {}
    for entry in _builtin_entries().values():
        for rel, want in (entry.get("file_hashes") or {}).items():
            got = _sha256(ROOT / rel)
            if got != want:
                stale[rel] = (want[:12], got[:12])
    assert stale == {}


def test_every_sdk_builtin_package_matches_the_sdk_unit():
    sdk = _sdk_root()
    checked = 0
    stale = {}
    for name, entry in _builtin_entries().items():
        unit = sdk / entry["url"].removeprefix("sdk://")
        if not unit.is_file():
            continue
        checked += 1
        vendored = ROOT / entry["files"][0]
        if _sha256(unit) != _sha256(vendored):
            stale[name] = str(vendored.relative_to(ROOT))
    assert checked > 0, f"no SDK-builtin unit was found under {sdk}"
    assert stale == {}, f"refresh each with: rr pkg add <package> --yes: {stale}"


@pytest.mark.parametrize("directory", SEARCHED)
def test_no_test_or_tool_allows_a_duplicate_design_unit(directory):
    word = "RR_ALLOW_DUPLICATE" + "_DESIGN_UNITS"
    proc = subprocess.run(["git", "grep", "-l", word, "--", directory], cwd=ROOT,
                          capture_output=True, text=True, check=False)
    assert proc.stdout.split() == []
