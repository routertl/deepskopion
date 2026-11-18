"""DS-T1.21: Multi-bit words crossing clock domains must use a coherent protocol
or a documented, constraint-bounded quasi-static multicycle path. Plain 2-FF chains
with ASYNC_REG do not make multi-bit words coherent.

Source: DS-P1.72 timing-constraint audit (docs/DS-P1.72-timing-constraint-audit.md,
audit draft label DS-T1.21).

Invariants verified:
1. ddr_commit_addr (dynamic 32-bit word, clk_ddr -> clk_sys) uses a coherent
   toggle request/acknowledge handshake with data held stable at the transmitter,
   not an incoherent plain 2-FF chain.
2. Independent diagnostic and protocol lines (wp_dbg, spi_edge, i2c_edge, can_edge)
   use dedicated scalar 2-flop shift registers, not multi-bit ASYNC_REG vectors.
3. EMIO probe readback reaches the regbank through its per-bit synchronizer.
4. REA quasi-static MCPs (rr_rea_sync_word) carry documented, constraint-bounded
   waivers (REA-REQ-959, rr_rea_scoped.xdc/.sdc). DS-P2.193: the waiver is the
   UPSTREAM inline pragma rr-rea 1.12.3 ships (REA-P2.26), in the vendored file
   whose bytes ip.lock pins, so a re-vendor reinstalls it. The DS-P2.189 local
   copy in cdc_lint_waivers.yml is gone (tools/cdc_lint_waivers.py still gates).
5. Whole-design RouteRTL CDC check is clean with 0 unwaived findings for both
   RR-CDC-002 (no multi-bit bus through a plain 2-FF ASYNC_REG chain) and, since
   DS-P2.188, RR-CDC-005 (routertl RTL-P2.1672: no unsynchronized multi-bit
   capture). RR-CDC-005 fails a capture with no synchroniser, a handshake that
   captures a register not launched with the request (mutant B, 899d75b6), and
   a live bus captured at a handshake point (c16bd4df). It does NOT catch a
   handshake whose in-flight guard is removed (mutant A): that structure is a
   valid open-loop toggle MCP statically, so invariant 1's behavioural sim
   stays the guard for it. Inline pragmas waive either rule; the only RR-CDC-005
   pragmas are the vendored rr-ddr-stream base-address crossings (DDR-P2.29).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import cdc_lint_waivers  # noqa: E402

CORE_VHDL = ROOT / "hw" / "rtl" / "deepskopion_core.vhd"
TOP_VHDL = ROOT / "hw" / "rtl" / "deepskopion_top.vhd"
REA_CDC_VHDL = ROOT / "libs" / "routertl" / "rea" / "rtl" / "rr_rea_cdc.vhd"


COCOTB_COMMIT_ADDR_TEST = (
    ROOT / "hw" / "sim" / "cocotb" / "tests" / "infra"
    / "commit_addr_crossing" / "test_commit_addr_crossing.py"
)
RESULTS_COMMIT_ADDR = ROOT / "sim" / "results" / COCOTB_COMMIT_ADDR_TEST.stem / "latest.json"
RESULTS_XML_COMMIT_ADDR = RESULTS_COMMIT_ADDR.with_name("results.xml")
BUILD_ID_PKG = ROOT / "generated" / "rr_rea_build_id_pkg.vhd"
N_COCOTB_TESTS = 1


def _cocotb_failures() -> str:
    """Every per-test <failure> message/text the sim's result_collector wrote."""
    if not RESULTS_XML_COMMIT_ADDR.exists():
        return "(no results.xml)"
    out = []
    for tc in ET.parse(RESULTS_XML_COMMIT_ADDR).iter("testcase"):
        for f in tc.iter("failure"):
            out.append(f"{tc.get('name')}: {f.get('message') or ''} "
                       f"{(f.text or '').strip()[-1500:]}")
    return "\n".join(out) or "(no failure nodes)"


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout,
        env={**os.environ, "RR_QUEUE_SIMS": "0", "RR_QUEUE_BUILDS": "0"},
    )


