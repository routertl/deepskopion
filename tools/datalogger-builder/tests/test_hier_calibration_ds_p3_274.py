# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P3.274: a calibration prefix that matches no row of a hierarchy CSV is an error, never zero resources.

DS-P2.326 moved the PolarFire core under `u_fabric/`, so the pre-refactor gather prefix
`u_core/u_datalogger_gather/` matches nothing in a post-refactor Libero hierarchy CSV. Summed with a plain
`startswith` filter that reads as a gather block of 0 4LUT / 0 DFF / 0 LSRAM, a plausible calibration.
hier_calibration.rows_under (and its CLI) refuse it, naming the prefix and the CSV.

The post-refactor case is a fixture cut from the 43b88faa pfsoc_datalogger build's compile netlist CSV
(pfsoc_hier_u_fabric_excerpt.csv: the header and every row under u_fabric/u_core/u_datalogger_gather/ plus
three rows outside it), so the guard is exercised on the real hierarchy shape.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from test_cli_configurator import _FIXTURES_DIR  # also puts tools/datalogger-builder on sys.path

import hier_calibration as hc  # noqa: E402

TOOL = Path(hc.__file__)
OLD_FIXTURE = _FIXTURES_DIR / "pfsoc_compile_netlist_hier_resources.csv"
POST_REFACTOR = _FIXTURES_DIR / "pfsoc_hier_u_fabric_excerpt.csv"
OLD_GATHER = "u_core/u_datalogger_gather/"
NEW_GATHER = "u_fabric/u_core/u_datalogger_gather/"


def test_old_prefix_on_the_post_refactor_hierarchy_is_refused_with_its_name():
    rows = hc.load_hier_csv(POST_REFACTOR)
    with pytest.raises(hc.CalibrationError) as exc:
        hc.rows_under(rows, OLD_GATHER, POST_REFACTOR)
    assert OLD_GATHER in str(exc.value) and POST_REFACTOR.name in str(exc.value)


def test_cli_exits_non_zero_and_names_the_prefix_on_a_zero_match():
    r = subprocess.run([sys.executable, str(TOOL), str(POST_REFACTOR), OLD_GATHER],
                       capture_output=True, text=True, errors="replace")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert OLD_GATHER in r.stderr and POST_REFACTOR.name in r.stderr
    assert r.stdout == ""


def test_cli_sums_the_post_refactor_gather_under_u_fabric():
    r = subprocess.run([sys.executable, str(TOOL), str(POST_REFACTOR), NEW_GATHER],
                       capture_output=True, text=True, errors="replace")
    assert r.returncode == 0, r.stderr
    fields = dict(f.split("=") for f in r.stdout.strip().split("\t")[1:])
    assert int(fields["rows"]) == 185
    assert int(fields["4LUT"]) > 0 and int(fields["DFF"]) > 0


def test_the_pre_refactor_fixture_still_sums_under_its_own_prefix():
    rows = hc.load_hier_csv(OLD_FIXTURE)
    assert len(hc.rows_under(rows, OLD_GATHER, OLD_FIXTURE)) == 189


def test_a_file_that_is_not_a_hierarchy_report_is_refused(tmp_path):
    bad = tmp_path / "x.csv"
    bad.write_text("Module Name,Foo\nu_core,1\n", encoding="utf-8")
    with pytest.raises(hc.CalibrationError):
        hc.load_hier_csv(bad)
