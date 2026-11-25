#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Headless wave_stream client — record the daemon's decoded stream to JSON-lines.

Connects to the deepskopion daemon (:7475), reads the self-describing HELO
channel map, then logs every ANNOTATIONS (decoded transaction) frame to a
JSON-lines transaction log for the DS-P1.13 demo package. Does NOT touch the
ring — the daemon owns the decode; this is the pure consumer half.
"""
import socket
import msgpack
import json
import time
import argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--host", required=True, help="deployment-owned board hostname/address")
parser.add_argument("--port", type=int, default=7475)
parser.add_argument("--duration", type=float, default=25.0)
parser.add_argument("--out", default="capture.jsonl")
args = parser.parse_args()
HOST, PORT = args.host, args.port
DURATION = args.duration
OUT = args.out

TAG = {0:"HELO",1:"HELO_OK",2:"SAMPLES",3:"RATE",4:"EOF",5:"ERR",6:"ANNOTATIONS",
       7:"EDGE_EVENTS",8:"CONTROL",9:"SYNC",10:"CONTROL_ACK",11:"STATS",12:"DIGEST"}

s = socket.create_connection((HOST, PORT), timeout=5)
s.settimeout(2.0)
up = msgpack.Unpacker(raw=False)
counts, chan_map, helo = {}, {}, None
n_annot = 0
last_ts = 0          # most recent annotation ts, to stamp ts-less EDGE_EVENTS onto the stream
# DS-P2.80: the daemon replays a bounded BACKFILL ring to every new subscriber
# before the live stream, then sends TAG_SYNC as the catch-up→live boundary
# (DS-P3.63/64/67). Without recording that boundary a capture silently mixes
# another era's traffic with this session's, and every count/window computed
# over the file is wrong — measured: three captures under three DIFFERENT
# hardware states (5N1, 8N1, UART deselected from the EMIO mux) were identical,
# because they were all replayed history. Stamp each record so the consumer can
# tell them apart instead of guessing from timestamp gaps.
phase = "backfill"
n_backfill = 0
t0 = time.time()
with open(OUT, "w") as f:
    while time.time() - t0 < DURATION:
        try:
            data = s.recv(65536)
        except socket.timeout:
            continue
        if not data:
            break
        up.feed(data)
        for obj in up:
            tag, payload = obj[0], obj[1]
            counts[TAG.get(tag, tag)] = counts.get(TAG.get(tag, tag), 0) + 1
            if tag == 0 and helo is None:
                helo = payload
                for c in payload.get("channels", []):
                    chan_map[c["id"]] = c.get("name", str(c["id"]))
                # Handshake: the daemon aborts if we don't reply HELO_OK within 2s.
                s.sendall(msgpack.packb([1, {"consumer": "ds-p1.13-capture"}],
                                        use_bin_type=True))
            elif tag == 9:  # SYNC — the daemon's catch-up→live boundary (DS-P3.67)
                phase = "live"
            elif tag == 6:  # ANNOTATIONS — the decoded transaction log
                anns = payload.get("annotations") or payload.get("anns") or []
                # A keyframe carries each lane's LAST-KNOWN state (DS-P3.66), not
                # traffic from this session — never count it as live.
                kf = bool(payload.get("keyframe"))
                for a in anns:
                    rec = dict(a)
                    rec["_channel_name"] = chan_map.get(a.get("channel_id"), "?")
                    rec["_phase"] = "keyframe" if kf else phase
                    last_ts = max(last_ts, a.get("ts_start", 0))
                    f.write(json.dumps(rec) + "\n")
                    n_annot += 1
                    if rec["_phase"] != "live":
                        n_backfill += 1
            elif tag == 7:  # EDGE_EVENTS — raw logic-analyzer edges (no ts; word-encoded)
                words = payload.get("words") or []
                seq = payload.get("seq")
                for w in words:
                    ch = (w >> 27) & 0x1F  # EV_WORD[31:27] = edge channel index
                    rec = {"ts_start": last_ts, "ts_end": last_ts, "channel_id": ch,
                           "kind": "edge", "text": f"edge word=0x{w:08x}", "is_error": False,
                           "_channel_name": "edge", "_edge_ch": ch, "_seq": seq,
                           "_phase": phase}
                    f.write(json.dumps(rec) + "\n")
                    n_annot += 1
                    if phase != "live":
                        n_backfill += 1
s.close()
print("frames by tag:", counts)
print("channels:", len(chan_map))
print(f"annotations logged: {n_annot} -> {OUT}")
print(f"  backfill/keyframe (replayed history): {n_backfill}")
print(f"  live (this session):                  {n_annot - n_backfill}")
if phase != "live":
    print("  ⚠️  no SYNC seen — the daemon never signalled the live boundary, so"
          " EVERY record above is replayed history, not this session.")