def test_ddr_commit_addr_uses_coherent_handshake() -> None:
    """DS-T1.21: ddr_commit_addr is a dynamic 32-bit DMA counter. It must NOT cross
    via plain 2-FF ASYNC_REG registers, and MUST use a coherent toggle req/ack handshake.

    Verified via behavioral cocotb simulation (hw/sim/cocotb/tests/infra/commit_addr_crossing/
    test_commit_addr_crossing.py):
    - clk_ddr (7 ns) and clk_sys (10 ns) driven at different, non-integer-related periods.
    - ddr_wr_commit_addr changes every clk_ddr cycle with alternating patterns whose upper
      and lower halves differ (0x0000FFFF vs 0xFFFF0000).
    - Skew is deposited between upper and lower halves (half-cycle offset).
    - Asserts that every captured word in clk_sys equals one valid held pattern (never torn).
    - Asserts that commit_addr_held_ddr never changes while an exchange is in flight.
    """
    rr = shutil.which("rr")
    assert rr, "rr CLI not on PATH — this gate cannot run the sanctioned sim"
    sync = _run([rr, "workspace", "sync"], timeout=600)
    assert sync.returncode == 0, (
        "rr workspace sync failed — cannot stage the sim's locked packages:\n"
        + sync.stdout[-2000:] + sync.stderr[-2000:]
    )

    if RESULTS_COMMIT_ADDR.exists():
        RESULTS_COMMIT_ADDR.unlink()
    if RESULTS_XML_COMMIT_ADDR.exists():
        RESULTS_XML_COMMIT_ADDR.unlink()

    # The sanctioned entry runs the rr_rea_build_id_sim hook that writes this
    # gitignored package. Running the cocotb file with python skips the hook, so
    # analysis fails on a clean checkout and passes on any tree that still has
    # the file. Removing it here stops a warm tree hiding that.
    BUILD_ID_PKG.unlink(missing_ok=True)
    proc = _run([rr, "sim", "run", COCOTB_COMMIT_ADDR_TEST.stem], timeout=900)
    assert BUILD_ID_PKG.exists(), (
        "generated/rr_rea_build_id_pkg.vhd is missing: the sim ran without the "
        "rr_rea_build_id_sim hook, so any later operator errors in the REA RTL are "
        f"knock-on errors (rc={proc.returncode}); sim output tail:\n"
        + proc.stdout[-2000:] + proc.stderr[-1000:]
    )
    assert RESULTS_COMMIT_ADDR.exists(), (
        "Commit-address CDC crossing cocotb test did not complete "
        f"(rc={proc.returncode}); sim output tail:\n"
        + proc.stdout[-4000:] + proc.stderr[-2000:]
    )

    verdict = json.loads(RESULTS_COMMIT_ADDR.read_text(encoding="utf-8"))
    assert (
        proc.returncode == 0
        and verdict.get("passed") == N_COCOTB_TESTS
        and verdict.get("failed") == 0
        and verdict.get("errors") == 0
        and verdict.get("skipped") == 0
        and verdict.get("total") == N_COCOTB_TESTS
    ), (
        f"Commit-address CDC crossing cocotb test not green: "
        f"passed={verdict.get('passed')} failed={verdict.get('failed')} "
        f"errors={verdict.get('errors')} skipped={verdict.get('skipped')} "
        f"total={verdict.get('total')} (expected {N_COCOTB_TESTS} pass); "
        f"recorded cocotb failures:\n{_cocotb_failures()}"
    )


