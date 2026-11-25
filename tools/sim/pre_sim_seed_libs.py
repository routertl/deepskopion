#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-P3.55 pre_sim seed — work around the rr-sim per-top closure under-population.

WHY THIS EXISTS (and why it is a WORKAROUND, not the real fix)
--------------------------------------------------------------
`rr sim run` builds the per-library nvc analyse list from a per-testbench-top
dependency closure. For deepskopion that closure drops two things that the
SYNTH flow (build_env.tcl `g_libraries`) resolves correctly:

  1. TICH.ZS_MSG_PKG — `datalogger_gather.vhd` does `use tich.zs_msg_pkg.all`
     (make_msg_type + severity/domain constants), but the sim closure omits
     `hw/rtl/pkg/zs_msg_pkg.vhd` from the `tich` analyse list. nvc then fails
     with `design unit ZS_MSG_PKG not found in library TICH`.

  2. RR.<*>_XPM_BODY — `rr_fifo_async_mixed.vhd` / `rr_fifo_sync.vhd` bind
     `entity work.rr_fifo_*_xpm_body` inside a `GEN_XPM` generate. The bodies
     live under `libs/routertl/rr_ddr_stream/rtl/support/xilinx/` (or, for a
     non-Xilinx target, the pure-stub equivalents under `.../intel/` — see
     DE-R0 below) and are tagged `synth_only_files` in ip.lock, so the sim
     compose drops them — but VHDL still requires the entity to exist at
     analysis time, so nvc fails with `design unit RR_FIFO_*_XPM_BODY not
     found in library WORK`.

DE-R0 (DS-P1.39): the rr_ddr_stream ip.yml declares `sources_per_vendor` so
the resolved SYNTH closure never puts a real `library xpm;` file in front of
a non-Xilinx target — but this SDK build does not act on that key at all
(measured: `rr sim run --target <altera> --refresh-lock --print-sources`
still reports the `xilinx/rr_fifo_*_xpm_body.vhd` files unchanged; ip.lock's
`files:` entry for routertl/rr_ddr_stream is vendor-flat). This hook is the
ONE place that actually injects those files into a target's simulated
closure (see point 2 above), so it is also the one place that must pick the
right body set — otherwise `library xpm;` would leak into every vendor's
sim closure, including a nominally XPM-free altera one. The seed choice
below is keyed off the ACTIVE TARGET's `hardware.vendor` (RR_TARGET env, or
project.yml's `target:` pointer), mirroring the resolution order
`routertl_core.project_manager._active_target_name` uses.

