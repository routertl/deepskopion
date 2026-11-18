"""Unit tests for sw.stream.session_summary (JSON-lines log -> markdown)."""

from __future__ import annotations

import json

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import session_summary as S

# One synthetic capture: UART + I2C + EDGE on the shared ns clock, one I2C error.
# Big early gap so the load() backfill-split keeps the "live" tail.
_ROWS = (
    [{"ts_start": 1_000, "ts_end": 1_050, "channel_id": 100, "kind": "uart_byte",
      "text": "'A'", "is_error": False, "_channel_name": "uart_byte"}]
    + [{"ts_start": 2_000_000_000 + i * 1000, "ts_end": 2_000_000_000 + i * 1000 + 50,
        "channel_id": 100, "kind": "uart_byte", "text": "'X'", "is_error": False,
        "_channel_name": "uart_byte"} for i in range(10)]
    + [{"ts_start": 2_100_000_000 + i * 1000, "ts_end": 2_100_000_000 + i * 1000 + 50,
        "channel_id": 101, "kind": "i2c_byte", "text": "0x00",
        "is_error": (i == 4), "_channel_name": "i2c_frame"} for i in range(5)]
    + [{"ts_start": 2_200_000_000 + i * 100, "ts_end": 2_200_000_000 + i * 100,
        "channel_id": 1, "kind": "edge", "text": "edge word=0x1",
        "is_error": False, "_channel_name": "edge"} for i in range(8)]
)


def _write_log(tmp_path):
    p = tmp_path / "cap.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in _ROWS) + "\n", encoding="utf-8")
    return p


def test_load_drops_backfill_before_the_big_gap(tmp_path):
    # DS-P3.255: load() returns (live_rows, provenance) since DS-P2.80 (6c291d06); this call site still took the
    # tuple as the row list, so len() read 2. The fixture has no _phase, so the largest-gap heuristic decides.
    rows, prov = S.load(str(_write_log(tmp_path)))
    assert prov["method"] == "heuristic"
    # the lone ts=1_000 backfill event is dropped; the 23 live events survive
    assert len(rows) == 23
    assert all(r["ts_start"] >= 2_000_000_000 for r in rows)


def test_summary_reports_every_protocol_and_the_error(tmp_path, capsys):
    rc = S.main(str(_write_log(tmp_path)))
    assert rc == 0
    md = capsys.readouterr().out
    assert "## Protocols observed" in md
    assert "UART" in md and "I2C" in md and "EDGE" in md
    # the single I2C error is surfaced in the anomalies section, honestly noted
    assert "## Anomalies" in md
    assert "i2c" in md.lower() and "NACK" in md
    # shared-timebase proof table present
    assert "Shared timebase" in md


def test_no_annotations_returns_nonzero(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert S.main(str(empty)) == 1
