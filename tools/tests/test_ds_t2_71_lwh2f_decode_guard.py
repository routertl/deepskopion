"""DS-T2.71: the build must refuse a dt_node the fabric does not decode.

Boot7/boot8 (2026-08-23) is the configuration under test.  Two CTU CAN-FD
nodes were declared ENABLED at 0x20010000/0x20020000, ``rr linux dtb`` turned
them into a live board DTB, and the kernel wedged the NoC byte-identically
right after "CAN device driver interface" — console dead, cold power cycle to
recover.  Nothing in the build refused it, because a dt_node is free text
until silicon dereferences it.

Every assertion below is made at a surface a caller actually reaches: the
``tools/de25_linux_image.py`` CLI's exit code and its printed diagnosis.  The
build leg additionally proves the crosscheck runs BEFORE any ``rr`` step, by
putting a recording stub named ``rr`` first on PATH and asserting it was never
invoked — a detector wired in after ``rr linux dtb`` would still ship the dtb
that kills the board.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = Path(
    os.environ.get("DE25_TEST_ROOT", Path(__file__).resolve().parents[2])
).resolve()
TOOL = ROOT / "tools/de25_linux_image.py"
TARGET_REL = "targets/de25_datalogger.yml"
MAP_REL = "sw/linux/de25/system_memory_map.yml"
# Everything the crosscheck reads: the declared nodes, the declared map, and
# the two RTL files it derives the real decode windows from.
_FIXTURE_FILES = (
    TARGET_REL,
    MAP_REL,
    "hw/rtl/de25/deepskopion_de25_top.vhd",
    "hw/rtl/de25/axil_demux3.vhd",
    "project.yml",
)
_CAN_NODES = ("/soc@0/can@20010000", "/soc@0/can@20020000")


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    for rel in _FIXTURE_FILES:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    return root


def _run(root: Path, command: str, *, path_prefix: Path | None = None):
    env = os.environ.copy()
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        [sys.executable, str(TOOL), command, "--root", str(root)],
        capture_output=True, text=True, env=env,
    )


def _set_can_status(root: Path, status: str | None) -> None:
    """Rewrite both CAN dt_nodes to (or away from) ``status: disabled``."""
    path = root / TARGET_REL
    target = yaml.safe_load(path.read_text(encoding="utf-8"))
    nodes = target["linux"].setdefault("dt_nodes", [])
    existing = {n.get("path"): n for n in nodes if n.get("path") in _CAN_NODES}
    for p in _CAN_NODES:
        if p not in existing:
            node = {
                "path": p,
                "properties": {
                    "compatible": "ctu,ctucanfd",
                    "reg": [0x20010000 if "2001" in p else 0x20020000, 0x10000],
                    "interrupts": [0, 18 if "2001" in p else 19, 4],
                    "clocks": [0x7F01],
                },
            }
            nodes.append(node)
            existing[p] = node
    for path_str, node in existing.items():
        if status is None:
            node["properties"].pop("status", None)
        else:
            node["properties"]["status"] = status
    path.write_text(yaml.safe_dump(target, sort_keys=False), encoding="utf-8")


def _drop_map_devices(root: Path, names: set[str]) -> None:
    path = root / MAP_REL
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    before = len(data["devices"])
    data["devices"] = [d for d in data["devices"] if d.get("name") not in names]
    assert len(data["devices"]) == before - len(names), "fixture lost a device"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _boot78_tree(tmp_path: Path) -> Path:
    """The exact boot7/boot8 shape: CAN nodes ENABLED, map never declares them."""
    root = _tree(tmp_path)
    _set_can_status(root, None)
    _drop_map_devices(root, {"canfd0", "canfd1"})
    return root


# ── the shipped configuration ────────────────────────────────────────────


def test_shipped_configuration_passes_the_crosscheck(monkeypatch) -> None:
    """Positive control: the tree as committed must build."""
    monkeypatch.setenv("RR_TARGET", str(ROOT / TARGET_REL))  # DS-P2.342: DE25 whatever `rr target set` chose here
    done = _run(ROOT, "check-dt-decode")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "crosscheck passed" in done.stdout


def test_can_nodes_are_enabled_and_their_windows_declared() -> None:
    """DS-P2.121: de25_datalogger is CAN-free by default; CTU nodes are omitted."""
    target = yaml.safe_load((ROOT / TARGET_REL).read_text(encoding="utf-8"))
    nodes = {n["path"]: n for n in target["linux"]["dt_nodes"]
             if n.get("path") in _CAN_NODES}
    assert set(nodes) == set(), (
        f"CTU CAN nodes {nodes} present in default target {TARGET_REL}; "
        "DS-P2.121 requires de25_datalogger to be CAN-free"
    )
    windows = {
        (int(d["base_addr"]), int(d["span"]))
        for d in yaml.safe_load((ROOT / MAP_REL).read_text(encoding="utf-8"))["devices"]
    }
    assert (0x20010000, 0x10000) in windows
    assert (0x20020000, 0x10000) in windows


# ── direction A: an enabled node no map declares ─────────────────────────


def test_boot78_configuration_is_refused_by_the_cli(tmp_path: Path) -> None:
    """The configuration that wedged the NoC must not survive the check."""
    done = _run(_boot78_tree(tmp_path), "check-dt-decode")
    assert done.returncode == 1
    message = done.stdout + done.stderr
    for path in _CAN_NODES:
        assert path in message, message
    assert "is ENABLED at 0x20010000+0x10000" in message
    assert "boot7/boot8" in message


def test_build_refuses_boot78_before_invoking_any_rr_step(tmp_path: Path) -> None:
    """The check must gate the build, not merely exist.

    ``rr linux dtb`` is what turns dt_nodes into a live dtb.  A stub named
    ``rr`` records every invocation; a refusal that lets even one rr step run
    first is a detector wired in too late.
    """
    root = _boot78_tree(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    receipt = tmp_path / "rr-invocations.txt"
    stub = bindir / "rr"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{receipt}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    done = _run(root, "build", path_prefix=bindir)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "lwh2f decode crosscheck refused" in done.stdout + done.stderr
    assert not receipt.exists(), (
        "the build ran rr steps before refusing: "
        + receipt.read_text(encoding="utf-8")
    )


def test_disabled_status_exempts_an_undeclared_node(tmp_path: Path) -> None:
    """`status: disabled` is the declared way to keep a node without a slave."""
    root = _boot78_tree(tmp_path)
    _set_can_status(root, "disabled")
    done = _run(root, "check-dt-decode")
    assert done.returncode == 0, done.stdout + done.stderr


def test_dropping_one_declaration_refuses_only_that_node(tmp_path: Path) -> None:
    """The crosscheck is per-window, not an all-or-nothing switch."""
    root = _tree(tmp_path)
    _set_can_status(root, None)
    _drop_map_devices(root, {"canfd1"})
    done = _run(root, "check-dt-decode")
    assert done.returncode == 1
    message = done.stdout + done.stderr
    assert "/soc@0/can@20020000" in message
    assert "/soc@0/can@20010000" not in message


# ── direction B: a declaration the fabric does not decode ────────────────


def test_declaring_the_decerr_window_is_refused(tmp_path: Path) -> None:
    """Direction A alone would be satisfiable by inventing an address.

    0x20030000 is selector 3 of axil_demux3 — hardwired to DECERR.  Declaring
    it in the map and pointing a node at it is precisely "an endpoint the
    fabric does not decode", and must not pass.
    """
    root = _tree(tmp_path)
    map_path = root / MAP_REL
    data = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    data["devices"].append({
        "name": "phantom", "base_addr": 0x20030000, "span": 0x10000,
        "requires_bridge": "lwh2f", "uio": False,
    })
    map_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    target_path = root / TARGET_REL
    target = yaml.safe_load(target_path.read_text(encoding="utf-8"))
    target["linux"]["dt_nodes"].append({
        "path": "/soc@0/can@20030000",
        "properties": {"compatible": "ctu,ctucanfd",
                       "reg": [0x20030000, 0x10000]},
    })
    target_path.write_text(yaml.safe_dump(target, sort_keys=False), encoding="utf-8")

    done = _run(root, "check-dt-decode")
    assert done.returncode == 1
    message = done.stdout + done.stderr
    assert "device 'phantom'" in message
    assert "wedges the NoC" in message


def test_decode_windows_are_read_from_the_rtl_not_restated(tmp_path: Path) -> None:
    """Mutating the fabric's decoder must move the guard with it.

    Narrowing the bridge aperture to 17 bits deletes selector 2 (0x20020000)
    from what the fabric can reach.  A guard carrying a hand-written window
    list would not notice; this one must refuse canfd1.
    """
    root = _tree(tmp_path)
    top = root / "hw/rtl/de25/deepskopion_de25_top.vhd"
    source = top.read_text(encoding="utf-8")
    assert "lite_addr_g => 18" in source
    top.write_text(source.replace("lite_addr_g => 18", "lite_addr_g => 17", 1),
                   encoding="utf-8")

    done = _run(root, "check-dt-decode")
    assert done.returncode == 1
    message = done.stdout + done.stderr
    assert "device 'canfd1'" in message, message


def test_unparseable_rtl_fails_closed(tmp_path: Path) -> None:
    """A crosscheck that cannot read the design must refuse, never pass."""
    root = _tree(tmp_path)
    demux = root / "hw/rtl/de25/axil_demux3.vhd"
    demux.write_text(
        demux.read_text(encoding="utf-8").replace("wr_sel_r <= to_integer", "-- gone"),
        encoding="utf-8",
    )
    done = _run(root, "check-dt-decode")
    assert done.returncode == 1
    assert "cannot read the decode map out of the design" in done.stdout + done.stderr


def test_node_on_the_aperture_without_a_reg_window_is_refused(tmp_path: Path) -> None:
    """A unit address on the aperture with no `reg` cannot be proved decoded."""
    root = _tree(tmp_path)
    target_path = root / TARGET_REL
    target = yaml.safe_load(target_path.read_text(encoding="utf-8"))
    target["linux"]["dt_nodes"].append({
        "path": "/soc@0/mystery@20010000",
        "properties": {"compatible": "generic-uio"},
    })
    target_path.write_text(yaml.safe_dump(target, sort_keys=False), encoding="utf-8")
    done = _run(root, "check-dt-decode")
    assert done.returncode == 1
    assert "declares no parseable `reg` window" in done.stdout + done.stderr
