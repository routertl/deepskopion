"""DS-T1.13: the write-plane ARMING observability must reach the register a
devmem caller actually reads, and its 32-bit packing must be exact.

The DS-T1.12 wedge (rr_ddr_stream 0.3.2 wedges, 0.3.1 commits) is proven on
silicon by A/B but does NOT reproduce in simulation, so no sim test can catch
the wedge itself.  What DS-T1.13 adds — and what this guard protects — is the
OBSERVABILITY that turns a multi-hour silicon mystery into a single ``devmem``
read of DDR_RESET_STATUS (0x50): the CDC empty flag the uploader arms on, and
the sticky "did AW/W/B ever fire since rst_ddr" latches that name the first
divergence vs the 0.3.1 control.

Every assertion is made against the RTL the BUILD consumes
(``hw/rtl/deepskopion_core.vhd``) and the register contract the PS reads
(``project_regbank.yml``) — the deployed surfaces — not a helper return value.
The load-bearing one is ``test_ddr_reset_status_packing_is_exactly_32_bits``:
a mis-sized concatenation (a bit dropped or doubled) would shift every field
and make the on-silicon witness quote the WRONG signal as the divergence, so
the guard recomputes the packing width from the signal declarations.

POWER (run against the pre-DS-T1.13 core to see it fail):
  git show HEAD~0:hw/rtl/deepskopion_core.vhd  # the arming bits are absent →
  the packing has only bits[2:0] driven and the arming/binding tests FAIL.
MUTATION: rewire a sticky to '0', or restore ``ddr_csr_o => open``, or drop a
field from the DDR_RESET_STATUS concat → the matching test below FAILS.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(
    os.environ.get("DE25_TEST_ROOT", Path(__file__).resolve().parents[2])
).resolve()
CORE = ROOT / "hw/rtl/deepskopion_core.vhd"
REGBANK_YML = ROOT / "project_regbank.yml"


# ── helpers ──────────────────────────────────────────────────────────────


def _core() -> str:
    return CORE.read_text(encoding="utf-8")


def _signal_widths(src: str) -> dict[str, int]:
    """Map every ``signal <name> : <type>`` declaration to its bit width."""
    widths: dict[str, int] = {}
    for m in re.finditer(
        r"signal\s+(\w+)\s*:\s*std_logic_vector\s*\(\s*(\d+)\s+downto\s+(\d+)\s*\)",
        src,
    ):
        widths[m.group(1)] = int(m.group(2)) - int(m.group(3)) + 1
    for m in re.finditer(r"signal\s+(\w+)\s*:\s*std_logic\b(?!_vector)", src):
        widths[m.group(1)] = 1
    return widths


def _ddr_reset_status_rhs(src: str) -> str:
    """The RHS driving telemetry_in(C_INDEX_DDR_RESET_STATUS/4).data <= ... ;"""
    m = re.search(
        r"telemetry_in\(C_INDEX_DDR_RESET_STATUS\s*/\s*4\)\.data\s*<=(.*?);",
        src,
        re.DOTALL,
    )
    assert m, "DDR_RESET_STATUS .data assignment not found in the core"
    return m.group(1)


def _term_width(term: str, widths: dict[str, int]) -> int:
    """Width of one concatenation term (an aggregate, an indexed bit, or a name)."""
    term = re.sub(r"--.*", "", term).strip()
    # (H downto L => '0')  -> H - L + 1
    agg = re.match(r"\(\s*(\d+)\s+downto\s+(\d+)\s*=>", term)
    if agg:
        return int(agg.group(1)) - int(agg.group(2)) + 1
    # name(H downto L) -> slice width
    sl = re.match(r"[\w.]+\(\s*(\d+)\s+downto\s+(\d+)\s*\)", term)
    if sl:
        return int(sl.group(1)) - int(sl.group(2)) + 1
    # name(i) -> single bit
    if re.match(r"[\w.]+\(\s*\d+\s*\)", term):
        return 1
    # a record field access like ddr_csr_w.fifoStatus.chip_empty -> 1 bit
    if "." in term:
        return 1
    # a bare signal name -> its declared width (default 1 for std_logic)
    return widths.get(term, 1)


# ── the deployed register packing ────────────────────────────────────────


def test_ddr_reset_status_packing_is_exactly_32_bits() -> None:
    """A mis-sized concat shifts every field → the silicon witness names the
    wrong signal. Recompute the width from the declarations, not by trust."""
    src = _core()
    widths = _signal_widths(src)
    rhs = _ddr_reset_status_rhs(src)
    terms = [t for t in rhs.split("&") if t.strip()]
    total = sum(_term_width(t, widths) for t in terms)
    assert total == 32, (
        f"DDR_RESET_STATUS packs {total} bits, not 32 — "
        f"terms: {[t.strip() for t in terms]}"
    )


def test_ddr_reset_status_preserves_low_bits() -> None:
    """Backward compatibility: bits[2:0] stay write_idle / ever-deassert / live
    rst_ddr (existing PS decoders + the DS-T1.12 evidence read 0x6 there)."""
    rhs = _ddr_reset_status_rhs(_core())
    # the three lowest concat terms, in order, are the preserved [2:0]
    # (strip inline comments the same way _term_width does)
    terms = [re.sub(r"--.*", "", t).strip() for t in rhs.split("&")]
    tail = [t for t in terms if t][-3:]
    assert re.match(r"ddr_write_idle_sync\(\s*1\s*\)", tail[0]), tail
    assert tail[1] == "rst_ddr_ever_deassert", tail
    assert re.match(r"rst_ddr_sync\(\s*1\s*\)", tail[2]), tail


def test_ddr_reset_status_carries_the_arming_probes() -> None:
    """The KEY divergence probes must reach the register the PS reads: the CDC
    empty flag and the sticky AW/W/B/write-active latches (via wp_seen_sync)."""
    rhs = _ddr_reset_status_rhs(_core())
    assert "ddr_csr_w.fifoStatus.chip_empty" in rhs, "CDC empty (KEY) not packed"
    assert "wp_seen_sync" in rhs, "sticky arming latches not packed"
    assert "wp_dbg_sync" in rhs, "dbg_reset_status not packed"
    for f in ("empty", "full", "write_error"):
        assert f"ddr_csr_w.fifoStatus.{f}" in rhs, f"{f} not packed"


# ── the arming path end to end (real IP signals, not tie-offs) ───────────


def test_ip_write_plane_ports_are_bound_not_open() -> None:
    """The previously-`open` taps must be wired, or there is nothing to sample."""
    src = _core()
    assert re.search(r"ddr_csr_o\s*=>\s*ddr_csr_w", src), "ddr_csr_o left open"
    assert re.search(
        r"dbg_reset_status_o\s*=>\s*ddr_dbg_reset_status", src
    ), "dbg_reset_status_o not wired"


def test_sticky_latches_are_driven_by_real_axi_handshake() -> None:
    """The 'ever seen' latches must set on the ACTUAL uploader AXI signals —
    a latch tied to a constant or the wrong bus would read plausibly but lie."""
    src = _core()
    # normalise whitespace to make the source-line checks robust
    flat = re.sub(r"\s+", " ", src)
    assert (
        "if axi_h2c.AWVALID = '1' then aw_valid_seen_ddr <= '1'" in flat
    ), "aw_valid_seen not set by axi_h2c.AWVALID (the 'first-write arm')"
    assert (
        "if axi_h2c.AWVALID = '1' and axi_c2h.AWREADY = '1' then aw_hs_seen_ddr <= '1'"
        in flat
    ), "aw_hs_seen not set by the AW handshake"
    assert (
        "if axi_c2h.BVALID = '1' then b_valid_seen_ddr <= '1'" in flat
    ), "b_valid_seen not set by axi_c2h.BVALID"
    assert (
        "if ddr_write_idle_ddr_sync(1) = '0' then wr_active_seen_ddr <= '1'" in flat
    ), "wr_active_seen not set by ddr_write_idle_ddr_sync(1)"


def test_stickies_are_two_ff_synced_into_clk_sys() -> None:
    """Each clk_ddr sticky crosses through its own 2-FF scalar chain."""
    src = _core()
    flat = re.sub(r"\s+", " ", src)
    sources = {
        "aw_valid_seen_sync_r": "aw_valid_seen_ddr",
        "aw_hs_seen_sync_r": "aw_hs_seen_ddr",
        "w_valid_seen_sync_r": "w_valid_seen_ddr",
        "b_valid_seen_sync_r": "b_valid_seen_ddr",
        "wr_active_seen_sync_r": "wr_active_seen_ddr",
        "wr_err_seen_sync_r": "wr_err_seen_ddr",
    }
    for sync, source in sources.items():
        assert f"{sync} <= {sync}(0) & {source}" in flat, f"missing scalar chain for {source}"
        assert re.search(
            rf'attribute async_reg of {sync}\s*:\s*signal is "TRUE"', src
        ), f"{sync} not marked ASYNC_REG"
    assert (
        "wp_seen_sync <= wr_err_seen_sync_r(1) & wr_active_seen_sync_r(1) "
        "& b_valid_seen_sync_r(1) & w_valid_seen_sync_r(1) "
        "& aw_hs_seen_sync_r(1) & aw_valid_seen_sync_r(1)" in flat
    ), "wp_seen_sync bit order does not match the documented decode"


def test_rea_probe_routes_the_write_plane_word() -> None:
    """The single USER1 REA must sample the clk_sys write-plane word."""
    assert re.search(
        r"rea_probe_o\s*<=\s*write_plane_dbg_word", _core()
    ), "rea_probe_o not routed to write_plane_dbg_word"

# ── the register contract documents the new bits ─────────────────────────


def test_regbank_yml_documents_the_arming_bits() -> None:
    """A caller decoding 0x50 needs the bit map; keep the contract in sync."""
    desc = REGBANK_YML.read_text(encoding="utf-8")
    m = re.search(r"name:\s*DDR_RESET_STATUS.*?(?=\n\s*-\s*name:)", desc, re.DOTALL)
    assert m, "DDR_RESET_STATUS entry not found in project_regbank.yml"
    block = m.group(0)
    assert "Write-plane arming" in block, "new bits undocumented in the register contract"
    assert "chip_empty" in block and "aw_valid" in block, (
        "the KEY probes (chip_empty / aw_valid) are not documented for 0x50"
    )
