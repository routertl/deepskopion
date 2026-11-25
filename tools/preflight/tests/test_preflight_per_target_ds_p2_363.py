# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.363: the build preflight judges the SELECTED target against that target's own declaration.

Before, tools/preflight/supported_environment.yml declared one board (Zybo Z7-20), one tool (Vivado 2024.1) and the
Zynq traffic-source DT nodes, and `ds_preflight.py --stage build` compared every target with them: on de25_datalogger
it exited 1 with three false MISSING (target.board, target.tool_version, traffic_source.dt_nodes). Each release
target now has a `targets.<name>.build` block; a target without one is refused by name.
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import shutil
import sys

import pytest
import yaml

_PREFLIGHT_DIR = pathlib.Path(__file__).resolve().parents[1]
_ROOT = _PREFLIGHT_DIR.parents[1]
sys.path.insert(0, str(_PREFLIGHT_DIR))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ds_preflight as P                                   # noqa: E402
from test_ds_preflight import _ok_probe                    # noqa: E402

TARGETS = {
    "zybo_datalogger": ("xc7z020clg400-1 on zybo-z7-20", "2024.1"),
    "de25_datalogger": ("A5ED013BB32AE4SCS on de25-standard", "25.3.1"),
    "pfsoc_datalogger": ("MPFS095T-1FCSG325E on polarfire-soc-discovery", "2025.2"),
}
TARGET_CHECKS = ("target.board", "target.tool_version", "traffic_source.dt_nodes")


@contextlib.contextmanager
def _selected(path: pathlib.Path):
    previous = os.environ.get("RR_TARGET")
    os.environ["RR_TARGET"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("RR_TARGET", None)
        else:
            os.environ["RR_TARGET"] = previous


def _clone(tmp: pathlib.Path, name: str) -> pathlib.Path:
    root = tmp / "clone"
    for rel in ("project.yml", f"targets/{name}.yml", "tools/datalogger-builder/config/datalogger_config.yml"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_ROOT / rel, root / rel)
    return root


def _results(root: pathlib.Path, name: str) -> dict:
    with _selected(root / "targets" / f"{name}.yml"):
        return {r.id: r for r in P.run_checks("build", probe=_ok_probe(), root=root)}


def _mutate(root: pathlib.Path, name: str, fn) -> None:
    path = root / "targets" / f"{name}.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_each_release_target_passes_its_own_target_checks(tmp_path, name):
    r = _results(_clone(tmp_path, name), name)
    for cid in TARGET_CHECKS:
        assert r[cid].ok, (cid, r[cid].found)
    assert r["target.board"].found == TARGETS[name][0]
    assert r["target.tool_version"].found == TARGETS[name][1]


def test_only_the_vivado_target_probes_the_host_tool(tmp_path):
    zybo = _results(_clone(tmp_path / "z", "zybo_datalogger"), "zybo_datalogger")
    assert "vivado.version" in zybo and "tool.version" not in zybo
    for name in ("de25_datalogger", "pfsoc_datalogger"):
        r = _results(_clone(tmp_path / name, name), name)
        assert "vivado.version" not in r
        assert r["tool.version"].ok and r["tool.version"].found.startswith("not probed")


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_a_wrong_part_fails_for_every_target(tmp_path, name):
    root = _clone(tmp_path, name)
    _mutate(root, name, lambda d: d["hardware"].update(part="XC_WRONG_PART"))
    r = _results(root, name)["target.board"]
    assert not r.ok and r.blocking and "XC_WRONG_PART" in r.found


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_a_wrong_tool_pin_fails_for_every_target(tmp_path, name):
    root = _clone(tmp_path, name)
    _mutate(root, name, lambda d: d["build_options"].update(tool_version="1999.9"))
    r = _results(root, name)["target.tool_version"]
    assert not r.ok and r.blocking and r.found == "1999.9"


@pytest.mark.parametrize("name", ["de25_datalogger", "pfsoc_datalogger"])
def test_dropping_the_reserved_ring_node_fails(tmp_path, name):
    root = _clone(tmp_path, name)
    _mutate(root, name, lambda d: d["linux"].update(dt_nodes=[
        n for n in d["linux"]["dt_nodes"] if n.get("path") != "/reserved-memory/deepskopion-ring@a0000000"]))
    r = _results(root, name)["traffic_source.dt_nodes"]
    assert not r.ok and r.blocking and "deepskopion-ring@a0000000" in r.found


def test_an_undeclared_target_is_refused_by_name(tmp_path):
    root = _clone(tmp_path, "de25_datalogger")
    shutil.copy2(root / "targets/de25_datalogger.yml", root / "targets/new_board.yml")
    r = _results(root, "new_board")
    for cid in ("target.board", "target.tool_version", "traffic_source.dt_nodes", "vivado.version"):
        assert not r[cid].ok and r[cid].blocking, cid
        assert r[cid].found == "target new_board is not declared in supported_environment.yml"


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_each_declaration_matches_its_target_yml(name):
    d = P.load_declaration()
    build = d["targets"][name]["build"]
    target = yaml.safe_load((_ROOT / "targets" / f"{name}.yml").read_text(encoding="utf-8"))
    assert build["part"] == target["hardware"]["part"]
    assert build["platform"] == target["hardware"]["platform"]
    assert str(build["tool"]["version"]) == str(target["build_options"]["tool_version"])
    dt = {n.get("path") for n in target["linux"]["dt_nodes"] if isinstance(n, dict)}
    assert set(build.get("required_dt_nodes") or []) <= dt


def test_the_zybo_declaration_is_the_golden_path_sections():
    d = P.load_declaration()
    build = d["targets"]["zybo_datalogger"]["build"]
    assert (build["part"], build["platform"]) == (d["board"]["part"], d["board"]["platform"])
    assert str(build["tool"]["version"]) == str(d["vivado"]["version"]) and build["tool"]["probe"] == "vivado"
    assert build["traffic_source"] == "golden_path"
