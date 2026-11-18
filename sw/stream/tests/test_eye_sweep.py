"""Dry-run tests for the eye-scan sweep orchestrator (DS-P2.66, Phase 3).

Exercises the full host vertical slice with NO hardware: a MockEyeRegbank models
eye_engine (a locked phase-window with a BER gradient, closed at the edges), and
the tests prove eye_sweep drives the right CSR sequence, the sweep traces a real
bathtub, the DOMAIN_EYE wire frame round-trips through decode_eye, and the
adversarial cases (eye-closed cell, wedged engine) surface honestly.

bug-finding-is-success: a sweep where EVERY cell locked would be a suspicious
all-green — a real eye has CLOSED edges. test_sweep_traces_a_bathtub asserts both
that the centre locks AND that the edges are closed.
"""

from __future__ import annotations

import pytest

from sw.stream import zs_msg, decoders
from sw.stream.eye_sweep import (
    EyeEngineDriver, FreqProfile, sweep, sweep_frames, pack_control,
    eye_width, max_frequency, steps_per_ui, PS_STEP_PS,
)
from sw.stream.regbank import FakeRegbank, Regs


# ── EYE_STATUS bits (mirror eye_sweep) ──
_ST_DONE = 1 << 1
_ST_LOCKED = 1 << 2


def _eye_window(freq_sel: int) -> tuple[int, int]:
    """Modelled locking phase-window: wide at the slow reference profile,
    narrower as frequency rises (the eye closes)."""
    return {0: (6, 25), 1: (10, 21), 2: (14, 18)}.get(freq_sel, (15, 16))


class MockEyeRegbank(FakeRegbank):
    """FakeRegbank that reacts to an EYE_CONTROL start edge like eye_engine:
    inside the window it locks (BER rises toward the edges); outside it never
    locks (bit_count=0). Synchronous — DONE is set on the start write."""

    def __init__(self, bit_target_lock_latency: int = 40):
        super().__init__()
        self._prev_start = 0
        self._latency = bit_target_lock_latency

    def write_u32(self, offset: int, value: int) -> None:
        super().write_u32(offset, value)
        if offset == Regs.EYE_CONTROL:
            start = value & 1
            if start and not self._prev_start:
                self._run_point(value)
            self._prev_start = start

    @staticmethod
    def _errors(phase: int, lo: int, hi: int) -> int:
        center = (lo + hi) / 2.0
        half = max(1.0, (hi - lo) / 2.0)
        d = abs(phase - center) / half        # 0 at centre, 1 at the window edge
        if d < 0.6:
            return 0                            # clean centre of the eye
        return int(round((d - 0.6) * 50))       # BER ramps toward the edges

    def _run_point(self, ctrl: int) -> None:
        freq_sel = (ctrl >> 12) & 0xF
        phase = (ctrl >> 4) & 0x3F
        bit_target = self.read_u32(Regs.EYE_BIT_TARGET)
        lo, hi = _eye_window(freq_sel)
        if lo <= phase <= hi:
            locked = True
            bits = max(0, bit_target - self._latency)
            err = self._errors(phase, lo, hi)
        else:
            locked, bits, err = False, 0, 0
        self.set(Regs.EYE_STATUS, _ST_DONE | (_ST_LOCKED if locked else 0))
        self.set(Regs.EYE_ERR_COUNT_LO, err & 0xFFFFFFFF)
        self.set(Regs.EYE_ERR_COUNT_HI, (err >> 32) & 0xFFFF)
        self.set(Regs.EYE_BIT_COUNT_LO, bits & 0xFFFFFFFF)
        self.set(Regs.EYE_BIT_COUNT_HI, (bits >> 32) & 0xFFFF)


FREQ0 = FreqProfile(sel=0, hz=50_000_000)
BIT_TARGET = 1_000_000


def _driver(rb=None):
    return EyeEngineDriver(rb or MockEyeRegbank(), bit_target=BIT_TARGET,
                           poll_timeout_s=1.0)


