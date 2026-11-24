#!/usr/bin/env python3
"""DS-T2.84 — judge a LIVE EDGE wire capture: is every S=1 word a true readback?

On DE25 silicon (DS-P1.71, deepskopion 28da400) three kinds of word reached
EDGE_EVENTS with S=1: the real level readback, the DS-T2.43 ENABLED/DISABLED
lifecycle frame (type "100" = S=1/F=0/E=0), and an edge detected on the read
cycle (deferred, written with S=1 and the pre-edge level). A consumer that
seeds a lane level from S=1 words drew idle-high CS low at every enable and
disable. The ticket is done when a live capture shows only true readbacks as
S=1 level words.

This judge reads what the daemon sent a consumer (``wire.json``: HELO + every
frame, recorded by ``tools/capture/edge_wire_identity_session.py``) and the
board's own stimulus log (``stimulus.json``), and checks:

  1. exactly one S=1 word per armed logger. ``edge4_live_stimulus.arm()``
     writes CONFIG "10" (enable + one read) once per logger, so one readback
     each is the whole budget. A lifecycle frame or a deferred coincident edge
     on EDGE_EVENTS is an extra S=1 word;
  2. each S=1 word carries the line's real idle level when the logger was
     armed (``ds_stimulus_gen.vhd`` p_spi: SCLK 0, CS 1, MOSI = bit 0 of the
     last byte shifted since power-up (power-up value 0); MISO 0, because the
     responder reloads zeros from its empty FIFO at the 8th SCLK fall, spi_slave.vhd DS-T2.8);
  3. each logger's ENABLED marker arrived as an ``edge_lifecycle`` annotation
     (msg_type 0x0608), and, when the loggers were running before ``arm()``,
     each DISABLED marker too. So the markers are still on the wire, but as
     something a consumer can tell apart from a level;
  4. ``ERR_CNT_EDGE`` did not move.

Only post-SYNC frames are judged: the daemon replays a backfill of an earlier
era on connect and marks the live boundary with one TAG_SYNC.

    python3 -m sw.stream.testbench.edge_wire_identity_verdict <phase dir>
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

TAG_EDGE_EVENTS, TAG_ANNOTATIONS, TAG_SYNC = 7, 6, 9

# Idle level of each SPI line while ds_stimulus_gen p_spi sits in S_SPI_IDLE
# (MOSI is not fixed: see mosi_idle()).
FIXED_IDLE = {"sclk": 0, "miso": 0, "cs": 1}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def mosi_idle(prior_mosi_byte: Optional[int]) -> int:
    """p_spi leaves MOSI at bit 0 of the last byte it shifted (the 8th SCLK
    fall moves to S_SPI_CS_HOLD without driving MOSI again). With no transfer
    since power-up MOSI holds its power-up value '0'."""
    return 0 if prior_mosi_byte is None else prior_mosi_byte & 1


def live_frames(captured: Sequence) -> List[Tuple[int, dict]]:
    """Frames after the first TAG_SYNC (all frames if there is none)."""
    frames = [(int(t), p) for t, p in captured]
    for i, (tag, _p) in enumerate(frames):
        if tag == TAG_SYNC:
            return frames[i + 1:]
    return frames


def word_fields(word: int) -> Tuple[int, int, int, int, int]:
    """(instance, S, F, E, ts24) of an EDGE_EVENTS word."""
    return ((word >> 27) & 0x1F, (word >> 26) & 1, (word >> 25) & 1,
            (word >> 24) & 1, word & 0xFFFFFF)


def edge_words(frames: Sequence[Tuple[int, dict]]) -> List[int]:
    return [int(w) for tag, p in frames if tag == TAG_EDGE_EVENTS
            for w in p.get("words", [])]


def lifecycle_markers(frames: Sequence[Tuple[int, dict]]) -> List[dict]:
    """``payload`` of every ``edge_lifecycle`` annotation, in wire order."""
    return [dict(a.get("payload", {})) for tag, p in frames if tag == TAG_ANNOTATIONS
            for a in p.get("annotations", []) if a.get("kind") == "edge_lifecycle"]


def judge(stim: dict, captured: Sequence, phase: dict) -> List[Check]:
    line_of = {int(k): str(v) for k, v in stim["edge_lines"].items()}
    expected = dict(FIXED_IDLE, mosi=mosi_idle(phase.get("prior_mosi_byte")))
    frames = live_frames(captured)
    words = [word_fields(w) for w in edge_words(frames)]
    s1 = [(inst, e, ts) for inst, s, _f, e, ts in words if s == 1]
    markers = lifecycle_markers(frames)
    checks: List[Check] = []

    per_inst: Dict[int, List[Tuple[int, int]]] = {}
    for inst, e, ts in s1:
        per_inst.setdefault(inst, []).append((e, ts))
    wrong_count = {inst: len(per_inst.get(inst, [])) for inst in line_of
                   if len(per_inst.get(inst, [])) != 1}
    stray = sorted(set(per_inst) - set(line_of))
    checks.append(Check(
        "exactly one S=1 word per armed logger (its CONFIG 10 readback)",
        not wrong_count and not stray,
        f"S=1 words per instance={ {i: len(v) for i, v in sorted(per_inst.items())} }; "
        f"wrong count={wrong_count}; S=1 on a logger that was not armed={stray}; "
        f"words={[hex(w) for w in edge_words(frames) if (w >> 26) & 1]}"))

    wrong_level = {line_of[inst]: [e for e, _ts in per_inst.get(inst, [])]
                   for inst in line_of
                   if any(e != expected[line_of[inst]] for e, _ts in per_inst.get(inst, []))}
    checks.append(Check(
        "every S=1 word carries the line's real idle level",
        not wrong_level and bool(s1),
        f"expected={ {line_of[i]: expected[line_of[i]] for i in sorted(line_of)} }; "
        f"observed={ {line_of.get(i, f'inst{i}'): [e for e, _ in v] for i, v in sorted(per_inst.items())} }; "
        f"wrong={wrong_level}"))

    states: Dict[int, Dict[str, int]] = {}
    for m in markers:
        st = states.setdefault(int(m.get("channel", -1)), {})
        st[str(m.get("state"))] = st.get(str(m.get("state")), 0) + 1
    want = {"enabled": 1}
    if phase.get("expect_disabled"):
        want["disabled"] = 1
    bad_markers = {line_of[i]: states.get(i, {}) for i in line_of
                   if any(states.get(i, {}).get(k, 0) != n for k, n in want.items())}
    checks.append(Check(
        "every lifecycle marker arrived as an edge_lifecycle annotation",
        not bad_markers,
        f"wanted per logger={want}; observed={ {line_of.get(i, f'ch{i}'): v for i, v in sorted(states.items())} }; "
        f"wrong={bad_markers}"))

    err = (int(stim["regs_after"]["ERR_CNT_EDGE"]) - int(stim["regs_before"]["ERR_CNT_EDGE"]))
    checks.append(Check("ERR_CNT_EDGE did not move", err == 0, f"delta={err}"))
    return checks


def facts(stim: dict, captured: Sequence) -> dict:
    """Not judged: counts that let a reader re-check the verdict by hand."""
    frames = live_frames(captured)
    words = edge_words(frames)
    line_of = {int(k): str(v) for k, v in stim["edge_lines"].items()}
    per_line: Dict[str, dict] = {}
    for inst, line in sorted(line_of.items()):
        mine = [(i, w) for i, w in enumerate(words) if (w >> 27) == inst]
        s1_at = [i for i, w in mine if (w >> 26) & 1]
        after = [w for i, w in mine if not (w >> 26) & 1 and s1_at and i > s1_at[0]]
        per_line[line] = {"s1_words": [hex(w) for _i, w in mine if (w >> 26) & 1],
                          "first_s0_word_after_first_s1": hex(after[0]) if after else None}
    markers = lifecycle_markers(frames)
    pkt = (int(stim["regs_after"]["PKT_CNT_EDGE"]) - int(stim["regs_before"]["PKT_CNT_EDGE"]))
    return {
        "edge_events_words": len(words),
        "s1_words": sum(1 for w in words if (w >> 26) & 1),
        "s0_words": sum(1 for w in words if not (w >> 26) & 1),
        "edge_lifecycle_annotations": len(markers),
        "lifecycle_markers": markers,
        "pkt_cnt_edge_delta": pkt,
        "per_line": per_line,
        "git_hash_before_after": [hex(int(stim["regs_before"]["GIT_HASH"])),
                                  hex(int(stim["regs_after"]["GIT_HASH"]))],
    }


def judge_dir(d: Path) -> Tuple[bool, List[Check], dict]:
    stim = json.loads((d / "stimulus.json").read_text())
    captured = json.loads((d / "wire.json").read_text())["captured"]
    phase = json.loads((d / "phase.json").read_text())
    checks = judge(stim, captured, phase)
    return all(c.ok for c in checks), checks, facts(stim, captured)


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print(__doc__.splitlines()[0], file=sys.stderr)
        return 2
    d = Path(argv[0])
    ok, checks, info = judge_dir(d)
    verdict = {"verdict": "PASS" if ok else "FAIL",
               "checks": [asdict(c) for c in checks], "facts": info}
    (d / "verdict.json").write_text(json.dumps(verdict, indent=1) + "\n")
    lines = [f"# {d.name}: {verdict['verdict']}", "",
             "| check | result | detail |", "|---|---|---|"]
    lines += [f"| {c.name} | {'PASS' if c.ok else '**FAIL**'} | {c.detail} |" for c in checks]
    (d / "verdict.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
