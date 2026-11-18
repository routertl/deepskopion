# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.205: the private half of the deployment contract.

The export drops this file with the PolarFire tree. The target and the riscv64
facts live in sw/linux/pfsoc/deployment_contract.yml, and docs/GOLDEN_PATH.md
does not ship, so the checks on them live here.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_ROOT = ROOT
SCRIPT = ROOT / "sw" / "linux" / "deployment_contract.py"
SIDECAR = ROOT / "sw" / "linux" / "pfsoc" / "deployment_contract.yml"


def _contract():
    spec = importlib.util.spec_from_file_location("deployment_contract_p2_205", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def decl():
    return {"boot": yaml.safe_load(SIDECAR.read_text(encoding="utf-8"))}


def test_pfsoc_datalogger_is_a_declared_linux_target(decl):
    """PF-L0 (DS-P2.125): the third board (PolarFire SoC kit) builds a Linux
    rootfs, so its boot behaviour must be declared here — this is the exact
    violation `check_targets` raised LIVE while targets/pfsoc_datalogger.yml
    shipped a `rootfs:` block before it was listed in boot.targets.

    The second assertion pins the guard to the real precondition: it only
    demands the declaration while the target actually builds a rootfs, so it is
    not a tautology that would still pass if the pfsoc target lost its rootfs.
    """
    assert "pfsoc_datalogger" in decl["boot"]["targets"], (
        "pfsoc_datalogger builds a Linux rootfs but is not a declared Linux "
        "target — the board whose boot behaviour nobody described"
    )
    target = (_ROOT / "targets" / "pfsoc_datalogger.yml").read_text(encoding="utf-8")
    assert "rootfs:" in target, (
        "guard precondition gone: pfsoc_datalogger no longer declares a rootfs, "
        "so revisit whether it still belongs in boot.targets"
    )


def test_the_public_declaration_does_not_list_it():
    contract = _contract()
    assert "pfsoc_datalogger" not in contract.load_declaration()["boot"]["targets"]


def test_the_check_needs_the_sidecar(tmp_path):
    """Without the sidecar the same target is an undeclared board."""
    contract = _contract()
    targets = tmp_path / "targets"
    targets.mkdir()
    (targets / "pfsoc_datalogger.yml").write_text("linux:\n  rootfs: {}\n", encoding="utf-8")
    decl = {"boot": {"targets": ["pfsoc_datalogger"]}}
    assert contract.check_targets(decl, tmp_path) == []

    decl = {"boot": {"targets": ["other"]}}
    (targets / "other.yml").write_text("linux:\n  rootfs: {}\n", encoding="utf-8")
    fails = contract.check_targets(decl, tmp_path)
    assert [f.what for f in fails] == ["targets/pfsoc_datalogger.yml"]

    side = tmp_path / "sw" / "linux" / "pfsoc"
    side.mkdir(parents=True)
    (side / "deployment_contract.yml").write_text("targets: [pfsoc_datalogger]\n", encoding="utf-8")
    assert contract.check_targets(decl, tmp_path) == []


def test_a_sidecar_target_with_no_rootfs_fails(tmp_path):
    contract = _contract()
    targets = tmp_path / "targets"
    targets.mkdir()
    (targets / "other.yml").write_text("linux:\n  rootfs: {}\n", encoding="utf-8")
    side = tmp_path / "sw" / "linux" / "pfsoc"
    side.mkdir(parents=True)
    (side / "deployment_contract.yml").write_text("targets: [gone]\n", encoding="utf-8")
    fails = contract.check_targets({"boot": {"targets": ["other"]}}, tmp_path)
    assert [f.what for f in fails] == ["boot.targets: gone"]


def test_a_sidecar_with_no_target_list_fails(tmp_path):
    contract = _contract()
    targets = tmp_path / "targets"
    targets.mkdir()
    (targets / "other.yml").write_text("linux:\n  rootfs: {}\n", encoding="utf-8")
    side = tmp_path / "sw" / "linux" / "pfsoc"
    side.mkdir(parents=True)
    (side / "deployment_contract.yml").write_text("autostart: []\n", encoding="utf-8")
    fails = contract.check_targets({"boot": {"targets": ["other"]}}, tmp_path)
    assert [f.what for f in fails] == ["sw/linux/pfsoc/deployment_contract.yml"]
