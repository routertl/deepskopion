# rr unknown-llm (AI-generated)
"""PMOD eye-scan / BERT sweep orchestrator (DS-P2.66, Phase 3).

The eye instrument is ACTIVE, not passive: the PL (eye_engine) measures the BER
of one (frequency, sample-phase) loopback point ON COMMAND, and the PS drives
the grid here — for each frequency profile, for each IDELAY tap, it pokes
EYE_CONTROL, waits for EYE_STATUS.done, reads the 48-bit error/bit counters, and
frames the result as a DOMAIN_EYE zs_msg so RouteWave's BathtubAnalyzer
(LT-P3.217) fills the eye in live through the SAME wave_stream path the dataloggers
use.

This module is backend-agnostic: it talks to a `regbank.Regbank` (the /dev/mem
MmioRegbank on silicon, a FakeRegbank/mock under test), so the whole sweep can
be dry-run against a modelled eye_engine with no hardware (the Phase-3 gate).

Adversarial honesty (bug-finding-is-success): a point that never locks is
reported with bit_count==0 and locked=False — "no measurement", never a
fabricated BER=0. The sweep does not retry-until-green or smooth the data.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional

from . import zs_msg
from .regbank import Regbank, Regs

logger = logging.getLogger(__name__)


# ── EYE_CONTROL bit layout (mirror regbank.py Regs.EYE_CONTROL doc + RTL) ──
_CTRL_START        = 1 << 0
_CTRL_AGGRESSOR_EN = 1 << 1
_PHASE_SHIFT       = 4          # bits[9:4]
_PHASE_MASK        = 0x3F
_FREQ_SHIFT        = 12         # bits[15:12]
_FREQ_MASK         = 0xF
_CTRL_PS_STEP      = 1 << 16    # DS-P3.118 sample-clock fine phase-shift
_CTRL_PS_INCDEC    = 1 << 17    # 1 = advance, 0 = retard
_CTRL_PS_RST       = 1 << 18    # 0→1 zeroes the fabric phase-step counter

# EYE_STATUS bits
_ST_BUSY   = 1 << 0
_ST_DONE   = 1 << 1
_ST_LOCKED = 1 << 2

# EYE_PHASE_STATUS bits (DS-P3.118)
_PS_COUNT_MASK = 0xFFFF
_PS_BUSY       = 1 << 16

# 48-bit counter recombination
_HI_MASK = 0xFFFF

# ── MMCM fine phase-shift geometry (DS-P3.118) ─────────────────────────────
# The eye MMCM runs a fixed 1000 MHz VCO (CLKFBOUT_MULT=10 from the 100 MHz FCLK).
# One PSEN step shifts CLKOUT1 by VCO_period/56, and one UI at the swept bit-clock
# f = VCO/D spans 56*D steps. So the phase axis is calibrated in real time with no
# board characterisation: step_ps is exact and UI_steps follows from the divider.
VCO_HZ          = 1_000_000_000
PS_STEPS_PER_VCO = 56
PS_STEP_PS      = 1e12 / (PS_STEPS_PER_VCO * VCO_HZ)   # ≈ 17.857 ps
# IDELAYE2 resolution at the 200 MHz refclk (~78 ps/tap) — the legacy/fine axis.
# Used to stamp phase_ps on IDELAY-tap measurements so the consumer's true-UI X
# axis (LT-P3.219) works on both the IDELAY interim and the MMCM-PS path.
IDELAY_TAP_PS   = 78.0


def steps_per_ui(freq_hz: int) -> float:
    """MMCM fine-PS steps spanning one unit interval at freq_hz (= 56 * VCO/f)."""
    if freq_hz <= 0:
        raise ValueError("freq_hz must be positive")
    return PS_STEPS_PER_VCO * VCO_HZ / freq_hz


def _to_signed16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def pack_control(freq_sel: int, phase_sel: int,
                 start: bool = False, aggressor: bool = False,
                 ps_step: bool = False, ps_incdec: bool = False,
                 ps_rst: bool = False) -> int:
    """Assemble the EYE_CONTROL word from its fields."""
    return (
        ((freq_sel & _FREQ_MASK) << _FREQ_SHIFT)
        | ((phase_sel & _PHASE_MASK) << _PHASE_SHIFT)
        | (_CTRL_AGGRESSOR_EN if aggressor else 0)
        | (_CTRL_START if start else 0)
        | (_CTRL_PS_STEP if ps_step else 0)
        | (_CTRL_PS_INCDEC if ps_incdec else 0)
        | (_CTRL_PS_RST if ps_rst else 0)
    )


@dataclass(frozen=True)
class FreqProfile:
    """A clk_drp frequency profile: the index written to EYE_CONTROL.freq_sel
    plus the actual TX bit-clock it produces (Hz), carried into the eye frame so
    RouteWave can label the frequency axis in real units."""
    sel: int
    hz: int


# ── DS-P3.118 phase-scan / max-frequency result types (host-only) ──────────
@dataclass(frozen=True)
class PhaseSample:
    """One eye point taken at a known MMCM fine-PS offset (ps_count steps)."""
    ps_count: int
    point: zs_msg.EyePoint


@dataclass(frozen=True)
class PhaseScan:
    """A full sample-phase sweep at one frequency (the column of the eye)."""
    freq: FreqProfile
    phase_sel: int
    samples: List[PhaseSample]


@dataclass(frozen=True)
class EyeWidth:
    """The open eye at one frequency: the longest contiguous error-free PS run."""
    width_steps: int       # longest run of in-spec sample phases
    width_ps: float        # width_steps * PS_STEP_PS
    width_ui: float        # width_ps / UI(freq) — the phase-independent metric
    n_steps: int           # total steps scanned (the UI coverage)


@dataclass(frozen=True)
class FreqResult:
    freq: FreqProfile
    width: EyeWidth
    passed: bool           # eye_width_ui >= eye_margin_ui


@dataclass(frozen=True)
class MaxFreqResult:
    """The headline: the highest frequency whose eye stays open >= the margin."""
    max_freq_hz: int       # 0 if NO profile passes (link fails even at the slowest)
    results: List[FreqResult]
    ber_threshold: float
    eye_margin_ui: float


class EyeEngineDriver:
    """Host driver for one eye_engine instance over a regbank backend."""

    def __init__(self, regbank: Regbank, bit_target: int = 1_000_000,
                 poll_timeout_s: float = 2.0, poll_interval_s: float = 0.0):
        self._rb = regbank
        self._bit_target = bit_target
        self._poll_timeout_s = poll_timeout_s
        self._poll_interval_s = poll_interval_s

    def _read_u48(self, lo_off: int, hi_off: int) -> int:
        lo = self._rb.read_u32(lo_off)
        hi = self._rb.read_u32(hi_off) & _HI_MASK
        return (hi << 32) | lo

    def measure(self, freq: FreqProfile, phase_sel: int,
                aggressor: bool = False,
                monotonic=time.monotonic,
                phase_ps: Optional[int] = None) -> zs_msg.EyePoint:
        """Measure one (freq, phase) point and return its EyePoint.

        phase_ps stamps the ABSOLUTE sample-phase offset (ps) onto the point for
        the consumer's true-UI axis (LT-P3.219). If not given it is derived from
        the IDELAY tap (phase_sel * IDELAY_TAP_PS); the MMCM-PS scan passes the
        real ps_count * PS_STEP_PS instead.

        Sequence: latch BIT_TARGET → write control with start=0 (set freq/phase)
        → write control with start=1 (the 0→1 edge eye_engine launches on) →
        poll EYE_STATUS.done → read the frozen counters.

        Raises TimeoutError if DONE never asserts (a wedged engine must surface,
        not silently return a bogus point)."""
        self._rb.write_u32(Regs.EYE_BIT_TARGET, self._bit_target)
        # Two writes give eye_engine the required 0→1 START edge even if the
        # previous point left START high (re-arm idiom).
        self._rb.write_u32(Regs.EYE_CONTROL,
                           pack_control(freq.sel, phase_sel, start=False, aggressor=aggressor))
        self._rb.write_u32(Regs.EYE_CONTROL,
                           pack_control(freq.sel, phase_sel, start=True, aggressor=aggressor))

        deadline = monotonic() + self._poll_timeout_s
        status = 0
        while True:
            status = self._rb.read_u32(Regs.EYE_STATUS)
            if status & _ST_DONE:
                break
            if monotonic() >= deadline:
                raise TimeoutError(
                    f"eye_engine DONE timeout at freq_sel={freq.sel} "
                    f"phase_sel={phase_sel} (status={status:#x})")
            if self._poll_interval_s:
                time.sleep(self._poll_interval_s)

        err = self._read_u48(Regs.EYE_ERR_COUNT_LO, Regs.EYE_ERR_COUNT_HI)
        bits = self._read_u48(Regs.EYE_BIT_COUNT_LO, Regs.EYE_BIT_COUNT_HI)
        eff_phase_ps = (int(round(phase_sel * IDELAY_TAP_PS))
                        if phase_ps is None else int(phase_ps))
        return zs_msg.EyePoint(
            freq_sel=freq.sel,
            phase_sel=phase_sel,
            err_count=err,
            bit_count=bits,
            locked=bool(status & _ST_LOCKED),
            freq_hz=freq.hz,
            phase_ps=eff_phase_ps,
        )

    # ── DS-P3.118 sample-clock fine phase-shift (the full-UI eye axis) ──────
    def _write_ctrl(self, freq_sel: int, phase_sel: int, **kw) -> None:
        self._rb.write_u32(Regs.EYE_CONTROL, pack_control(freq_sel, phase_sel, **kw))

    def read_ps_status(self) -> tuple[int, bool]:
        """(ps_count, ps_busy) — net applied MMCM steps and whether one is in flight."""
        v = self._rb.read_u32(Regs.EYE_PHASE_STATUS)
        return _to_signed16(v & _PS_COUNT_MASK), bool(v & _PS_BUSY)

    def ps_reset(self, freq_sel: int, phase_sel: int = 0) -> None:
        """Zero the fabric phase-step counter (0→1 edge on EYE_CONTROL.ps_rst).
        NB the real sample phase also re-zeroes on every freq reconfig — this
        keeps the host's notion of phase aligned with the hardware."""
        self._write_ctrl(freq_sel, phase_sel, ps_rst=False)
        self._write_ctrl(freq_sel, phase_sel, ps_rst=True)
        self._write_ctrl(freq_sel, phase_sel, ps_rst=False)

    def ps_step(self, freq_sel: int, incdec: bool = True, phase_sel: int = 0,
                monotonic=time.monotonic) -> int:
        """Advance (incdec=True) / retard the sample_clk phase by one MMCM step.
        Drives a clean 0→1 on ps_step, then polls ps_busy low. Returns the new
        ps_count. Raises TimeoutError if the step never completes (a wedged MMCM
        PS must surface, not silently mis-calibrate the phase axis)."""
        self._write_ctrl(freq_sel, phase_sel, ps_step=False, ps_incdec=incdec)
        self._write_ctrl(freq_sel, phase_sel, ps_step=True, ps_incdec=incdec)
        self._write_ctrl(freq_sel, phase_sel, ps_step=False, ps_incdec=incdec)
        deadline = monotonic() + self._poll_timeout_s
        while True:
            cnt, busy = self.read_ps_status()
            if not busy:
                return cnt
            if monotonic() >= deadline:
                raise TimeoutError(
                    f"PS step busy timeout at freq_sel={freq_sel} (ps_count={cnt})")
            if self._poll_interval_s:
                time.sleep(self._poll_interval_s)

    def scan_phase(self, freq: FreqProfile, n_steps: Optional[int] = None,
                   phase_sel: int = 0, aggressor: bool = False,
                   coverage_ui: float = 1.1) -> PhaseScan:
        """Sweep the MMCM sample-clock phase across `coverage_ui` unit intervals,
        measuring the BER at each step — the column of the eye at one frequency.
        n_steps defaults to span coverage_ui (a little over one UI so the open
        window is fully bracketed). Phase is re-zeroed first so ps_count is the
        absolute offset from the nominal (post-reconfig) phase."""
        if n_steps is None:
            n_steps = int(math.ceil(coverage_ui * steps_per_ui(freq.hz)))
        self.ps_reset(freq.sel, phase_sel)
        samples: List[PhaseSample] = []
        for _ in range(n_steps):
            cnt, _busy = self.read_ps_status()
            pt = self.measure(freq, phase_sel, aggressor=aggressor,
                              phase_ps=int(round(cnt * PS_STEP_PS)))
            samples.append(PhaseSample(ps_count=cnt, point=pt))
            self.ps_step(freq.sel, incdec=True, phase_sel=phase_sel)
        logger.debug("phase scan freq_sel=%d hz=%d steps=%d locked=%d",
                     freq.sel, freq.hz, n_steps,
                     sum(1 for s in samples if s.point.locked))
        return PhaseScan(freq=freq, phase_sel=phase_sel, samples=samples)