def test_pack_control_field_layout():
    c = pack_control(freq_sel=0xA, phase_sel=0x1F, start=True, aggressor=True)
    assert c & 1 == 1                          # start
    assert (c >> 1) & 1 == 1                    # aggressor
    assert (c >> 4) & 0x3F == 0x1F             # phase
    assert (c >> 12) & 0xF == 0xA              # freq


def test_measure_clean_centre_point():
    rb = MockEyeRegbank()
    drv = _driver(rb)
    lo, hi = _eye_window(0)
    centre = (lo + hi) // 2
    pt = drv.measure(FREQ0, centre)
    assert pt.locked and pt.err_count == 0
    assert pt.bit_count == BIT_TARGET - 40
    assert pt.ber == 0.0
    assert pt.freq_hz == 50_000_000


def test_measure_drives_expected_csr_sequence():
    rb = MockEyeRegbank()
    drv = _driver(rb)
    drv.measure(FREQ0, 12)
    # The writes log: BIT_TARGET, then control(start=0), then control(start=1).
    offs = [o for o, _ in rb.writes]
    assert offs[:3] == [Regs.EYE_BIT_TARGET, Regs.EYE_CONTROL, Regs.EYE_CONTROL]
    _, v_target = rb.writes[0]
    assert v_target == BIT_TARGET
    (_, v0), (_, v1) = rb.writes[1], rb.writes[2]
    assert v0 & 1 == 0 and v1 & 1 == 1                       # 0→1 start edge
    assert (v1 >> 4) & 0x3F == 12 and (v1 >> 12) & 0xF == 0  # phase/freq carried


def test_sweep_traces_a_bathtub():
    drv = _driver()
    pts = list(sweep(drv, [FREQ0], range(32)))
    assert len(pts) == 32
    locked_phases = [p.phase_sel for p in pts if p.locked]
    closed_phases = [p.phase_sel for p in pts if not p.locked]

    lo, hi = _eye_window(0)
    assert locked_phases == list(range(lo, hi + 1)), "lock window must match the model"
    # Adversarial floor: a real eye has BOTH an open centre AND closed edges.
    assert locked_phases, "eye must be open somewhere"
    assert closed_phases, "eye must be CLOSED at the edges — an all-locked sweep is suspect"

    # Closed cells report no measurement, never a false BER=0.
    for p in pts:
        if not p.locked:
            assert p.bit_count == 0 and p.ber == 1.0

    # BER is lowest at the centre and rises toward the window edges (bathtub).
    centre = (lo + hi) // 2
    centre_ber = next(p.ber for p in pts if p.phase_sel == centre)
    edge_ber = next(p.ber for p in pts if p.phase_sel == lo)
    assert centre_ber == 0.0
    assert edge_ber > centre_ber


def test_eye_frame_roundtrips_through_decode():
    drv = _driver()
    lo, hi = _eye_window(0)
    frame = zs_msg.pack_eye_point(drv.measure(FREQ0, lo))   # an edge cell: locked, BER>0
    header = zs_msg.parse_header(frame)
    assert header.domain == zs_msg.DOMAIN_EYE
    payload = frame[zs_msg.HEADER_SIZE:]
    dec = decoders.decode_frame(header, payload, sample_clk_hz=50_000_000)
    assert dec is not None and dec.kind == "eye_point"
    assert dec.fields["phase_sel"] == lo and dec.fields["locked"] is True
    assert dec.fields["ber"] is not None and dec.fields["ber"] > 0


def test_closed_cell_decodes_as_eye_closed():
    drv = _driver()
    pt = drv.measure(FREQ0, 0)              # tap 0 is outside the window
    assert not pt.locked and pt.bit_count == 0
    frame = zs_msg.pack_eye_point(pt)
    header = zs_msg.parse_header(frame)
    dec = decoders.decode_frame(header, frame[zs_msg.HEADER_SIZE:], 50_000_000)
    assert dec.kind == "eye_closed" and dec.fields["ber"] is None