def test_independent_lines_use_scalar_synchronizers() -> None:
    """DS-T1.21: Independent status/protocol lines (wp_dbg, spi_edge, i2c_edge, can_edge)
    must NOT cross as multi-bit ASYNC_REG vectors. Each asynchronous source must use
    exactly one scalar 2-flop chain; native clk_sys sources need no synchronizer and
    launch flops merged by synthesis must not fan out into duplicate chains."""
    text_core = CORE_VHDL.read_text(encoding="utf-8", errors="replace")
    text_top = TOP_VHDL.read_text(encoding="utf-8", errors="replace")

    # Multi-bit vector synchronizer names that must be absent
    banned_vector_names = ["wp_dbg_meta", "spi_edge_meta_r", "i2c_edge_meta_r", "can_edge_meta_r"]
    offenders = []
    for name in banned_vector_names:
        if re.search(rf"\bsignal\s+{name}\s*:\s*std_logic_vector", text_core, re.IGNORECASE):
            offenders.append(f"core:{name}")
        if re.search(rf"\bsignal\s+{name}\s*:\s*std_logic_vector", text_top, re.IGNORECASE):
            offenders.append(f"top:{name}")

    assert not offenders, f"multi-bit ASYNC_REG vector found for independent lines: {offenders}"

    # Independent edge inputs must retain one scalar synchronizer per line.
    required_scalar_syncs = [
        "spi_edge_sclk_sync_r",
        "spi_edge_mosi_sync_r",
        "spi_edge_miso_sync_r",
        "spi_edge_ss_sync_r",
        "i2c_edge_scl_sync_r",
        "i2c_edge_sda_sync_r",
        "can_edge_rx_sync_r",
        "can_edge_tx_sync_r",
    ]
    missing_scalars = [
        sig for sig in required_scalar_syncs
        if not re.search(rf"\bsignal\s+{sig}\s*:\s*std_logic_vector\(1\s+downto\s+0\)", text_core, re.IGNORECASE)
    ]
    assert not missing_scalars, f"multi-bit ASYNC_REG vector found for independent lines: missing scalars {missing_scalars}"

    # DS-P2.167 already supplies the write-plane debug synchronization. Preserve
    # that source-aware topology: pipeBusy is native to clk_sys, pipeBusyDdr has
    # one 2-FF chain, and the two synthesized-identical CS outputs share one
    # chain before fanning out in clk_sys. This prevents both double-syncing and
    # CDC-11 launch-flop fan-out while retaining scalar crossings.
    flat_core = re.sub(r"\s+", " ", text_core)
    required_wp_async = [
        "wp_dbg_pbusy_ddr_meta",
        "wp_dbg_pbusy_ddr_sync",
        "wp_dbg_cs_meta",
        "wp_dbg_cs_sync",
    ]
    missing_wp_async = [
        sig for sig in required_wp_async
        if not re.search(
            rf'attribute\s+async_reg\s+of\s+{sig}\s*:\s*signal\s+is\s+"TRUE"',
            text_core,
            re.IGNORECASE,
        )
    ]
    assert not missing_wp_async, (
        "write-plane scalar synchronizers missing ASYNC_REG stages: "
        f"{missing_wp_async}"
    )
    assert "wp_dbg_pipe_busy_r <= ddr_dbg_reset_status(3)" in flat_core, (
        "clk_sys-native pipeBusy must be registered directly, not double-synchronized"
    )
    assert (
        "wp_dbg_pbusy_ddr_meta <= ddr_dbg_reset_status(2)" in flat_core
        and "wp_dbg_pbusy_ddr_sync <= wp_dbg_pbusy_ddr_meta" in flat_core
    ), "pipeBusyDdr must use exactly one scalar 2-FF synchronizer"
    assert (
        # DS-P3.196: the CS source is first registered in clk_ddr (wp_dbg_cs_ddr_r);
        # still one scalar 2-FF chain into clk_sys.
        "wp_dbg_cs_ddr_r <= ddr_dbg_reset_status(1)" in flat_core
        and "wp_dbg_cs_meta <= wp_dbg_cs_ddr_r" in flat_core
        and "wp_dbg_cs_sync <= wp_dbg_cs_meta" in flat_core
        and "wp_dbg_sync <= wp_dbg_pipe_busy_r & wp_dbg_pbusy_ddr_sync & wp_dbg_cs_sync & wp_dbg_cs_sync" in flat_core
        and not re.search(r"wp_dbg_\w+\s*<=\s*ddr_dbg_reset_status\(0\)", flat_core)
    ), "merged CS launch source must use one scalar 2-FF chain before clk_sys fan-out"


def test_emio_probe_readback_is_synchronized() -> None:
    """DS-T1.21: EMIO probe readback (raw ja/jb/PS7 EMIO pins) must reach the
    regbank through its per-bit synchronizer, never as the raw async word
    (DS-P3.166 Finding F5)."""
    text_core = CORE_VHDL.read_text(encoding="utf-8", errors="replace")

    # EMIO probe readback (raw ja/jb/PS7 EMIO pins) must reach the regbank through
    # its per-bit synchronizer, never as the raw async word (DS-P3.166 Finding F5).
    assert re.search(
        r"telemetry_in\(C_INDEX_EMIO_PROBE\s*/\s*4\)\.data\s*<=\s*rea_trig_sticky\s*&\s*emio_probe_sync_w\s*;",
        text_core, re.IGNORECASE,
    ) and re.search(
        r"emio_probe_sync_r\(i\)\s*<=\s*emio_probe_sync_r\(i\)\(0\)\s*&\s*emio_probe_word\(i\)",
        text_core, re.IGNORECASE,
    ), "EMIO probe readback bypasses its per-bit 2-FF synchronizer (raw async pins reach the regbank)"