def sweep(driver: EyeEngineDriver,
          freqs: Iterable[FreqProfile],
          phases: Iterable[int],
          aggressor: bool = False) -> Iterator[zs_msg.EyePoint]:
    """Yield one EyePoint per (freq, phase) grid cell, freq-outer/phase-inner."""
    phase_list = list(phases)
    for freq in freqs:
        for phase in phase_list:
            point = driver.measure(freq, phase, aggressor=aggressor)
            logger.debug("eye point freq_sel=%d phase=%d ber=%.3e locked=%s",
                         point.freq_sel, point.phase_sel, point.ber, point.locked)
            yield point


def sweep_frames(driver: EyeEngineDriver,
                 freqs: Iterable[FreqProfile],
                 phases: Iterable[int],
                 aggressor: bool = False) -> Iterator[bytes]:
    """Like sweep(), but yield each point already framed as a DOMAIN_EYE zs_msg
    ready to push down the wave_stream path."""
    for point in sweep(driver, freqs, phases, aggressor=aggressor):
        yield zs_msg.pack_eye_point(point)


# ── DS-P3.118 eye-width / max-frequency (the "unequivocal" link metric) ────
def eye_width(scan: PhaseScan, ber_threshold: float) -> EyeWidth:
    """The open eye at one frequency = the LONGEST CONTIGUOUS run of sample
    phases that both locked AND held BER <= the threshold. Collapsing the phase
    axis to a single width is what makes the result phase-independent: it does
    not matter WHERE the eye sits in the scan, only how wide it is.

    A phase that never locked (bit_count==0) breaks the run — it is eye-closed,
    never a free pass. width_ui (width_ps / UI) is the headline metric; it is
    directly comparable across frequencies and is what crosses the margin line."""
    best = cur = 0
    for s in scan.samples:
        p = s.point
        in_spec = p.locked and p.bit_count > 0 and p.ber <= ber_threshold
        cur = cur + 1 if in_spec else 0
        if cur > best:
            best = cur
    ui_steps = steps_per_ui(scan.freq.hz)
    width_ps = best * PS_STEP_PS
    width_ui = (best / ui_steps) if ui_steps else 0.0
    return EyeWidth(width_steps=best, width_ps=width_ps, width_ui=width_ui,
                    n_steps=len(scan.samples))


