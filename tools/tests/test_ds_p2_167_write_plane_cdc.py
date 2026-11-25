# rr unknown-llm (AI-generated)
"""DS-P2.167 — Write-plane debug instrumentation crossings synchronization.

Vivado report_cdc on Zybo datalogger identified three CDC violations in the
write-plane debug instrumentation added in DS-T1.13 / DS-T2.18 (Finding F3):

(a) ddr_write_idle is a clk_sys domain signal (write_idle_o), but p_wp_sticky
    sampled it directly on clk_ddr with no synchronizer (CDC-1 Critical).
    Fix: 2-FF synchronizer on ddr_write_idle into clk_ddr (ddr_write_idle_ddr_sync)
    with ASYNC_REG = TRUE.

(b) uploader_cs_r and downloader_cs_r share identical drivers in ddr_stream_axi
    and merge into a single launch register in synthesis. Sampling both into
    wp_dbg_meta caused that single launch flop to fan out into two independent
    synchronizer chains (CDC-11 Critical).
    Fix: single 2-FF synchronizer chain for CS into clk_sys with ASYNC_REG,
    fanning out within clk_sys to drive both uploader_cs and downloader_cs in wp_dbg_sync.

(c) wp_seen_meta was marked ASYNC_REG, but wp_seen_sync lacked it (CDC-2 Warning).
    ddr_dbg_reset_status combined pipeBusy (clk_sys native), pipeBusyDdr (clk_ddr),
    and CS (clk_ddr) into a single vector synchronizer chain wp_dbg_meta
    (Quartus CDC-50012).
    Fix: ASYNC_REG on wp_seen_sync, and dedicated per-source chains:
    - pipeBusy native clk_sys without ASYNC_REG;
    - pipeBusyDdr dedicated 2-FF with ASYNC_REG;
    - CS dedicated 2-FF with ASYNC_REG.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_VHD = ROOT / "hw" / "rtl" / "deepskopion_core.vhd"


def _core() -> str:
    assert CORE_VHD.is_file(), f"missing deepskopion_core.vhd at {CORE_VHD}"
    return CORE_VHD.read_text(encoding="utf-8")


def test_ddr_write_idle_synchronized_into_clk_ddr() -> None:
    """DS-P2.167 finding F3(a): ddr_write_idle is a clk_sys signal. Sampling it
    in p_wp_sticky on clk_ddr without a synchronizer caused CDC-1 Critical
    violations. It must be 2-FF synchronized into clk_ddr with ASYNC_REG, and
    p_wp_sticky must sample the synchronized stage."""
    src = _core()
    flat = re.sub(r"\s+", " ", src)
    assert re.search(
        r"signal\s+ddr_write_idle_ddr_sync\s*:\s*std_logic_vector\s*\(\s*1\s+downto\s+0\s*\)",
        src,
    ), "ddr_write_idle not 2-FF synchronized into clk_ddr: missing signal declaration"
    assert re.search(
        r"attribute\s+async_reg\s+of\s+ddr_write_idle_ddr_sync\s*:\s*signal\s+is\s+\"TRUE\"",
        src,
    ), "ddr_write_idle not 2-FF synchronized into clk_ddr: missing async_reg attribute"
    assert (
        "ddr_write_idle_ddr_sync <= ddr_write_idle_ddr_sync(0) & ddr_write_idle" in flat
    ), "ddr_write_idle not 2-FF synchronized into clk_ddr: missing shift register assignment"
    assert (
        "if ddr_write_idle_ddr_sync(1) = '0' then wr_active_seen_ddr <= '1'" in flat
    ), "ddr_write_idle not 2-FF synchronized into clk_ddr: p_wp_sticky does not sample sync stage 1"


def test_wp_seen_both_stages_marked_async_reg() -> None:
    """DS-P2.167 finding F3(c) + DS-T1.21: each sticky status bit must use a
    complete scalar 2-FF ASYNC_REG chain, never a shared multi-bit chain."""
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
    assert not re.search(r"\bsignal\s+wp_seen_meta\s*:\s*std_logic_vector", src), (
        "wp_seen stickies still share a multi-bit synchronizer"
    )
    for sync, source in sources.items():
        assert re.search(
            rf"signal\s+{sync}\s*:\s*std_logic_vector\s*\(\s*1\s+downto\s+0\s*\)",
            src,
        ), f"{sync} scalar 2-FF chain missing"
        assert re.search(
            rf'attribute\s+async_reg\s+of\s+{sync}\s*:\s*signal\s+is\s+"TRUE"',
            src,
        ), f"{sync} not marked ASYNC_REG"
        assert f"{sync} <= {sync}(0) & {source}" in flat, (
            f"{sync} does not shift its dedicated source {source}"
        )


def test_dbg_reset_status_sources_have_dedicated_chains() -> None:
    """DS-P2.167 finding F3(b,c): dbg_reset_status sources must not share a
    single vector synchronizer chain.
    - pipeBusy is native clk_sys (no ASYNC_REG).
    - pipeBusyDdr is clk_ddr (dedicated 2-FF with ASYNC_REG).
    - uploader_cs_r / downloader_cs_r share one launch flop in the IP; syncing
      via a single 2-FF chain into clk_sys prevents CDC-11 launch fan-out."""
    src = _core()
    flat = re.sub(r"\s+", " ", src)
    assert not re.search(
        r"attribute\s+async_reg\s+of\s+wp_dbg_meta\s*:\s*signal\s+is\s+\"TRUE\"", src
    ), "dbg_reset_status sources must not share a synchronizer chain: wp_dbg_meta still present"
    assert re.search(
        r"attribute\s+async_reg\s+of\s+wp_dbg_pbusy_ddr_meta\s*:\s*signal\s+is\s+\"TRUE\"", src
    ), "dbg_reset_status sources must not share a synchronizer chain: missing wp_dbg_pbusy_ddr_meta ASYNC_REG"
    assert re.search(
        r"attribute\s+async_reg\s+of\s+wp_dbg_pbusy_ddr_sync\s*:\s*signal\s+is\s+\"TRUE\"", src
    ), "dbg_reset_status sources must not share a synchronizer chain: missing wp_dbg_pbusy_ddr_sync ASYNC_REG"
    assert re.search(
        r"attribute\s+async_reg\s+of\s+wp_dbg_cs_meta\s*:\s*signal\s+is\s+\"TRUE\"", src
    ), "dbg_reset_status sources must not share a synchronizer chain: missing wp_dbg_cs_meta ASYNC_REG"
    assert re.search(
        r"attribute\s+async_reg\s+of\s+wp_dbg_cs_sync\s*:\s*signal\s+is\s+\"TRUE\"", src
    ), "dbg_reset_status sources must not share a synchronizer chain: missing wp_dbg_cs_sync ASYNC_REG"
    assert (
        "wp_dbg_sync <= wp_dbg_pipe_busy_r & wp_dbg_pbusy_ddr_sync & wp_dbg_cs_sync & wp_dbg_cs_sync" in flat
    ), "dbg_reset_status sources must not share a synchronizer chain: wp_dbg_sync composition missing"
