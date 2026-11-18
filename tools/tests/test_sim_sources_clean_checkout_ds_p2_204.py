# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.204: a default-target sim does not name an opt-in core.

The core-seam suite listed the CTU CAN FD sources by path. That core left the lock
when CAN became opt-in, so the suite failed at analysis on every clean checkout,
and nothing reported it. A checkout holding a copy of the core hid the failure.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGETS = ("targets/zybo_datalogger.yml", "targets/de25_datalogger.yml")
# A source path built in Python: f"{root}/libs/x/...", or root / "libs" / "x".
PATH_RE = re.compile(
    r"""[}"'/]/?libs/(?P<a>[\w.-]+)/|["']libs["']\s*/\s*["'](?P<b>[\w.-]+)["']"""
)


def _packages(target: str) -> set[str]:
    data = yaml.safe_load((ROOT / target).read_text(encoding="utf-8"))
    return set(data.get("packages") or {})


def named_opt_in_sources(text: str, namespaces: set[str]) -> list[str]:
    """Return each opt-in library directory a sim's source text builds a path to."""
    found = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in PATH_RE.finditer(line):
            if (match.group("a") or match.group("b")) in namespaces:
                found.append(line.strip())
    return found


def _sim_python_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "hw/sim/*.py", "hw/sim/**/*.py"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    ).stdout
    return sorted(set(out.split()))


@pytest.mark.parametrize("line", [
    'f"{DEEPSKOPION_ROOT}/libs/ctu-fee/ctucanfd_ip_core/src/can_top_apb.vhd",',
    '"libs/ctu-fee/ctucanfd_ip_core/src/can_top_apb.vhd",',
    'ROOT / "libs" / "ctu-fee" / "ctucanfd_ip_core",',
])
def test_the_scan_reports_each_way_of_naming_the_path(line):
    assert named_opt_in_sources(line, {"ctu-fee"}) == [line]


@pytest.mark.parametrize("line", [
    'f"{root}/libs/routertl/rr_ddr_stream/rtl/ddr_stream_pkg.vhd",',
    '# the oracle comes from libs/ctu-fee/ctucanfd_ip_core',
    'f"{r}/tools/sim/vendor_stubs/ctu_can_fd/can_top_apb_stub.vhd",',
])
def test_the_scan_ignores_other_libraries_comments_and_the_stub(line):
    assert named_opt_in_sources(line, {"ctu-fee"}) == []


def test_the_core_seam_suite_uses_the_stub():
    common = ROOT / "hw/sim/cocotb/tests/stimulus/core_seam/_core_seam_common.py"
    assert "vendor_stubs/ctu_can_fd/can_top_apb_stub.vhd" in common.read_text(encoding="utf-8")
