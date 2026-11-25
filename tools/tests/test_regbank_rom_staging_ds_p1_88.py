# rr unknown-llm (AI-generated)
"""DS-P1.88: every target that builds the regbank ROMs gets its hex files into the tool's run directory.

system_regbank_wrapper reads hw/rtl/regbank/regbank_schema.hex and
config_schema.hex with VHDL textio at elaboration (generics default to
rom_info_pkg's C_REGBANK_HEX / C_CONFIG_HEX, clone-relative paths). A vendor
tool resolves them against its own run directory. Measured 2026-11-05 on
PolarFire (queue job dbb840b8f23f): Synplify could not open them (CD695/CD677),
warned instead of failing, and both ROMs reduced to constants (CL189).

A target reaches the files in one of two ways: rr stages them because the target
declares them under ``sources.data`` (RTL-P1.297), or a pre-synthesis hook copies
them (the Zybo and DE25 stage_regbank_roms_pre.tcl). This test refuses a target
whose top builds the ROMs and that has neither, for product and bench targets
alike. Red-first fixture: the PolarFire target as it was before 81ab7770.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files, product_target_files  # noqa: E402

HEX_FILES = ("hw/rtl/regbank/regbank_schema.hex", "hw/rtl/regbank/config_schema.hex")
STAGING_HOOK = re.compile(r"stage_regbank_roms[^/]*\.tcl$")


def _target_files() -> list[Path]:
    """Products and, in a checkout that has them, the bench targets (the export has none)."""
    return sorted(all_target_files(ROOT))


def _entity_file(entity: str) -> Path | None:
    pattern = re.compile(rf"^\s*entity\s+{re.escape(entity)}\s+is\b", re.I | re.M)
    for path in sorted((ROOT / "hw").rglob("*.vhd")):
        try:
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                return path
        except OSError:
            continue
    return None


def builds_the_regbank_roms(doc: dict) -> bool:
    """The target synthesizes a top that instantiates deepskopion_core (which
    holds system_regbank_wrapper) or the wrapper itself."""
    top = (doc.get("project") or {}).get("top_module")
    if not top:
        return False
    # A testbench top (bench/targets/de25_core_baseline.yml: tb_datalogger_to_ddr,
    # "this target never builds or programs anything") is simulated, never
    # synthesized, so no vendor tool resolves the hex paths for it.
    if top.startswith("tb_"):
        return False
    path = _entity_file(top)
    if path is None:
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return bool(re.search(r"\b(deepskopion_core|system_regbank_wrapper)\b", text))


def staging_problems(doc: dict, name: str) -> list[str]:
    """Why ``doc`` would leave the ROM hex files out of the tool run directory."""
    data = ((doc.get("sources") or {}).get("data")) or []
    declared = {str(x).strip() for x in data}
    hooks = []
    for stage in (doc.get("tcl_hooks") or {}).values():
        for entry in stage if isinstance(stage, list) else [stage]:
            hooks.append(str(entry.get("script") if isinstance(entry, dict) else entry))
    if any(STAGING_HOOK.search(h.split()[0]) for h in hooks if h.strip()):
        return []
    missing = [h for h in HEX_FILES if h not in declared]
    if missing:
        return [f"{name}: builds the regbank ROMs but neither declares "
                f"{', '.join(missing)} under sources.data nor runs a "
                f"stage_regbank_roms hook"]
    return []


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def test_the_rom_paths_are_the_ones_the_wrapper_reads():
    wrapper = (ROOT / "hw/rtl/regbank/system_regbank_wrapper.vhd").read_text(encoding="utf-8")
    assert "G_REGBANK_HEX_FILE : string := C_REGBANK_HEX" in wrapper
    assert "G_CONFIG_HEX_FILE  : string := C_CONFIG_HEX" in wrapper
    for hex_file in HEX_FILES:
        assert (ROOT / hex_file).is_file(), hex_file


def test_every_product_target_builds_the_roms():
    products = product_target_files(ROOT)
    assert {p.stem for p in products} == {"zybo_datalogger", "de25_datalogger", "pfsoc_datalogger"}
    for path in products:
        assert builds_the_regbank_roms(_load(path)), path.stem


@pytest.mark.parametrize("path", _target_files(), ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_a_target_that_builds_the_roms_stages_their_hex_files(path):
    doc = _load(path)
    if not builds_the_regbank_roms(doc):
        pytest.skip(f"{path.stem}: top does not build the regbank ROMs")
    assert staging_problems(doc, path.stem) == []


def test_the_testbench_exemption_covers_only_simulation_tops():
    """The tb_ exemption must not hide a synthesized target: every target it
    skips names its testbench as top_sim too."""
    for path in _target_files():
        project = _load(path).get("project") or {}
        top = project.get("top_module") or ""
        if top.startswith("tb_"):
            assert project.get("top_sim") == top, path.stem


def test_the_pfsoc_target_before_81ab7770_is_refused():
    """Red-first on real history: the PolarFire target as it was when DS-P1.88
    was measured, before its hex files were declared."""
    old = subprocess.run(
        ["git", "show", "81ab7770^:targets/pfsoc_datalogger.yml"],
        cwd=ROOT, capture_output=True, text=True, errors="replace")
    if old.returncode != 0:
        pytest.skip("history before 81ab7770 not in this clone")
    doc = yaml.safe_load(old.stdout) or {}
    problems = staging_problems(doc, "pfsoc_datalogger@81ab7770^")
    assert problems and "regbank_schema.hex" in problems[0]


def test_removing_the_data_entries_is_refused_and_a_hook_is_accepted():
    doc = _load(ROOT / "targets/pfsoc_datalogger.yml")
    doc["sources"] = dict(doc.get("sources") or {})
    doc["sources"]["data"] = ["hw/rtl/regbank/config_schema.hex"]
    problems = staging_problems(doc, "pfsoc_datalogger")
    assert problems and "regbank_schema.hex" in problems[0]
    assert "config_schema.hex," not in problems[0]
    doc["tcl_hooks"] = {"pre_synth": [{"script": "hw/libero/stage_regbank_roms_pre.tcl"}]}
    assert staging_problems(doc, "pfsoc_datalogger") == []
