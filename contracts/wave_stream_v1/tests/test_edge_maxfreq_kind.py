"""wave_stream_v1 2.6.0 (DS-P3.117) — the edge datalogger's max-frequency datum
is a registered annotation kind.

The daemon decodes the in-band EVT_EDGE_MAXFREQ frame to an ``edge_maxfreq``
annotation and sends it to the consumer. Until 2.6.0 the kind was not
registered, so its role keys were nobody's contract and its two time-like keys
had no unit row (the unit walk reaches registered kinds only).
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTRACT_DIR = HERE.parent
sys.path.insert(0, str(CONTRACT_DIR))

import wave_stream_contract as C  # noqa: E402  (path set above)

GOLDEN = C.load_fixture(CONTRACT_DIR / "fixtures" / "session_golden.json")


def test_edge_maxfreq_is_registered_with_typed_keys_and_units():
    """The consumer shows the fastest activity seen on a captured line from these
    keys. Unregistered, a renamed key reaches it as an empty value and nothing
    raises, and the two time-like keys carry no unit. The keys and types are
    hard-coded here: reading them back from the contract would agree with
    itself."""
    spec = C.ANNOTATION_PAYLOAD.get("edge_maxfreq")
    assert spec is not None, "edge_maxfreq is not a registered annotation kind"
    assert spec["req"] == {"channel": "uint", "min_interval_ticks": "uint",
                           "freq_hz": "num?"}
    assert "ANNOTATIONS.payload[edge_maxfreq].min_interval_ticks" in C.TIME_UNITS
    assert "ANNOTATIONS.payload[edge_maxfreq].freq_hz" in C.TIME_UNITS
    # The golden session carries one measured and one re-armed (null) datum,
    # so both shapes are exercised by every vendored copy's fixture run.
    seen = [a["payload"]["freq_hz"]
            for f in GOLDEN["frames"] if f.get("tag") == C.TAG_BY_NAME["ANNOTATIONS"]
            for a in f["payload"]["annotations"] if a["kind"] == "edge_maxfreq"]
    assert None in seen and any(isinstance(v, float) for v in seen), seen
    # A freq_hz carried as text is a producer bug the checker must name.
    bad = copy.deepcopy(spec)
    out = []
    C._check_map("p", {"channel": 3, "min_interval_ticks": 50,
                       "freq_hz": "1 MHz"}, bad, out)
    assert [f.field for f in out] == ["p.freq_hz"]