def test_sweep_frames_are_wellformed_eye_msgs():
    drv = _driver()
    frames = list(sweep_frames(drv, [FREQ0], range(4)))
    assert len(frames) == 4
    for f in frames:
        h = zs_msg.parse_header(f)
        assert h.domain == zs_msg.DOMAIN_EYE and h.event_id == zs_msg.EVT_EYE_POINT
        assert h.payload_len == zs_msg.EYE_POINT_SIZE


def test_wedged_engine_times_out():
    """Adversarial: an engine that never asserts DONE must raise, not hang or
    return a bogus point."""
    rb = FakeRegbank()                      # plain fake: EYE_STATUS stays 0 forever
    drv = EyeEngineDriver(rb, bit_target=BIT_TARGET, poll_timeout_s=0.05)

    ticks = iter([0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 1.0])
    with pytest.raises(TimeoutError):
        drv.measure(FREQ0, 12, monotonic=lambda: next(ticks))


# ─────────────────────────────────────────────────────────────────────────────
# DS-P3.118: MMCM fine-PS sample-phase scan + eye-width + max-frequency
# ─────────────────────────────────────────────────────────────────────────────

# The dense clk_drp profiles (mirror the RTL ROM): sel 0-3 PINNED, 4-9 the grid.
DENSE_FREQS = [
    FreqProfile(sel=0, hz=100_000_000),
    FreqProfile(sel=1, hz=125_000_000),
    FreqProfile(sel=2, hz=166_666_667),
    FreqProfile(sel=3, hz=200_000_000),
    FreqProfile(sel=4, hz=111_111_111),
    FreqProfile(sel=5, hz=142_857_143),
    FreqProfile(sel=6, hz=83_333_333),
    FreqProfile(sel=7, hz=62_500_000),
    FreqProfile(sel=8, hz=50_000_000),
    FreqProfile(sel=9, hz=25_000_000),
]
_SEL_HZ = {f.sel: f.hz for f in DENSE_FREQS}


class MockPsEyeRegbank(FakeRegbank):
    """Models the DUAL-CLOCK eye over the MMCM fine-PS axis (DS-P3.118).

    The eye is a contiguous window of sample-clock phase steps (ps_count) that is
    WIDE at low frequency and NARROWS as frequency rises, closing entirely above
    `f_close`. Inside the window the loopback locks clean (BER 0); outside it
    never locks (bit_count 0) — eye-closed, not a false BER 0. The window can be
    re-centred (`center_frac`) WITHOUT changing its width, so a test can prove the
    eye-width metric is phase-independent.
    """

    F_CLOSE_HZ = 250_000_000

    def __init__(self, center_frac: float = 0.5, lock_latency: int = 40):
        super().__init__()
        self._ps_count = 0
        self._prev_start = 0
        self._prev_step = 0
        self._prev_rst = 0
        self._center_frac = center_frac
        self._latency = lock_latency

    # EYE_PHASE_STATUS is computed live from the tracked counter (ps_busy=0: the
    # modelled MMCM completes each step instantly).
    def read_u32(self, offset: int) -> int:
        if offset == Regs.EYE_PHASE_STATUS:
            return self._ps_count & 0xFFFF
        return super().read_u32(offset)

    def write_u32(self, offset: int, value: int) -> None:
        super().write_u32(offset, value)
        if offset != Regs.EYE_CONTROL:
            return
        rst = (value >> 18) & 1
        step = (value >> 16) & 1
        incdec = (value >> 17) & 1
        start = value & 1
        if rst and not self._prev_rst:
            self._ps_count = 0
        elif step and not self._prev_step:
            self._ps_count += 1 if incdec else -1
        if start and not self._prev_start:
            self._run_point(value)
        self._prev_rst, self._prev_step, self._prev_start = rst, step, start

    def _open_window(self, hz: int):
        """(lo, hi) ps_count of the open eye at hz, or None if fully closed."""
        ui_steps = steps_per_ui(hz)
        open_frac = max(0.0, 1.0 - hz / self.F_CLOSE_HZ)
        width = open_frac * ui_steps
        if width <= 0:
            return None
        center = self._center_frac * ui_steps
        return (center - width / 2.0, center + width / 2.0)

    def _run_point(self, ctrl: int) -> None:
        freq_sel = (ctrl >> 12) & 0xF
        hz = _SEL_HZ.get(freq_sel, 100_000_000)
        bit_target = self.read_u32(Regs.EYE_BIT_TARGET)
        win = self._open_window(hz)
        if win is not None and win[0] <= self._ps_count <= win[1]:
            locked, bits, err = True, max(0, bit_target - self._latency), 0
        else:
            locked, bits, err = False, 0, 0
        self.set(Regs.EYE_STATUS, _ST_DONE | (_ST_LOCKED if locked else 0))
        self.set(Regs.EYE_ERR_COUNT_LO, err & 0xFFFFFFFF)
        self.set(Regs.EYE_ERR_COUNT_HI, (err >> 32) & 0xFFFF)
        self.set(Regs.EYE_BIT_COUNT_LO, bits & 0xFFFFFFFF)
        self.set(Regs.EYE_BIT_COUNT_HI, (bits >> 32) & 0xFFFF)