def test_rea_quasi_static_mcp_waiver() -> None:
    """DS-T1.21: REA trigger/condition words (rr_rea_sync_word) are quasi-static MCPs
    stable while armed (REA-REQ-959, bounded in rr_rea_scoped.xdc/.sdc). They must carry
    a documented waiver, and rr_rea_cdc.vhd must have 0 unwaived RR-CDC-002 findings.

    DS-P2.193: the waiver is the inline pragma upstream rr-rea 1.12.3 ships on s1/s2.
    It must sit in the ip.lock-pinned bytes (a hand edit of the vendored file is a
    local patch, which a re-vendor drops: 30c53d04 did), and no local waiver for the
    file may remain in cdc_lint_waivers.yml (the DS-P2.189 copy)."""
    lock = yaml.safe_load((ROOT / "ip.lock").read_text(encoding="utf-8"))["routertl/rea"]
    data = REA_CDC_VHDL.read_bytes()
    rel = REA_CDC_VHDL.relative_to(ROOT).as_posix()
    assert hashlib.sha256(data).hexdigest() == lock["file_hashes"][rel], (
        f"REA quasi-static MCP waiver missing: {rel} is not the ip.lock-pinned "
        f"upstream file of routertl/rea {lock['resolved_version']} (a local patch)"
    )
    text = data.decode("utf-8")
    for sig in ("s1", "s2"):
        decl = re.search(rf"^\s*signal\s+{sig}\s*:.*$", text, re.M)
        assert decl and re.search(
            r"rr-lint-disable-line\s+RR-CDC-002.*REA-REQ-959.*rr_rea_scoped", decl.group(0)
        ), (
            f"REA quasi-static MCP waiver missing: upstream {sig} declaration in {rel} "
            "carries no RR-CDC-002 pragma citing REA-REQ-959 / rr_rea_scoped"
        )

    waivers, _ = cdc_lint_waivers.load_waivers(ROOT, ROOT / cdc_lint_waivers.WAIVERS_FILE)
    local = [w.id for w in waivers if w.path == rel]
    assert not local, (
        f"REA quasi-static MCP waiver duplicated: local waiver(s) {local} for {rel} "
        "remain in cdc_lint_waivers.yml; upstream 1.12.3 carries it"
    )

    unwaived, refused = cdc_lint_waivers.check(ROOT)
    rea = [str(f) for f in unwaived if f.path.endswith("rr_rea_cdc.vhd")]
    assert not rea and not refused, (
        f"REA quasi-static MCP waiver missing: unwaived findings remain in "
        f"rr_rea_cdc.vhd: {rea}; refused waivers: {refused}"
    )


def test_whole_design_rr_cdc_vhdl_provisional_clean() -> None:
    """DS-T1.21: Every VHDL source file in the design closure must have 0 unwaived
    RR-CDC-002 findings (no multi-bit bus through a plain 2-FF ASYNC_REG chain)
    and, since DS-P2.188, 0 unwaived RR-CDC-005 findings (no unsynchronized
    multi-bit capture). Inline pragmas (per rule) and cdc_lint_waivers.yml
    (DS-P2.189, RR-CDC-002 only) are the only waivers.

    RR-CDC-005 catches ddr_commit_addr_sys captured from the live clk_ddr bus
    (test_whole_design_gate_fails_on_unsynchronized_capture). It cannot catch a
    removed in-flight guard (mutant A); test_ddr_commit_addr_uses_coherent_handshake
    guards that behaviourally."""
    assert cdc_lint_waivers.scan_files(ROOT), "No VHDL files discovered in design closure"

    unwaived, refused = cdc_lint_waivers.check(ROOT)
    assert not unwaived and not refused, (
        f"RR-CDC finding detected in design closure: {[str(f) for f in unwaived]}; "
        f"refused waivers: {refused}"
    )


