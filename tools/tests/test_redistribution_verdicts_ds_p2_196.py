# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.196: every vendor-generated or third-party path has exactly one verdict.

``docs/REDISTRIBUTION.md`` is the record the public export follows. These tests
read the real document and the real git index, so a new ``.bin`` or a new file
under ``ip/`` turns them red until someone decides what happens to it.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "REDISTRIBUTION.md"
sys.path.insert(0, str(ROOT / "tools"))

from public_artifact_hygiene import _matches_drop_list  # noqa: E402

VERDICTS = {"SHIP", "REGENERATE", "DROP", "OPEN"}
ROW_RE = re.compile(r"^\| `([^`]+)` \| ([A-Z]+) \| (.+) \|$")
REQUIRED_RE = re.compile(
    r"^(ip/|out/)|^docs/(.*/)?evidence/|\.(bin|job|qsys|ip)$"
)
SPDX_RE = re.compile(r"\b(MIT|CERN-OHL-W-2\.0|MPL-2\.0|Apache-2\.0|BSD-[0-9])\b")


def parse_rows(text: str) -> list[tuple[str, str, str]]:
    rows = []
    for line in text.splitlines():
        match = ROW_RE.match(line)
        if match:
            rows.append(match.groups())
    return rows


def pattern_regex(pattern: str) -> re.Pattern[str]:
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(f"^{out}$")


def verdict_rows_for(path: str, rows: list[tuple[str, str, str]]) -> list[str]:
    return [p for p, _, _ in rows if pattern_regex(p).match(path)]


def coverage_errors(paths: list[str], rows: list[tuple[str, str, str]]) -> list[str]:
    """Name each required path without exactly one verdict."""
    errors = []
    for path in paths:
        matched = verdict_rows_for(path, rows)
        in_drop_list = _matches_drop_list(path) is not None
        # The drop list enforces the document's DROP rows, so it is a verdict of
        # its own only for a path that has no row.
        count = len(matched) or (1 if in_drop_list else 0)
        if count == 0 and REQUIRED_RE.search(path):
            errors.append(f"{path}: no verdict")
        elif count > 1:
            errors.append(f"{path}: {count} verdicts: {matched}")
    return errors


def tracked_paths() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


@pytest.fixture(scope="module")
def rows() -> list[tuple[str, str, str]]:
    parsed = parse_rows(DOC.read_text(encoding="utf-8"))
    assert parsed, "docs/REDISTRIBUTION.md has no verdict rows"
    return parsed


def test_every_required_path_has_exactly_one_verdict(rows):
    assert coverage_errors(tracked_paths(), rows) == []


def test_an_uncovered_vendor_path_is_reported():
    rows = [("ip/known/**", "DROP", "x")]
    assert coverage_errors(["ip/new/vendor.v", "ip/known/a.v"], rows) == [
        "ip/new/vendor.v: no verdict"
    ]


def test_a_path_with_two_verdicts_is_reported():
    rows = [("ip/**", "DROP", "x"), ("ip/a.v", "SHIP", "MIT")]
    assert len(coverage_errors(["ip/a.v"], rows)) == 1


def test_a_vendor_extension_outside_the_named_directories_is_required():
    assert coverage_errors(["hw/new/blob.bin"], []) == ["hw/new/blob.bin: no verdict"]


def test_every_verdict_is_a_known_word(rows):
    assert {verdict for _, verdict, _ in rows} <= VERDICTS


def test_every_row_matches_a_tracked_file(rows):
    tracked = tracked_paths()
    # A public export has none of the dropped files, by design.
    private = (ROOT / "context.json").is_file()
    stale = [p for p, verdict, _ in rows
             if (private or verdict != "DROP")
             and not any(pattern_regex(p).match(path) for path in tracked)]
    assert stale == []


def test_every_ship_row_names_its_licence(rows):
    missing = [p for p, verdict, basis in rows
               if verdict == "SHIP" and not SPDX_RE.search(basis)]
    assert missing == []


def test_every_regenerate_row_names_an_rr_command(rows):
    missing = [p for p, verdict, basis in rows
               if verdict == "REGENERATE" and "`rr " not in basis]
    assert missing == []


def test_open_rows_are_limited_to_the_intel_files(rows):
    open_rows = {p for p, verdict, _ in rows if verdict == "OPEN"}
    assert open_rows <= {
        "ip/agilex_hps/agilex_hps.ip",
        "ip/agilex_hps/agilex_hps/**",
        "ip/de25_fabric_pll/**",
        "ip/emif_io96b_hps/emif_io96b_hps.ip",
    }


def test_no_verdict_is_open(rows):
    """DS-P2.196: the DE25 target regenerates its Intel IP (hps_regenerate,
    vendor_ip_variants), so no vendor file is left whose verdict is open."""
    assert [p for p, verdict, _ in rows if verdict == "OPEN"] == []


@pytest.mark.parametrize("name", [
    "README.md",
    "docs/REDISTRIBUTION.md",
])  # DS-P1.103 param split: the two bench target files run in the _bench.py sibling
def test_ctu_can_fd_licence_and_patent_notice_are_stated(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert "LicenseRef-CTU-CAN-FD-NonCommercial" in text
    assert re.search(r"Bosch.*patent|patent.*Bosch", text, re.IGNORECASE)


ITEM_REF_RE = re.compile(r"\bitem (\d+)\b", re.IGNORECASE)


def items_to_confirm(text: str) -> dict[int, str]:
    """Numbered entries of the 'Items to confirm' section, by number."""
    section = text.split("## Items to confirm", 1)
    if len(section) < 2:
        return {}
    body = section[1].split("\n## ", 1)[0]
    return {int(m.group(1)): m.group(2)
            for m in re.finditer(r"^(\d+)\. (.+)$", body, re.MULTILINE)}


def item_reference_errors(text: str) -> list[str]:
    """Every 'item N' a verdict row cites must exist and name that row's file."""
    items = items_to_confirm(text)
    errors = []
    for path, _verdict, reason in parse_rows(text):
        for ref in ITEM_REF_RE.findall(reason):
            entry = items.get(int(ref))
            if entry is None:
                errors.append(f"{path}: cites item {ref}, which does not exist")
            elif Path(path).name not in entry:
                errors.append(f"{path}: cites item {ref}, which does not name "
                              f"{Path(path).name}")
    return errors


def test_every_cited_item_exists_and_names_the_row_file():
    """DS-P2.196: removing an item renumbers the rest; a row still citing the
    old number points at the wrong item or at none."""
    assert item_reference_errors(DOC.read_text(encoding="utf-8")) == []


def test_a_row_citing_a_missing_item_is_reported():
    text = ("| `a/x.tar.gz` | DROP | Release asset, subject to item 2. |\n\n"
            "## Items to confirm\n\n1. Terms for `a/x.tar.gz`.\n")
    assert item_reference_errors(text) == [
        "a/x.tar.gz: cites item 2, which does not exist"]


def test_a_row_citing_an_item_about_another_file_is_reported():
    text = ("| `a/x.tar.gz` | DROP | Release asset, subject to item 1. |\n\n"
            "## Items to confirm\n\n1. Terms for `b/y.bin`.\n")
    assert item_reference_errors(text) == [
        "a/x.tar.gz: cites item 1, which does not name x.tar.gz"]