def _ps_driver(rb):
    return EyeEngineDriver(rb, bit_target=BIT_TARGET, poll_timeout_s=1.0)


def test_steps_per_ui_calibration():
    # 56 * VCO/f: 280 steps/UI at 200 MHz (D=5), 1120 at 50 MHz (D=20).
    assert round(steps_per_ui(200_000_000)) == 280
    assert round(steps_per_ui(50_000_000)) == 1120
    assert abs(PS_STEP_PS - 1e12 / (56 * 1e9)) < 1e-9   # ~17.857 ps


def test_ps_reset_and_step_csr_sequence():
    rb = MockPsEyeRegbank()
    drv = _ps_driver(rb)
    drv.ps_reset(freq_sel=2)
    cnt = drv.ps_step(freq_sel=2, incdec=True)
    assert cnt == 1
    cnt = drv.ps_step(freq_sel=2, incdec=True)
    assert cnt == 2
    cnt = drv.ps_step(freq_sel=2, incdec=False)   # retard
    assert cnt == 1
    # ps_step must drive a clean 0→1→0 on EYE_CONTROL.ps_step (bit16).
    step_bits = [(v >> 16) & 1 for o, v in rb.writes if o == Regs.EYE_CONTROL]
    assert 1 in step_bits and 0 in step_bits


def test_scan_phase_traces_a_window_with_closed_edges():
    """A scan at a mid frequency must show a CONTIGUOUS locked window bracketed by
    CLOSED phases on both sides — an all-locked scan would be suspect."""
    rb = MockPsEyeRegbank()
    drv = _ps_driver(rb)
    scan = drv.scan_phase(FreqProfile(sel=2, hz=166_666_667))
    locked = [s for s in scan.samples if s.point.locked]
    closed = [s for s in scan.samples if not s.point.locked]
    assert locked, "eye must be open somewhere"
    assert closed, "eye must be CLOSED at the scan edges (no all-green)"
    # the open phases are contiguous (a single window, not scattered)
    idx = [i for i, s in enumerate(scan.samples) if s.point.locked]
    assert idx == list(range(idx[0], idx[-1] + 1)), "open eye must be one contiguous run"
    # closed cells never fabricate a BER
    for s in closed:
        assert s.point.bit_count == 0 and s.point.ber == 1.0


def test_eye_width_matches_modelled_open_fraction():
    """eye_width(UI) must equal the modelled open fraction (1 - f/f_close)."""
    rb = MockPsEyeRegbank()
    drv = _ps_driver(rb)
    hz = 166_666_667
    scan = drv.scan_phase(FreqProfile(sel=2, hz=hz))
    w = eye_width(scan, ber_threshold=1e-3)
    expected_ui = 1.0 - hz / MockPsEyeRegbank.F_CLOSE_HZ      # ≈ 0.333
    assert abs(w.width_ui - expected_ui) < 0.02
    assert abs(w.width_ps - w.width_steps * PS_STEP_PS) < 1e-6


