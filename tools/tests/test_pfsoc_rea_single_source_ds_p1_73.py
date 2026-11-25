# rr unknown-llm (AI-generated)
"""DS-P1.73: pfsoc_datalogger takes its REA from ONE source, routertl/rea.

History: DS-P2.128 vendored upstream rr-rea 1.2.0 under hw/vendor/rr_rea_upstream
and listed it in sources.libraries.rr_rea, because the registry then stopped at
1.0.0 (RTL-P2.1318) and a single-target package was pruned (RTL-P2.1319). Both
are fixed: every other target now declares routertl/rea ^1.12.0 and ip.lock
holds 1.12.0. Left in place, the vendored copy and libs/routertl/rea both supply
rr_rea_regbank.vhd (non-identical), so rr refuses to build_env a fresh
worktree ("conflicting non-identical copies"), and the door's use of the 1.12
files was undeclared for this target. rr refusing is correct; these tests pin
the fix at the target and at the tree.
"""

from __future__ import annotations

import sys
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"


def _cfg() -> dict:
    return yaml.safe_load(TARGET.read_text(encoding="utf-8"))


def test_target_declares_the_registry_rea_package() -> None:
    pkgs = _cfg().get("packages") or {}
    assert "routertl/rea" in pkgs, (
        "pfsoc_datalogger compiles the REA door but does not declare routertl/rea: "
        "rr refuses 'files from locked packages it does not declare'"
    )
    spec = pkgs["routertl/rea"]
    spec = spec["version"] if isinstance(spec, dict) else spec
    spec = str(spec)
    if spec.startswith("commit:"):
        # DS-P3.174: until the REA release carrying REA-P2.24 is tagged, every
        # target pins the same commit (one shared ip.lock). The pin must be the
        # commit ip.lock resolved, and the vendored package must be the 1.12.2
        # line that carries REA-P2.24 (the 1.12.0 copy still passes ^1.12).
        vendored = yaml.safe_load(
            (ROOT / "libs/routertl/rea/ip.yml").read_text(encoding="utf-8")
        )
        locked = yaml.safe_load((ROOT / "ip.lock").read_text(encoding="utf-8"))
        assert re.fullmatch(r"commit:[0-9a-f]{40}", spec), spec
        assert spec == f"commit:{locked['routertl/rea']['commit']}", spec
        assert tuple(int(x) for x in str(vendored["version"]).split(".")) >= (
            1, 12, 2,
        ), vendored["version"]
    elif spec.startswith("=="):
        # DS-P2.193: an exact release pin (1.12.3 carries REA-P2.24 and the
        # upstream rr_rea_sync_word waiver) must be the version ip.lock resolved.
        locked = yaml.safe_load((ROOT / "ip.lock").read_text(encoding="utf-8"))
        assert spec == f"=={locked['routertl/rea']['resolved_version']}", spec
        assert tuple(int(x) for x in spec[2:].split(".")) >= (1, 12, 2), spec
    else:
        assert spec.startswith("^1.12"), spec


def test_target_lists_no_second_rea_copy() -> None:
    """No file of any rr_rea_* basename may be listed by the target itself: the
    package is the only provider (a second, non-identical copy is the defect)."""
    cfg = _cfg()
    libs = (cfg.get("sources") or {}).get("libraries") or {}
    assert "rr_rea" not in libs, "sources.libraries.rr_rea re-introduces a second REA copy"
    listed = [f for files in libs.values() for f in files]
    listed += list((cfg.get("sources") or {}).get("syn") or [])
    dupes = sorted(f for f in listed if Path(f).name.startswith("rr_rea_"))
    assert not dupes, f"target lists REA files besides the routertl/rea package: {dupes}"


def test_jtag_only_package_sdc_is_not_applied_to_the_mmio_door_build() -> None:
    """routertl/rea's Microchip SDC clocks UJTAG UDRCK and bounds JTAG CDC
    cells; the MMIO door has neither, so Libero's synthesis SDC check aborts
    (SDC0001/SDC0023, job 27478cc4415a). The target must keep that SDC out of
    g_package_sdc.

    DS-P2.172: since aa2db13 the target does that with
    ``package_constraints: {exclude: [routertl/rea]}`` (RTL-P2.1595), not the
    old ``vendor_overrides.microchip.package_sdc: ""`` blank. The effect is
    checked through resolve_package_scoped_sdc, the resolver project_manager
    uses to emit g_package_sdc for microchip; without the exclude it returns
    the SDC. This test and the target yml need an rr carrying 9494bb884
    (RTL-P2.1595); released rr 4.9.3 has no package_constraints and would
    apply the SDC. That routertl floor is RTL-P3.1992 (see also DS-P3.169).
    No skip for an older rr: a missing resolver must fail loudly."""
    from routertl_core.constraints_resolver import resolve_package_scoped_sdc

    cfg = _cfg()
    excluded = (cfg.get("package_constraints") or {}).get("exclude") or []
    assert "routertl/rea" in excluded, cfg.get("package_constraints")
    applied = resolve_package_scoped_sdc(ROOT, vendor="microchip", config=cfg)
    leaked = [str(p) for p in applied if "rr_rea_scoped_microchip" in str(p)]
    assert not leaked, f"routertl/rea's JTAG SDC still reaches g_package_sdc: {leaked}"


