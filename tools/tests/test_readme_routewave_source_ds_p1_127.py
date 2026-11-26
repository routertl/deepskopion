# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""The README says where RouteWave comes from and how a download is checked (DS-P1.127)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
RELEASES = "https://github.com/djmazure/routewave/releases"
NOT_VERIFIED = "RouteRTL does not verify this download yet."


def _problems(text: str) -> list[str]:
    """DS-P3.370 (owner 2026-11-26): RouteWave is installed through RouteRTL; the release page stays for a direct
    download and a manual check, and the README does not imply that RouteRTL verifies the download."""
    problems = []
    row = next((line for line in text.splitlines() if line.startswith("| RouteWave |")), "")
    if not re.match(r"\| RouteWave \| `>= [0-9.]+`, installed through RouteRTL \(see below\) \|$", row):
        problems.append("the Requirements row does not say RouteWave is installed through RouteRTL")
    section = text.split("### RouteWave", 1)[1].split("\n### ", 1)[0] if "### RouteWave" in text else ""
    if f"[release page]({RELEASES})" not in section:
        problems.append("the RouteWave section does not link the RouteWave release page")
    blocks = re.findall(r"```bash\n(.*?)```", section, flags=re.S)
    block = blocks[0] if blocks else ""
    for step in ("pip install routertl", "rr ws update-viewer"):
        if step not in block:
            problems.append(f"the install block lacks {step!r}")
    if "SHA256SUMS.txt" not in section:
        problems.append("the RouteWave section does not name SHA256SUMS.txt")
    if " ".join(NOT_VERIFIED.split()) not in " ".join(section.split()):
        problems.append("the RouteWave section does not say RouteRTL leaves the download unverified")
    return problems


def test_the_readme_names_the_source_assets_and_the_check():
    text = README.read_text(encoding="utf-8")
    assert _problems(text) == []


def test_a_readme_without_the_source_or_with_a_wrong_fingerprint_is_reported():
    text = README.read_text(encoding="utf-8")
    assert RELEASES in text and NOT_VERIFIED.split()[0] in text
    no_link = text.replace(f"[release page]({RELEASES})", "release page", 1)
    assert "the RouteWave section does not link the RouteWave release page" in _problems(no_link)
    no_update = text.replace("rr ws update-viewer", "rr routewave", 1)
    assert "the install block lacks 'rr ws update-viewer'" in _problems(no_update)
    claims_verified = re.sub(r"RouteRTL does not verify\s+this download yet\.", "RouteRTL verifies the download.", text)
    assert "the RouteWave section does not say RouteRTL leaves the download unverified" in _problems(claims_verified)
    row = text.replace("| RouteWave | `>= 2.4.0`, installed through RouteRTL (see below) |", "| RouteWave | `>= 2.4.0` |")
    assert "the Requirements row does not say RouteWave is installed through RouteRTL" in _problems(row)
    # A row that names RouteRTL without saying RouteWave is installed through it is reported too.
    near = text.replace("| RouteWave | `>= 2.4.0`, installed through RouteRTL (see below) |",
                        "| RouteWave | `>= 2.4.0`, see RouteRTL (below) |")
    assert "the Requirements row does not say RouteWave is installed through RouteRTL" in _problems(near)
    negated = text.replace("| RouteWave | `>= 2.4.0`, installed through RouteRTL (see below) |",
                           "| RouteWave | `>= 2.4.0`, not installed through RouteRTL (see below) |")
    assert "the Requirements row does not say RouteWave is installed through RouteRTL" in _problems(negated)
