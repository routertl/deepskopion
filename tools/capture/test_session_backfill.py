"""DS-P2.80 — the Session Summary must report THIS session, not replayed history.

The daemon replays a bounded backfill ring to every new subscriber before the
live stream (DS-P3.63/64). Three captures taken under three DIFFERENT hardware
states — 5N1, 8N1, and with the UART deselected from the EMIO mux entirely —
came back with identical contents, because all three were replayed history. Any
count, window or error rate computed over the whole capture file therefore
reports another era's traffic as this session's result.

These tests pin the two properties that matter:
  * live/backfill separation uses the daemon's EXACT SYNC boundary when present;
  * the old largest-gap heuristic cannot silently eat real traffic without the
    summary saying so — this demo's driver makes one sustained pass per protocol
    with multi-second gaps, which is exactly what that heuristic mistakes for a
    backfill boundary.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_summary import load  # noqa: E402


def _write(tmp_path, rows):
    p = tmp_path / "cap.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(p)


def _rec(ts, phase=None, ch="uart_byte"):
    r = {"ts_start": ts, "ts_end": ts + 10, "_channel_name": ch, "kind": ch}
    if phase is not None:
        r["_phase"] = phase
    return r


def test_backfill_is_excluded_using_the_exact_sync_boundary(tmp_path):
    rows = [_rec(1000 + i, "backfill") for i in range(50)]
    rows += [_rec(2000 + i, "live") for i in range(7)]
    live, prov = load(_write(tmp_path, rows))

    assert len(live) == 7, "only the live records belong to this session"
    assert prov["method"] == "exact"
    assert "50 of 57" in prov["detail"]


def test_keyframe_records_are_not_counted_as_live(tmp_path):
    """A keyframe carries each lane's last-known STATE (DS-P3.66), not traffic."""
    rows = [_rec(500, "keyframe"), _rec(2000, "live"), _rec(2001, "live")]
    live, prov = load(_write(tmp_path, rows))
    assert len(live) == 2
    assert prov["method"] == "exact"


def test_a_capture_that_is_pure_backfill_yields_nothing(tmp_path):
    """An idle board replays history and produces NO live traffic. The summary
    must return empty rather than presenting history as a session — this is the
    literal DS-P2.75 misdiagnosis, in test form."""
    rows = [_rec(1000 + i, "backfill") for i in range(200)]
    live, prov = load(_write(tmp_path, rows))
    assert live == []
    assert prov["method"] == "exact"


def test_legacy_log_without_phase_falls_back_but_flags_itself(tmp_path):
    rows = [_rec(1_000_000_000 + i * 1000) for i in range(5)]        # old burst
    rows += [_rec(9_000_000_000 + i * 1000) for i in range(5)]       # after a >1s gap
    live, prov = load(_write(tmp_path, rows))

    assert prov["method"] == "heuristic"
    # The warning must name the risk, not just note the fallback: a silent
    # fallback is how the wrong numbers reached the artefact in the first place.
    assert "discard real traffic" in prov["detail"]
    assert len(live) == 5


def test_heuristic_can_eat_real_traffic_and_that_is_why_it_is_flagged(tmp_path):
    """Guard on the specific hazard: the demo driver runs ONE SUSTAINED PASS PER
    PROTOCOL, so live traffic itself contains multi-second gaps. With no _phase
    markers the largest-gap split lands INSIDE the live session and throws away
    the earlier protocols. The fallback is allowed to be wrong; it is NOT allowed
    to be quiet."""
    rows = [_rec(1_000_000_000 + i * 1000, ch="uart_byte") for i in range(10)]
    rows += [_rec(30_000_000_000 + i * 1000, ch="i2c_frame") for i in range(10)]
    live, prov = load(_write(tmp_path, rows))

    kept = {r["_channel_name"] for r in live}
    assert kept == {"i2c_frame"}, "the heuristic dropped a whole real protocol"
    assert prov["method"] == "heuristic" and "discard real traffic" in prov["detail"]


@pytest.mark.parametrize("phase", ["backfill", "keyframe"])
def test_non_live_phases_never_leak_into_the_session(tmp_path, phase):
    rows = [_rec(100, phase), _rec(2000, "live")]
    live, _ = load(_write(tmp_path, rows))
    assert [r["ts_start"] for r in live] == [2000]
