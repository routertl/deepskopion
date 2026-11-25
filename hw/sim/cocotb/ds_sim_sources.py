# rr unknown-llm (AI-generated)
"""Scoped autodiscovery of a sim DUT's compile set — kills hand-curated lists.

A cocotb test must NOT carry a hand-curated `custom_libraries`/`hdl_sources`
list: such lists silently DRIFT when the design changes (a datalogger enabled in
datalogger_config.yml, a conditioner the gather starts instantiating) and they
MASK broken sibling testbenches by scoping around them. That is the exact
anti-pattern the pure-routertl flow exists to kill ("Pure-routertl
Tool Flow").

`rr sim`'s no-args auto-compose (RTL-P2.356) is the right idea but too coarse
for a sub-entity DUT: it compiles every testbench in `sources.sim`, so one
broken sibling tb breaks every unit test. This helper does the scoping with
routertl's own `DependencyResolver`: it walks ONLY the DUT's instantiation tree,
ordered correctly, with zero hand-curation — so a newly enabled datalogger or a new
conditioner instance is picked up automatically (proven: it yields the exact
39-file set for tb_datalogger_gather, auto-including glitch_filter + jtag_datalogger).

The portable core no longer needs vendor libraries. The remaining XPM bodies
belong only to rr_ddr_stream's optional Xilinx implementation and are outside
the datalogger-core closure. This helper therefore buckets open-logic package
sources into `olo` and project/utility sources into `tich`; it never invents a
vendor-library bucket for portable RTL.

Intended usage once RTL-P3.703 lands:

    from ds_sim_sources import scoped_libraries
    run_simulation(
        top_level="tb_datalogger_gather",
        module="test_datalogger_gather",
        custom_libraries=scoped_libraries("tb_datalogger_gather"),
        generics=GENERICS, work_lib="tich",
    )
"""

from pathlib import Path
from typing import Dict, List, Optional


def deepskopion_root() -> Path:
    """Repo root (holds hw/, libs/, project.yml). This file lives at
    <root>/hw/sim/cocotb/ds_sim_sources.py."""
    return Path(__file__).resolve().parents[3]


_HDL_SUFFIXES = (".vhd", ".vhdl", ".v", ".sv")


def declared_runtime_sources(root: Path) -> List[Path]:
    """HDL files tools/generated_sources.yml declares as runtime outputs.

    DS-P2.235: these are gitignored build outputs (DS-P2.231: rom_info_pkg.vhd),
    written by a pre_build or pre_sim hook. The resolver skips gitignored files,
    so they are handed to it explicitly; a declared output not on disk yet is
    left out, and the sim's analysis then names the missing unit.
    """
    import yaml

    manifest = root / "tools" / "generated_sources.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    found: List[Path] = []
    for family in (data.get("runtime_families") or {}).values():
        for rel in (family or {}).get("outputs") or []:
            path = root / rel
            if path.suffix.lower() in _HDL_SUFFIXES and path.is_file():
                found.append(path)
    return found


def scoped_libraries(
    top_entity: str,
    root: Optional[Path] = None,
    extra_src_dirs: Optional[List[str]] = None,
) -> Dict[str, List[str]]:
    """Resolve the ordered compile set for `top_entity` and bucket it into the
    named libraries the deepskopion sim uses: open-logic sources land in
    library ``olo`` and project RTL + libs/routertl utils + the tb land in
    ``tich``. The file SET and ORDER come entirely from routertl's
    DependencyResolver — no hand-curation, so the list can never go stale.
    """
    from routertl_core.dependency_resolver import DependencyResolver

    root = root or deepskopion_root()
    src_dirs = ["hw/rtl", "hw/sim", "libs/routertl", "libs/open-logic"]
    if extra_src_dirs:
        src_dirs.extend(extra_src_dirs)
    src_paths = [root / d for d in src_dirs if (root / d).exists()]
    # DS-P2.235: the gitignored generated runtime sources the manifest declares.
    src_paths.extend(declared_runtime_sources(root))

    dr = DependencyResolver(src_paths=src_paths, verbose=False)
    order = dr.get_compilation_order_with_libs(top_entity)

    # DS-T3.22: emit PROJECT-RELATIVE paths so a file the run_simulation
    # locked-package injector also folds in (it returns project-relative paths,
    # e.g. "libs/routertl/infilter/infilter.vhd") dedups against the scoped entry
    # at simulation.py's `if _f not in _existing` merge. Returning absolute paths
    # here made the same infilter.vhd appear twice in the `tich` analyse list
    # (scoped-absolute + injected-relative), so nvc re-analysed it and qspi_datalogger
    # — bound to the first copy — failed "depends on an obsolete version of
    # TICH.INFILTER". Relative form is what nonedge's hand list and the injector
    # both already use; the backend resolves them against PROJECT_ROOT.
    libs: Dict[str, List[str]] = {}
    for _lib, path in order:
        target = "olo" if "open-logic" in path.parts else "tich"
        try:
            rel = path.resolve().relative_to(root.resolve())
            rendered = str(rel)
        except ValueError:
            # Outside the repo root (shouldn't happen for src_dirs under root) —
            # keep absolute so the file is still found.
            rendered = str(path)
        libs.setdefault(target, []).append(rendered)
    return libs
