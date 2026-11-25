# rr unknown-llm (AI-generated)
"""DS-P1.71 — the four-channel live EDGE judge must be able to fail.

The judge (``sw/stream/testbench/edge4_live_verdict.py``) decides whether the
live DE25 capture RouteWave drew matches the known stimulus. A judge that only
ever says PASS proves nothing, so each defect class the ticket names is planted
into an otherwise perfect session and must turn exactly its own check FAIL:

  * an edge drawn one 65.536 ms window late (LT-P1.24 defect 1/2 signature);
  * an edge drawn twice;
  * an edge missing;
  * lanes left at the ``edge_<id>`` fallback instead of a HELO name;
  * the S=1 enable readback drawn as a transition (LT-P1.24 defect 3).

The perfect session is built from the RTL timing model alone
(``expected_transfer``), so the model is also pinned here against hand-derived
``ds_stimulus_gen.vhd`` ``p_spi`` positions.
"""
from __future__ import annotations

import pytest

from sw.stream.testbench import edge4_live_verdict as V
from sw.stream.testbench.edge4_live_stimulus import PHASES, EDGE_LINES

DEV_OF = {line: dev for dev, line in EDGE_LINES.items()}
GOOD_HELO_NAMES = {1: "spi_sclk", 2: "spi_mosi", 3: "spi_miso", 6: "spi_cs"}


def _session(phase="dense", n=6, helo_names=None, period_ns=1_000_000, start_ns=5_000_000_000):
    """A perfect session: stimulus log, RouteWave lanes, HELO, wire frames."""
    ph = PHASES[phase]
    transfers = [{"k": k, "mosi": m, "miso": s, "mono_ns": 10**12 + k * period_ns}
                 for k, (m, s) in enumerate(ph.transfers[:n])]
    stim = {"clk_div": ph.clk_div, "edge_lines": {str(d): ln for d, ln in EDGE_LINES.items()},
            "transfers": transfers}
    names = helo_names if helo_names is not None else GOOD_HELO_NAMES
    lanes = {}
    for line in V.LINES:
        dev = DEV_OF[line]
        lanes[line] = V.Lane(code=line[0], name=names.get(dev, f"edge_{dev}"),
                             initial=V.IDLE_LEVEL.get(line, 0))
    mosi_level = 0
    for k, x in enumerate(transfers):
        fall = start_ns + k * period_ns
        exp = V.expected_transfer(x["mosi"], x["miso"], ph.clk_div, mosi_level)
        mosi_level = V.mosi_after(x["mosi"], mosi_level)
        for line in V.LINES:
            for e in exp[line]:
                lanes[line].transitions.append((fall + e.offset_ns + (30 if e.lagged else 0), e.level))
    helo = {"channels": [{"id": d, "name": n} for d, n in names.items()]}
    readback_words = [(dev << 27) | (1 << 26) | ((V.IDLE_LEVEL.get(line, 0)) << 24)
                      for line, dev in DEV_OF.items()]
    captured = [[V.TAG_EDGE_EVENTS, {"anchor_ns": start_ns - 10**8, "words": readback_words}]]
    return stim, list(lanes.values()), helo, captured


def _verdict(session):
    return {c.name: c.ok for c in V.judge(*session)}


def test_expected_transfer_matches_p_spi_positions():
    # D = 200 cycles = 2000 ns. 0xA5 = 1010_0101 MSB-first.
    exp = V.expected_transfer(0xA5, 0x3C, 200, mosi_before=0)
    d = 2000
    assert [(e.offset_ns, e.level) for e in exp["cs"]] == [(0, 0), (18 * d, 1)]
    assert [(e.offset_ns, e.level) for e in exp["sclk"]][:4] == [
        (2 * d, 1), (3 * d, 0), (4 * d, 1), (5 * d, 0)]
    assert len(exp["sclk"]) == 16 and exp["sclk"][-1].offset_ns == 17 * d
    # MOSI: b7=1 at D, then 0,1,0,0,1,0,1 at the falls 3D,5D,...,15D.
    assert [(e.offset_ns, e.level) for e in exp["mosi"]] == [
        (d, 1), (3 * d, 0), (5 * d, 1), (7 * d, 0), (11 * d, 1), (13 * d, 0), (15 * d, 1)]
    # MISO 0x3C = 0011_1100: m7=0 at CS fall (no edge), m6=0 at 3D, m5=1 at 5D,
    # m4..m2=1, m1=0 at 13D; the 0x00 reload at 17D is no change.
    assert [(e.offset_ns, e.level) for e in exp["miso"]] == [(5 * d, 1), (13 * d, 0)]
    assert all(e.lagged for e in exp["miso"])


def test_perfect_session_passes_every_check():
    verdict = _verdict(_session())
    assert verdict and all(verdict.values()), verdict


