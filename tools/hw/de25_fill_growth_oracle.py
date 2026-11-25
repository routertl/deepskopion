#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Oracle of DS-DE25-DRAIN-001: the DE25 ring fill does not grow over a >= 60 s window.

Why an oracle: the routertl hw contract cannot read two registers in one
transaction and bound their wrap-aware difference (RTL-P3.2480). The fill is
such a difference, f = (RING_COMMIT_PTR - RING_READ_PTR) mod 2^28, so the
contract records the samples (tools/hw/de25_probe_instr.py) and this oracle
judges them. It retires when RTL-P3.2480 lands.

Input: the probe transcript (JSON lines), from ``--transcript`` or
``$DS_PROBE_TRANSCRIPT``. The samples judged are the ``peek_instr`` records
after the LAST poke of ``--stim-value`` to ``--stim-addr`` (the stimulus-on
step of this requirement). There must be exactly three:

    S0  window start
    S1  about 2 s after S0: the ring write rate w is measured over S0 -> S1
    S2  window end, >= 60 s after S0

Layers (the routertl layered-oracle schema; the contract asserts
``fill_growth``, which passes only when every layer before it passed):

    instrument   exactly three samples, each with commit, read and uptime
    stimulus     w = ((c1 - c0) mod 2^28) / (u1 - u0) inside [18.0e6, 22.0e6] B/s,
                 with u1 - u0 short enough that a 22.0e6 B/s commit cannot wrap
                 the ring (2^28 / 22.0e6 = 12.2 s). A dead stimulus gives
                 F = 0, so it must not read green.
    window       u2 - u0 >= 60.0 s (board uptime)
    fill_growth  F = f2 - f0 <= 655 360 B (one 4096-frame tick of 160 B), and
                 DDR_BACKPRESSURE_CNT unchanged from S0 to S2. The second
                 clause is part of the fill reading, not only of the loss
                 check: a completely full ring has RING_COMMIT_PTR ==
                 RING_READ_PTR (rr-ddr-stream ptr_mgr.vhd tells full from
                 empty by history only), so f reads 0 when the ring is full.
                 A full ring under the stimulus counts backpressure, so a
                 moved counter means f2 cannot be read as a fill.

A layer that is not judged because an earlier one did not pass is
``skipped`` (VOID in the PREREG sense: not green, not a measured red).

Output: one JSON object on stdout; exit 0 whenever the JSON was written.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RING_BYTES = 1 << 28
TICK_BYTES = 655_360                 # 4096 frames x 160 B
W_BAND = (18.0e6, 22.0e6)            # ring write rate, DS-P1.74 run 3 judged band
MIN_WINDOW_S = 60.0
MAX_W_SPAN_S = RING_BYTES / W_BAND[1]


def _layer(name: str, status: str, metrics: dict, suspects: list | None = None) -> dict:
    return {"name": name, "status": status, "metrics": metrics,
            "suspects": list(suspects or [])}


