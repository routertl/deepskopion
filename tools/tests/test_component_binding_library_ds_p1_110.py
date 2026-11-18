# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.110: a component that default binding cannot find is a silent black box.

VHDL default binding looks for a component's entity in the library the instantiating unit is compiled into (work).
If a target's source map compiles that entity only into another library (``sources.libraries``) and nothing binds the
instance there (an `entity <lib>.<name>` instantiation or a configuration specification), Synplify keeps the
instance as a black box (MT246) and Libero later fails to read the netlist (NETLIST:011). Measured on the PolarFire
REA image 2026-11-16 (rr queue 208fd899c7d5): rr_rea_ujtag_local compiled into library rr_rea, instantiated by
component from work, black-boxed.

For every target: the HDL it compiles into work is its sources.syn plus, with auto_sources, every VHDL file under
hw/rtl. No such file may declare a component whose entity the same target compiles only into a non-work library,
unless the file binds it with a configuration specification naming that library.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from target_files import all_target_files  # noqa: E402

_ENTITY = re.compile(r"^\s*entity\s+(\w+)\s+is\b", re.I | re.M)
_COMPONENT = re.compile(r"^\s*component\s+(\w+)\b", re.I | re.M)


def _strip_comments(text: str) -> str:
    return "\n".join(ln.split("--", 1)[0] for ln in text.splitlines())


def _vhdl(path: Path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8", errors="replace"))


def _work_files(cfg: dict) -> set[Path]:
    src = cfg.get("sources") or {}
    files = {ROOT / p for p in (src.get("syn") or []) if str(p).endswith((".vhd", ".vhdl"))}
    if cfg.get("auto_sources"):
        files |= set((ROOT / "hw" / "rtl").rglob("*.vhd"))
    return {f for f in files if f.is_file()}


def _offenders() -> list[str]:
    bad = []
    for target in all_target_files(ROOT):
        cfg = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        libs = ((cfg.get("sources") or {}).get("libraries") or {})
        placed = {}  # entity name -> non-work library it is compiled into
        for lib, files in libs.items():
            if lib == "work":
                continue
            for f in files or []:
                p = ROOT / f
                if p.is_file() and p.suffix in (".vhd", ".vhdl"):
                    for ent in _ENTITY.findall(_vhdl(p)):
                        placed[ent.lower()] = lib
        if not placed:
            continue
        for f in sorted(_work_files(cfg)):
            text = _vhdl(f)
            for comp in _COMPONENT.findall(text):
                lib = placed.get(comp.lower())
                if not lib:
                    continue
                bound = re.search(rf"\bfor\s+[\w,\s]+:\s*{comp}\s+use\s+entity\s+{lib}\.{comp}\b", text, re.I)
                if not bound:
                    bad.append(f"{target.relative_to(ROOT)}: {f.relative_to(ROOT)} declares component {comp}, "
                               f"which this target compiles only into library {lib} (black box by default binding)")
    return bad


def test_no_component_is_compiled_only_into_another_library():
    assert _offenders() == []
