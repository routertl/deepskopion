# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""The README says where RouteWave comes from and how a download is checked (DS-P1.127)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
RELEASES = "https://github.com/djmazure/routewave/releases"
# The fingerprint gpg prints for the key that signs RouteWave 2.5.4 and later.
FINGERPRINT = "B545 F7DE 394B 58A2 65F8  3101 BF17 E0A1 F3D0 9ED1"


def _problems(text: str) -> list[str]:
    problems = []
    row = next((line for line in text.splitlines() if line.startswith("| RouteWave |")), "")
    if RELEASES not in row:
        problems.append("the Requirements row does not link the RouteWave release page")
    section = text.split("### RouteWave", 1)[1] if "### RouteWave" in text else ""
    for asset in ("routewave-<version>-windows-x86_64.zip", "RouteWave-<version>-x86_64.AppImage"):
        if asset not in section:
            problems.append(f"the RouteWave section does not name {asset}")
    blocks = re.findall(r"```bash\n(.*?)```", section, flags=re.S)
    block = blocks[0] if blocks else ""
    for step in ("SHA256SUMS.txt", "sha256sum --check", "gpg --verify SHA256SUMS.txt.asc SHA256SUMS.txt",
                 f"{RELEASES}/download/v"):
        if step not in block:
            problems.append(f"the check block lacks {step!r}")
    if f"Primary key fingerprint: {FINGERPRINT}" not in section:
        problems.append("the RouteWave section does not state the signing key fingerprint")
    return problems


def test_the_readme_names_the_source_assets_and_the_check():
    text = README.read_text(encoding="utf-8")
    assert _problems(text) == []


def test_a_readme_without_the_source_or_with_a_wrong_fingerprint_is_reported():
    text = README.read_text(encoding="utf-8")
    assert RELEASES in text and FINGERPRINT in text
    no_link = text.replace(f"[release page]({RELEASES})", "release page", 1)
    assert "the Requirements row does not link the RouteWave release page" in _problems(no_link)
    wrong_key = text.replace(FINGERPRINT, "1BF6 3B05 68BB 7744 3CAE  E91B 038B DB73 DD81 0ECB")
    assert "the RouteWave section does not state the signing key fingerprint" in _problems(wrong_key)
    no_check = text.replace("sha256sum --check", "true", 1)
    assert "the check block lacks 'sha256sum --check'" in _problems(no_check)
