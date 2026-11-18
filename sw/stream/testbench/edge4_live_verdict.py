#!/usr/bin/env python3
"""DS-P1.71 — judge a LIVE four-channel EDGE capture as RouteWave rendered it.

Inputs, all produced by one live session (``tools/capture/edge4_live_session.py``):

  * ``stimulus.json`` — the board-side log of ``edge4_live_stimulus`` (bytes,
    divider, per-transfer CLOCK_MONOTONIC stamps);
  * ``routewave.vcd`` — RouteWave's own ``export`` of its session after the
    live stream, i.e. the edges exactly as RouteWave placed them;
  * ``wire.json`` — every frame RouteWave received, recorded by the TCP tee
    between the daemon and RouteWave (HELO + captured frames).

The expected waveform is not fitted to the capture. ``expected_transfer``
rebuilds each SPI transfer from ``ds_stimulus_gen.vhd`` ``p_spi`` and
``spi_slave.vhd`` alone; the only quantity taken from the capture is the time
of each transfer's CS falling edge, and that edge is itself checked against
the board's own clock. A check that fails is reported, never relaxed.

Usage (host):

    python3 -m sw.stream.testbench.edge4_live_verdict <session dir>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

WRAP_NS = 65_536_000           # 16-bit µs edge timestamp window
NS_PER_CYCLE = 10              # 100 MHz clk_sys (DS-P2.155)
# Every observed line passes the same 2-FF sync + glitch filter, so generator
# edges keep their exact cycle spacing; one tick of slack covers the 10 ns
# timestamp quantum.
EDGE_TOL_NS = NS_PER_CYCLE
# MISO is re-timed by spi_slave's own 3-FF SCLK/CS synchroniser before it
# reaches the edge logger: it lags its cause by a few clk_sys cycles.
MISO_LAG_NS = (0, 6 * NS_PER_CYCLE)
# Transfer starts are paced by Python on the HPS; the CS fall of transfer k must
# land within this of the board's CLOCK_MONOTONIC stamp for k (relative to k=0).
# A 65.536 ms misplacement is >10x this.
PACING_TOL_NS = 5_000_000

LINES = ("sclk", "mosi", "miso", "cs")
IDLE_LEVEL = {"sclk": 0, "miso": 0, "cs": 1}


@dataclass(frozen=True)
class Edge:
    offset_ns: int       # relative to the transfer's CS falling edge
    level: int
    lagged: bool = False  # MISO: lags its cause by MISO_LAG_NS


def _bits_msb_first(byte: int) -> List[int]:
    return [(byte >> (7 - i)) & 1 for i in range(8)]


def expected_transfer(mosi: int, miso: int, clk_div: int,
                      mosi_before: int) -> Dict[str, List[Edge]]:
    """Exact edges of one mode-0 transfer, relative to CS fall (t=0).

    ds_stimulus_gen p_spi, D = clk_div cycles: CS falls at 0; MOSI <- b7 at D;
    SCLK rises at (2+2k)D and falls at (3+2k)D, k=0..7; MOSI <- b(6-k) at
    (3+2k)D, k=0..6; CS rises at 18D (CS_HOLD counts D after the last fall).
    spi_slave (TX FIFO = [miso, 0x00]): MISO <- m7 at CS fall, <- m(6-k) at the
    falls k=0..6, <- 0 (the reloaded 0x00) at the 8th fall; released to 0 at
    CS rise. Only level CHANGES are edges."""
    d = clk_div * NS_PER_CYCLE
    out: Dict[str, List[Edge]] = {
        "cs": [Edge(0, 0), Edge(18 * d, 1)],
        "sclk": [],
        "mosi": [],
        "miso": [],
    }
    for k in range(8):
        out["sclk"] += [Edge((2 + 2 * k) * d, 1), Edge((3 + 2 * k) * d, 0)]
    mb = _bits_msb_first(mosi)
    level = mosi_before
    for t, b in [(d, mb[0])] + [((3 + 2 * k) * d, mb[k + 1]) for k in range(7)]:
        if b != level:
            out["mosi"].append(Edge(t, b))
            level = b
    sb = _bits_msb_first(miso)
    level = 0
    for t, b in ([(0, sb[0])] + [((3 + 2 * k) * d, sb[k + 1]) for k in range(7)]
                 + [(17 * d, 0)]):
        if b != level:
            out["miso"].append(Edge(t, b, lagged=True))
            level = b
    return out


def mosi_after(mosi: int, mosi_before: int) -> int:
    """MOSI holds the last shifted bit after a transfer (spi_mosi_r)."""
    return mosi & 1


# ── RouteWave VCD export ────────────────────────────────────────────────────

@dataclass
class Lane:
    code: str
    name: str
    initial: Optional[int] = None
    transitions: List[Tuple[int, int]] = field(default_factory=list)


def parse_vcd(text: str) -> List[Lane]:
    """Parse RouteWave's ``export`` VCD (1-bit wires, ``$timescale 1ns``).

    Duplicate names are kept apart by identifier code: an edge lane that adopts
    a HELO name shares it with the HELO's own channel."""
    m = re.search(r"\$timescale\s+(\d+)\s*(\w+)\s+\$end", text)
    scale = {"ns": 1, "ps": 0.001, "us": 1000, "ms": 1_000_000, "s": 1_000_000_000}
    mult = int(m.group(1)) * scale[m.group(2)] if m else 1
    lanes: Dict[str, Lane] = {}
    order: List[str] = []
    for vm in re.finditer(r"\$var\s+\w+\s+\d+\s+(\S+)\s+(\S+)\s+\$end", text):
        lanes[vm.group(1)] = Lane(code=vm.group(1), name=vm.group(2))
        order.append(vm.group(1))
    body = text.split("$enddefinitions", 1)[1] if "$enddefinitions" in text else ""
    t: Optional[int] = None
    in_dumpvars = False
    for tok in body.split():
        if tok == "$dumpvars":
            in_dumpvars = True
            continue
        if tok == "$end":
            in_dumpvars = False
            continue
        if tok.startswith("#"):
            t = int(round(int(tok[1:]) * mult))
            continue
        if tok[0] in "01xXzZ" and tok[1:] in lanes:
            v = int(tok[0]) if tok[0] in "01" else None
            lane = lanes[tok[1:]]
            if in_dumpvars or t is None:
                lane.initial = v
            elif v is not None:
                # A value change only: a VCD may re-state the current value
                # (RouteWave writes `0A` then `0A` for idle-high CS drawn low),
                # and counting that as an edge false-passes an edge the viewer
                # never drew (DS-P1.71 landing review).
                prev = lane.transitions[-1][1] if lane.transitions else lane.initial
                if v != prev:
                    lane.transitions.append((t, v))
    return [lanes[c] for c in order]