def test_eye_width_is_phase_independent():
    """The SAME width window at a different position in the UI yields the SAME
    eye width — the property the IDELAY-only instrument lacked (DS-P3.118)."""
    f = FreqProfile(sel=2, hz=166_666_667)
    w_lo = eye_width(_ps_driver(MockPsEyeRegbank(center_frac=0.3)).scan_phase(f), 1e-3)
    w_hi = eye_width(_ps_driver(MockPsEyeRegbank(center_frac=0.7)).scan_phase(f), 1e-3)
    assert abs(w_lo.width_ui - w_hi.width_ui) < 0.02


def test_max_frequency_finds_the_crossing_not_all_pass():
    """The headline: max usable frequency is the HIGHEST profile whose eye stays
    open >= the margin. With f_close=250 MHz and a 0.30-UI margin the eye closes
    below margin at 200 MHz but holds at 166.7 MHz → f_max = 166.7 MHz. An
    all-pass result would be a red flag (200 MHz must FAIL)."""
    drv = _ps_driver(MockPsEyeRegbank())
    res = max_frequency(drv, DENSE_FREQS, ber_threshold=1e-3, eye_margin_ui=0.30)
    assert res.max_freq_hz == 166_666_667, f"got {res.max_freq_hz}"
    by_hz = {r.freq.hz: r.passed for r in res.results}
    assert by_hz[200_000_000] is False, "200 MHz eye is below margin — must fail"
    assert by_hz[166_666_667] is True
    assert by_hz[50_000_000] is True
    # monotone-ish: every passing freq is <= f_max
    assert all(r.freq.hz <= res.max_freq_hz for r in res.results if r.passed)


def test_max_frequency_zero_when_link_dead():
    """Adversarial: if even the slowest profile cannot hold the margin (a dead /
    shorted jumper), max_freq_hz must be 0 — never a fabricated pass."""
    class DeadEye(MockPsEyeRegbank):
        F_CLOSE_HZ = 1            # everything is above f_close → always closed
    drv = _ps_driver(DeadEye())
    res = max_frequency(drv, DENSE_FREQS, ber_threshold=1e-3, eye_margin_ui=0.30)
    assert res.max_freq_hz == 0
    assert all(not r.passed for r in res.results)


# ─────────────────────────────────────────────────────────────────────────────
# DS-P3.119 / LT-P3.219+243: true-UI phase_ps on EYE_POINT + DOMAIN_EYE_SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
from sw.stream.eye_sweep import (   # noqa: E402
    max_frequency_frame, IDELAY_TAP_PS,
)


def test_eye_point_phase_ps_roundtrips_and_gives_true_ui():
    pt = zs_msg.EyePoint(freq_sel=3, phase_sel=0, err_count=0, bit_count=999_960,
                         locked=True, freq_hz=200_000_000, phase_ps=2500)
    frame = zs_msg.pack_eye_point(pt)
    h = zs_msg.parse_header(frame)
    assert h.payload_len == zs_msg.EYE_POINT_SIZE == 28
    back = zs_msg.parse_eye_point(frame[zs_msg.HEADER_SIZE:])
    assert back.phase_ps == 2500
    # 2500 ps at 200 MHz (UI=5000 ps) → 0.5 UI
    assert abs(back.phase_ui - 0.5) < 1e-9


def test_eye_point_v1_legacy_payload_still_parses():
    """A pre-phase_ps 24-byte payload must still parse (phase_ps defaults to 0)."""
    v1 = zs_msg.EYE_POINT_STRUCT.pack(2, 7, zs_msg.EYE_FLAG_LOCKED, 0,
                                      166_666_667, 3, 1000)
    assert len(v1) == zs_msg.EYE_POINT_SIZE_V1 == 24
    pt = zs_msg.parse_eye_point(v1)
    assert pt.freq_sel == 2 and pt.phase_sel == 7 and pt.phase_ps == 0


