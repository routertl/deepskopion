# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.277: every constraint file under hw/ that rr resolves for a target is a file that target names.

Measured 2026-11-12 (queue job c5896780364c, build/classic_eye/project/deepskopion.xpr): the classic_eye Vivado
project carried hw/xdc/classic_eye.xdc AND eye_loopback.xdc, pblock_ctu.xdc and timing.xdc, the three its target
says it does not subscribe. Its `paths.constraints: hw/xdc` is a directory, and rr auto-globs a directory root
(`rr schema --field paths.constraints`). canfd_demo had the same form and pulled in the Classic board's
classic_eye.xdc.

The resolved set is rr's own: routertl_core.constraints_resolver.resolve_constraint_files on the target's loaded
config (the function `rr sources --xdc` uses). A target names a file when `paths.constraints` is that file, or a
`sources.{xdc,sdc}` entry (string or {path: ...}, read by rr's constraint_entry_path) is that file.
"""
from __future__ import annotations

from pathlib import Path

import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files, with_bench  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
TARGETS = all_target_files(ROOT)


def _rel(p: str) -> str:
    q = Path(p)
    return str(q.resolve().relative_to(ROOT)) if q.is_absolute() else str(q)


def _named(cfg: dict) -> set[str]:
    from routertl_core.constraints_resolver import constraint_entry_path
    named = set()
    pc = (cfg.get("paths") or {}).get("constraints")
    if isinstance(pc, str) and Path(pc).suffix in (".xdc", ".sdc"):
        named.add(pc.strip())
    sources = cfg.get("sources") if isinstance(cfg.get("sources"), dict) else {}
    for key in ("xdc", "sdc"):
        for entry in (sources or {}).get(key) or []:
            path = constraint_entry_path(entry)
            if path and Path(path).suffix in (".xdc", ".sdc"):
                named.add(path)
    return named


def _resolved_unnamed(target: Path, monkeypatch) -> tuple[list[str], list[str]]:
    from routertl_core.constraints_resolver import resolve_constraint_files
    from sdk.cli.project_config import load_project_config
    monkeypatch.setenv("RR_TARGET", str(target))
    cfg = load_project_config(str(ROOT / "project.yml"))
    hw = [_rel(p) for p in resolve_constraint_files(cfg, ROOT) if _rel(p).startswith("hw/")]
    return hw, sorted(p for p in hw if p not in _named(cfg))


@pytest.mark.parametrize("target", TARGETS, ids=[t.stem for t in TARGETS])
def test_every_resolved_constraint_file_is_named_by_the_target(target, monkeypatch):
    _hw, unnamed = _resolved_unnamed(target, monkeypatch)
    assert not unnamed, f"{target.name} resolves constraint files it does not name: {unnamed}"


def test_the_scan_sees_constraint_files(monkeypatch):
    """Presence: the resolver returns hw/ constraint files for targets known to have them."""
    # DS-P1.103: the products always; the bench targets only where bench/ exists.
    for name in sorted(with_bench({"zybo_datalogger", "de25_datalogger"}, {"classic_eye", "canfd_demo"}, ROOT)):
        hw, _ = _resolved_unnamed(next(t for t in TARGETS if t.stem == name), monkeypatch)
        assert hw, f"{name}: rr resolved no hw/ constraint file"
