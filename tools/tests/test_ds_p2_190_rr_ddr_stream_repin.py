"""DS-P2.190 — the vendored rr_ddr_stream is a published release and carries the
DDR-P2.29 RR-CDC-005 waivers.

THE DEFECT. ip.lock pinned routertl/rr_ddr_stream at ea23b3a (0.3.3). On that
pin the four base-address captures

    ddr_stream_axi.vhd   wr_base_addr_meta <= wr_base_addr_sys;
                         rd_base_addr_meta <= rd_base_addr_sys;
    ddr_stream_avmm.vhd  wr_base_addr_meta <= wr_base_addr_sys;
                         rd_base_addr_meta <= rd_base_addr_sys;

raise RR-CDC-005 (unsynchronized multi-bit crossing) with no waiver, so
DS-P2.188 could not turn the rule on. Upstream DDR-P2.29 (rr-ddr-stream fad97ec)
states the quasi-static protocol (DDR-REQ-211) on each capture as an inline
``rr-lint-disable-line RR-CDC-005`` pragma, and rr_ddr_stream 0.3.4
(tag v0.3.4 = 8cfd7c9, published) first carried it. The pin moved to 0.3.6 (DS-P2.195),
because 0.3.4 is yanked and its source is private, and is now 0.3.7 (98f69bea, DS-P2.232).

THE FIX is a repin through ``rr pkg add``, never a hand edit of a vendored
file (the ticket prohibits copying the waiver lines in). Three parts, each
guarded here on its own:

  * cdc005-waivers: RR-CDC-005, run on the vendored files, finds the four
    captures and every one is waived.
  * lock-pin: ip.lock pins the published RELEASE_COMMIT, and every vendored
    file it hashes has exactly those bytes on disk.
  * target-requests: every target and ref_design that declares the package
    requests REQUEST, the version the lock resolved. A target left on
    ^ea23b3a4b6ba re-resolves the shared lock back to the unwaived pin the
    next time someone runs ``rr pkg add`` with that target active.

The DE25 silicon check of this pin is
``test_ds_p2_190_de25_silicon_witness.py``.
"""
from __future__ import annotations

import sys
import hashlib
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files, per_tree  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
IP_LOCK = ROOT / "ip.lock"
PKG = "routertl/rr_ddr_stream"
VENDORED = ROOT / "libs" / "routertl" / "rr_ddr_stream"

#: rr_ddr_stream v0.3.7 as published (registry row v0.3.7 @ 2319261cb86f, checked 2026-11-12).
#: Record of the repin (MC ruling 2026-11-12, DS-P2.269): commit 98f69bea (DS-P2.232), which landed
#: it with the run-5 DE25 cold-boot witness of DS-P1.79. The request stays ^0.3.6, which admits 0.3.7.
RELEASE_VERSION = "0.3.7"
RELEASE_COMMIT = "2319261cb86f39b3a91d37cff0e716b12ea65112"
REQUEST = "^0.3.6"

RULE = "RR-CDC-005"
#: The four base-address captures DDR-P2.29 waives, as (file, destination).
CAPTURES = {
    ("rtl/ddr_stream_axi.vhd", "wr_base_addr_meta"),
    ("rtl/ddr_stream_axi.vhd", "rd_base_addr_meta"),
    ("rtl/ddr_stream_avmm.vhd", "wr_base_addr_meta"),
    ("rtl/ddr_stream_avmm.vhd", "rd_base_addr_meta"),
}

UNWAIVED = "RR-CDC-005 unwaived on a DDR-P2.29 base-address capture"
PIN_FAIL = f"ip.lock does not pin the published rr_ddr_stream {RELEASE_VERSION}"
HASH_FAIL = "vendored rr_ddr_stream bytes differ from ip.lock file_hashes"
REQ_FAIL = f"a target still requests an rr_ddr_stream other than {REQUEST}"

_ASSIGN_RE = re.compile(r"^\s*(\w+)\s*<=")


def _cdc005(rel: str) -> list[tuple[str, int, bool]]:
    """(destination, line, waived) for every RR-CDC-005 finding in a vendored file."""
    from routertl_core.lint_checkers.rr_cdc_domain_vhdl import (
        check_rr_cdc_005_unsynchronized_multibit_crossing,
    )
    from routertl_core.lint_runners import _line_suppresses

    path = VENDORED / rel
    text = path.read_text()
    lines = text.splitlines()
    out = []
    for line_no, _col, _msg in check_rr_cdc_005_unsynchronized_multibit_crossing(path, text):
        src = lines[line_no - 1]
        m = _ASSIGN_RE.match(src)
        out.append((m.group(1) if m else src.strip(), line_no, _line_suppresses(RULE, src)))
    return out


def _lock_entry() -> dict:
    return yaml.safe_load(IP_LOCK.read_text())[PKG]


def _declared_requests() -> dict[str, str]:
    """{yml path: requested version} for every target/ref_design declaring the package."""
    got: dict[str, str] = {}
    for p in all_target_files(ROOT):
        spec = (yaml.safe_load(p.read_text()) or {}).get("packages", {}).get(PKG)
        if spec is not None:
            got[str(p.relative_to(ROOT))] = spec["version"] if isinstance(spec, dict) else spec
    for p in sorted(ROOT.glob("ref_design*.yml")):
        for dep in (yaml.safe_load(p.read_text()) or {}).get("uses", []) or []:
            if isinstance(dep, dict) and dep.get("ref") == PKG:
                got[str(p.relative_to(ROOT))] = dep.get("version")
    return got


# ── part cdc005-waivers ───────────────────────────────────────────────────────

def test_rr_cdc_005_base_address_captures_are_waived():
    found = {(rel, dst): waived
             for rel in sorted({r for r, _ in CAPTURES})
             for dst, _line, waived in _cdc005(rel)}
    # The rule must still SEE all four captures. A finding that vanished is
    # not a waiver: it means the checker or the RTL changed, and this guard
    # would otherwise go green on nothing.
    missing = sorted(CAPTURES - set(found))
    assert not missing, f"RR-CDC-005 no longer reports {missing}; re-derive this guard"
    open_ = sorted(k for k, waived in found.items() if not waived)
    assert not open_, f"{UNWAIVED}: {open_}"


# ── part lock-pin ─────────────────────────────────────────────────────────────

def test_ip_lock_pins_the_published_release_and_hashes_match():
    lock = _lock_entry()
    assert (lock["commit"], lock["resolved_version"]) == (RELEASE_COMMIT, RELEASE_VERSION), (
        f"{PIN_FAIL}: pins {lock['commit'][:12]} ({lock['resolved_version']})")
    bad = sorted(rel for rel, want in lock["file_hashes"].items()
                 if not (ROOT / rel).is_file()
                 or hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() != want)
    assert not bad, f"{HASH_FAIL}: {bad}"
    for rel, _dst in CAPTURES:
        assert f"libs/routertl/rr_ddr_stream/{rel}" in lock["file_hashes"], rel


# ── part target-requests ──────────────────────────────────────────────────────

def test_every_declaring_target_requests_the_locked_release():
    got = _declared_requests()
    # 12 targets + 3 ref designs declared the package when it was repinned; DS-P1.103 deleted
    # 4 of those targets (the datalogger mix flavours), so 8 targets + 3 ref designs remain.
    # The public export carries the three products and the four public ref designs (7).
    assert len(got) >= per_tree(7, 11, ROOT), f"only {len(got)} declarations found: {sorted(got)}"
    stale = {k: v for k, v in got.items() if v != REQUEST}
    assert not stale, f"{REQ_FAIL}: {stale}"
    assert _lock_entry()["requested_version"] == REQUEST