# ── Wire (tee) capture ──────────────────────────────────────────────────────

TAG_HELO, TAG_EDGE_EVENTS, TAG_SYNC = 0, 7, 9


def helo_names(helo: dict) -> Dict[int, str]:
    return {int(c["id"]): str(c["name"]) for c in helo.get("channels", [])}


def wire_edge_words(captured: Sequence) -> List[Tuple[int, int]]:
    """``(anchor_ns, word)`` for every EDGE_EVENTS word, in wire order."""
    out: List[Tuple[int, int]] = []
    for tag, payload in captured:
        if int(tag) == TAG_EDGE_EVENTS:
            for w in payload.get("words", []):
                out.append((int(payload.get("anchor_ns", 0)), int(w)))
    return out


def word_fields(word: int) -> Tuple[int, int, int, int]:
    """(instance, S, F, E) of an EDGE_EVENT word."""
    return (word >> 27) & 0x1F, (word >> 26) & 1, (word >> 25) & 1, (word >> 24) & 1


# ── Judge ───────────────────────────────────────────────────────────────────

@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _level_at(transitions: List[Tuple[int, int]], initial: Optional[int], t: int) -> Optional[int]:
    lvl = initial
    for tt, v in transitions:
        if tt > t:
            break
        lvl = v
    return lvl


