# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.306: a commit that adds an HDL file no lock lists yet is judged against every target lock.

ccc83cb0 (hw/sim/tb_pfsoc_fic0_regbank_read.vhd) and 906b6a14 (hw/sim/tb_default_mix_core.vhd) each added one
bench file without refreshing the per-target locks and passed the commit hook: the leg checks a target only when
the commit stages a path that target's lock lists, and a new file is in no lock, so it checked nothing. Afterwards
`lock_check_leg.py --all` reported all 9 product locks STALE. These tests replay that shape in the scratch
repository of test_lock_check_leg_per_target_ds_p2_287.py (the hook's own leg, a stand-in rr).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "lock_leg_per_target_p2_306", Path(__file__).resolve().parent / "test_lock_check_leg_per_target_ds_p2_287.py")
base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(base)


def _stage_new(repo: Path, rel: str, body: str = "-- new bench\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    base._git(repo, "add", rel)


def test_an_added_vhd_with_no_lock_refresh_is_refused(tmp_path):
    """The ccc83cb0 shape: one new .vhd, no lock refresh, the new file enters beta's closure (stand-in rr: beta
    stale). Red on the leg before DS-P2.306, which checked no target and passed."""
    repo = base._repo(tmp_path)
    _stage_new(repo, "hw/sim/tb_new_bench.vhd")
    r = base._run_leg(repo, base._fake_rr(tmp_path, stale="beta"))
    assert r.returncode != 0 and "LEG_PASSED" not in r.stdout, r.stdout + r.stderr
    assert "beta: STALE" in r.stdout, r.stdout


def test_positive_control_an_added_vhd_with_locks_in_step_checks_every_target_and_passes(tmp_path):
    repo = base._repo(tmp_path)
    _stage_new(repo, "hw/sim/tb_new_bench.vhd")
    r = base._run_leg(repo, base._fake_rr(tmp_path))
    assert r.returncode == 0 and "LEG_PASSED" in r.stdout, r.stdout + r.stderr
    assert base._checked_targets(tmp_path) == ["alpha", "beta"]


def test_an_added_constraint_file_checks_every_target(tmp_path):
    repo = base._repo(tmp_path)
    _stage_new(repo, "hw/xdc/new_pins.xdc", "set_property PULLUP true [get_ports {ja[0]}]\n")
    r = base._run_leg(repo, base._fake_rr(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert base._checked_targets(tmp_path) == ["alpha", "beta"]


def test_an_added_non_hdl_file_still_checks_nothing(tmp_path):
    """The narrowing stays: a new doc enters no target's closure."""
    repo = base._repo(tmp_path)
    _stage_new(repo, "docs/new_page.md", "# New\n")
    r = base._run_leg(repo, base._fake_rr(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert base._checked_targets(tmp_path) == []