def load_records(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def window_samples(records: list[dict], stim_addr: str, stim_value: str) -> list[dict]:
    """The peek_instr records after the last stimulus-on poke."""
    a, v = int(stim_addr, 16), int(stim_value, 16)
    last = None
    for i, r in enumerate(records):
        if (r.get("op") == "poke" and int(r.get("addr", "0x0"), 16) == a
                and int(r.get("value", "0x0"), 16) == v):
            last = i
    if last is None:
        return []
    return [r for r in records[last + 1:] if r.get("op") == "peek_instr"]


def fill(rec: dict) -> int:
    return (int(rec["commit"], 16) - int(rec["observed"], 16)) % RING_BYTES


def judge(samples: list[dict], min_window_s: float = MIN_WINDOW_S) -> dict:
    """The layered oracle object for three window samples.

    ``min_window_s`` is the frozen 60 s; a desk replay of a shorter recorded
    window (DS-P1.74 run 3) passes 0 to judge its fill growth alone."""
    layers = []

    # instrument
    ok = len(samples) == 3
    parsed = []
    if ok:
        try:
            parsed = [(int(s["commit"], 16), int(s["observed"], 16),
                       float(s["board_uptime"])) for s in samples]
            bp0, bp2 = int(samples[0]["backpressure"], 16), int(samples[2]["backpressure"], 16)
        except (KeyError, TypeError, ValueError):
            ok = False
    layers.append(_layer("instrument", "pass" if ok else "skipped",
                         {"samples": len(samples)},
                         [] if ok else ["expected exactly 3 peek_instr samples with "
                                        "commit, observed, backpressure and board_uptime"]))
    if not ok:
        for n in ("stimulus", "window", "fill_growth"):
            layers.append(_layer(n, "skipped", {}, ["instrument not present"]))
        return {"layers": layers}

    (c0, r0, u0), (c1, r1, u1), (c2, r2, u2) = parsed

    # stimulus
    dt01 = u1 - u0
    if dt01 <= 0 or dt01 >= MAX_W_SPAN_S:
        w, ok = None, False
        why = f"S0->S1 is {dt01:.2f} s; must be in (0, {MAX_W_SPAN_S:.1f}) s"
    else:
        w = ((c1 - c0) % RING_BYTES) / dt01
        ok = W_BAND[0] <= w <= W_BAND[1]
        why = f"w = {w:.0f} B/s outside [{W_BAND[0]:.0f}, {W_BAND[1]:.0f}]"
    layers.append(_layer("stimulus", "pass" if ok else "skipped",
                         {"w_Bps": w, "dt_s": round(dt01, 2), "band_Bps": list(W_BAND)},
                         [] if ok else [why]))
    if not ok:
        for n in ("window", "fill_growth"):
            layers.append(_layer(n, "skipped", {}, ["stimulus not present"]))
        return {"layers": layers}

    # window
    dt02 = u2 - u0
    ok = dt02 >= min_window_s
    layers.append(_layer("window", "pass" if ok else "skipped",
                         {"dt_s": round(dt02, 2), "min_s": min_window_s},
                         [] if ok else [f"window {dt02:.2f} s < {min_window_s} s"]))
    if not ok:
        layers.append(_layer("fill_growth", "skipped", {}, ["window too short"]))
        return {"layers": layers}

    # fill_growth
    f0, f2 = (c0 - r0) % RING_BYTES, (c2 - r2) % RING_BYTES
    growth = f2 - f0
    bp_delta = (bp2 - bp0) % (1 << 32)
    suspects = []
    if bp_delta:
        suspects.append(f"DDR_BACKPRESSURE_CNT moved by {bp_delta}: the ring was full, "
                        "so f2 is not a fill reading")
    if growth > TICK_BYTES:
        suspects.append(f"fill grew {growth} B > {TICK_BYTES} B over {dt02:.2f} s")
    layers.append(_layer("fill_growth", "fail" if suspects else "pass",
                         {"f0_B": f0, "f2_B": f2, "F_B": growth,
                          "F_per_s": round(growth / dt02) if dt02 else None,
                          "limit_B": TICK_BYTES, "backpressure_delta": bp_delta,
                          "dt_s": round(dt02, 2)},
                         suspects))
    return {"layers": layers}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--transcript", default=os.environ.get("DS_PROBE_TRANSCRIPT"))
    ap.add_argument("--stim-addr", required=True)
    ap.add_argument("--stim-value", required=True)
    a = ap.parse_args(argv)
    if not a.transcript or not Path(a.transcript).is_file():
        out = {"layers": [_layer("instrument", "skipped", {"samples": 0},
                                 [f"no probe transcript ({a.transcript!r}); "
                                  "set --transcript or DS_PROBE_TRANSCRIPT"])]
               + [_layer(n, "skipped", {}, ["instrument not present"])
                  for n in ("stimulus", "window", "fill_growth")]}
    else:
        out = judge(window_samples(load_records(Path(a.transcript)),
                                   a.stim_addr, a.stim_value))
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
