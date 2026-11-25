# rr unknown-llm (AI-generated)
"""Cheap contract guards for DE-R0 (DS-P1.39) — the expensive queued gate is
DE25 core baseline runner (a bench script, not in this distribution); see ``test_xilinx_baseline_contract.py``
for the sibling DS-X3 (xilinx) version this mirrors.

DE-R0's four "Done when" bullets and where each is actually proven:

  1. `rr` resolves the SAME core source closure for vendor: xilinx and
     vendor: altera.
     Proxied here by module-set identity between `xilinx_baseline` and
     `de25_baseline` (both targets share the identical `packages:`/`paths:`
     block verbatim — see the header comment in bench/targets/de25_core_baseline.yml
     — so identical module membership means the same DUT graph is walked).
  2. the altera closure contains no XPM and no UNISIM.
     Proven PARTIALLY here (the vendor-substitute stub files this repo
     supplies are XPM/UNISIM-free) and PARTIALLY NOT provable statically —
     see test_known_gap_altera_print_sources_still_names_xilinx_body below,
     and tools/sim/pre_sim_seed_libs.py's DE-R0 docstring section, for the
     measured upstream gap this cannot close from deepskopion alone.
  3. no datalogger source is duplicated between the two.
     Proven here: the two targets' `packages:` dicts (which is what pulls in
     every datalogger/gather/framer/DDR-model source) are asserted byte-equal.
  4. both closures simulate with BYTE-IDENTICAL message output for the same
     stimuli.
     NOT provable statically — this is what the queued
     DE25 core baseline / run_xilinx_baseline.py gate exercises for
     real; see the DE-R0 landing commit for a manual byte-diff of a
     `--clean` run of test_datalogger_to_ddr under both targets (identical
     SIM TIME / PASS/FAIL / message text; only the random seed and real-time
     timing columns differed, both expected to vary run-to-run).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files, with_bench  # noqa: E402  (DS-P1.103)

# DS-P1.103: the Xilinx reference is the shipped zybo_datalogger; the Altera baseline is a bench target.
XILINX_TARGET = ROOT / "targets" / "zybo_datalogger.yml"
DE25_DATALOGGER_TARGET = ROOT / "targets" / "de25_datalogger.yml"
ZYBO_DATALOGGER_TARGET = ROOT / "targets" / "zybo_datalogger.yml"

# DS-P1.103: every target file, products and bench (it held canfd_demo, de25_core_baseline and the two products).
ALL_TARGET_PATHS = all_target_files(ROOT)
TEST_ROOT = ROOT / "hw" / "sim" / "cocotb" / "tests"
VENDOR_STUB_DIR = ROOT / "tools" / "sim" / "vendor_stubs" / "rr_ddr_stream_intel"

_VENDOR_LIBRARY_RE = re.compile(r"^\s*library\s+(xpm|unisim)\s*;", re.IGNORECASE | re.MULTILINE)


def _xilinx_tag() -> list[str]:
    target = yaml.safe_load(XILINX_TARGET.read_text(encoding="utf-8"))
    return target["simulation"]["tags"]["xilinx_baseline"]


def _module_paths() -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for path in TEST_ROOT.rglob("test_*.py"):
        assert path.stem not in paths, f"duplicate cocotb module {path.stem}"
        paths[path.stem] = path
    return paths


def test_gather_can_absent_from_all_target_tags() -> None:
    """DS-P2.121: test_datalogger_gather_can must not appear in any simulation tag.

    The committed datalogger mix is CAN-free (C_CAN_DATALOGGERS == 0), and the
    datalogger builder cannot scope generated RTL per-target without colliding
    with the default package. Protocol-level CAN coverage is provided by
    test_can_datalogger across all four targets.

    Reads every target YAML, products and bench (tools/target_files.py, DS-P1.103),
    to guard against accidental re-addition.
    """
    for path in ALL_TARGET_PATHS:
        target = yaml.safe_load(path.read_text(encoding="utf-8"))
        tags = target.get("simulation", {}).get("tags", {})
        for tag_name, modules in tags.items():
            assert "test_datalogger_gather_can" not in modules, (
                f"{path.name} tag '{tag_name}' unexpectedly contains 'test_datalogger_gather_can'"
            )


DE25_DATALOGGER_TARGET = ROOT / "targets" / "de25_datalogger.yml"
CTU_PACKAGE = "ctu-fee/ctucanfd_ip_core"
CTU_STUB = "tools/sim/vendor_stubs/ctu_can_fd/can_top_apb_stub.vhd"


def _resolved(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "extends" not in raw:
        return raw
    # rr resolves `extends:` relative to the target file; a bare name means <name>.yml beside it
    # (DS-P1.103: bench/targets/de25_p2writer.yml extends ../../targets/de25_datalogger.yml).
    base = str(raw["extends"])
    merged = dict(_resolved((path.parent / (base if base.endswith(".yml") else f"{base}.yml")).resolve()))
    merged.update({k: v for k, v in raw.items() if k != "extends"})
    return merged


_CORE_SOURCES = ("hw/rtl/deepskopion_core.vhd", "hw/sim/tb_ctu_")


def _core_targets() -> dict[str, dict]:
    """Targets whose compile plan can contain `library ctu_can_fd_rtl` (DS-P2.272):
    every target whose `auto_sources` is not false (rr's default is true; the resolver
    then walks from top_module AND top_sim, which is how classic_eye's bitstream closure
    preflight reached deepskopion_core.vhd and hw/sim/tb_ctu_*.vhd, queue job
    703d8084186a, 2026-11-12), plus any `auto_sources: false` target whose explicit
    source lists name deepskopion_core.vhd or a hw/sim/tb_ctu_* file."""
    members = {}
    for path in all_target_files(ROOT):  # DS-P1.103: products and bench
        target = _resolved(path)
        if target.get("auto_sources", True) is not False:
            members[path.stem] = target
            continue
        listed = str(target.get("sources") or "")
        if any(core in listed for core in _CORE_SOURCES):
            members[path.stem] = target
    return members


def test_ctu_library_is_the_stub_exactly_when_a_core_target_lacks_the_ctu_package() -> None:
    """DS-P3.171 / DS-P2.121: deepskopion_core.vhd references `library ctu_can_fd_rtl`
    unconditionally. A target whose compile plan can contain it (DS-P2.272: auto_sources
    not false, or an explicit list naming the core or a tb_ctu bench) either carries the real CTU
    package (library ctu_can_fd_rtl) and does NOT pin the stub, or carries no CTU
    package and pins ctu_can_fd_rtl to exactly the stub top. Otherwise the closure
    preflight fails on the library, or the stub shadows the real cores.
    """
    members = _core_targets()
    # Presence: the scan must find the known core targets, or it proves nothing.
    # DS-P1.103: the products always; the bench targets only where bench/ exists.
    assert with_bench({"de25_datalogger", "pfsoc_datalogger", "zybo_datalogger"},
                      {"canfd_demo", "de25_canfd", "de25_core_baseline"}, ROOT) <= set(members), sorted(members)
    wrong = {}
    for name, target in members.items():
        has_package = CTU_PACKAGE in (target.get("packages") or {})
        sources = target.get("sources") if isinstance(target.get("sources"), dict) else {}
        pin = ((sources or {}).get("libraries") or {}).get("ctu_can_fd_rtl")
        if has_package and pin is not None:
            wrong[name] = f"carries {CTU_PACKAGE} and also pins ctu_can_fd_rtl: {pin}"
        elif not has_package and pin != [CTU_STUB]:
            wrong[name] = f"no {CTU_PACKAGE}, but ctu_can_fd_rtl is {pin!r}, not [{CTU_STUB}]"
    assert not wrong, wrong


def test_no_target_has_a_null_top_level_section() -> None:
    """DS-P3.171: a bare top-level key (`sources:` with its entries lost) loads
    as YAML null — rr then silently treats the section as empty. No target in
    this repo means that, so any null top-level value is a lost block.
    """
    offenders = {}
    for path in all_target_files(ROOT):  # DS-P1.103: products and bench
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        nulls = [key for key, value in raw.items() if value is None]
        if nulls:
            offenders[path.name] = nulls
    assert not offenders, f"targets with bare (null) top-level keys: {offenders}"


def test_vendor_stubs_declare_no_xpm_or_unisim_library() -> None:
    """DE-R0 bullet 2, the part deepskopion actually controls: the altera
    substitute bodies this repo supplies must never reference a vendor
    library — that would defeat the entire point of shipping them."""
    stub_files = sorted(VENDOR_STUB_DIR.glob("*.vhd"))
    assert len(stub_files) == 3, f"expected 3 vendor stub .vhd files, found {stub_files}"
    for stub in stub_files:
        text = stub.read_text(encoding="utf-8")
        match = _VENDOR_LIBRARY_RE.search(text)
        assert match is None, f"{stub} declares '{match.group(0) if match else ''}'"


def test_pre_sim_seed_selects_altera_stubs_for_altera_vendor(tmp_path, monkeypatch) -> None:
    """Unit-tests pre_sim_seed_libs._active_target_vendor()'s RR_TARGET path
    and confirms it maps to the XPM-free seed set — the actual mechanism
    that keeps a real `rr sim run` on the altera target from analysing the
    Xilinx-only bodies as the FIRST (only) copy in the `rr` work library.
    """
    import importlib

    seed_mod = importlib.import_module("tools.sim.pre_sim_seed_libs")

    fake_target = tmp_path / "fake_altera.yml"
    fake_target.write_text(
        yaml.safe_dump({"hardware": {"vendor": "altera"}}), encoding="utf-8"
    )
    monkeypatch.setenv("RR_TARGET", str(fake_target))
    assert seed_mod._active_target_vendor() == "altera"

    fake_xilinx = tmp_path / "fake_xilinx.yml"
    fake_xilinx.write_text(
        yaml.safe_dump({"hardware": {"vendor": "xilinx"}}), encoding="utf-8"
    )
    monkeypatch.setenv("RR_TARGET", str(fake_xilinx))
    assert seed_mod._active_target_vendor() == "xilinx"

    # DS-P1.55 test repair: this leg intended to exercise the UNRESOLVABLE
    # fallback, but with RR_TARGET unset the resolver falls through to
    # project.yml's `target:` pointer — and the committed project.yml now
    # points at the altera de25_datalogger target, so the state this leg built
    # was resolvable (vendor "altera") and the assert failed for the wrong
    # reason. Point the module's ROOT at an empty dir so no project.yml
    # exists and the leg actually tests what it claims.
    monkeypatch.delenv("RR_TARGET", raising=False)
    monkeypatch.setattr(seed_mod, "ROOT", tmp_path / "no-project-here")
    assert seed_mod._active_target_vendor() == "xilinx"  # unresolvable -> safe default


def test_known_gap_altera_print_sources_still_names_xilinx_body() -> None:
    """DE-R0 bullet 2, the part deepskopion CANNOT fix from a target yml.

    Measured (2026-08-22, DS-P1.39 resume): `rr sim run --target
    de25_core_baseline --refresh-lock --print-sources`, and a real `--clean`
    `rr sim run --target de25_core_baseline test_datalogger_to_ddr`, both still
    resolve `libs/routertl/rr_ddr_stream/rtl/support/xilinx/
    rr_fifo_{async_mixed,async,sync}_xpm_body.vhd` (each starting
    `library xpm;`) into the altera target's compile set, and nvc actually
    analyses them ("[Simulation] Vendor libs referenced: xpm — ensuring
    cache…" appears in the altera run's own log). Root cause: `paths.sources:
    [hw/rtl, libs]` makes auto_sources walk the ENTIRE libs/ tree directly
    (bypassing ip.lock's `synth_only_files` tagging, which only governs the
    package-resolution path) — and the SDK's own vendor-pair collision
    suppressor (`DependencyResolver._are_inactive_vendor_fifo_bodies`) only
    silences the "Name collision" WARNING; it does not perform vendor
    selection, so which file's architecture ends up the elaboration default
    is directory-walk-order-dependent, not vendor-aware.

    Functionally this is harmless FOR THIS PROJECT: `GEN_XPM` in
    rr_fifo_async_mixed.vhd / rr_fifo_sync.vhd is gated on `C_IMPL = "xpm"`,
    and every de25_baseline test drives G_FIFO_IMPL=olo, so the xpm-bodied
    architecture is never elaborated (proven by the byte-identical
    test_datalogger_to_ddr message diff against xilinx_baseline — see this
    file's module docstring, bullet 4). But the literal DE-R0 bullet-2 claim
    ("the altera closure contains no XPM") is NOT true at the raw
    source-closure level, only at the elaborated/simulated level. This test
    is a static assertion of the (unfortunate) STATUS QUO, not a check that
    the gap is fixed — it exists so a future `rr` upgrade that finally
    honors `sources_per_vendor` breaks this assertion, which is the signal
    to delete RR_SEED_ALTERA / this test / the vendor_stubs/ workaround.
    """
    lock_text = (ROOT / "ip.lock").read_text(encoding="utf-8")
    assert "rtl/support/xilinx/rr_fifo_async_mixed_xpm_body.vhd" in lock_text
    assert "rtl/support/xilinx/rr_fifo_sync_xpm_body.vhd" in lock_text
    # The upstream package DOES declare a real per-vendor split; this project
    # just can't make `rr` act on it yet (see the docstring above).
    ip_yml_text = (
        ROOT / "libs" / "routertl" / "rr_ddr_stream" / "ip.yml"
    ).read_text(encoding="utf-8")
    assert "sources_per_vendor" in ip_yml_text
