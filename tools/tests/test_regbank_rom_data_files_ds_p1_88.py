"""DS-P1.88 — every target that builds the regbank ROMs declares their hex files.

deepskopion_core instantiates system_regbank_wrapper, whose two ROMs are sized
and filled at elaboration from rom_info_pkg's C_REGBANK_HEX and C_CONFIG_HEX
(VHDL textio, DS-P1.82). Measured 2026-11-05 on pfsoc_datalogger (queue job
dbb840b8f23f): Synplify could not open either file (CD695) and both ROMs
reduced to a constant word, while the build read green. RouteRTL names such
files in ``sources.data``: their content keys the build cache, and staging
them into each tool's run directory is RTL-P1.297.

The guard: a target whose top builds the ROMs declares exactly the two paths
rom_info_pkg reads, and a target whose top does not, declares neither.

DS-P2.275: "builds the ROMs" is entity-graph reach of regbank_rom from the
target's top_module or top_sim, over every VHDL file under hw/. It was "the
top, found under hw/rtl only, instantiates deepskopion_core", which missed
classic_eye and eye_loopback: their tops live in hw/classic_eye/ and
hw/eye_loopback/ and instantiate system_regbank_wrapper directly. Measured
2026-11-12 (queue job 0257120161ba): classic_eye's synth_design failed
"[Synth 8-3302] unable to open file 'hw/rtl/regbank/regbank_schema.hex'".
"""
from __future__ import annotations

import sys
import atexit
import functools
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files, per_tree  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
ROM_INFO_REL = "hw/rtl/pkg/rom_info_pkg.vhd"
MARKER = "DS-P1.88 regbank ROM hex files are not declared in sources.data"


@functools.lru_cache(maxsize=1)
def _generated_rom_info() -> Path:
    """rom_info_pkg.vhd as the build generates it.

    DS-P2.231: the file is a gitignored build output, so a fresh clone has
    none. Generate it the way the pre_sim hook does (regen_regbank.sh
    --rom-info-only) into a scratch directory; the generated VHDL still names
    the canonical clone-relative memory images.
    """
    out = Path(tempfile.mkdtemp(prefix="ds-p1-88-rom-info-"))
    atexit.register(shutil.rmtree, out, True)
    r = subprocess.run(
        ["bash", "tools/regen_regbank.sh", "--rom-info-only", "--output-dir",
         str(out), "--regbank-dir", "hw/rtl/regbank", "--library", "tich"],
        cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    return out / "rom_info_pkg.vhd"


def _rom_paths() -> list[str]:
    text = _generated_rom_info().read_text(encoding="utf-8")
    paths = []
    for name in ("C_REGBANK_HEX", "C_CONFIG_HEX"):
        m = re.search(rf'constant\s+{name}\s*:\s*string\s*:=\s*"([^"]+)"', text)
        assert m, f"{MARKER}: {name} not found in the generated {ROM_INFO_REL}"
        paths.append(m.group(1))
    return paths


_ENTITY = re.compile(r"(?im)^\s*entity\s+(\w+)\s+is\b")
_INSTANCES = (
    re.compile(r"(?i)\bentity\s+\w+\.(\w+)"),
    re.compile(r"(?im)^\s*\w+\s*:\s*(?:component\s+)?(\w+)\s*(?:generic|port)\s+map"),
)


@functools.lru_cache(maxsize=1)
def _entity_graph() -> tuple[dict[str, Path], dict[Path, frozenset[str]]]:
    """VHDL entity -> its file, and file -> entities it instantiates, over hw/."""
    entity_file: dict[str, Path] = {}
    instances: dict[Path, frozenset[str]] = {}
    for p in sorted((ROOT / "hw").rglob("*.vhd")):
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in _ENTITY.finditer(text):
            entity_file.setdefault(m.group(1).lower(), p)
        instances[p] = frozenset(m.group(1).lower() for rx in _INSTANCES for m in rx.finditer(text))
    return entity_file, instances


def _reaches_rom(top: str) -> bool:
    entity_file, instances = _entity_graph()
    seen: set[str] = set()
    stack = [top.lower()] if top else []
    while stack:
        name = stack.pop()
        if name in seen or name not in entity_file:
            continue
        seen.add(name)
        children = instances[entity_file[name]]
        if "regbank_rom" in children:
            return True
        stack.extend(children)
    return False


def _builds_the_core(cfg_tops: tuple[str, str]) -> bool:
    """The target's top_module or top_sim reaches regbank_rom (DS-P2.275)."""
    return any(_reaches_rom(t) for t in cfg_tops if t)


def _targets() -> list[tuple[str, tuple[str, str], list]]:
    out = []
    for f in all_target_files(ROOT):
        cfg = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        project = cfg.get("project") or {}
        top = project.get("top_module") or cfg.get("top_module") or ""
        top_sim = project.get("top_sim") or ""
        data = (cfg.get("sources") or {}).get("data") or []
        out.append((f.name, (top, top_sim), data))
    return out


def test_rom_paths_exist_and_are_clone_relative() -> None:
    for rel in _rom_paths():
        assert not Path(rel).is_absolute(), f"{MARKER}: {rel} is absolute"
        assert (ROOT / rel).is_file(), f"{MARKER}: {rel} does not exist in the tree"


def test_every_core_target_declares_both_rom_files() -> None:
    want = _rom_paths()
    core_targets = [(n, d) for n, tops, d in _targets() if _builds_the_core(tops)]
    want_n = per_tree(3, 7, ROOT)  # DS-P1.103: the three products in the export; products and bench privately
    assert len(core_targets) >= want_n, f"{MARKER}: expected the {want_n} core targets, found {core_targets}"
    for name, data in core_targets:
        missing = [p for p in want if p not in data]
        assert not missing, f"{MARKER}: targets/{name} lacks {missing} under sources.data"


def test_no_other_target_declares_them() -> None:
    want = set(_rom_paths())
    for name, tops, data in _targets():
        if _builds_the_core(tops):
            continue
        assert not want & set(data), (
            f"targets/{name} (tops {tops!r}) does not reach regbank_rom but declares {sorted(want & set(data))}")