Both are the SAME upstream gap: routertl RTL-P3.643 (the sim per-library
resolver doesn't reach synth-parity for transitive package + entity deps).
The OLO library divergence (the third blocker) was fixed properly via a
project.yml per-package `library: olo` override (RTL-P2.656 machinery) and is
NOT handled here.

This hook pre-analyses the dropped units into the same nvc work libraries that
`rr sim` uses (`hw/sim/work/nvc/<lib>`), in dependency order, BEFORE the sim
compile runs. nvc persists units in the on-disk library and `rr sim` does not
wipe pre-existing libs, so the closure compile then finds them. It is
idempotent (skips when the units already exist) and a no-op for non-nvc /
non-deepskopion setups.

Remove this hook once RTL-P3.643 lands and the sim closure reaches the same
files build_env.tcl's `g_libraries` already groups.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
from active_target import load_active_target  # noqa: E402

WORK = ROOT / "hw" / "sim" / "work" / "nvc"
STD = "--std=2008"

# Dropped units, per nvc work library, in dependency order. Mirrors the
# build_env.tcl g_libraries grouping for exactly the files the sim closure omits.
#
#   3. TICH.INFILTER — the per-top sim closure compiles the libs/routertl util
#      `infilter_array.vhd` into `work` (it is an auto-discovered library source),
#      but that file does `i_infilter : entity tich.infilter` while the closure
#      assigns `infilter` to the `tich` bucket and the unit-test custom_libraries
#      override drops it — so nvc fails `design unit INFILTER not found in library
#      TICH` during the work pre-compile, before the DUT is even touched. This hit
#      EVERY eye/util unit test (test_prbs_gen, test_eye_engine, test_clk_drp).
#      Seeding infilter into `tich` lets the spuriously-compiled infilter_array
#      bind. Same upstream gap (divergent enumerators, routertl RTL-T2.47); the
#      real fix is to scope the closure to the DUT subtree so an orphaned util is
#      never compiled.
#      CAVEAT: a datalogger top whose OWN closure already compiles infilter (from the
#      SDK source `routertl/src/units/infilter.vhd`) will then double-compile it
#      (this seed + the closure copy) and fail `…depends on an obsolete version of
#      TICH.INFILTER`. That datalogger double-compile is a SEPARATE facet of the same
#      RTL-T2.47 SDK bug and was already broken ("INFILTER not found") before this
#      seed — it is NOT fixed here. This seed unblocks the eye/util unit tests.
TICH_SEED = [
    "hw/rtl/pkg/zs_msg_pkg.vhd",
    # DS-T3.22: the rr-sim per-top closure folds the declared util package
    # `infilter_array` into the `work` library (no ip.lock `library:` attribution
    # → default_lib=work), but infilter_array does `i_infilter : entity
    # tich.infilter`. The closure compiles `work` BEFORE the custom `tich`
    # bucket, so nvc fails `design unit INFILTER not found in library TICH`
    # (RTL-P3.643 / RTL-T2.47 tich↔work cross-lib ordering; the closure's
    # _scope_packages_to_dut prune can't reach a tb top that lives under hw/sim,
    # outside paths.sources, so infilter_array is never pruned). Pre-seeding
    # `infilter` into `tich` makes the entity resolvable when `work` compiles.
    # IEEE-only leaf — no further deps to seed. Idempotent via _already_seeded.
    "libs/routertl/infilter/infilter.vhd",
]
# The xpm bodies are analysed into the SAME library the consumer compiles into
# (the `rr` library — ddr_stream_axi). On a Xilinx target they reference
# `library xpm`, which the shared routertl vendor_libs XPM cache provides
# (RTL-P2.1102 — the vendored libs/xpm_vhdl clone is removed); nvc resolves it
# via the -L search path once the xpm lib is present. We seed them into `rr`
# so `entity work.*_xpm_body` binds.
# DS-P2.72: un-vendored — these xpm bodies now come from the registry copy
# (libs/routertl/rr_ddr_stream), not the removed libs/local/ddr_stream_axi.
RR_SEED_XILINX = [
    "libs/routertl/rr_ddr_stream/rtl/support/xilinx/rr_fifo_async_mixed_xpm_body.vhd",
    "libs/routertl/rr_ddr_stream/rtl/support/xilinx/rr_fifo_async_xpm_body.vhd",
    "libs/routertl/rr_ddr_stream/rtl/support/xilinx/rr_fifo_sync_xpm_body.vhd",
]
# DE-R0 (DS-P1.39): the non-Xilinx equivalent. Pure IEEE entity-declaration
# stubs (no `library xpm;`, no `library unisim;`) vendored from the SAME
# ip.lock-pinned rr_ddr_stream commit (d0075f4d3ae9a6438ee5c603ef81bcd9f0aa1a56)
# as RR_SEED_XILINX above — they exist upstream precisely so a non-Xilinx
# static entity-binding check (Quartus, and — this hook's reason for
# existing — nvc) can resolve the untaken GEN_XPM branch without ever
# naming a vendor library. See libs/routertl/rr_ddr_stream/ip.yml
# `synthesis.sources_per_vendor.altera` — a key this SDK build does not act
# on (see DE-R0 note above), which is why these live under
# tools/sim/vendor_stubs/ instead of anywhere under libs/: see
# tools/sim/vendor_stubs/rr_ddr_stream_intel/README.md for the two ways a
# libs/ location breaks (pkg-install pruning, and duplicate auto_sources
# discovery racing this hook's explicit seed).
RR_SEED_ALTERA = [
    "tools/sim/vendor_stubs/rr_ddr_stream_intel/rr_fifo_async_mixed_xpm_body_stub.vhd",
    "tools/sim/vendor_stubs/rr_ddr_stream_intel/rr_fifo_async_xpm_body_stub.vhd",
    "tools/sim/vendor_stubs/rr_ddr_stream_intel/rr_fifo_sync_xpm_body_stub.vhd",
]


def _active_target_vendor() -> str:
    """``hardware.vendor`` of the active target, defaulting to ``xilinx``.

    The target is resolved as rr resolves it (tools/active_target.py:
    $RR_TARGET, then ``rr target set``'s local file, then project.yml) without
    importing the shared bench-wide SDK install. Any failure to resolve falls
    back to ``xilinx`` — the only vendor this project built for before DE-R0,
    so an unresolvable target keeps today's behavior rather than silently
    dropping XPM support.
    """
    path, target_cfg = load_active_target(ROOT)
    if path is None:
        return "xilinx"
    hardware = target_cfg.get("hardware")
    vendor = hardware.get("vendor") if isinstance(hardware, dict) else None
    return str(vendor).lower() if vendor else "xilinx"


def _nvc() -> str | None:
    cand = shutil.which("nvc")
    if cand:
        return cand
    # DS-P1.27: no hard-coded developer-home path — honor an explicit NVC
    # override, then the conventional per-user install prefix (the location
    # docs/SUPPORTED_ENVIRONMENT.md prescribes), relative to WHOEVER runs us.
    env_nvc = os.environ.get("NVC")
    if env_nvc and Path(env_nvc).exists():
        return env_nvc
    fixed = Path.home() / ".local/opt/nvc/usr/bin/nvc"
    return str(fixed) if fixed.exists() else None


def _unit_names(vhd_path: Path) -> list[str]:
    """Primary design-unit names a VHDL file declares (package / entity /
    architecture-of), UPPERCASE. nvc persists each as a ``<LIB>.<UNIT>`` file in
    the library dir, so the names let us tell whether the unit is already there.
    """
    text = vhd_path.read_text(encoding="utf-8", errors="replace")
    names: set[str] = set()
    names.update(m.group(1).upper()
                 for m in re.finditer(r"(?im)^\s*package\s+(?!body\b)(\w+)", text))
    names.update(m.group(1).upper()
                 for m in re.finditer(r"(?im)^\s*entity\s+(\w+)", text))
    names.update(m.group(1).upper()
                 for m in re.finditer(r"(?im)^\s*architecture\s+\w+\s+of\s+(\w+)", text))
    return sorted(names)


def _already_seeded(lib_dir: Path, lib: str, vhd_path: Path) -> bool:
    """True iff every design unit *vhd_path* declares already exists in *lib_dir*.

    RTL-T3.14: this is the idempotency the docstring always promised but never
    implemented. Re-analysing an already-present package (with the seed's
    ``--relaxed`` flags) mints a DIFFERENT checksum and overwrites the copy the
    rr-sim closure compiled — while the persisted dependents (compiled against
    the old checksum, source-unchanged so incrementally skipped) are NOT
    re-analysed, so elaboration fails with `depends on … checksum X but the
    library has checksum Y`. Skipping the re-analyse when the unit is already
    present (whether seeded earlier OR compiled by the closure) keeps the library
    self-consistent.
    """
    units = _unit_names(vhd_path)
    if not units:
        return False
    return all((lib_dir / f"{lib.upper()}.{u}").exists() for u in units)


def _analyze(nvc: str, lib: str, files: list[str]) -> int:
    lib_dir = WORK / lib
    WORK.mkdir(parents=True, exist_ok=True)
    # Init the lib if rr sim hasn't created it yet (nvc tolerates re-analyse).
    if not (lib_dir / "_NVC_LIB").exists():
        subprocess.run(
            [nvc, "-L", str(WORK), STD, f"--work={lib}:{lib_dir}", "--init"],
            cwd=ROOT, check=False,
        )
    abs_files = []
    for rel in files:
        p = ROOT / rel
        if not p.exists():
            print(f"  [pre_sim_seed] missing (skip): {rel}")
            continue
        # Idempotency (RTL-T3.14): never re-analyse a unit that is already in the
        # lib — that overwrite is what desyncs persisted dependents' checksums.
        if _already_seeded(lib_dir, lib, p):
            print(f"  [pre_sim_seed] present, skip (idempotent): {rel}")
            continue
        abs_files.append(str(p))
    if not abs_files:
        return 0
    cmd = [nvc, "-L", str(WORK), STD, f"--work={lib}:{lib_dir}", "-a", "--relaxed", *abs_files]
    res = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if res.returncode != 0:
        # Don't fail the whole sim — print so the cause is visible (no-silent-failures).
        print(f"  [pre_sim_seed] WARNING: seeding {lib} returned {res.returncode}")
        sys.stderr.write(res.stderr)
    else:
        print(f"  [pre_sim_seed] seeded {lib}: {len(abs_files)} file(s)")
    return res.returncode


def main() -> int:
    nvc = _nvc()
    if not nvc:
        print("  [pre_sim_seed] nvc not found — skipping (no-op)")
        return 0
    vendor = _active_target_vendor()
    rr_seed = RR_SEED_ALTERA if vendor in ("altera", "intel") else RR_SEED_XILINX
    print(f"  [pre_sim_seed] active target vendor={vendor!r}; seeding rr from "
          f"{'RR_SEED_ALTERA (no XPM)' if rr_seed is RR_SEED_ALTERA else 'RR_SEED_XILINX'}")
    _analyze(nvc, "tich", TICH_SEED)
    _analyze(nvc, "rr", rr_seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
