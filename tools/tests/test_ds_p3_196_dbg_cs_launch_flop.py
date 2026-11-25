# rr unknown-llm (AI-generated)
"""DS-P3.196: the write-plane debug CS crossing launches from a clk_ddr flop.

Since rr_ddr_stream 0.3.4 (d1c8d53, DDR-T2.17) the IP drives
dbg_reset_status_o(1) combinationally: uploader_cs <= uploader_cs_r and not
pipeBusyDdr (ddr_stream_axi.vhd). deepskopion_core sampled that bit straight
into its clk_sys synchronizer (wp_dbg_cs_meta), so the AND fed the first
ASYNC_REG stage. Vivado report_cdc flagged it CDC-10 Critical (zybo synth,
rr queue job 85cc36856791, 2026-11-03): the AND can glitch when cs_r rises on
the same clk_ddr edge pipeBusyDdr re-asserts.

Invariants:
1. ddr_dbg_reset_status(1) is registered once in a process clocked by clk_ddr.
2. The clk_sys first stage wp_dbg_cs_meta samples that register, never the raw
   IP port.
3. No clk_sys process reads ddr_dbg_reset_status(1) directly.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_VHD = ROOT / "hw" / "rtl" / "deepskopion_core.vhd"

_PROCESS = re.compile(
    r"\b(\w+)\s*:\s*process\s*\(([^)]*)\)(.*?)\bend\s+process\b",
    re.IGNORECASE | re.DOTALL,
)


def _strip_comments(text: str) -> str:
    return re.sub(r"--[^\n]*", "", text)


def _processes() -> list[tuple[str, str, str]]:
    """(label, clock, body) for every clocked process in deepskopion_core."""
    text = _strip_comments(CORE_VHD.read_text(encoding="utf-8", errors="replace"))
    out = []
    for label, _sens, body in _PROCESS.findall(text):
        clk = re.search(r"rising_edge\s*\(\s*(\w+)\s*\)", body, re.IGNORECASE)
        if clk:
            out.append((label, clk.group(1).lower(), re.sub(r"\s+", " ", body)))
    return out


def _assigning(target: str) -> list[tuple[str, str, str]]:
    pat = re.compile(rf"\b{target}\s*<=\s*([^;]+);", re.IGNORECASE)
    return [
        (label, clk, m.group(1).strip())
        for label, clk, body in _processes()
        for m in pat.finditer(body)
    ]


def test_cs_bit_registered_in_clk_ddr() -> None:
    drivers = _assigning("wp_dbg_cs_ddr_r")
    assert len(drivers) == 1, f"expected one driver of wp_dbg_cs_ddr_r, got {drivers}"
    label, clk, rhs = drivers[0]
    assert clk == "clk_ddr", f"{label}: wp_dbg_cs_ddr_r must be clocked by clk_ddr, is {clk}"
    assert rhs.replace(" ", "").lower() == "ddr_dbg_reset_status(1)", rhs


def test_cs_meta_samples_the_clk_ddr_register() -> None:
    drivers = _assigning("wp_dbg_cs_meta")
    assert len(drivers) == 1, f"expected one driver of wp_dbg_cs_meta, got {drivers}"
    label, clk, rhs = drivers[0]
    assert clk == "clk_sys", f"{label}: wp_dbg_cs_meta is the clk_sys first stage, is {clk}"
    assert rhs.lower() == "wp_dbg_cs_ddr_r", (
        f"{label}: wp_dbg_cs_meta must sample the clk_ddr launch flop, not {rhs!r} "
        "(the IP's CS bit is combinational since rr_ddr_stream 0.3.4: CDC-10)"
    )


def test_no_clk_sys_process_reads_the_raw_cs_bit() -> None:
    raw = re.compile(r"ddr_dbg_reset_status\s*\(\s*1\s*\)", re.IGNORECASE)
    offenders = [label for label, clk, body in _processes() if clk == "clk_sys" and raw.search(body)]
    assert not offenders, f"clk_sys process(es) sample the raw combinational CS bit: {offenders}"