def max_frequency(driver: EyeEngineDriver,
                  freqs: Iterable[FreqProfile],
                  ber_threshold: float = 1e-6,
                  eye_margin_ui: float = 0.30,
                  phase_sel: int = 0,
                  aggressor: bool = False,
                  coverage_ui: float = 1.1) -> MaxFreqResult:
    """Define the MAXIMUM usable link frequency unequivocally: for each candidate
    frequency, sweep the full-UI sample phase, measure the open eye width, and
    pass the frequency iff its eye stays open >= eye_margin_ui at BER <= threshold.
    The max usable frequency is the HIGHEST passing profile.

    Every frequency is scanned (no early-stop): a real eye-width-vs-frequency
    curve can be non-monotonic near the edge, and the full curve is what the
    RouteWave 'link margin' view (LT-P3.243) plots. bug-finding-is-success: if NO
    profile passes, max_freq_hz is 0 (the link fails even at the slowest rate) —
    never a fabricated pass."""
    results: List[FreqResult] = []
    for freq in sorted(freqs, key=lambda f: f.hz):
        scan = driver.scan_phase(freq, phase_sel=phase_sel, aggressor=aggressor,
                                 coverage_ui=coverage_ui)
        width = eye_width(scan, ber_threshold)
        passed = width.width_ui >= eye_margin_ui
        results.append(FreqResult(freq=freq, width=width, passed=passed))
        logger.debug("max-freq probe hz=%d width_ui=%.3f pass=%s",
                     freq.hz, width.width_ui, passed)
    passing = [r.freq.hz for r in results if r.passed]
    return MaxFreqResult(
        max_freq_hz=max(passing) if passing else 0,
        results=results,
        ber_threshold=ber_threshold,
        eye_margin_ui=eye_margin_ui,
    )