# The DS-T1.21 commit-address handshake capture in deepskopion_core.vhd, and the
# two historical mutants of it RR-CDC-005 must fail (DS-P2.188).
_HANDSHAKE_CAPTURE = (
    "      if commit_req_sync_sys(1) /= commit_ack_tgl_sys then\n"
    "        ddr_commit_addr_sys <= commit_addr_held_ddr;\n"
    "        commit_ack_tgl_sys  <= commit_req_sync_sys(1);\n"
    "      end if;\n"
)
_CDC_MUTANTS = {
    # 899d75b6: the handshake captures the live clk_ddr bus, not the held word.
    "mutant_b_899d75b6": (
        "      if commit_req_sync_sys(1) /= commit_ack_tgl_sys then\n"
        "        ddr_commit_addr_sys <= ddr_wr_commit_addr;\n"
        "        commit_ack_tgl_sys  <= commit_req_sync_sys(1);\n"
        "      end if;\n"
    ),
    # c16bd4df: the handshake is gone; the live bus is sampled every clk_sys.
    "no_handshake_c16bd4df": (
        "      ddr_commit_addr_sys <= ddr_wr_commit_addr;\n"
        "      commit_ack_tgl_sys  <= commit_req_sync_sys(1);\n"
    ),
}


def _scratch_design(dest: Path, core_text: str) -> Path:
    """The whole-design closure (every scanned VHDL file + the waiver file)
    under *dest*, with deepskopion_core.vhd replaced by *core_text*."""
    for src in cdc_lint_waivers.scan_files(ROOT):
        rel = src.relative_to(ROOT)
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest / rel)
    shutil.copyfile(ROOT / cdc_lint_waivers.WAIVERS_FILE, dest / cdc_lint_waivers.WAIVERS_FILE)
    (dest / CORE_VHDL.relative_to(ROOT)).write_text(core_text, encoding="utf-8")
    return dest


def _design_gate(root: Path) -> tuple[int, str]:
    """The whole-design gate CLI (the command the pre-commit leg runs)."""
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "cdc_lint_waivers.py"), "check",
         "--root", str(root)],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    return out.returncode, out.stdout


@pytest.mark.parametrize("mutant", sorted(_CDC_MUTANTS))
def test_whole_design_gate_fails_on_unsynchronized_capture(tmp_path, mutant) -> None:
    """DS-P2.188: the whole-design gate must go red when the commit-address
    handshake captures a register not launched with the request (mutant B,
    899d75b6) or samples the live bus with no handshake (c16bd4df). RR-CDC-002
    alone stayed green on both; RR-CDC-005 reports the capture line."""
    core = CORE_VHDL.read_text(encoding="utf-8")
    assert core.count(_HANDSHAKE_CAPTURE) == 1, (
        "the DS-T1.21 commit-address handshake capture changed shape in "
        "deepskopion_core.vhd; update _HANDSHAKE_CAPTURE / _CDC_MUTANTS"
    )
    core_rel = CORE_VHDL.relative_to(ROOT).as_posix()

    # The unmutated design is gate-clean (test_whole_design_rr_cdc_vhdl_provisional_clean),
    # so the RR-CDC-005 finding pinned to the capture line below is the mutant's.
    mutated = core.replace(_HANDSHAKE_CAPTURE, _CDC_MUTANTS[mutant])
    capture = 1 + next(i for i, line in enumerate(mutated.splitlines())
                       if "ddr_commit_addr_sys <= ddr_wr_commit_addr;" in line)
    rc, out = _design_gate(_scratch_design(tmp_path / "mutant", mutated))
    assert rc == 1 and re.search(
        rf"^UNWAIVED {re.escape(core_rel)}:{capture}:\d+: RR-CDC-005 .*ddr_commit_addr_sys",
        out, re.M,
    ), (
        f"unsynchronized commit-address capture not caught by the whole-design gate "
        f"({mutant}, exit {rc}): expected RR-CDC-005 at {core_rel}:{capture}\n{out}"
    )


_PLUMBING_VHD = "libs/routertl/synthetic/rtl/unit.vhd"