def test_tree_has_a_single_tracked_rea_source() -> None:
    """Every tracked rr_rea_*.vhd that the pfsoc source roots can reach lives
    under libs/routertl/rea (the other roots are hw/rtl, hw/pfsoc_rea, libs)."""
    out = subprocess.run(
        ["git", "ls-files", "*rr_rea_*.vhd"], cwd=ROOT, capture_output=True,
        text=True, check=True,
    ).stdout.split()
    stray = sorted(f for f in out if not f.startswith("libs/routertl/rea/")
                   and Path(f).name != "rr_rea_ujtag_local.vhd")
    assert not stray, f"a second rr_rea source tree is tracked: {stray}"
    assert not (ROOT / "hw" / "vendor" / "rr_rea_upstream").exists()


#: hw/rtl files that instantiate the REA wrapper by its library (DS-P2.168).
_REA_INSTANTIATING_TOPS = {"hw/rtl/deepskopion_top.vhd", "hw/rtl/de25/deepskopion_de25_top.vhd",
                           "hw/rtl/pfsoc/rr_rea_ujtag_local.vhd"}


def test_only_the_rea_tops_under_a_shared_root_reference_library_rr_rea() -> None:
    """A file under hw/rtl (walked by EVERY auto-sourcing target) that names
    library rr_rea fails every target whose compile set has no such library —
    measured 2026-09-24 when pfsoc_rea_door.vhd first sat in hw/rtl/pfsoc.

    DS-P2.168: routertl/rea's generated build-id output is locked to library
    `rr_rea`, so the REA tops must name it. That is safe only while each target
    that walks hw/rtl compiles that library: via the routertl/rea package, or
    (pfsoc_datalogger) the vendored upstream copy."""
    offenders = sorted(
        str(p.relative_to(ROOT))
        for p in (ROOT / "hw" / "rtl").rglob("*.vhd")
        if "library rr_rea" in p.read_text(encoding="utf-8", errors="replace").lower()
    )
    assert offenders == sorted(_REA_INSTANTIATING_TOPS), offenders

    for target in all_target_files(ROOT):
        cfg = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        if "hw/rtl" not in ((cfg.get("paths") or {}).get("sources") or []):
            continue
        has_package = "routertl/rea" in (cfg.get("packages") or {})
        has_library = "rr_rea" in ((cfg.get("sources") or {}).get("libraries") or {})
        assert has_package or has_library, (
            f"{target.name} walks hw/rtl but compiles no library rr_rea, so "
            f"{sorted(_REA_INSTANTIATING_TOPS)} fail its closure preflight"
        )


HOOK = ROOT / "hw" / "libero" / "pfsoc" / "pfsoc_mss_subsys.tcl"


def test_libero_hook_orders_every_declared_library() -> None:
    """The Libero hook hands SYNTHESIZE an explicit per-library fileset; a
    library missing from its order list never reaches Synplify (measured: CD592
    "rr_rea.rr_rea_axi4lite has not been analyzed", synth eedc234ed40c)."""
    import re

    text = HOOK.read_text(encoding="utf-8")
    m = re.search(r"foreach lib \{([^}]*)\} \{", text)
    assert m, "the per-library SYNTHESIZE order loop was not found in the hook"
    ordered = m.group(1).split()
    cfg = yaml.safe_load(TARGET.read_text(encoding="utf-8"))
    declared = set(cfg["sources"]["libraries"]) - {"work"}
    missing = sorted(declared - set(ordered))
    assert not missing, f"libraries {missing} are declared but not in the hook order {ordered}"
    assert ordered.index("rr_rea") < ordered.index("tich")


def test_declared_library_sources_have_unique_basenames() -> None:
    """Libero import_files flattens sources into <project>/hdl/ by basename, so
    two declared files sharing one basename silently overwrite each other."""
    cfg = yaml.safe_load(TARGET.read_text(encoding="utf-8"))
    seen: dict[str, str] = {}
    clashes = []
    for lib, files in cfg["sources"]["libraries"].items():
        for f in files:
            base = Path(f).name
            if base in seen and seen[base] != f:
                clashes.append((base, seen[base], f))
            seen.setdefault(base, f)
    assert not clashes, clashes


def test_door_bench_expects_the_package_rea_version() -> None:
    """The cocotb door bench compiles libs/routertl/rea, so its VERSION word
    must be that package's C_REA_VERSION (0x5245410F at 1.12), not the deleted
    vendored 1.2.0 value (0x52454109) — the landing refusal of DS-P1.73."""
    import re

    pkg = (ROOT / "libs/routertl/rea/rtl/rr_rea_pkg.vhd").read_text(encoding="utf-8")
    want = re.search(r'C_REA_VERSION\s*:\s*[^:]*:=\s*x"([0-9A-Fa-f]{8})"', pkg)
    assert want, "C_REA_VERSION not found in routertl/rea rr_rea_pkg.vhd"
    bench = (ROOT / "hw/sim/cocotb/tests/pfsoc/test_pfsoc_rea_csr_door.py").read_text(
        encoding="utf-8")
    got = re.search(r"^REA_VERSION\s*=\s*0x([0-9A-Fa-f]{8})", bench, re.M)
    assert got and got.group(1).upper() == want.group(1).upper(), (got, want.group(1))
