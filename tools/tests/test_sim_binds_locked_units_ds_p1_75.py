"""DS-P1.75 — the sims tools/tests drives compile the units the project locks.

ip.lock pins the RouteRTL single-file units (routertl/edge_detector, ...) to
copies under libs/routertl/, and synthesis builds those. The edge and UART
datalogger sims listed rr's own src/units/ copies instead. Those copies have
moved on (edge_detector: RTL-T3.71), so the sims checked a different edge
detector from the one that is built. rr's duplicate-unit refusal
(RTL-P2.1785) then stopped both sims from running at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SIM_SOURCE_LISTS = {
    "edge": ROOT / "hw/sim/cocotb/tests/datalogger/nonstd/edge_datalogger/test_edge_datalogger.py",
    "uart": ROOT / "hw/sim/cocotb/tests/datalogger/uart/uart_datalogger_test_utils.py",
}
_SDK_UNIT = re.compile(r'\{SDK_SRC_PATH\}/units/(\w+)\.vhd')


def _locked_sdk_units() -> dict[str, str]:
    """unit name -> its locked file, for every ip.lock entry served from rr's src/units."""
    lock = yaml.safe_load((ROOT / "ip.lock").read_text(encoding="utf-8"))
    units = {}
    for entry in lock.values():
        if not isinstance(entry, dict):
            continue
        m = re.fullmatch(r"sdk://src/units/(\w+)\.vhd", str(entry.get("url", "")))
        if m:
            units[m.group(1)] = entry["files"][0]
    return units


def _code(path: Path) -> str:
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))


@pytest.mark.parametrize("sim", sorted(SIM_SOURCE_LISTS))
def test_sim_lists_the_locked_copy_of_every_sdk_unit(sim: str) -> None:
    locked = _locked_sdk_units()
    assert "edge_detector" in locked, f"ip.lock no longer locks routertl/edge_detector: {sorted(locked)}"
    code = _code(SIM_SOURCE_LISTS[sim])
    sdk_copies = sorted(u for u in _SDK_UNIT.findall(code) if u in locked)
    assert not sdk_copies, (
        f"{sim} sim compiles rr's src/units copy of locked unit(s) {sdk_copies}; "
        f"synthesis builds {[locked[u] for u in sdk_copies]}")
    assert locked["edge_detector"] in code, (
        f"{sim} sim does not compile the locked {locked['edge_detector']}")
