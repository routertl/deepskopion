"""EPIC RTL-P2.719 / ticket RTL-P3.851 — point the routertl-native
AxiStreamMonitor at deepskopion's REAL wave_stream egress port (the
``zs_msg_framer`` OUT_* bus) and get AMBA AXI4-Stream conformance + corner-case
observation for free.

This hunts a DIFFERENT bus port than the already-hunted ``system_regbank``
S_AXI slave (which found RTL-T2.26). The ``zs_msg_framer`` is the datalogger DATA
PATH egress: it wraps each datalogger message into a ``zs_msg`` frame and emits it
as a 32-bit word stream into the rr-ddr-stream DDR ring. Its output
(``OUT_DATA`` / ``OUT_VALID`` / ``OUT_READY``) is a ready/valid stream MASTER —
exactly the master-output-under-backpressure axis where this campaign's defects
clustered (DDR-T2.8/T2.9, VHD-T2.7). ``OUT_VALID``→tvalid, ``OUT_READY``→tready,
``OUT_DATA``→tdata; no tlast on this bus (frames are delimited by VALID-deassert,
not tlast), so the active AMBA IHI0051 clauses are:

  AXIS_DATA_HOLD     OUT_DATA must stay stable while OUT_VALID=1 && OUT_READY=0
                     (the master may not change the offered word before the sink
                     accepts it).
  AXIS_VALID_STICKY  once OUT_VALID=1, it must stay 1 until an OUT_READY=1 accept
                     — the master may not withdraw an offered beat.
  AXIS_RST           OUT_VALID must be 0 during reset.

The monitor is the validated half (its KAT proves it catches each clause). We
bring the DUT and drive ADVERSARIAL back-pressure on OUT_READY. Any violation
here is the framer's, not the testbench's — and the non-vacuity guard proves the
green bar actually exercised the stalled-offer corner (OUT_VALID=1 & OUT_READY=0)
rather than passing trivially.

The DUT elaborates directly as ``zs_msg_framer`` (self-contained — only the
``zs_msg_pkg`` package + the framer; no XPM, no gather). The existing
``test_zs_msg_framer`` suite covers FUNCTIONAL round-trip parity vs the PS
oracle; THIS test adds the AMBA stream-PROTOCOL conformance layer on the egress
port — a genuinely different lens (handshake legality, not byte content).

Run:  rr sim run test_framer_axistream_conformance
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

import routertl_core as _rrc

sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

# routertl verification moat — the validated passive AXI-Stream monitor.
from rr_cocotb_tb.drivers.axi_stream import AxiStreamBus              # noqa: E402
from rr_cocotb_tb.monitors.axi_stream_monitor import AxiStreamMonitor  # noqa: E402

_LOG = logging.getLogger("framer_axistream_conformance")

CLK_PERIOD_NS = 10
GENERICS = {"G_PAYLOAD_WORDS": 1}
FRAME_WORDS = 5 + GENERICS["G_PAYLOAD_WORDS"]   # 5 header + N payload words


# ── A representative datalogger-message tuple (values are immaterial to the
#    stream-protocol checks — they just give the framer something to emit). ──
def _drive_tuple(dut, *, datalogger_id, msg_class, msg_type, flags, timestamp, payload):
    dut.DATALOGGER_ID.value = datalogger_id & 0xFF
    dut.MSG_CLASS.value  = msg_class & 0xFF
    dut.MSG_TYPE.value   = msg_type & 0xFFFF
    dut.FLAGS.value      = flags & 0xFFFF
    dut.TIMESTAMP.value  = timestamp & ((1 << 64) - 1)
    dut.PAYLOAD.value    = payload & 0xFFFFFFFF
    dut.IN_VALID.value   = 1


def _quiesce_inputs(dut):
    dut.IN_VALID.value = 0
    dut.OUT_READY.value = 0
    dut.DATALOGGER_ID.value = 0
    dut.MSG_CLASS.value = 0
    dut.MSG_TYPE.value = 0
    dut.FLAGS.value = 0
    dut.TIMESTAMP.value = 0
    dut.PAYLOAD.value = 0


async def _reset(dut, n_cycles=8):
    dut.RST.value = 1
    _quiesce_inputs(dut)
    await ClockCycles(dut.CLK, n_cycles)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


class _StallWitness:
    """Independent, monitor-free witness that the back-pressure stimulus
    actually coincided with an offered-but-unaccepted beat — the precondition
    the AXIS_DATA_HOLD / AXIS_VALID_STICKY clauses need to be NON-vacuous.

    Samples in ReadOnly so it sees post-edge settled values, matching the
    monitor's sampling phase. Counts:
      * accepted_beats      : OUT_VALID & OUT_READY  (real traffic happened)
      * stalled_offers      : OUT_VALID & !OUT_READY (a beat offered & stalled)
    """

    def __init__(self, dut):
        self.dut = dut
        self.accepted_beats = 0
        self.stalled_offers = 0
        self._stop = False

    async def run(self):
        while not self._stop:
            await RisingEdge(self.dut.CLK)
            await ReadOnly()
            try:
                v = int(self.dut.OUT_VALID.value)
                r = int(self.dut.OUT_READY.value)
            except ValueError:
                continue
            if v == 1 and r == 1:
                self.accepted_beats += 1
            elif v == 1 and r == 0:
                self.stalled_offers += 1

    def stop(self):
        self._stop = True


async def _emit_one_frame_with_stall(dut, tuple_kw, *, stall_after, stall_len):
    """Drive one tuple, then mid-emission hold OUT_READY low for ``stall_len``
    cycles (after ``stall_after`` accepted beats) before resuming. Exercises
    AXIS_DATA_HOLD (word held stable across the stall) + AXIS_VALID_STICKY
    (OUT_VALID stays asserted through the stall)."""
    # Ensure framer is ready (S_IDLE).
    while int(dut.IN_READY.value) != 1:
        await RisingEdge(dut.CLK)
    dut.OUT_READY.value = 1
    _drive_tuple(dut, **tuple_kw)
    await RisingEdge(dut.CLK)
    dut.IN_VALID.value = 0

    # Let `stall_after` beats drain.
    await ClockCycles(dut.CLK, stall_after)
    # Stall: hold OUT_READY low — OUT_VALID must stay high, OUT_DATA frozen.
    dut.OUT_READY.value = 0
    await ClockCycles(dut.CLK, stall_len)
    dut.OUT_READY.value = 1
    # Drain the rest of the frame.
    await ClockCycles(dut.CLK, FRAME_WORDS + 6)


# ═════════════════════════════════════════════════════════════════════════
# THE HUNT — adversarial AXI-Stream conformance on the framer egress.
# ═════════════════════════════════════════════════════════════════════════


@cocotb.test()
async def framer_egress_axistream_conformance(dut):
    """Drive ADVERSARIAL back-pressure on the framer's OUT_* stream; the
    native AxiStreamMonitor watches OUT_VALID/OUT_READY/OUT_DATA and must
    report zero AMBA AXIS violations while real beats are both offered-under-
    stall AND accepted (non-vacuous)."""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await _reset(dut)

    # ── attach the routertl-native monitor — passive, never drives. ──
    bus = AxiStreamBus(
        tvalid=dut.OUT_VALID,
        tready=dut.OUT_READY,
        tdata=dut.OUT_DATA,
    )
    mon = AxiStreamMonitor(bus, dut.CLK, dut.RST, log=_LOG,
                           rst_active_low=False, channel="framer_egress")
    cocotb.start_soon(mon.run())

    witness = _StallWitness(dut)
    cocotb.start_soon(witness.run())

    base = dict(datalogger_id=0x42, msg_class=1, msg_type=0xABCD,
                flags=0xBEEF, timestamp=0xDEADBEEF_C0FFEE12, payload=0xCAFEBABE)

    # ── Phase 1: per-word backpressure sweep. Stall after each beat index
    #    of the frame, for several stall lengths. Forces OUT_VALID to hold a
    #    word stable across a multi-cycle stall at every emission position. ──
    for stall_after in range(0, FRAME_WORDS):
        for stall_len in (1, 3, 7):
            t = dict(base, datalogger_id=(0x10 + stall_after) & 0xFF,
                     payload=(0x1000_0000 | (stall_after << 8) | stall_len))
            await _emit_one_frame_with_stall(
                dut, t, stall_after=stall_after, stall_len=stall_len)

    # ── Phase 2: single-cycle OUT_READY toggle storm across a sustained
    #    stream of frames. Every other cycle the sink refuses — the master
    #    must never withdraw an offered beat (VALID_STICKY) and must hold the
    #    offered word (DATA_HOLD). ──
    await _reset(dut)
    ready_val = 1

    async def toggle_ready():
        nonlocal ready_val
        for _ in range(FRAME_WORDS * 12):
            dut.OUT_READY.value = ready_val
            ready_val ^= 1
            await RisingEdge(dut.CLK)
        dut.OUT_READY.value = 1   # leave high to drain

    cocotb.start_soon(toggle_ready())
    # Feed frames continuously while the sink toggles.
    for n in range(6):
        while int(dut.IN_READY.value) != 1:
            await RisingEdge(dut.CLK)
        _drive_tuple(dut, **dict(base, datalogger_id=(0x80 + n) & 0xFF,
                                 timestamp=0xBEEF_0000_0000_0000 | n))
        await RisingEdge(dut.CLK)
        dut.IN_VALID.value = 0
        # Allow ample cycles for this frame to drain through the toggling sink.
        await ClockCycles(dut.CLK, FRAME_WORDS * 3)
    dut.OUT_READY.value = 1
    await ClockCycles(dut.CLK, FRAME_WORDS + 8)

    # NB: reset-during-mid-emission (the AXIS_RST defect, DS-T2.47) is NOT driven
    # here — it lives in its own expect_fail reproducer below so a real, ticketed
    # DUT defect never weakens this clean conformance verdict. This test stresses
    # every other adversarial back-pressure vector and the egress is conformant
    # on all of them (DATA_HOLD + VALID_STICKY hold under per-word stalls and the
    # single-cycle ready toggle storm).

    witness.stop()

    # ── VERDICT: conformance + non-vacuous coverage ──────────────────
    _LOG.info(mon.report())
    _LOG.info("observed corner-cases: %s", mon.observations())
    _LOG.info("witness: accepted_beats=%d stalled_offers=%d",
              witness.accepted_beats, witness.stalled_offers)

    assert mon.violation_count == 0, (
        f"DUT framer egress committed AMBA AXI-Stream violation(s):\n{mon.report()}\n"
        f"observations={mon.observations()}"
    )

    # Non-vacuous: real beats were accepted AND the back-pressure stimulus
    # actually coincided with offered-but-stalled beats (so DATA_HOLD /
    # VALID_STICKY had something to check). Without the stalled_offers proof
    # the clean verdict could be trivially vacuous.
    assert witness.accepted_beats >= FRAME_WORDS, (
        f"vacuous: only {witness.accepted_beats} accepted beats — the framer "
        f"emitted no real stream traffic; the demo proved nothing."
    )
    assert witness.stalled_offers >= FRAME_WORDS, (
        f"vacuous: only {witness.stalled_offers} stalled-offer cycles "
        f"(OUT_VALID=1 & OUT_READY=0) — the back-pressure never coincided with "
        f"an offered beat, so AXIS_DATA_HOLD / AXIS_VALID_STICKY were never "
        f"exercised. Stimulus is not adversarial."
    )


@cocotb.test()
async def reset_during_offered_beat_is_axis_violation(dut):
    """DS-T2.47 REGRESSION GUARD (was the defect reproducer) — the framer egress
    must hold OUT_VALID LOW throughout RST (AMBA IHI0051 AXIS_RST / §2.7.1 —
    tvalid must be 0 during reset).

    ROOT CAUSE (proven by reading the RTL + monitor evidence): OUT_VALID is purely
    combinational off the FSM state — ``OUT_VALID <= '1' when state_r = S_EMIT``
    — and ``state_r`` only returns to S_IDLE on the SYNCHRONOUS reset edge
    (``if RST='1' then state_r <= S_IDLE`` inside the clocked p_fsm). So when RST
    asserts while a frame is mid-emission with the sink stalled, OUT_VALID stays
    HIGH until the next rising edge clocks state_r to S_IDLE — i.e. it is high for
    the first cycle of reset. This is the SAME clause RTL-T2.26 was filed under on
    the regbank slave; there the fix was a combinational mask
    (``S_AXI_BVALID <= b_valid_r and not RST``). The framer has no such mask.

    FIXED (mirrors RTL-T2.26): zs_msg_framer.vhd now masks the output with reset —
    ``OUT_VALID <= '1' when (state_r = S_EMIT and RST = '0') else '0'`` — so VALID
    is forced low THROUGHOUT reset, not just after the first sync edge. This was
    @cocotb.test(expect_fail=True) while the unmasked combinational assignment
    shipped; the unweakened AMBA assertion was kept intact and now PASSES with the
    mask in place — the auto-flip the expect_fail discipline guarantees. Permanent
    conformance guard."""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await _reset(dut)

    bus = AxiStreamBus(tvalid=dut.OUT_VALID, tready=dut.OUT_READY, tdata=dut.OUT_DATA)
    mon = AxiStreamMonitor(bus, dut.CLK, dut.RST, log=_LOG,
                           rst_active_low=False, channel="framer_egress_rst")
    cocotb.start_soon(mon.run())

    # Drive a frame into S_EMIT, drain 2 beats, then STALL the sink so OUT_VALID
    # is held HIGH (state_r holds in S_EMIT). Assert RST while OUT_VALID is high.
    while int(dut.IN_READY.value) != 1:
        await RisingEdge(dut.CLK)
    dut.OUT_READY.value = 1
    _drive_tuple(dut, datalogger_id=0xF0, msg_class=1, msg_type=0xABCD,
                 flags=0xBEEF, timestamp=0xDEADBEEF_C0FFEE12, payload=0xCAFEBABE)
    await RisingEdge(dut.CLK)
    dut.IN_VALID.value = 0
    await ClockCycles(dut.CLK, 2)        # 2 beats drained — mid-frame
    dut.OUT_READY.value = 0              # stall: OUT_VALID stays HIGH (offered beat)
    await ClockCycles(dut.CLK, 3)
    dut.RST.value = 1                    # reset while offering an unaccepted beat
    await ClockCycles(dut.CLK, 6)        # AMBA: OUT_VALID must be 0 throughout here
    dut.RST.value = 0
    dut.OUT_READY.value = 1
    await ClockCycles(dut.CLK, FRAME_WORDS + 6)

    _LOG.info(mon.report())

    assert mon.observed("AXIS_RST") == 0, (
        f"DS-T2.47: AxiStreamMonitor flagged OUT_VALID-during-reset. OUT_VALID is "
        f"unmasked combinational off state_r (`OUT_VALID <= '1' when state_r = "
        f"S_EMIT`), and state_r only clears on the SYNCHRONOUS reset edge — so "
        f"OUT_VALID stays high the first cycle of RST. Mask it with `and RST='0'` "
        f"(cf. RTL-T2.26 on the regbank slave).\n{mon.report()}"
    )


# ─────────────────────────────────────────────────────────────────────────
#  Standalone Execution (ROUTERTL-001 — engine.simulation.run_simulation only)
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from engine.simulation import run_simulation

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
    DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

    run_simulation(
        top_level="zs_msg_framer",
        module="test_framer_axistream_conformance",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/zs_msg_pkg.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/zs_msg_framer/zs_msg_framer.vhd",
            ],
        },
    )
