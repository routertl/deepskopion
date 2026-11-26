# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""The README's Live capture section shows a real GIF with its provenance, and the protocol list names CAN FD (DS-P3.370).

Owner 2026-11-26: the section is "Live capture", the whole RouteWave window is shown as a GIF, and CAN FD is listed as a
supported protocol.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
GIF_REL = "docs/images/live_capture.gif"
MAX_BYTES = 2_000_000


def _section(text: str, heading: str) -> str:
    if heading not in text:
        return ""
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def _problems(text: str, gif: bytes | None) -> list[str]:
    problems = []
    live = _section(text, "\n## Live capture\n")
    if not live:
        problems.append("the README has no 'Live capture' section")
    if f"]({GIF_REL})" not in live:
        problems.append("the Live capture section does not show the GIF")
    if gif is None:
        problems.append("the GIF is not in the repository")
    else:
        if not gif.startswith((b"GIF87a", b"GIF89a")):
            problems.append("the GIF file is not a GIF")
        if len(gif) > MAX_BYTES:
            problems.append(f"the GIF is larger than {MAX_BYTES} bytes")
    if not re.search(r"Recorded \d{4}-\d{2}-\d{2} with RouteWave \d+\.\d+\.\d+ installed by RouteRTL \d+\.\d+\.\d+,"
                     r"\s+on the board image built from\s+commit `[0-9a-f]{8,40}`\.", live):
        problems.append("the GIF has no provenance line (date, RouteWave and RouteRTL versions, commit)")
    if re.search(r"/home/|/tmp/|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", live):
        problems.append("the Live capture section carries a host path or an address")
    protocols = _section(text, "\n## Supported Protocols\n")
    if "\n* CAN FD\n" not in protocols:
        problems.append("Supported Protocols does not list CAN FD")
    return problems


def _gif() -> bytes | None:
    path = ROOT / GIF_REL
    return path.read_bytes() if path.is_file() else None


def test_the_live_capture_section_shows_the_gif_with_its_provenance():
    text = README.read_text(encoding="utf-8")
    problems = _problems(text, _gif())
    assert problems == []
    live = _section(text, "\n## Live capture\n")
    assert f"]({GIF_REL})" in live


def test_a_missing_gif_provenance_or_can_fd_row_is_reported():
    text = README.read_text(encoding="utf-8")
    gif = _gif()
    assert gif is not None and _problems(text, gif) == []
    assert "the GIF is not in the repository" in _problems(text, None)
    assert "the GIF file is not a GIF" in _problems(text, b"\x89PNG" + gif[4:])
    assert f"the GIF is larger than {MAX_BYTES} bytes" in _problems(text, gif[:6] + b"\0" * MAX_BYTES)
    no_prov = re.sub(r"Recorded \d{4}-\d{2}-\d{2} with RouteWave", "Made with RouteWave", text)
    assert "the GIF has no provenance line (date, RouteWave and RouteRTL versions, commit)" in _problems(no_prov, gif)
    renamed = text.replace("\n## Live capture\n", "\n## Captures\n")
    assert "the README has no 'Live capture' section" in _problems(renamed, gif)
    no_can_fd = text.replace("* CAN\n* CAN FD\n", "* CAN\n")
    assert "Supported Protocols does not list CAN FD" in _problems(no_can_fd, gif)
    leaked = text.replace("they arrive.\n", "they arrive from 198.51.100.7.\n", 1)
    assert "the Live capture section carries a host path or an address" in _problems(leaked, gif)
