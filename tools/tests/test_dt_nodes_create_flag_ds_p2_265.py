"""DS-P2.265: Zynq-7000 dt_nodes entries that rr must CREATE carry `create: true`.

RTL-P2.2279 makes rr refuse a linux.dt_nodes entry whose path is not in the base
device tree unless it carries `create: true` (parent present, or created by an
earlier entry of the same list). Judged against the Lopper linux_dt tree of a
Zynq-7000 design on Vivado/Vitis 2024.1 (the RTL-P2.2284 fixture):

- CREATE: nodes absent from the base tree;
- EXISTING: nodes the base tree has — `create: true` there is wrong;
- OPEN: PL nodes whose SDT path is not decided (canfd_demo's CAN FD cores);
  they carry no flag until judged against canfd_demo's own DTB.

Every dt_nodes path of every Zynq-7000 target must be in one of the three sets,
so a new entry has to be classified here.
"""
from __future__ import annotations

from pathlib import Path

import sys
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files, with_bench  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)

CREATE = (
    "/axi/spi@e0006000/spidev@0",
    "/axi/ring_watermark@0",
    "/axi/ethernet@e000b000/mdio",
    "/axi/ethernet@e000b000/mdio/ethernet-phy",
)
EXISTING = (
    "/",
    "/axi/spi@e0006000",
    "/axi/can@e0008000",
    "/axi/can@e0009000",
    "/axi/gpio@e000a000",
    "/axi/ethernet@e000b000",
)
OPEN = ("/axi/can@80000000", "/axi/can@80010000")
BASE_TREE = set(EXISTING) | {"/axi"}


def _zynq7000_targets() -> list[Path]:
    out = []
    for path in all_target_files(ROOT):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        part = str((data.get("hardware") or {}).get("part", ""))
        if part.startswith("xc7z"):
            out.append(path)
    return out


def _entries(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(((data.get("linux") or {}).get("dt_nodes")) or [])


TARGETS = _zynq7000_targets()


def test_the_zynq7000_target_set_is_the_one_the_ticket_names():
    names = {p.stem for p in TARGETS if _entries(p)}
    assert names == with_bench({"zybo_datalogger"}, {"canfd_demo", "classic_eye"}, ROOT), names


@pytest.mark.parametrize("target", TARGETS, ids=lambda p: p.stem)
def test_create_flag_matches_the_base_tree(target):
    for entry in _entries(target):
        path, flag = entry["path"], entry.get("create")
        assert path in CREATE + EXISTING + OPEN, f"{target.name}: unclassified dt_nodes path {path}"
        if path in CREATE:
            assert flag is True, f"{target.name}: {path} is created by rr and lacks create: true"
        else:
            assert flag is None, f"{target.name}: {path} must not carry create ({flag!r})"


@pytest.mark.parametrize("target", TARGETS, ids=lambda p: p.stem)
def test_a_created_node_has_its_parent_before_it(target):
    seen = set(BASE_TREE)
    for entry in _entries(target):
        path = entry["path"]
        if entry.get("create") is True:
            parent = path.rsplit("/", 1)[0] or "/"
            assert parent in seen, f"{target.name}: {path} is created before its parent {parent}"
        seen.add(path)
