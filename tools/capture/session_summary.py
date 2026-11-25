#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-P1.13 Session Summary generator — a JSON-lines transaction log → markdown.

The artefact a closed-architecture box can't produce: a cross-protocol summary
on ONE shared microsecond timebase (durations, transaction counts, anomalies)
that captures INTENT, not signals. Consumes the annotations recorded by the
headless wave_stream client (ws_capture.py); board-independent.

Honesty discipline (project_deepskopion_positioning): OBSERVED FACTS only —
counts, durations, error events verbatim. Capture-side artifacts (daemon stream
resyncs, EMIO-mux-switch discontinuities) are labelled as such, NOT as bus
anomalies. No inferred-likely-cause section.
"""
import json
import sys
from collections import Counter, defaultdict

# _channel_name → (friendly protocol, is a real bus protocol?)
PROTO = {
    "uart_byte":   ("UART", True),
    "i2c_frame":   ("I2C", True),
    "i2c_frame:4": ("I2C (cam B #4)", True),
    "i2c_frame:5": ("I2C (cam A #5)", True),
    "qspi_xact":   ("SPI (4-wire)", True),
    "spi_xact":    ("SPI", True),
    "can_frame":   ("CAN", True),
    "jtag_xact":   ("JTAG", True),
    "edge":        ("EDGE (logic-analyzer)", True),
    "zs_resync":   ("(capture stream resync)", False),
}


def load(path):
    """Return (live_rows, provenance) — ONLY this session's traffic.

    The daemon replays a bounded backfill ring to every new subscriber before
    the live stream (DS-P3.63/64), so a capture file is (history + live). Every
    count, window and error rate computed over the whole file therefore reports
    another era's traffic as this session's — the exact failure DS-P2.80 was
    filed for, after three captures taken under three DIFFERENT hardware states
    came back identical because they were all replayed history.

    ws_capture.py now stamps each record with _phase from the daemon's own
    TAG_SYNC boundary (DS-P3.67), which is exact. Older logs have no _phase, so
    they fall back to the previous largest-gap heuristic — kept only for those,
    and it is NOT trustworthy: this demo's driver makes one sustained pass per
    protocol with multi-second gaps between them, so the heuristic can split
    inside real traffic and silently discard whole protocols. Which path was
    used is returned so the summary can say so out loud.
    """
    rows = [json.loads(ln) for ln in open(path) if ln.strip()]
    total = len(rows)

    if any("_phase" in r for r in rows):
        live = [r for r in rows if r.get("_phase") == "live"]
        return live, {
            "method": "exact",
            "detail": f"daemon SYNC boundary; {total - len(live)} of {total} "
                      f"records were replayed backfill/keyframe and are excluded",
        }

    ts = sorted(r["ts_start"] for r in rows)
    if len(ts) > 2:
        gaps = [(ts[i + 1] - ts[i], i) for i in range(len(ts) - 1)]
        g, idx = max(gaps)
        split = ts[idx + 1] if g > 1e9 else ts[0]
        rows = [r for r in rows if r["ts_start"] >= split]
    return rows, {
        "method": "heuristic",
        "detail": f"NO _phase markers in this log (pre-DS-P2.80 capture) — fell back "
                  f"to a largest-gap split, which can discard real traffic. "
                  f"{total - len(rows)} of {total} records dropped. Re-capture with "
                  f"a current ws_capture.py before trusting these numbers.",
    }


def fmt_us(ns):
    return f"{ns/1000:.1f} µs" if ns < 1e6 else f"{ns/1e6:.2f} ms" if ns < 1e9 else f"{ns/1e9:.3f} s"


def main(path, out=None):
    rows, prov = load(path)
    if not rows:
        print(f"no LIVE annotations ({prov['detail']})", file=sys.stderr)
        return 1

    t0 = min(r["ts_start"] for r in rows)
    t1 = max(r["ts_end"] for r in rows)
    span = t1 - t0

    by_ch = defaultdict(list)
    for r in rows:
        by_ch[r.get("_channel_name", "?")].append(r)

    L = []
    L.append("# Deepskopion — Session Summary (demo config #1)\n")
    L.append(f"**Session span:** {fmt_us(span)} &nbsp;·&nbsp; "
             f"**{len(rows)} decoded events** &nbsp;·&nbsp; "
             "single shared µs timebase (all events on one 100 MHz sample clock)\n")
    # DS-P2.80: say where the boundary came from. The span above is measured from
    # the data (first→last event), never assumed from the requested capture
    # duration, so replayed history can never be printed as a session length.
    if prov["method"] == "exact":
        L.append(f"<sub>Live events only — {prov['detail']}.</sub>\n")
    else:
        L.append(f"> ⚠️ **Provenance uncertain.** {prov['detail']}\n")

    real = [c for c in by_ch if PROTO.get(c, (None, False))[1]]
    L.append("## Protocols observed\n")
    L.append("| Protocol | Transactions | Window | Errors | Event kinds |")
    L.append("|---|---|---|---|---|")
    for ch in sorted(real, key=lambda c: -len(by_ch[c])):
        evs = by_ch[ch]
        name = PROTO.get(ch, (ch, True))[0]
        cts = sorted(e["ts_start"] for e in evs)
        win = fmt_us(cts[-1] - cts[0]) if len(cts) > 1 else "—"
        errs = sum(1 for e in evs if e.get("is_error"))
        kinds = ", ".join(f"{k}×{v}" for k, v in Counter(e["kind"] for e in evs).most_common(4))
        L.append(f"| **{name}** | {len(evs)} | {win} | {errs} | {kinds} |")
    L.append("")

    # Shared-timebase proof: every protocol's first event stamped on the one clock.
    L.append("## Shared timebase (the cross-protocol proof)\n")
    L.append("Every protocol is stamped against the same monotonic sample clock, so "
             "transactions across protocols are directly time-orderable:\n")
    firsts = sorted(((by_ch[c][0]["ts_start"], PROTO.get(c, (c, True))[0], by_ch[c][0])
                     for c in real), key=lambda x: x[0])
    L.append("| t (µs from start) | Protocol | First event |")
    L.append("|---|---|---|")
    for ts, name, e in firsts:
        L.append(f"| {(ts - t0)/1000:.1f} | {name} | {e.get('text','')} |")
    L.append("")

    # Anomalies — observed facts, honestly classified.
    L.append("## Anomalies (observed facts)\n")
    anoms = [r for r in rows if r.get("is_error")]
    if not anoms:
        L.append("None detected in this capture.\n")
    else:
        by = Counter((r.get("_channel_name", "?"), r["kind"]) for r in anoms)
        L.append("| Channel | Kind | Count | Note |")
        L.append("|---|---|---|---|")
        NOTE = {
            "zs_resync":   "capture-side: daemon zs_msg stream resync at EMIO-mux "
                           "switches during self-injection — NOT a bus error",
            "i2c_frame":   "I2C read terminated by the master NACK on the last byte "
                           "(normal end-of-read); verify against decoder semantics",
            "i2c_frame:4": "I2C read terminated by the master NACK on the last byte "
                           "(normal end-of-read); verify against decoder semantics",
            "i2c_frame:5": "I2C read terminated by the master NACK on the last byte "
                           "(normal end-of-read); verify against decoder semantics",
        }
        for (ch, kind), n in by.most_common():
            L.append(f"| {ch} | {kind} | {n} | {NOTE.get(ch, 'observed')} |")
        L.append("")

    md = "\n".join(L)
    if out:
        open(out, "w").write(md)
        print(f"wrote {out} ({len(md)} bytes)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
