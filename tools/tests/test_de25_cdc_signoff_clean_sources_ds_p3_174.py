# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""DS-P3.174 — Guards clean source roots for DE25 CDC signoff.

Under `rr cdc check --signoff` / `--strict`, any Verilog file residing
under scanned source roots (`paths.sources`: `hw/rtl`, `libs`, `generated`)
that sits outside the top's closure is treated as an unanalysed file and fails
the gate.

Guards:
  1. test_no_loose_verilog_in_scanned_source_roots:
     Scanned source directories of de25_datalogger must contain zero loose
     Verilog / SystemVerilog files.
  2. test_verilog_counter_removed_from_de25:
     routertl/verilog_counter is an unused package and must not be declared in
     targets/de25_datalogger.yml or present under libs/.
  3. test_pfsoc_mss_top_wrapper_isolated_from_de25_sources:
     pf_mss_top_wrapper.v must reside outside hw/rtl (in hw/pfsoc/) and be
     correctly referenced in bench/targets/pfsoc_mss.yml.
"""
from __future__ import annotations

from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]
DE25_TARGET = ROOT / "targets" / "de25_datalogger.yml"


def test_no_loose_verilog_in_scanned_source_roots() -> None:
    """All source directories scanned by de25_datalogger must be free of loose Verilog."""
    config = yaml.safe_load(DE25_TARGET.read_text(encoding="utf-8"))
    src_dirs = config.get("paths", {}).get("sources", [])
    assert src_dirs, "de25_datalogger must declare paths.sources"

    found_verilog: list[str] = []
    for rel_d in src_dirs:
        d = ROOT / rel_d
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.suffix.lower() in (".v", ".sv"):
                found_verilog.append(str(p.relative_to(ROOT)))

    assert not found_verilog, (
        f"Loose Verilog files found under scanned sources: {found_verilog}. "
        "Any Verilog outside the top closure fails rr cdc check --signoff."
    )


def test_verilog_counter_removed_from_de25() -> None:
    """routertl/verilog_counter must not be declared or present under libs/."""
    config = yaml.safe_load(DE25_TARGET.read_text(encoding="utf-8"))
    pkgs = config.get("packages", {})
    assert "routertl/verilog_counter" not in pkgs, (
        "routertl/verilog_counter must not be in targets/de25_datalogger.yml packages"
    )
    vcounter_dir = ROOT / "libs" / "routertl" / "verilog_counter"
    assert not vcounter_dir.exists(), (
        f"Unused verilog_counter directory must not exist: {vcounter_dir}"
    )


def test_no_testbench_under_hw_rtl() -> None:
    """Testbenches must sit under hw/sim (paths.simulation), never hw/rtl (RTL-P1.78)."""
    strays = sorted(
        str(p.relative_to(ROOT)) for p in (ROOT / "hw" / "rtl").rglob("tb_*")
    )
    assert not strays, f"Testbench files under hw/rtl: {strays}; move them to hw/sim"


def test_de25_cdc_elaborate_timeout_configured() -> None:
    """targets/de25_datalogger.yml must configure cdc.elaborate_timeout_s >= 2400.

    Measured Yosys elaboration time on deepskopion_de25_top is 1778.15 s (~29.6m).
    Without this configuration, GHDL->Yosys elaboration times out at default 120 s.
    """
    config = yaml.safe_load(DE25_TARGET.read_text(encoding="utf-8"))
    timeout = (config.get("cdc") or {}).get("elaborate_timeout_s")
    assert timeout is not None, "de25_datalogger.yml must define cdc.elaborate_timeout_s"
    assert timeout >= 2400, f"cdc.elaborate_timeout_s must be >= 2400 (measured 1778s), got {timeout}"


def test_qspi_datalogger_has_no_process_keep_attributes() -> None:
    """qspi_datalogger.vhd must not place attribute keep on clocked process signals.

    Yosys RTLIL translation fails with 'ERROR: wire not found for $posedge'
    when attribute keep is attached to signals assigned in a clocked process.
    """
    vhd = (ROOT / "hw/rtl/datalogger/qspi/qspi_datalogger.vhd").read_text(encoding="utf-8")
    assert 'attribute keep of msgVld_d' not in vhd, (
        "qspi_datalogger.vhd contains attribute keep on clocked process signals"
    )