def lane_for(device_id: int, lanes: List[Lane], names: Dict[int, str]) -> Tuple[Optional[Lane], str]:
    """The VCD lane RouteWave drew for edge ``device_id`` and the name it used.

    RouteWave labels an edge lane with the HELO channel whose id equals the
    device_id, unless that is a level-sampler ``level_pin[...]`` name, else
    ``edge_<id>`` (logic-trace edge_naming.hpp). Prefer the candidate that
    actually carries transitions."""
    candidates = [f"edge_{device_id}"]
    helo = names.get(device_id)
    if helo is not None and not helo.startswith("level_pin["):
        candidates.insert(0, helo)
    for cand in candidates:
        with_edges = [ln for ln in lanes if ln.name == cand and ln.transitions]
        if with_edges:
            return with_edges[0], cand
    for cand in candidates:
        named = [ln for ln in lanes if ln.name == cand]
        if named:
            return named[0], cand
    return None, candidates[-1]


def _fmt_tr(transitions: Iterable[Tuple[int, int]]) -> List[Tuple[str, int]]:
    """Transitions for a verdict detail, time in seconds: raw 12-digit ns
    stamps read as bench cable serials to the DS-C1 hygiene scan."""
    return [(f"{t / 1e9:.9f} s", v) for t, v in transitions]