def summary_from_result(result: MaxFreqResult) -> zs_msg.EyeSummary:
    """Convert a host MaxFreqResult into the daemon-authoritative EyeSummary that
    RouteWave's link-margin view (LT-P3.243) consumes in preference to recomputing
    f_max itself. A frequency is flagged `partial` if its scan did not cover a full
    UI (the partial-eye guard the consumer must honour — never expected with the
    DS-P3.118 full-UI scan, but carried so the consumer can trust the flag)."""
    sfreqs = []
    for r in result.results:
        ui_steps = steps_per_ui(r.freq.hz)
        partial = r.width.n_steps < ui_steps     # scan shorter than one UI
        sfreqs.append(zs_msg.EyeSummaryFreq(
            freq_hz=r.freq.hz,
            eye_width_ps=int(round(r.width.width_ps)),
            eye_width_ui=r.width.width_ui,
            passed=r.passed,
            partial=partial,
        ))
    sfreqs.sort(key=lambda f: f.freq_hz)
    return zs_msg.EyeSummary(
        max_freq_hz=result.max_freq_hz,
        ber_threshold=result.ber_threshold,
        eye_margin_ui=result.eye_margin_ui,
        freqs=sfreqs,
    )


def max_frequency_frame(driver: EyeEngineDriver,
                        freqs: Iterable[FreqProfile],
                        **kwargs) -> tuple[MaxFreqResult, bytes]:
    """Run max_frequency and also return the packed DOMAIN_EYE_SUMMARY frame ready
    to push down the wave_stream path (the LT-P3.243 link-margin view's input)."""
    result = max_frequency(driver, freqs, **kwargs)
    return result, zs_msg.pack_eye_summary(summary_from_result(result))


# Default Zybo PMOD-loopback grid: 32 IDELAY taps; frequency profiles are
# board-specific and supplied by the caller (Phase 5 wires the real clk_drp
# reconfig ROM). 50 MHz (RMII rate) is the wide-open reference point.
DEFAULT_PHASES: List[int] = list(range(32))
DEFAULT_FREQS: List[FreqProfile] = [FreqProfile(sel=0, hz=50_000_000)]