def test_sparse_divider_session_passes():
    verdict = _verdict(_session("sparse", n=2, period_ns=5_250_000_000))
    assert all(verdict.values()), verdict


def test_edge_shifted_one_wrap_window_fails_shift_check():
    s = _session()
    lanes = s[1]
    sclk = next(ln for ln in lanes if ln.name == "spi_sclk")
    t, v = sclk.transitions[5]
    sclk.transitions[5] = (t + V.WRAP_NS, v)
    verdict = _verdict(s)
    assert verdict["none shifted by 65.536 ms"] is False
    assert verdict["every edge present"] is False


def test_whole_transfer_shifted_one_wrap_window_fails_shift_check():
    s = _session()
    for ln in s[1]:
        ln.transitions = [(t + V.WRAP_NS if t >= 5_003_000_000 else t, v) for t, v in ln.transitions]
    verdict = _verdict(s)
    assert verdict["none shifted by 65.536 ms"] is False


def test_duplicated_edge_fails_duplicate_check():
    s = _session()
    mosi = next(ln for ln in s[1] if ln.name == "spi_mosi")
    mosi.transitions.insert(3, mosi.transitions[3])
    verdict = _verdict(s)
    assert verdict["no edge duplicated / extra"] is False


def test_missing_edge_fails_presence_check():
    s = _session()
    miso = next(ln for ln in s[1] if ln.name == "spi_miso")
    del miso.transitions[2]
    verdict = _verdict(s)
    assert verdict["every edge present"] is False


def test_edge_n_fallback_names_fail_naming_check():
    s = _session(helo_names={1: "level_pin[1]", 2: "level_pin[2]", 3: "level_pin[3]",
                             6: "level_pin[6]"})
    # RouteWave skips level_pin[...] (LT-P3.260), so the lanes are edge_<id>.
    for line, ln in zip(V.LINES, s[1]):
        ln.name = f"edge_{DEV_OF[line]}"
    verdict = _verdict(s)
    assert verdict["channels named from HELO (not edge_N)"] is False
    assert verdict["every edge present"] is True


def test_readback_drawn_at_enable_fails_glitch_check():
    s = _session()
    cs = next(ln for ln in s[1] if ln.name == "spi_cs")
    # The S=1 readback of the idle-high CS drawn as a transition at enable.
    cs.initial = 0
    cs.transitions.insert(0, (4_500_000_000, 1))
    verdict = _verdict(s)
    assert verdict["no glitch at enable"] is False


def test_wrong_idle_level_fails_glitch_check():
    s = _session()
    cs = next(ln for ln in s[1] if ln.name == "spi_cs")
    cs.initial = 0   # CS drawn low before the first transfer: its fall is invisible
    verdict = _verdict(s)
    assert verdict["no glitch at enable"] is False


def test_parse_vcd_keeps_duplicate_names_apart():
    text = """$timescale 1ns $end
$var wire 1 ! uart_rx $end
$var wire 1 % uart_rx $end
$enddefinitions $end
$dumpvars
0!
1%
$end
#100
0%
#250
1%
"""
    lanes = V.parse_vcd(text)
    assert [(ln.code, ln.name, ln.initial) for ln in lanes] == [("!", "uart_rx", 0), ("%", "uart_rx", 1)]
    assert lanes[0].transitions == [] and lanes[1].transitions == [(100, 0), (250, 1)]


def test_parse_vcd_restated_value_is_not_an_edge():
    # DS-P1.71 landing review: RouteWave exported idle-high CS as `0A` in
    # $dumpvars and then `0A` again at the CS fall. A re-stated value is not a
    # transition; counting it false-passed an edge the viewer never drew.
    text = """$timescale 1ns $end
$var wire 1 A edge_6 $end
$enddefinitions $end
$dumpvars
0A
$end
#100
0A
#250
1A
#400
1A
"""
    (cs,) = V.parse_vcd(text)
    assert cs.initial == 0
    assert cs.transitions == [(250, 1)]


def test_invisible_opening_cs_fall_fails_presence_check():
    # The judge sees lanes only through parse_vcd: a CS drawn low before the
    # first transfer, whose fall is re-stated rather than drawn, must FAIL.
    s = _session()
    cs = next(ln for ln in s[1] if ln.name == "spi_cs")
    fall_t, fall_v = cs.transitions[0]
    assert fall_v == 0
    lines = ["$timescale 1ns $end", "$var wire 1 A spi_cs $end", "$enddefinitions $end",
             "$dumpvars", "0A", "$end"]
    for t, v in cs.transitions:
        lines += [f"#{t}", f"{v}A"]
    (parsed,) = V.parse_vcd("\n".join(lines) + "\n")
    cs.initial, cs.transitions = parsed.initial, parsed.transitions
    verdict = _verdict(s)
    assert verdict["every edge present"] is False
    assert verdict["no glitch at enable"] is False
