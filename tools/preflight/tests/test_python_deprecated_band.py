# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""Python 3.10 is outside the supported envelope but still builds: the build preflight warns, it does not fail.

The supported envelope starts at Python 3.11 because RouteRTL drops 3.10 in a coming minor release. Nothing in
DeepSkopion needs 3.11, and the RouteRTL releases DeepSkopion is verified against still run on 3.10, so a 3.10 host
can build a bitstream today. Failing the build there refused every Zybo build on an Ubuntu 22.04 host whose rr runs on
the system 3.10. The declaration keeps min 3.11 and adds a deprecated band from 3.10: inside it the check is a WARN
naming the deprecation; below it, or at the upper bound, it is still a FAIL.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PREFLIGHT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PREFLIGHT_DIR))
import ds_preflight as P  # noqa: E402


def _python_result(version):
    d = P.load_declaration()
    probe = P.Probe(python_version=version)
    return P._check_python(probe, d, _PREFLIGHT_DIR.parents[1])


def test_the_declaration_keeps_311_as_the_supported_minimum_and_names_a_310_deprecated_band():
    d = P.load_declaration()["python"]
    assert d["min"] == "3.11" and d["max_exclusive"] == "3.15" and d["severity"] == "fail"
    assert d["deprecated"]["min"] == "3.10"


def test_a_310_interpreter_is_a_warning_that_does_not_fail_the_build():
    r = _python_result((3, 10, 12))
    assert not r.ok and r.severity == "warn" and not r.blocking, r
    assert "3.10.12" in r.found and "deprecated" in r.requirement.lower(), r


@pytest.mark.parametrize("version", [(3, 11, 0), (3, 12, 3), (3, 14, 1)])
def test_a_supported_interpreter_passes(version):
    r = _python_result(version)
    assert r.ok, r


@pytest.mark.parametrize("version", [(3, 9, 18), (3, 15, 0), None])
def test_below_the_deprecated_band_at_the_upper_bound_or_unknown_still_fails(version):
    r = _python_result(version)
    assert not r.ok and r.severity == "fail" and r.blocking, r


def test_the_build_stage_passes_on_a_310_host_that_meets_everything_else(monkeypatch):
    """The whole build stage, not just the one check: 3.10 alone must not make it exit non-zero."""
    import test_ds_preflight as T
    probe = T._ok_probe()
    probe.python_version = (3, 10, 12)
    with T._select_xilinx_target(_PREFLIGHT_DIR.parents[1]):
        results = P.run_checks("build", probe=probe)
    blocking = [r.id for r in results if r.blocking]
    assert blocking == [], blocking
    assert [r.id for r in results if not r.ok] == ["python"]