def test_scan_phase_stamps_mmcm_phase_ps():
    from sw.stream.eye_sweep import PS_STEP_PS
    drv = _ps_driver(MockPsEyeRegbank())
    scan = drv.scan_phase(FreqProfile(sel=2, hz=166_666_667), n_steps=10)
    # ps_count advances 0,1,2,… and phase_ps tracks ps_count * PS_STEP_PS
    for s in scan.samples:
        assert s.point.phase_ps == round(s.ps_count * PS_STEP_PS)
    assert scan.samples[0].point.phase_ps == 0
    assert scan.samples[5].point.phase_ps == round(5 * PS_STEP_PS)


def test_measure_defaults_phase_ps_from_idelay_tap():
    drv = _driver(MockEyeRegbank())
    pt = drv.measure(FREQ0, 10)         # no explicit phase_ps → tap*IDELAY_TAP_PS
    assert pt.phase_ps == round(10 * IDELAY_TAP_PS)


def test_eye_summary_roundtrips():
    summ = zs_msg.EyeSummary(
        max_freq_hz=166_666_667, ber_threshold=1e-6, eye_margin_ui=0.30,
        freqs=[
            zs_msg.EyeSummaryFreq(166_666_667, 2000, 0.333, True, False),
            zs_msg.EyeSummaryFreq(200_000_000, 1000, 0.200, False, False),
        ])
    frame = zs_msg.pack_eye_summary(summ)
    h = zs_msg.parse_header(frame)
    assert h.domain == zs_msg.DOMAIN_EYE and h.event_id == zs_msg.EVT_EYE_SUMMARY
    back = zs_msg.parse_eye_summary(frame[zs_msg.HEADER_SIZE:])
    assert back.max_freq_hz == 166_666_667
    assert abs(back.ber_threshold - 1e-6) < 1e-12
    assert abs(back.eye_margin_ui - 0.30) < 1e-6
    assert len(back.freqs) == 2
    assert back.freqs[0].passed is True and back.freqs[1].passed is False
    assert abs(back.freqs[0].eye_width_ui - 0.333) < 0.001


def test_max_frequency_frame_decodes_as_authoritative_summary():
    """The end-to-end DS-P3.119 deliverable: a sweep produces a DOMAIN_EYE_SUMMARY
    frame that decodes to the MAX RATE headline + per-freq curve for LT-P3.243."""
    drv = _ps_driver(MockPsEyeRegbank())
    result, frame = max_frequency_frame(drv, DENSE_FREQS,
                                        ber_threshold=1e-3, eye_margin_ui=0.30)
    assert result.max_freq_hz == 166_666_667
    header = zs_msg.parse_header(frame)
    dec = decoders.decode_frame(header, frame[zs_msg.HEADER_SIZE:], 0)
    assert dec.kind == "eye_summary"
    assert "166.7 MHz" in dec.text and "MAX RATE" in dec.text
    assert dec.fields["max_freq_hz"] == 166_666_667
    curve = {c["freq_hz"]: c for c in dec.fields["curve"]}
    assert curve[200_000_000]["passed"] is False
    assert curve[166_666_667]["passed"] is True
    # full-UI scan → no partial-eye flag anywhere (the LT guard stays clean)
    assert all(c["partial"] is False for c in dec.fields["curve"])


def test_eye_summary_flags_link_failure():
    """Adversarial: a link that holds no rate decodes as an ERROR 'LINK FAILS',
    max_freq_hz 0 — never a fabricated MAX RATE."""
    class DeadEye(MockPsEyeRegbank):
        F_CLOSE_HZ = 1
    drv = _ps_driver(DeadEye())
    _result, frame = max_frequency_frame(drv, DENSE_FREQS[:3],
                                         ber_threshold=1e-3, eye_margin_ui=0.30)
    header = zs_msg.parse_header(frame)
    dec = decoders.decode_frame(header, frame[zs_msg.HEADER_SIZE:], 0)
    assert dec.kind == "eye_summary" and dec.is_error
    assert dec.fields["max_freq_hz"] == 0 and "LINK FAILS" in dec.text
