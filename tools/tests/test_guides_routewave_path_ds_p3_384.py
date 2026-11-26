# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P3.384: the board guides call RouteWave the way the README unpacks it, and the preflight page lists every stage.

The README unpacks RouteWave without putting it on PATH and calls it by path (``RW=...``). A guide that runs a bare
``routewave`` fails with "command not found" for a reader who followed the README, and a guide whose RW path differs
from the README's breaks when only one of them is updated.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUIDES = [ROOT / "docs/getting_started" / n for n in ("polarfire_soc_discovery.md", "de25.md", "zybo_z7_20.md")]
SUPPORTED = ROOT / "docs/SUPPORTED_ENVIRONMENT.md"
sys.path.insert(0, str(ROOT / "tools" / "preflight"))
import ds_preflight  # noqa: E402

BARE = re.compile(r"(^|[(;&|]\s*)routewave\s")


def _bash_lines(text: str) -> list[str]:
    blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
    return [ln.strip() for b in blocks for ln in b.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _readme_rw() -> str:
    """DS-P3.370: the README installs RouteWave with rr ws update-viewer and names the folder in its install block;
    the guides' RW is the program in that folder."""
    m = re.search(r"rr ws update-viewer\s+# installs RouteWave in (\S+)/", (ROOT / "README.md").read_text(encoding="utf-8"))
    assert m, "the README does not name the folder rr ws update-viewer installs RouteWave in"
    return f"RW={m.group(1)}/routewave"


@pytest.mark.parametrize("guide", GUIDES, ids=lambda p: p.stem)
def test_a_guide_calls_routewave_by_the_readme_path(guide):
    lines = _bash_lines(guide.read_text(encoding="utf-8"))
    assert [ln for ln in lines if BARE.search(ln)] == [], f"{guide.name} calls a bare routewave"
    calls = [ln for ln in lines if '"$RW"' in ln]
    assert calls, f"{guide.name} does not attach RouteWave"
    assert [ln for ln in lines if ln.startswith("RW=")] == [_readme_rw()], guide.name


def test_the_bare_call_pattern_catches_the_old_guide_lines():
    """Control: the pattern matches the line the guides used to carry, alone and inside a subshell."""
    for line in ('routewave -c "stream attach $BOARD_IP:7475"', 'routewave -c "stream attach $BOARD_IP:7475" )',
                 '( routewave -c "x" )'):
        assert BARE.search(line), line
    assert not BARE.search('"$RW" -c "stream attach $BOARD_IP:7475"')


def test_the_preflight_page_lists_every_stage():
    block = re.search(r"## Running the check\n\n```sh\n(.*?)```", SUPPORTED.read_text(encoding="utf-8"), re.S)
    assert block, "SUPPORTED_ENVIRONMENT.md has no Running the check block"
    listed = set(re.findall(r"--stage (\w+)", block.group(1)))
    assert listed == set(ds_preflight.STAGES), (sorted(listed), ds_preflight.STAGES)
    assert "`all` runs `build` and `bench`, not `linux`." in SUPPORTED.read_text(encoding="utf-8")
