# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P3.334: the telemetry exporter's deployment instructions cover all three boards.

The deployment contract once said "run it by hand over ssh" for every board, and the DE25 image has
no SSH server. The contract's sw/telemetry entry and the exporter doc must name each board's route,
and each route must write to the directory the capture daemon is staged in.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEST = "/usr/local/share/deepskopion"


def _telemetry_status() -> str:
    decl = yaml.safe_load((ROOT / "sw/linux/deployment_contract.yml").read_text(encoding="utf-8"))
    (entry,) = [e for e in decl["not_autostarted"] if e["component"] == "sw/telemetry"]
    return " ".join(entry["status"].split())


def _doc() -> str:
    return (ROOT / "docs/software/telemetry_exporter.md").read_text(encoding="utf-8")


def _row(doc: str, board: str) -> list[str]:
    (row,) = [line for line in doc.splitlines() if line.startswith(f"| {board} |")]
    return [c.strip() for c in row.strip("|").split("|")]


def _blocks(doc: str) -> list[str]:
    return re.findall(r"```sh\n(.*?)```", doc, flags=re.S)


def test_the_contract_names_each_boards_route_and_no_blanket_ssh():
    status = _telemetry_status()
    assert "by hand over ssh" not in status
    assert "ssh with cat on the Zybo Z7-20 and the PolarFire SoC Discovery Kit" in status
    assert "on the DE25, which has no SSH server, onto its card's root partition" in status


def test_each_board_row_names_its_route():
    doc = _doc()
    for board in ("Digilent Zybo Z7-20", "PolarFire SoC Discovery Kit"):
        route = _row(doc, board)[3]
        assert route.startswith("`ssh` with `cat`"), (board, route)
    de25_route, de25_root = _row(doc, "Terasic DE25")[3], _row(doc, "Terasic DE25")[2]
    assert "no SSH server" in de25_route and "card" in de25_route
    assert not re.search(r"\bscp\b|`ssh`", de25_route), de25_route
    assert "second partition" in de25_root


def test_the_ssh_route_writes_every_file_into_the_daemon_directory():
    (block,) = [b for b in _blocks(_doc()) if "ssh " in b]
    assert f'mkdir -p {DEST}/sw/telemetry' in block
    assert re.search(r'for f in sw/telemetry/\*\.py; do\s+ssh "root@\$BOARD_IP" "cat > ' + re.escape(DEST)
                     + r'/\$f" < "\$f"', block), block
    assert "scp" not in block


def test_the_card_route_checks_the_root_partition_before_writing():
    (block,) = [b for b in _blocks(_doc()) if "mount" in b]
    lines = [ln.strip() for ln in block.splitlines()]
    check = next(i for i, ln in enumerate(lines) if ln.startswith(f'test -e "$MNT{DEST}/sw/stream/__main__.py"'))
    write = next(i for i, ln in enumerate(lines) if ln.startswith("sudo mkdir -p") or ln.startswith("sudo cp"))
    assert check < write
    assert any(ln.startswith(f'sudo cp sw/telemetry/*.py "$MNT{DEST}/sw/telemetry/"') for ln in lines)
    assert any(ln.startswith("trap ") and "umount" in ln for ln in lines)
