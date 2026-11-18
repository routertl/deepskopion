"""Unit tests for the edge-capture codec (DS-P2.60). No simulator needed."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edge_capture import (  # noqa: E402
    EdgeCapture, EdgeCaptureWriter, decode_edge_word, edge_event_word,
    encode_edge_word, words_to_transitions, words_to_initial_level, FORMAT_TAG,
    maxfreq_is_sentinel, maxfreq_interval_to_hz,
)


def test_words_to_transitions_per_instance_wrap():
    """Interleaved channels must each unroll their own ts_us wrap (DS-T2.35).

    The live daemon emits SCL (instance 1) and SDA (instance 2) in separate
    EDGE_BATCH frames that interleave in the word stream. A SHARED wrap-counter
    would see instance 2's lower ts_us right after instance 1's higher ts_us and
    wrongly add a 65.536 ms wrap. Build such an interleaved stream and assert
    each instance reconstructs to its own clean, un-warped timeline."""
    # instance 1 rises at 10 µs, instance 2 rises at 12 µs — interleaved so the
    # stream order is [inst1@10us, inst2@12us]; a shared counter never wraps here,
    # so make it adversarial: inst1 at a HIGH ts_us, then inst2 at a LOW ts_us.
    w1 = edge_event_word(instance=1, timestamp_ns=50_000_000, s=0, f=0, e=0)  # 50 ms
    w2 = edge_event_word(instance=2, timestamp_ns=1_000_000,  s=0, f=0, e=0)  # 1 ms
    stream = [w1, w2]
    t1 = words_to_transitions(stream, instance=1)
    t2 = words_to_transitions(stream, instance=2)
    # Each keeps its own absolute time — no spurious +65.536 ms on instance 2.
    assert t1 == [(50_000_000, 1)], t1
    assert t2 == [(1_000_000, 1)], t2


def test_edge_event_word_preserves_sfe_verbatim():
    """edge_event_word (DS-T2.35) packs raw S/F/E + an absolute timestamp and
    round-trips losslessly through decode_edge_word — unlike encode_edge_word,
    which forces S=0/F=0 and derives E from a level. EDGE_BATCH entries carry
    real S (read-event) and F (FIFO pressure) bits that must survive to RouteWave."""
    for s, f, e in [(0, 0, 0), (0, 0, 1), (1, 0, 1), (0, 1, 0), (1, 1, 1)]:
        w = edge_event_word(instance=3, timestamp_ns=12_340, s=s, f=f, e=e, clk_mhz=100)
        d = decode_edge_word(w, clk_mhz=100)
        assert (d.s, d.f, d.e) == (s, f, e), (s, f, e, d)
        assert d.instance == 3
        assert d.timestamp_ns == 12_340
        # post-edge level: E direct when S=1, else !E (matches decode contract).
        assert d.level == (e if s else (1 - e))


def test_encode_decode_roundtrip_edge():
    # rising edge (level 1) on instance 1 at 12_340 ns, 100 MHz (10 ns/cycle)
    w = encode_edge_word(instance=1, timestamp_ns=12_340, level=1, clk_mhz=100)
    s = decode_edge_word(w, clk_mhz=100)
    assert s.instance == 1
    assert s.level == 1
    assert s.timestamp_ns == 12_340  # 12 µs (ts_us=12) + 34*10 ns ext
    assert s.s == 0


def test_encode_decode_roundtrip_falling():
    w = encode_edge_word(instance=5, timestamp_ns=8_000, level=0, clk_mhz=100)
    s = decode_edge_word(w, clk_mhz=100)
    assert s.instance == 5 and s.level == 0 and s.timestamp_ns == 8_000
    # E bit encodes !level for an edge → falling edge has E=1
    assert s.e == 1


def test_readback_sample_level_direct():
    w = encode_edge_word(instance=2, timestamp_ns=1_000, level=1, clk_mhz=100, readback=True)
    s = decode_edge_word(w, clk_mhz=100)
    assert s.s == 1 and s.level == 1  # S=1 → E holds the direct level


def test_ts_us_wrap_unrolled():
    # Two edges straddling the 16-bit µs wrap (65536 µs). Times: 65_000_000 ns
    # (65_000 µs) then 70_000_000 ns (70_000 µs → ts_us wraps to 4464).
    w0 = encode_edge_word(1, 65_000_000, 0, 100)
    w1 = encode_edge_word(1, 70_000_000, 1, 100)
    tr = words_to_transitions([w0, w1], clk_mhz=100, instance=1)
    assert [t for t, _ in tr] == [65_000_000, 70_000_000], tr
    assert [lv for _, lv in tr] == [0, 1]


def test_instance_filter():
    words = [encode_edge_word(1, 1000, 0, 100),
             encode_edge_word(2, 1500, 1, 100),
             encode_edge_word(1, 2000, 1, 100)]
    tr1 = words_to_transitions(words, 100, instance=1)
    assert [t for t, _ in tr1] == [1000, 2000]
    tr_all = words_to_transitions(words, 100, instance=None)
    assert len(tr_all) == 3


def test_capture_file_roundtrip(tmp_path):
    cap = EdgeCapture(clk_mhz=100, meta={"src": "unit-test", "device": "EDGE0"})
    words = [encode_edge_word(1, t, t // 1000 % 2, 100) for t in range(1000, 9000, 1000)]
    cap.extend(words)
    p = tmp_path / "cap.jsonl"
    cap.write(str(p))
    # header is the frozen format tag
    assert p.read_text().splitlines()[0].startswith(f'{{"format": "{FORMAT_TAG}"')
    back = EdgeCapture.read(str(p))
    assert back.clk_mhz == 100
    assert back.words == cap.words
    assert back.meta["device"] == "EDGE0"
    assert back.instances() == [1]


def test_streaming_writer(tmp_path):
    p = tmp_path / "stream.jsonl"
    with EdgeCaptureWriter(str(p), clk_mhz=100, meta={"live": True}) as w:
        w.append([encode_edge_word(3, 1000, 1, 100)])
        w.append([])  # empty tick — must be skipped
        w.append([encode_edge_word(3, 2000, 0, 100)])
        assert w.total_words == 2
    back = EdgeCapture.read(str(p))
    assert len(back.words) == 2
    assert back.transitions(instance=3) == [(1000, 1), (2000, 0)]


def test_reject_foreign_file(tmp_path):
    p = tmp_path / "foreign.jsonl"
    p.write_text('{"format": "something-else", "clk_mhz": 100}\n')
    with pytest.raises(ValueError):
        EdgeCapture.read(str(p))


# ─── DS-P3.117 max-frequency decode ────────────────────────────────────────

def test_maxfreq_sentinel_is_no_measurement():
    """All-ones ED_MAX_INTERVAL = 'no max yet / just re-armed' → None Hz."""
    assert maxfreq_is_sentinel(0xFFFFFF, width=24) is True
    assert maxfreq_interval_to_hz(0xFFFFFF, clk_hz=100_000_000, width=24) is None
    # A zero/garbage read is also rejected (guards a corrupt CSR fetch).
    assert maxfreq_interval_to_hz(0, clk_hz=100_000_000, width=24) is None


def test_maxfreq_interval_to_hz_hardcoded():
    """freq_max = clk / (2 * interval). Hard-coded expected values."""
    # interval=8 clk half-period @100 MHz → 100e6 / 16 = 6.25 MHz.
    assert maxfreq_interval_to_hz(8, clk_hz=100_000_000) == 6_250_000.0
    # interval=5 → 100e6 / 10 = 10 MHz.
    assert maxfreq_interval_to_hz(5, clk_hz=100_000_000) == 10_000_000.0
    # interval=50 → 100e6 / 100 = 1 MHz.
    assert maxfreq_interval_to_hz(50, clk_hz=100_000_000) == 1_000_000.0


# ── DS-P1.44: the S=1 state-readback word is NOT a transition ─────────────────
#
# These use the REAL EDGE_EVENT words from the committed DE-C0 silicon capture
# (DE-C0 silicon capture cap_edge.live.json), decoded at
# the sample clock that capture's HELO advertises (50 MHz). They are the words
# that exposed the defect, so they cannot drift away from it.
_SILICON_EDGE_WORDS = [215186193, 148077314, 164860719,
                       148090660, 164867886, 148090892, 164868122]
_SILICON_CLK_MHZ = 50          # helo["sample_clk_hz"] = 50_000_000
_SILICON_INSTANCE = 1


def test_state_readback_word_is_not_a_transition():
    """S=1 is a LEVEL SNAPSHOT at datalogger-enable, not an edge — the same split the
    RTL-side contract already makes ('s_bit must be 0 for every edge event
    (enable-capturing, not readback)', test_datalogger_gather_edge.py).

    BASELINE (verified): RED on the pre-DS-P1.44 decoder, which returned SEVEN
    entries here, the first of them the readback.
    MUTATION (verified): deleting the ``if s.s: continue`` guard in
    words_to_transitions turns this RED again."""
    tr = words_to_transitions(_SILICON_EDGE_WORDS, clk_mhz=_SILICON_CLK_MHZ,
                              instance=_SILICON_INSTANCE)
    assert tr == [(54139040, 1), (54163940, 0), (54191720, 1),
                  (54191920, 0), (54192240, 1), (54192520, 0)], tr
    assert len(tr) == 6, (
        f"expected 6 real edges from the silicon words, got {len(tr)} — a "
        f"state-readback word is being counted as a transition")


def test_transitions_are_monotonic_on_real_silicon_words():
    """words_to_transitions documents 'Output is monotonic in time'. It was NOT:
    the readback is stamped 300 ns AFTER the first real edge but arrives BEFORE it
    in the word stream, so the decoded timeline ran BACKWARDS
    (``(54139340, 0), (54139040, 1), ...``).

    BASELINE (verified): RED on the pre-DS-P1.44 decoder — this is the assertion
    that caught the defect."""
    tr = words_to_transitions(_SILICON_EDGE_WORDS, clk_mhz=_SILICON_CLK_MHZ,
                              instance=_SILICON_INSTANCE)
    backwards = [(a, b) for a, b in zip(tr, tr[1:]) if b[0] < a[0]]
    assert not backwards, (
        f"decoded edge timeline runs backwards at {backwards} — "
        f"words_to_transitions promises a monotonic timeline")


def test_initial_level_is_still_recoverable_from_the_readback():
    """Excluding S=1 from transitions must not LOSE the level it carries: a replay
    consumer needs the line's state at enable before the first edge.

    MUTATION (verified): making words_to_initial_level return None turns this RED."""
    got = words_to_initial_level(_SILICON_EDGE_WORDS, clk_mhz=_SILICON_CLK_MHZ,
                                 instance=_SILICON_INSTANCE)
    assert got == (54139340, 0), got
    # A stream with no readback word must say so, not invent a level.
    only_edges = [w for w in _SILICON_EDGE_WORDS if not ((w >> 26) & 1)]
    assert words_to_initial_level(only_edges, clk_mhz=_SILICON_CLK_MHZ,
                                  instance=_SILICON_INSTANCE) is None


def test_readback_word_still_drives_wrap_tracking():
    """A readback word is skipped as a transition but must still advance the
    per-instance ts_us wrap base, or excluding it would corrupt the timeline it
    was meant to clean up.

    Build: readback near the top of a ts_us epoch, then an edge after the wrap.
    MUTATION (verified): moving the ``if s.s`` guard ABOVE the wrap bookkeeping
    turns this RED (the wrap is missed and the edge lands 65.536 ms early)."""
    rb = encode_edge_word(3, 65_000_000, level=0, clk_mhz=100, readback=True)
    ed = encode_edge_word(3, 65_600_000, level=1, clk_mhz=100)
    tr = words_to_transitions([rb, ed], clk_mhz=100, instance=3)
    assert tr == [(65_600_000, 1)], tr


# ── DS-P1.44 revision: a SMALL backwards ts_us step is skew, not a wrap ───────
#
# The words below are the REAL EDGE_EVENT words from the capture that RETIRES
# edge_scl (silicon capture cap_edge.live.json,
# frame tag 7). They are not the P1.59 words above: here the readback is stamped
# 320 ns after the first edge and that skew CROSSES a µs boundary, so ts_us runs
# 40894 -> 40893. The decoder read that 1 µs step-back as a 16-bit wrap and put
# every transition 65.536 ms late — on the very capture used to close the case,
# while the P1.59 fixture (readback and first edge share ts_us=54139) could never
# show it.
_P1_44_EDGE_WORDS = [211795467, 144686381, 161469195,
                     144698414, 161475846, 144698646, 161475876]


def test_no_spurious_wrap_on_the_p1_44_retirement_capture():
    """The words that retire edge_scl must decode to ONE coherent timeline.

    BASELINE (verified): RED before this revision — ``words_to_initial_level``
    returned 40 894 220 ns while the first transition read 106 429 900 ns, a
    65.536 ms artifact inside a single capture.
    MUTATION (verified): dropping the ``> _TS_US_SKEW_TOLERANCE`` guard (any
    backwards step counts as a wrap) turns this RED again."""
    init = words_to_initial_level(_P1_44_EDGE_WORDS, clk_mhz=_SILICON_CLK_MHZ,
                                  instance=_SILICON_INSTANCE)
    tr = words_to_transitions(_P1_44_EDGE_WORDS, clk_mhz=_SILICON_CLK_MHZ,
                              instance=_SILICON_INSTANCE)
    assert init == (40894220, 0), init
    # The readback and the first edge are 320 ns apart on the wire. Anything near
    # a ts_us period apart is the decoder inventing a wrap, not datalogger time.
    assert abs(tr[0][0] - init[0]) < 1_000_000, (
        f"first transition {tr[0][0]} ns is {tr[0][0] - init[0]} ns from the "
        f"state readback {init[0]} ns on the SAME capture — a spurious "
        f"65.536 ms ts_us wrap (the readback is stamped 320 ns later but "
        f"arrives first, so ts_us steps back 40894 -> 40893)")
    assert tr == [(40893900, 1), (40915220, 0), (40940920, 1),
                  (40941120, 0), (40941440, 1), (40941720, 0)], tr


def test_small_backwards_step_is_skew_and_a_large_one_is_still_a_wrap():
    """The rule in one test, synthetically, so it does not depend on a capture:
    a later-stamped readback arriving FIRST must not unroll, and a genuine wrap
    after it still must. Same instance, so per-instance tracking cannot mask it."""
    # A readback stamped 200 ns LATER than the edge it precedes in the stream,
    # across a µs boundary — the silicon shape, synthesised: ts_us steps back 1.
    rb = encode_edge_word(4, 30_001_000, level=0, clk_mhz=100, readback=True)
    ed = encode_edge_word(4, 30_000_800, level=1, clk_mhz=100)
    assert ((rb >> 8) & 0xFFFF) - ((ed >> 8) & 0xFFFF) == 1, "fixture is not a 1 us step-back"
    assert words_to_transitions([rb, ed], clk_mhz=100, instance=4) == [(30_000_800, 1)]
    # ...and a real wrap after that skew is still unrolled. The tolerance costs
    # only 1.024 ms of forward range, so a wrap 60 ms later still resolves:
    # ts_us 30000 -> 24464 is a 5536 us step-back, far beyond the skew bound.
    late = encode_edge_word(4, 90_000_000, level=0, clk_mhz=100)
    assert words_to_transitions([rb, ed, late], clk_mhz=100, instance=4) == [
        (30_000_800, 1), (90_000_000, 0)]


def test_initial_level_and_transitions_unroll_identically():
    """``words_to_initial_level`` and ``words_to_transitions`` decode the same
    stream, so they must share one wrap rule — the P1.44 defect was exactly the
    two disagreeing by a period. Assert it on a stream with BOTH a skewed
    readback and a genuine wrap, where a divergence has somewhere to hide."""
    rb = encode_edge_word(5, 65_530_000, level=0, clk_mhz=100, readback=True)
    ed_skew = encode_edge_word(5, 65_529_000, level=1, clk_mhz=100)   # 1 us back
    ed_wrap = encode_edge_word(5, 65_600_000, level=0, clk_mhz=100)   # real wrap
    stream = [rb, ed_skew, ed_wrap]
    init = words_to_initial_level(stream, clk_mhz=100, instance=5)
    tr = words_to_transitions(stream, clk_mhz=100, instance=5)
    assert init == (65_530_000, 0), init
    assert tr == [(65_529_000, 1), (65_600_000, 0)], tr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
