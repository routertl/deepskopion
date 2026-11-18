"""DS-P3.168 — target-contract test that every u_rea reference in an XDC a target loads resolves against that target top.

Ticket:
  bench/targets/eye_loopback.yml sources.xdc lists hw/xdc/timing.xdc, but its top
  eye_loopback_top instantiates no u_rea. timing.xdc carries REA constraints
  written against deepskopion_top: bounds on [get_clocks -of_objects [get_pins u_rea/u_top/tck_i]]
  and -from [get_pins u_rea/u_bscane2/TCK]. On eye_loopback every one of those
  get_pins is empty, so Vivado raises 12-1008/12-4739 critical warnings on each line.
  Done = an eye_loopback build log with no 12-1008/12-4739 from timing.xdc,
  and a target-contract test that every u_rea reference in an XDC a target loads
  resolves against that target top.
"""
from __future__ import annotations

import sys
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
_MARKER = "target-contract violation"


def _code(path: Path) -> str:
    """Return text of file with comments stripped."""
    return "\n".join(line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines())


def _find_entity_file(root: Path, entity_name: str, search_paths: list[str]) -> tuple[Path | None, str | None]:
    """Find the VHDL file defining the given entity."""
    for sp in search_paths:
        sdir = root / sp
        if not sdir.exists():
            continue
        for f in sdir.rglob("*.vhd"):
            text = f.read_text(encoding="utf-8", errors="replace")
            if re.search(rf"^\s*entity\s+{re.escape(entity_name)}\b", text, re.M | re.I):
                return f, text
    return None, None


def _target_declared_xdcs(target_cfg: dict[str, Any]) -> list[str]:
    """Return XDC paths explicitly declared by the target under sources.xdc and paths.constraints."""
    xdcs: list[str] = []
    sources = target_cfg.get("sources") or {}
    for entry in sources.get("xdc") or []:
        if isinstance(entry, str):
            xdcs.append(entry)
        elif isinstance(entry, dict) and "path" in entry:
            xdcs.append(str(entry["path"]))
    constraints_path = (target_cfg.get("paths") or {}).get("constraints")
    if constraints_path and isinstance(constraints_path, str) and constraints_path.endswith(".xdc"):
        if constraints_path not in xdcs:
            xdcs.append(constraints_path)
    return xdcs


def _extract_u_rea_references(xdc_text: str) -> set[str]:
    """Extract all u_rea pin/cell references (e.g. u_rea/u_top/tck_i, u_rea/u_bscane2/TCK)."""
    return set(re.findall(r"\b(u_rea(?:/\w+)+)\b", xdc_text))


def test_target_xdc_rea_references_resolve_against_target_top() -> None:
    """Target contract (DS-P3.168): every u_rea reference in an XDC a target loads
    must resolve against that target top module. If the target top instantiates no u_rea,
    the target must not load constraints referencing u_rea pins/cells."""
    targets_dir = ROOT / "targets"
    assert targets_dir.is_dir(), f"{_MARKER}: targets directory not found"

    for yml in all_target_files(ROOT):  # DS-P1.103: products and bench
        target_name = yml.stem
        target_cfg = yaml.safe_load(yml.read_text(encoding="utf-8"))
        if not isinstance(target_cfg, dict):
            continue

        top_module = (target_cfg.get("project") or {}).get("top_module")
        if not top_module:
            continue

        xdc_files = _target_declared_xdcs(target_cfg)
        if not xdc_files:
            continue

        search_paths = (target_cfg.get("paths") or {}).get("sources") or ["hw/rtl", "libs"]

        for xdc_rel in xdc_files:
            xdc_path = ROOT / xdc_rel
            if not xdc_path.is_file():
                continue

            xdc_code = _code(xdc_path)
            rea_refs = _extract_u_rea_references(xdc_code)
            if not rea_refs:
                continue

            # This XDC references u_rea: top_module must resolve it
            top_file, top_text = _find_entity_file(ROOT, top_module, search_paths)
            assert top_file is not None, (
                f"{_MARKER}: target '{target_name}' top module '{top_module}' not found in {search_paths}"
            )

            has_u_rea = bool(re.search(r"^\s*u_rea\s*:\s*(?:entity\s+\S+|component\s+\S+|\w+)", top_text, re.M))
            assert has_u_rea, (
                f"{_MARKER}: target '{target_name}' (top '{top_module}') loads '{xdc_rel}' "
                f"whose u_rea constraints match nothing on target top: {sorted(rea_refs)}"
            )

            # If top has u_rea, ensure referenced sub-pins are known in the instantiated architecture
            top_inst_match = re.search(r"^\s*u_rea\s*:\s*entity\s+([\w.]+)", top_text, re.M)
            if top_inst_match:
                inst_entity = top_inst_match.group(1).split(".")[-1]
                wrapper_file, wrapper_text = _find_entity_file(ROOT, inst_entity, ["libs", "hw/rtl"])
                if wrapper_text:
                    for ref in sorted(rea_refs):
                        parts = ref.split("/")
                        if len(parts) >= 2:
                            sub_inst = parts[1]
                            assert re.search(rf"^\s*{re.escape(sub_inst)}\s*:", wrapper_text, re.M), (
                                f"{_MARKER}: {ref} sub-instance '{sub_inst}' not found in {inst_entity}"
                            )