def _plumbing_gate(tmp_path, monkeypatch, vhdl, findings, waivers="version: 1\nwaivers: []\n"):
    """Run the gate library on a one-file synthetic closure whose RR-CDC-005
    findings are *findings* (line, col, msg) and whose RR-CDC-002 checker finds
    nothing. The synthetic file sits under libs/, so these plumbing checks stay
    independent of whether the gate scans project RTL (that is the
    unsynchronized-capture test's invariant)."""
    from routertl_core.lint_checkers import rr_cdc_domain_vhdl, rr_cdc_vhdl

    vhd = tmp_path / _PLUMBING_VHD
    vhd.parent.mkdir(parents=True)
    vhd.write_text(vhdl, encoding="utf-8")
    (tmp_path / "evidence.md").write_text("REQ-1 quasi-static\n", encoding="utf-8")
    (tmp_path / "w.yml").write_text(waivers, encoding="utf-8")
    monkeypatch.setattr(rr_cdc_domain_vhdl, "check_rr_cdc_005_unsynchronized_multibit_crossing",
                        lambda path, text: list(findings))
    monkeypatch.setattr(rr_cdc_vhdl, "check_rr_cdc_002_multibit_two_ff_sync",
                        lambda path, text: [])
    return cdc_lint_waivers.check(tmp_path, tmp_path / "w.yml")


_CAPTURE_LAUNCH = [(3, 5, "unsynchronized multi-bit capture of bus_q"),
                   (4, 5, "unsynchronized multi-bit launch of bus_q")]


def _two_line_unit(pragma_on_capture: str = "") -> str:
    return (
        "architecture rtl of unit is\n"
        "begin\n"
        f"  bus_q <= bus_d;{pragma_on_capture}\n"
        "  bus_d <= bus_src;\n"
        "end architecture;\n"
    )


def test_whole_design_gate_honours_rr_cdc_005_pragma(tmp_path, monkeypatch) -> None:
    """DS-P2.188: an inline `rr-lint-disable-line RR-CDC-005` pragma waives the
    RR-CDC-005 finding on its own line (the vendored rr-ddr-stream base-address
    crossings rely on it), and only that line: the finding on the next line stays."""
    unwaived, errors = _plumbing_gate(
        tmp_path, monkeypatch,
        _two_line_unit("  -- rr-lint-disable-line RR-CDC-005 test waiver"), _CAPTURE_LAUNCH)
    assert [(f.rule, f.line) for f in unwaived] == [("RR-CDC-005", 4)], (
        f"RR-CDC-005 inline-pragma contract broken: expected only the unpragma'd "
        f"line 4 finding, got {[str(f) for f in unwaived]}"
    )
    assert not errors, errors


def test_rr_cdc_002_pragma_does_not_waive_rr_cdc_005(tmp_path, monkeypatch) -> None:
    """DS-P2.188: a pragma names ONE rule. `rr-lint-disable-line RR-CDC-002` on
    a line must not silence the RR-CDC-005 finding on it."""
    unwaived, errors = _plumbing_gate(
        tmp_path, monkeypatch,
        _two_line_unit("  -- rr-lint-disable-line RR-CDC-002 wrong rule"), _CAPTURE_LAUNCH)
    assert (("RR-CDC-005", 3) in [(f.rule, f.line) for f in unwaived]), (
        f"an RR-CDC-002 pragma waived the RR-CDC-005 finding on line 3: "
        f"{[str(f) for f in unwaived]}"
    )


def test_rr_cdc_002_yml_waiver_does_not_waive_rr_cdc_005(tmp_path, monkeypatch) -> None:
    """DS-P2.188: cdc_lint_waivers.yml waivers are RR-CDC-002 only. A waiver
    anchored on the very declaration an RR-CDC-005 finding lands on must leave
    that finding unwaived (the waiver is then stale, since no RR-CDC-002 finding
    matches it)."""
    vhdl = (
        "architecture rtl of unit is\n"
        "  signal bus_q : std_logic_vector(7 downto 0);\n"
        "begin\n"
        "end architecture;\n"
    )
    waivers = (
        "version: 1\nwaivers:\n"
        f"  - id: w1\n    rule: RR-CDC-002\n    path: {_PLUMBING_VHD}\n    entity: unit\n"
        "    signals: [bus_q]\n    reason: test\n"
        "    evidence:\n      - {file: evidence.md, contains: REQ-1}\n"
    )
    unwaived, errors = _plumbing_gate(
        tmp_path, monkeypatch, vhdl,
        [(2, 3, "unsynchronized multi-bit capture of bus_q")], waivers)
    assert [(f.rule, f.line) for f in unwaived] == [("RR-CDC-005", 2)], (
        f"an RR-CDC-002 yml waiver waived an RR-CDC-005 finding: {unwaived}"
    )
    assert any("stale" in e for e in errors), errors
