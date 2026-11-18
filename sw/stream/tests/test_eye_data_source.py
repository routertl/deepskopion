"""Dry-run tests for the ACTIVE eye-scan daemon source (DS-P2.66 Phase 5).

EyeSweepDataSource is the producer glue that turns the eye_sweep library into a
live wave_stream source: one EyeEngineDriver.measure() per next_batch(), emitted
as the DOMAIN_EYE eye_point / eye_closed annotation RouteWave's BathtubView
(LT-P3.218) consumes. These tests drive it with the MockEyeRegbank engine model
(NO hardware) and assert the live bathtub is honest:

  * a real eye — locked centre AND closed edges (an all-locked sweep would be a
    suspicious all-green, bug-finding-is-success);
  * a wedged engine that never asserts DONE (the no-PMOD-jumper case) degrades to
    eye_closed cells, never a fabricated BER, and never crashes the drain loop;
  * the annotation channel + field shape match decoders.decode_eye exactly;
  * apply_config re-triggers the sweep and toggles the crosstalk aggressor.
"""

from __future__ import annotations

from sw.stream.eye_sweep import FreqProfile, Regs
from sw.stream.regbank import FakeRegbank
from sw.stream.data_source import EyeSweepDataSource
from sw.stream.tests.test_eye_sweep import MockEyeRegbank


def _drain(src, max_cells=200):
    cells = []
    for _ in range(max_cells):
        b = src.next_batch()
        if b is None:
            break
        cells.extend(b.annotations)
    return cells


def test_live_bathtub_open_centre_closed_edges():
    """A full sweep over the modelled engine traces a real eye: locked window in
    the centre, closed cells at both edges."""
    rb = MockEyeRegbank()
    src = EyeSweepDataSource(rb, freqs=[FreqProfile(sel=0, hz=50_000_000)],
                             phases=list(range(32)), loop=False, poll_timeout_s=0.1)
    cells = _drain(src)

    assert len(cells) == 32, "one annotation per phase tap"
    open_phases = sorted(a.fields["phase_sel"] for a in cells if a.kind == "eye_point")
    shut_phases = sorted(a.fields["phase_sel"] for a in cells if a.kind == "eye_closed")
    assert open_phases and shut_phases, "a real eye has BOTH open and closed cells"
    # MockEyeRegbank lock window for freq_sel=0 is (6, 25).
    assert min(open_phases) == 6 and max(open_phases) == 25
    assert 0 in shut_phases and 31 in shut_phases, "edges must be closed"


def test_annotation_shape_matches_decode_eye_contract():
    """channel + kind + fields are exactly what RouteWave's BathtubView keys on."""
    rb = MockEyeRegbank()
    src = EyeSweepDataSource(rb, freqs=[FreqProfile(sel=0, hz=50_000_000)],
                             phases=list(range(32)), loop=False, poll_timeout_s=0.1)
    cells = _drain(src)
    locked = next(a for a in cells if a.kind == "eye_point")
    closed = next(a for a in cells if a.kind == "eye_closed")

    for a in (locked, closed):
        assert a.channel_id == 107          # channels.py "eye_point" channel
        assert set(a.fields) >= {"freq_sel", "phase_sel", "freq_hz",
                                 "locked", "err_count", "bit_count", "ber"}
    assert locked.fields["locked"] is True and locked.fields["ber"] is not None
    assert closed.fields["locked"] is False and closed.fields["ber"] is None
    assert locked.fields["freq_hz"] == 50_000_000


def test_wedged_engine_no_jumper_degrades_to_closed_not_crash():
    """A plain FakeRegbank never sets EYE_STATUS.done (mirrors a board with no
    PMOD loopback jumper → engine never locks). Every cell must time out into an
    eye_closed annotation — no exception escapes the drain loop, no fake BER."""
    rb = FakeRegbank()  # no engine model: DONE never asserts
    src = EyeSweepDataSource(rb, freqs=[FreqProfile(sel=0, hz=50_000_000)],
                             phases=list(range(4)), loop=False, poll_timeout_s=0.02)
    cells = _drain(src)
    assert len(cells) == 4
    assert all(a.kind == "eye_closed" for a in cells)
    assert all(a.fields["ber"] is None and a.fields["bit_count"] == 0 for a in cells)


def test_apply_config_retriggers_and_toggles_aggressor():
    rb = MockEyeRegbank()
    src = EyeSweepDataSource(rb, freqs=[FreqProfile(sel=0, hz=50_000_000)],
                             phases=list(range(32)), loop=True, poll_timeout_s=0.1)
    src.next_batch()
    src.next_batch()
    assert src._pos == 2
    assert src.apply_config(0, 0x2) is None      # bit1 = aggressor on
    assert src._aggressor is True
    assert src._pos == 0                          # re-armed from cell 0
    assert src.apply_config(0, 0x0) is None
    assert src._aggressor is False


def test_loop_mode_never_eofs():
    """loop=True keeps producing past one grid pass (continuous live update)."""
    rb = MockEyeRegbank()
    src = EyeSweepDataSource(rb, freqs=[FreqProfile(sel=0, hz=50_000_000)],
                             phases=list(range(8)), loop=True, poll_timeout_s=0.1)
    # Two full passes worth of cells, all non-None.
    assert all(src.next_batch() is not None for _ in range(16))
    assert src._pass >= 1


# Reference the Regs import so a stale-offset refactor surfaces here too.
def test_regs_offsets_present():
    for name in ("EYE_CONTROL", "EYE_BIT_TARGET", "EYE_STATUS",
                 "EYE_ERR_COUNT_LO", "EYE_BIT_COUNT_LO"):
        assert hasattr(Regs, name)


# ── DS-P3.119 max-frequency streaming mode ────────────────────────────────
from sw.stream.tests.test_eye_sweep import MockPsEyeRegbank, DENSE_FREQS  # noqa: E402


def test_max_freq_mode_streams_scan_then_summary():
    """In max-freq mode the source streams per-frequency phase-scan points (with
    phase_ps for the true-UI axis) and finishes with ONE authoritative eye_summary
    annotation carrying the MAX RATE verdict for LT-P3.243."""
    rb = MockPsEyeRegbank()
    src = EyeSweepDataSource(rb, freqs=DENSE_FREQS, phases=[0], loop=False,
                             poll_timeout_s=0.5, max_freq=True,
                             ber_threshold=1e-3, eye_margin_ui=0.30)
    cells = _drain(src, max_cells=64)

    points = [a for a in cells if a.kind in ("eye_point", "eye_closed")]
    summaries = [a for a in cells if a.kind == "eye_summary"]
    assert points, "max-freq mode must stream the per-step scan points"
    assert len(summaries) == 1, "exactly one summary verdict per sweep"

    # scan points carry the true-UI phase axis (LT-P3.219)
    assert all("phase_ps" in a.fields and "phase_ui" in a.fields for a in points)
    # a real scan has BOTH open and closed phases (no all-green)
    assert any(a.kind == "eye_point" for a in points)
    assert any(a.kind == "eye_closed" for a in points)

    summ = summaries[0]
    assert summ.channel_id == EyeSweepDataSource.EYE_SUMMARY_CHANNEL == 108
    assert summ.fields["max_freq_hz"] == 166_666_667
    assert "MAX RATE" in summ.text and "166.7 MHz" in summ.text
    curve = {c["freq_hz"]: c for c in summ.fields["curve"]}
    assert curve[200_000_000]["passed"] is False   # 200 MHz below margin
    assert curve[166_666_667]["passed"] is True