def judge(stim: dict, lanes: List[Lane], helo: dict, captured: Sequence) -> List[Check]:
    checks: List[Check] = []
    clk_div = int(stim["clk_div"])
    line_of = {int(k): v for k, v in stim["edge_lines"].items()}
    names = helo_names(helo)

    lane_by_line: Dict[str, Lane] = {}
    drawn_names: Dict[int, str] = {}
    for dev, line in sorted(line_of.items()):
        lane, used = lane_for(dev, lanes, names)
        drawn_names[dev] = used if lane is not None else "<absent>"
        if lane is not None:
            lane_by_line[line] = lane

    # 1. naming — each lane carries a HELO-declared name, not the edge_<id> fallback.
    bad = {d: n for d, n in drawn_names.items() if n == f"edge_{d}" or n == "<absent>"}
    helo_for = {d: names.get(d, "<none>") for d in line_of}
    checks.append(Check(
        "channels named from HELO (not edge_N)", not bad,
        f"drawn={drawn_names}; HELO descriptor for each device_id={helo_for}"))

    missing_lanes = [ln for ln in LINES if ln not in lane_by_line]
    if missing_lanes:
        checks.append(Check("all four lanes present", False, f"no RouteWave lane for {missing_lanes}"))
        return checks
    checks.append(Check("all four lanes present", True,
                        ", ".join(f"{ln}={lane_by_line[ln].name}" for ln in LINES)))

    tr = {ln: sorted(lane_by_line[ln].transitions) for ln in LINES}

    # Observed transfers = CS low pulses.
    cs = tr["cs"]
    pulses: List[Tuple[int, int]] = []
    i = 0
    while i < len(cs):
        if cs[i][1] == 0:
            rise = next((t for t, v in cs[i + 1:] if v == 1), None)
            pulses.append((cs[i][0], rise if rise is not None else cs[i][0]))
            i += 2
        else:
            i += 1

    transfers = stim["transfers"]
    first_fall = pulses[0][0] if pulses else None

    # 2. no glitch at enable — nothing drawn between session start and the first
    #    CS fall, and each lane idles at the line's true level there.
    pre = {ln: [x for x in tr[ln] if first_fall is None or x[0] < first_fall - EDGE_TOL_NS]
           for ln in LINES}
    readbacks = [word_fields(w) for _a, w in wire_edge_words(captured) if word_fields(w)[1] == 1]
    # The true MOSI level before the first transfer is the MOSI logger's own
    # S=1 readback (E = the current level when S=1), not what was drawn.
    mosi_dev = next(d for d, ln in line_of.items() if ln == "mosi")
    mosi0 = next((e for inst, _s, _f, e in readbacks if inst == mosi_dev), None)
    if mosi0 is None:
        mosi0 = _level_at(tr["mosi"], lane_by_line["mosi"].initial, (first_fall or 0) - 1)
    idle_ok = {ln: _level_at(tr[ln], lane_by_line[ln].initial, (first_fall or 0) - 1)
               for ln in LINES}
    idle_bad = {ln: v for ln, v in idle_ok.items()
                if ln in IDLE_LEVEL and v != IDLE_LEVEL[ln]}
    checks.append(Check(
        "no glitch at enable", not any(pre.values()) and not idle_bad,
        f"pre-stimulus transitions={ {ln: _fmt_tr(pre[ln][:4]) for ln in LINES if pre[ln]} or 'none'}; "
        f"pre-stimulus levels={idle_ok} (want sclk=0 miso=0 cs=1); "
        f"S=1 readback words on the wire={len(readbacks)}"))

    # 3. transfer identity — every stimulus transfer appears once, in order,
    #    decoded from its own MOSI/MISO edges.
    decoded: List[Tuple[int, int]] = []
    for fall, rise in pulses:
        rises = [t for t, v in tr["sclk"] if v == 1 and fall < t < rise]
        mo = mi = 0
        for t in rises[:8]:
            mo = (mo << 1) | (_level_at(tr["mosi"], lane_by_line["mosi"].initial, t) or 0)
            mi = (mi << 1) | (_level_at(tr["miso"], lane_by_line["miso"].initial, t) or 0)
        decoded.append((mo, mi) if len(rises) == 8 else (-1, -1))
    want = [(int(x["mosi"]), int(x["miso"])) for x in transfers]
    first_diff = next((k for k, (a, b) in enumerate(zip(decoded, want)) if a != b), None)
    ident_ok = len(decoded) == len(want) and first_diff is None
    checks.append(Check(
        "every transfer present once, in order", ident_ok,
        f"stimulus={len(want)} observed CS pulses={len(pulses)}"
        + (f"; first mismatch at #{first_diff}: drawn={decoded[first_diff]} want={want[first_diff]}"
           if first_diff is not None else "")))

    # 4. every edge present / none duplicated / order+spacing: per transfer,
    #    match the exact model edge-for-edge on every lane.
    used = {ln: [False] * len(tr[ln]) for ln in LINES}
    missing: List[Tuple[int, str, Edge, int, int]] = []   # (k, line, edge, lo, hi)
    n_expected = 0
    mosi_level = mosi0 if mosi0 is not None else 0
    for k, ((fall, _rise), x) in enumerate(zip(pulses, transfers)):
        exp = expected_transfer(int(x["mosi"]), int(x["miso"]), clk_div, mosi_level)
        mosi_level = mosi_after(int(x["mosi"]), mosi_level)
        for ln in LINES:
            n_expected += len(exp[ln])
            for e in exp[ln]:
                t_exp = fall + e.offset_ns
                lo, hi = ((t_exp + MISO_LAG_NS[0] - EDGE_TOL_NS, t_exp + MISO_LAG_NS[1] + EDGE_TOL_NS)
                          if e.lagged else (t_exp - EDGE_TOL_NS, t_exp + EDGE_TOL_NS))
                hit = next((j for j, (t, v) in enumerate(tr[ln])
                            if not used[ln][j] and lo <= t <= hi and v == e.level), None)
                if hit is None:
                    missing.append((k, ln, e, lo, hi))
                else:
                    used[ln][hit] = True
    extra = {ln: [tr[ln][j] for j, u in enumerate(used[ln]) if not u] for ln in LINES}
    # A missing edge whose twin (same lane, same level) was drawn exactly a
    # whole number of 65.536 ms windows away is the wrap-misplacement signature.
    off_by: List[str] = []
    for k, ln, e, lo, hi in missing:
        for t, v in extra[ln]:
            if v != e.level:
                continue
            n = round((t - (lo + hi) / 2) / WRAP_NS)
            if n != 0 and lo + n * WRAP_NS <= t <= hi + n * WRAP_NS:
                off_by.append(f"#{k} {ln} @+{e.offset_ns}ns drawn {n:+d} x 65.536 ms")
                break
    miss_txt = [f"#{k} {ln} level={e.level} @+{e.offset_ns}ns" for k, ln, e, _lo, _hi in missing]
    checks.append(Check("every edge present", not missing,
                        f"expected={n_expected} missing={len(missing)}"
                        + (f"; first: {miss_txt[:3]}" if missing else "")))
    checks.append(Check("no edge duplicated / extra", not any(extra.values()),
                        f"unmatched drawn transitions per lane="
                        f"{ {ln: len(v) for ln, v in extra.items()} }"
                        + (f"; first: { {ln: _fmt_tr(v[:3]) for ln, v in extra.items() if v} }"
                           if any(extra.values()) else "")))

    # 5. none shifted by 65.536 ms — inside a transfer (edge vs its own CS fall)
    #    and across transfers (CS fall vs the board's clock).
    mono0 = int(transfers[0]["mono_ns"]) if transfers else 0
    pace_err = []
    for k, ((fall, _r), x) in enumerate(zip(pulses, transfers)):
        pace_err.append((fall - (first_fall or 0)) - (int(x["mono_ns"]) - mono0))
    worst = max((abs(e) for e in pace_err), default=0)
    wrap_like = [k for k, e in enumerate(pace_err) if abs(e) > PACING_TOL_NS]
    checks.append(Check(
        "none shifted by 65.536 ms", not off_by and not wrap_like,
        f"in-transfer wrap-sized misplacements={len(off_by)}"
        + (f" {off_by[:3]}" if off_by else "")
        + f"; transfer-start vs board clock worst |err|={worst / 1e6:.3f} ms"
          f" (tolerance {PACING_TOL_NS / 1e6:.0f} ms)"
        + (f"; out of tolerance at transfers {wrap_like[:5]}" if wrap_like else "")))

    # 6. per-channel order and spacing — strictly increasing time on each lane;
    #    spacing is exact by construction of check 4 (every edge matched to its
    #    cycle-exact model position within one 10 ns tick).
    nonmono = {ln: sum(1 for a, b in zip(tr[ln], tr[ln][1:]) if b[0] <= a[0]) for ln in LINES}
    alternating = {ln: sum(1 for a, b in zip(tr[ln], tr[ln][1:]) if a[1] == b[1]) for ln in LINES}
    checks.append(Check(
        "per-channel order and spacing", not missing and not any(nonmono.values())
        and not any(alternating.values()),
        f"non-increasing steps={nonmono}; same-level repeats={alternating}; "
        f"spacing judged at +/-{EDGE_TOL_NS} ns against the cycle-exact model "
        f"(MISO lag window {MISO_LAG_NS} ns)"))
    return checks


def verdict_table(checks: Iterable[Check]) -> str:
    rows = ["| check | verdict | detail |", "|---|---|---|"]
    for c in checks:
        rows.append(f"| {c.name} | {'PASS' if c.ok else 'FAIL'} | {c.detail.replace('|', '/')} |")
    return "\n".join(rows)


def load_session(d: Path) -> Tuple[dict, List[Lane], dict, list]:
    stim = json.loads((d / "stimulus.json").read_text())
    lanes = parse_vcd((d / "routewave.vcd").read_text())
    wire = json.loads((d / "wire.json").read_text())
    return stim, lanes, wire["helo"], wire["captured"]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session", type=Path)
    ap.add_argument("--json", type=Path, help="write the checks as JSON here")
    args = ap.parse_args(argv)
    checks = judge(*load_session(args.session))
    print(verdict_table(checks))
    if args.json:
        args.json.write_text(json.dumps([c.__dict__ for c in checks], indent=2) + "\n")
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
