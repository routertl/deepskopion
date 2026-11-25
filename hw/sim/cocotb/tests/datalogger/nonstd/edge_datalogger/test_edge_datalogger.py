# rr unknown-llm (AI-generated)


import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer, ClockCycles, ReadOnly

# Locate routertl SDK cocotb dir via the pip-installed routertl_core
# (DS-P2.29 fallout — old shim walked up the tree for an `engine/`
# folder that no longer exists post-pip-decouple).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
    from engine.paths import SRC_PATH, SDK_SRC_PATH


except ImportError:
    print("Error: Could not import engine. Make sure the script is running in the correct environment.")
    sys.exit(1)

from sdk.cocotb_helpers import requires
from hw.sim.portable_fifo_sources import olo_fifo_sources

# Deepskopion project root
DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"


# entity edge_datalogger is
#   generic (
#     G_ED_ID_WIDTH     : integer                                     := 5;
#     G_ED_DEV_ID       : natural range 0 to (2 ** G_ED_ID_WIDTH) - 1 := 28;
#     G_TIMESTAMP_WIDTH : natural range 16 to 32                      := 16;
#     G_CONFIG_WIDTH    : integer                                     := 16;
#     G_ERROR_WIDTH     : integer                                     := 2;
#     G_ENABLE_INFILTER : integer                                     := 0;
#     G_ILA_EN          : integer                                     := 0
#   );
#   port (
#     CLK       : in std_logic;
#     RST       : in std_logic;
#     EDGE_IN   : in std_logic;
#     CONFIG    : in std_logic_vector(G_CONFIG_WIDTH - 1 downto 0);
#     TIMESTAMP : in std_logic_vector(G_TIMESTAMP_WIDTH - 1 downto 0);
#     -- QSPI
#     ED_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0) := (others => '0');
#     ED_MSG_VLD : out std_logic;
#     ED_MSG_RDY : in std_logic;
#     --
#     ED_ERRORS : out std_logic_vector(G_ERROR_WIDTH - 1 downto 0)
#   );
# end edge_datalogger;

# Default generics - modify as needed for your test
GENERICS = {
    "G_ED_ID_WIDTH": 5,
    "G_ED_DEV_ID": 28,
    "G_TIMESTAMP_WIDTH": 16,
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_ENABLE_INFILTER": 0,
    "G_ILA_EN": 0,
    # DS-P3.117 max-freq tracker. MIN_INTERVAL is the glitch floor: an
    # edge-to-edge interval strictly below this many clocks is rejected.
    "G_MAXFREQ_WIDTH": 24,
    "G_MAXFREQ_MIN_INTERVAL": 4,
}

# Hard-coded mirror of the GENERICS above for the in-test expected values
# (ROUTERTL-002: assert against constants, not values re-derived at runtime).
MAXFREQ_MIN_INTERVAL = 4
MAXFREQ_ALLONES = (1 << 24) - 1   # G_MAXFREQ_WIDTH all-ones sentinel

def main():
    """Run simulation for edge_datalogger"""
    run_simulation(
        top_level="edge_datalogger",
        module="test_edge_datalogger",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/edge_detector/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                # DS-T2.43: edge_datalogger now instantiates tich.datalogger_lifecycle
                # for the uniform ENABLED/DISABLED frame — must precede it.
                f"{DEEPSKOPION_RTL}/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/nonstd/edge_datalogger/edge_datalogger.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
    )

@cocotb.test()
@requires("EDGE-REQ-50")
async def test_reset_determinism(dut):
    """EDGE-REQ-50: post-reset, ED_MSG_VLD stays low during the idle
    window (no transitions on EDGE_IN). ED_ERRORS is now DRIVEN (DS-P3.24),
    so it must read a clean 0 (no overflow) throughout the idle window.
    """
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())

    # Hold reset active (active-high) + steady line
    dut.RST.value = 1
    dut.EDGE_IN.value = 0
    dut.ED_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 8)

    # Under reset: VLD low.
    assert int(dut.ED_MSG_VLD.value) == 0, (
        f"ED_MSG_VLD must be 0 under RST, got {int(dut.ED_MSG_VLD.value)}"
    )

    # Release reset; EDGE_IN held steady — no transitions, datalogger
    # must remain quiet.
    dut.RST.value = 0
    await RisingEdge(dut.CLK)
    await ClockCycles(dut.CLK, 200)

    assert int(dut.ED_MSG_VLD.value) == 0, (
        "ED_MSG_VLD pulsed after RST de-assert with EDGE_IN steady"
    )
    assert int(dut.ED_ERRORS.value) == 0, (
        f"ED_ERRORS must be 0 with no traffic, got {int(dut.ED_ERRORS.value)}"
    )

    dut._log.info("EDGE-REQ-50 reset-determinism check passed.")


# device_id=28 in CONFIG[15:11], control_code "01" (enable edge capturing) in [1:0]
_CFG_ENABLE = (28 << 11) | 0b01


async def _reset_and_enable(dut, rdy):
    """Reset, wait out the XPM FIFO RST_BUSY (writes are gated on it), then enable
    edge capturing. Without the RST_BUSY wait, fifoEdWrEn stays 0 and nothing is
    ever captured."""
    dut.RST.value = 1
    dut.EDGE_IN.value = 0
    dut.ED_MSG_RDY.value = rdy
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 64)        # FIFO reset-busy guard clears
    dut.CONFIG.value = _CFG_ENABLE
    await ClockCycles(dut.CLK, 4)


async def _toggle_edges(dut, n):
    """Drive n edges (one EDGE_IN transition per clock = one detected edge)."""
    for _ in range(n):
        dut.EDGE_IN.value = 1 - int(dut.EDGE_IN.value)
        await RisingEdge(dut.CLK)


@cocotb.test()
@requires("EDGE-REQ-51")
async def test_no_false_overflow_while_draining(dut):
    """EDGE-REQ-51 (DS-P3.24): with the consumer draining (ED_MSG_RDY=1), the
    internal FIFO never fills, so ED_ERRORS must stay 0 — no false overflow."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset_and_enable(dut, rdy=1)   # consumer keeps draining

    seen = 0
    for _ in range(40):              # plenty of edges, but drained each cycle
        dut.EDGE_IN.value = 1 - int(dut.EDGE_IN.value)
        await RisingEdge(dut.CLK)
        seen |= int(dut.ED_ERRORS.value) & 0x1
    assert seen == 0, "ED_ERRORS asserted while the FIFO was being drained (false overflow)"
    dut._log.info("EDGE-REQ-51 no-false-overflow check passed.")


@cocotb.test()
@requires("EDGE-REQ-52")
async def test_internal_fifo_overflow_counts_dropped_edges(dut):
    """EDGE-REQ-52 (DS-P3.24): the canonical fabric-rate overflow. With NO drain
    (ED_MSG_RDY=0) the internal FIFO fills, then each further edge is DROPPED and
    ED_ERRORS[0] pulses exactly once per dropped edge — so the lost-edge count is
    exact (the gather sums these into ERR_CNT_EDGE)."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset_and_enable(dut, rdy=0)   # NO drain → FIFO will overflow

    # The internal FIFO has G_FIFO_DEPTH=8 → 2**8 = 256 words. Fill
    # PAST that (no drain) so it is genuinely FULL before the precise count window.
    await _toggle_edges(dut, 300)
    assert int(dut.ED_MSG_VLD.value) == 1, "FIFO never captured an edge (not filling)"
    assert int(dut.fifoEdFull.value) == 1, "FIFO did not reach FULL after 300 undrained edges"
    # Hold the line steady so any in-flight overflow pulse settles before the window.
    await ClockCycles(dut.CLK, 6)
    assert int(dut.ED_ERRORS.value) & 0x1 == 0, "overflow pulse did not settle before the window"

    # Precise window: M more edges into the (still-full, undrained) FIFO. Each
    # MUST drop → exactly M ED_ERRORS[0] pulses over the window + register latency.
    M = 16
    drops = 0
    for _ in range(M):
        dut.EDGE_IN.value = 1 - int(dut.EDGE_IN.value)
        await RisingEdge(dut.CLK)
        drops += int(dut.ED_ERRORS.value) & 0x1
    for _ in range(3):               # flush the 1-cycle-registered tail pulses
        await RisingEdge(dut.CLK)
        drops += int(dut.ED_ERRORS.value) & 0x1

    assert drops == M, f"expected exactly {M} dropped-edge pulses, counted {drops}"
    dut._log.info(f"EDGE-REQ-52 overflow lost-count exact: {drops} drops == {M} edges.")


# device_id=28 in CONFIG[15:11], control_code "01", plus the re-arm bit (CONFIG[2]).
_CFG_ENABLE_REARM = (28 << 11) | (1 << 2) | 0b01


async def _drive_periodic(dut, half_period, n_edges):
    """Toggle EDGE_IN every `half_period` clocks `n_edges` times.

    Returns the list of (ED_MAX_INTERVAL, ED_MAX_NEW) sampled once per clock
    over the whole drive window, so a test can assert both the final latch and
    the exact cycles ED_MAX_NEW pulsed.
    """
    trace = []
    for _ in range(n_edges):
        dut.EDGE_IN.value = 1 - int(dut.EDGE_IN.value)
        for _ in range(half_period):
            await RisingEdge(dut.CLK)
            trace.append((int(dut.ED_MAX_INTERVAL.value),
                          int(dut.ED_MAX_NEW.value)))
    return trace


@cocotb.test()
@requires("EDGE-REQ-53")
async def test_maxfreq_periodic_line(dut):
    """EDGE-REQ-53 (DS-P3.117): a periodic line at a known half-period H drives
    ED_MAX_INTERVAL to exactly H, and ED_MAX_NEW pulses when it is first latched.
    The first edge only opens the window (no latch)."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset_and_enable(dut, rdy=1)

    # Sentinel before any measurement: all-ones (= "no max yet").
    assert int(dut.ED_MAX_INTERVAL.value) == MAXFREQ_ALLONES, (
        f"ED_MAX_INTERVAL must start all-ones, got "
        f"{int(dut.ED_MAX_INTERVAL.value):#x}")

    H = 8                                  # half-period in clocks (>= floor 4)
    trace = await _drive_periodic(dut, half_period=H, n_edges=10)
    await ClockCycles(dut.CLK, 2)

    final = int(dut.ED_MAX_INTERVAL.value)
    assert final == H, (
        f"ED_MAX_INTERVAL must equal the half-period {H}, got {final}")
    # ED_MAX_NEW pulsed at least once (the first time H was latched) and the
    # FIRST edge did NOT latch (sentinel survived past it).
    pulses = sum(new for _iv, new in trace)
    assert pulses >= 1, "ED_MAX_NEW never pulsed for a clean periodic line"
    # First edge opens window only: the interval is never latched as H+something
    # spurious — the only latched value across the run is exactly H.
    latched_vals = {iv for iv, new in trace if new == 1}
    assert latched_vals == {H}, (
        f"ED_MAX_NEW latched unexpected interval(s): {latched_vals} (want {{{H}}})")
    dut._log.info(f"EDGE-REQ-53 periodic-line max-interval == {final} clocks.")


@cocotb.test()
@requires("EDGE-REQ-54")
async def test_maxfreq_runt_glitch_rejected(dut):
    """EDGE-REQ-54 (DS-P3.117): a single SUB-FLOOR runt interval must NOT latch a
    spurious high frequency. This is the heart of the ticket — one runt pulse
    must not stick a false max forever."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset_and_enable(dut, rdy=1)

    # Establish a legitimate slow max first.
    H_SLOW = 12
    await _drive_periodic(dut, half_period=H_SLOW, n_edges=6)
    await ClockCycles(dut.CLK, 2)
    before = int(dut.ED_MAX_INTERVAL.value)
    assert before == H_SLOW, f"setup: expected max-interval {H_SLOW}, got {before}"

    # Inject a single RUNT: two edges only (MAXFREQ_MIN_INTERVAL-1) clocks apart —
    # strictly below the glitch floor. A naive running-min would latch this.
    runt = MAXFREQ_MIN_INTERVAL - 1
    assert runt >= 1
    saw_new = 0
    dut.EDGE_IN.value = 1 - int(dut.EDGE_IN.value)        # edge A (opens runt)
    for _ in range(runt):
        await RisingEdge(dut.CLK)
        saw_new |= int(dut.ED_MAX_NEW.value)
    dut.EDGE_IN.value = 1 - int(dut.EDGE_IN.value)        # edge B (closes runt)
    for _ in range(3):
        await RisingEdge(dut.CLK)
        saw_new |= int(dut.ED_MAX_NEW.value)

    after = int(dut.ED_MAX_INTERVAL.value)
    assert after == H_SLOW, (
        f"runt glitch latched a false max: {after} (must stay {H_SLOW})")
    assert saw_new == 0, "ED_MAX_NEW pulsed for a sub-floor runt (false max)"
    dut._log.info("EDGE-REQ-54 sub-floor runt rejected — no false max latched.")


@cocotb.test()
@requires("EDGE-REQ-55")
async def test_maxfreq_rearm_clears_stale_latch(dut):
    """EDGE-REQ-55 (DS-P3.117): the host re-ping. After a FAST line latches a
    small interval, toggling CONFIG[2] clears the latch; a subsequent SLOWER line
    then reports the new larger min-interval — proving the latch is not stuck."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset_and_enable(dut, rdy=1)

    # Fast line first → small interval.
    H_FAST = 5
    await _drive_periodic(dut, half_period=H_FAST, n_edges=8)
    await ClockCycles(dut.CLK, 2)
    fast = int(dut.ED_MAX_INTERVAL.value)
    assert fast == H_FAST, f"fast setup: expected {H_FAST}, got {fast}"

    # Host RE-PING: rising edge on CONFIG[2] (re-arm bit), keep enabled. Allow
    # config_control's 2-cycle register latency (config_r -> devConfiguration_r)
    # before the re-arm pulse reaches the tracker.
    dut.CONFIG.value = _CFG_ENABLE_REARM
    await ClockCycles(dut.CLK, 6)
    assert int(dut.ED_MAX_INTERVAL.value) == MAXFREQ_ALLONES, (
        "re-arm did not clear the latched max back to the all-ones sentinel")
    # Drop the re-arm bit (back to plain enable) so the next rising edge re-arms.
    dut.CONFIG.value = _CFG_ENABLE
    await ClockCycles(dut.CLK, 3)

    # Now a SLOWER line. Without re-arm this could never beat the old fast latch,
    # so observing the larger interval proves the latch was genuinely cleared.
    H_SLOW = 11
    assert H_SLOW > H_FAST
    await _drive_periodic(dut, half_period=H_SLOW, n_edges=8)
    await ClockCycles(dut.CLK, 2)
    slow = int(dut.ED_MAX_INTERVAL.value)
    assert slow == H_SLOW, (
        f"after re-arm the SLOWER line must report {H_SLOW}, got {slow} "
        f"(stale fast latch {H_FAST} not cleared)")
    dut._log.info(f"EDGE-REQ-55 re-arm: {H_FAST} -> sentinel -> {H_SLOW} (escaped stale latch).")


@cocotb.test()
@requires("EDGE-REQ-56")
async def test_maxfreq_event_driven_and_idle_safe(dut):
    """EDGE-REQ-56 (DS-P3.117): ED_MAX_NEW is event-driven (pulses only on a step
    to a smaller interval, never on equal/larger), and a long idle gap must not
    forge a small interval (the interval counter saturates, not wraps)."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset_and_enable(dut, rdy=1)

    # Decreasing intervals: 14, 14, 9, 9, 6, 6 → exactly 3 NEW pulses (14, 9, 6).
    # Equal repeats must NOT re-pulse; larger steps later must NOT pulse.
    new_count = 0
    latched_steps = []
    for H in (14, 14, 9, 9, 6, 6, 20, 20):    # 20s are SLOWER → must not latch
        for _ in range(2):                     # two edges per H to close one interval
            dut.EDGE_IN.value = 1 - int(dut.EDGE_IN.value)
            for _ in range(H):
                await RisingEdge(dut.CLK)
                if int(dut.ED_MAX_NEW.value) == 1:
                    new_count += 1
                    latched_steps.append(int(dut.ED_MAX_INTERVAL.value))

    assert latched_steps == [14, 9, 6], (
        f"ED_MAX_NEW must pulse exactly on the decreasing steps 14,9,6 — got "
        f"{latched_steps}")
    assert int(dut.ED_MAX_INTERVAL.value) == 6, (
        f"final max-interval must be the smallest seen (6), got "
        f"{int(dut.ED_MAX_INTERVAL.value)}")

    # Idle-gap safety: hold the line steady for a long gap, much longer than the
    # latched interval. No edge → no new latch, no wrap-induced false small value.
    await ClockCycles(dut.CLK, 400)
    assert int(dut.ED_MAX_NEW.value) == 0, "ED_MAX_NEW pulsed during idle gap"
    assert int(dut.ED_MAX_INTERVAL.value) == 6, (
        "idle gap corrupted the latched max (counter wrapped instead of saturating)")
    dut._log.info("EDGE-REQ-56 event-driven + idle-saturation verified.")


# ── DS-T2.84: lifecycle frames and coincident edges must not alias a readback ──
#
# A consumer reads an S=1 word as "the line is at level E" (edge_capture.py
# decode_edge_word, RouteWave EdgeStreams). On silicon (DS-P1.71, 2026-10-02)
# two other words reached EDGE_EVENTS in that shape: the DS-T2.43 lifecycle
# frame (type "100" == S=1,F=0,E=0) and the deferred word of an edge that
# coincided with a read (S=1, E = not EDGE_IN). Both read as "line is low" on
# an idle-high CS. The tests below judge the words the way that consumer does.

_DEV_ID = 28
_CFG_DISABLE = (_DEV_ID << 11) | 0b00
_CFG_ENABLE_READ = (_DEV_ID << 11) | 0b10    # enable + read trigger (capture on)
_LIFECYCLE_ENABLED = 0x03                    # edge_datalogger C_ED_EVENT_ENABLED
_LIFECYCLE_DISABLED = 0x00                   # edge_datalogger C_ED_EVENT_DISABLED


def _lifecycle_flag(dut) -> int:
    """ED_MSG_LIFECYCLE, or 0 on a DUT that has no such port: a DUT without the
    flag flags nothing, so every word it emits is judged as data."""
    try:
        return int(dut.ED_MSG_LIFECYCLE.value)
    except AttributeError:
        return 0


def _s(word):
    return (word >> 26) & 1


def _e(word):
    return (word >> 24) & 1


def _ts24(word):
    return word & 0xFFFFFF


async def _drive_timestamp(dut):
    """Free-running TIMESTAMP, one tick per clock, so word timestamps are real."""
    ts = 0
    while True:
        await RisingEdge(dut.CLK)
        ts = (ts + 1) & 0xFFFF
        dut.TIMESTAMP.value = ts


async def _collect(dut, sink, stop):
    """Record every word the consumer accepts: (word, ED_MSG_LIFECYCLE,
    internal lifecycleVld, ReadEvent_r)."""
    while not stop[0]:
        await RisingEdge(dut.CLK)
        await ReadOnly()
        rd_r = int(dut.ReadEvent_r.value)
        if int(dut.ED_MSG_VLD.value) == 1 and int(dut.ED_MSG_RDY.value) == 1:
            sink.append((int(dut.ED_MSG_OUT.value), _lifecycle_flag(dut),
                         int(dut.lifecycleVld.value), rd_r))
        elif rd_r:
            sink.append((None, 0, 0, rd_r))


async def _reset_idle(dut, level):
    """Reset with the line idle at `level` and the logger disabled; wait out the
    FIFO reset-busy window so writes are not gated."""
    dut.RST.value = 1
    dut.EDGE_IN.value = level
    dut.ED_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 64)


@cocotb.test()
@requires("EDGE-REQ-57")
async def test_lifecycle_frame_is_not_a_readback(dut):
    """EDGE-REQ-57 (DS-T2.84): enable and disable an idle-HIGH line with no read
    requested. The ENABLED and DISABLED frames must come out flagged on
    ED_MSG_LIFECYCLE, and no unflagged S=1 word may appear — with no read there
    is no readback, so an unflagged S=1 word is a marker posing as "line is low"."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset_idle(dut, level=1)
    cocotb.start_soon(_drive_timestamp(dut))
    words, stop = [], [False]
    cocotb.start_soon(_collect(dut, words, stop))

    dut.CONFIG.value = _CFG_ENABLE          # enable capture, no read
    await ClockCycles(dut.CLK, 20)
    dut.CONFIG.value = _CFG_DISABLE
    await ClockCycles(dut.CLK, 20)
    stop[0] = True
    await ClockCycles(dut.CLK, 2)

    emitted = [(w, flag) for w, flag, _lv, _rd in words if w is not None]
    as_readback = [w for w, flag in emitted if flag == 0 and _s(w) == 1]
    assert not as_readback, (
        "lifecycle frame reached the wire as an S=1 readback: "
        f"{[hex(w) for w in as_readback]} decode as 'line is "
        f"{[_e(w) for w in as_readback]}' while the line is held 1 and no read "
        f"was requested (all words: {[(hex(w), f) for w, f in emitted]})")
    flagged = [w for w, flag in emitted if flag == 1]
    assert [w & 0xFF for w in flagged] == [_LIFECYCLE_ENABLED, _LIFECYCLE_DISABLED], (
        f"expected flagged ENABLED then DISABLED frames, got {[hex(w) for w in flagged]}")
    assert all(w >> 27 == _DEV_ID for w in flagged), (
        f"lifecycle frame lost its device id: {[hex(w) for w in flagged]}")
    dut._log.info("EDGE-REQ-57: lifecycle frames flagged, none decodes as a readback.")


@cocotb.test()
@requires("EDGE-REQ-58")
async def test_coincident_edge_is_an_edge_not_a_readback(dut):
    """EDGE-REQ-58 (DS-T2.84): a real edge lands on the same cycle as a read.
    The readback takes that cycle's write and the edge is written one cycle
    later. The consumer must see exactly one readback (S=1, the new level) and
    the edge as an S=0 event with the polarity and timestamp of the cycle it was
    detected — not a second S=1 word reporting the OLD level."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset_and_enable(dut, rdy=1)          # line 0, capture enabled
    cocotb.start_soon(_drive_timestamp(dut))
    await ClockCycles(dut.CLK, 10)
    words, stop = [], [False]
    cocotb.start_soon(_collect(dut, words, stop))

    # Request a read and raise the line on the cycle the read trigger rises, so
    # both edge detectors fire on the same clock (ReadEvent and edgeDetected).
    dut.CONFIG.value = _CFG_ENABLE_READ
    for _ in range(10):
        await RisingEdge(dut.CLK)
        await ReadOnly()
        if int(dut.triggerRead.value) == 1:
            break
    await Timer(1, unit="ns")
    dut.EDGE_IN.value = 1
    await ClockCycles(dut.CLK, 20)
    stop[0] = True
    await ClockCycles(dut.CLK, 2)

    assert any(rd for _w, _f, _lv, rd in words), (
        "stimulus did not make the edge coincide with the read (ReadEvent_r never fired)")
    data = [w for w, _f, lv, _rd in words if w is not None and lv == 0]
    readbacks = [w for w in data if _s(w) == 1]
    assert len(readbacks) == 1 and _e(readbacks[0]) == 1, (
        "coincident-edge word decodes as a level readback: one read was requested "
        f"and the line is now 1, but the S=1 words are {[hex(w) for w in readbacks]} "
        f"(levels {[_e(w) for w in readbacks]})")
    edges = [w for w in data if _s(w) == 0]
    assert len(edges) == 1 and _e(edges[0]) == 0, (
        "coincident edge was not delivered as one S=0 rising-edge event: "
        f"{[hex(w) for w in edges]}")
    assert _ts24(edges[0]) == _ts24(readbacks[0]), (
        f"deferred edge lost its detection timestamp: edge ts {_ts24(edges[0]):#x} "
        f"!= readback ts {_ts24(readbacks[0]):#x} (both were detected on one cycle)")
    dut._log.info("EDGE-REQ-58: coincident edge delivered as an S=0 edge, one readback.")


@cocotb.test()
@requires("EDGE-REQ-57", "EDGE-REQ-58")
async def test_ds_p1_71_sparse_enable_replay(dut):
    """DS-P1.71 sparse phase, CS lane (DS-T2.84): the logger was armed on a low
    line, disarmed, the mux moved so the line now idles HIGH, then it was
    re-armed with CONFIG 10. Silicon emitted ENABLED (S=1,E=0), the readback
    (S=1,E=1) and a coincident-edge word (S=1,E=0). A consumer that takes the
    level from S=1 words drew CS low. Judged as that consumer: the only S=1
    level word is the readback, and the level it rebuilds ends at 1."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset_idle(dut, level=0)
    cocotb.start_soon(_drive_timestamp(dut))
    words, stop = [], [False]
    cocotb.start_soon(_collect(dut, words, stop))

    dut.CONFIG.value = _CFG_ENABLE
    await ClockCycles(dut.CLK, 20)
    dut.CONFIG.value = _CFG_DISABLE
    await ClockCycles(dut.CLK, 20)
    dut.EDGE_IN.value = 1                       # mux moved while the logger was off
    await ClockCycles(dut.CLK, 20)
    dut.CONFIG.value = _CFG_ENABLE_READ
    await ClockCycles(dut.CLK, 30)
    stop[0] = True
    await ClockCycles(dut.CLK, 2)

    emitted = [(w, flag) for w, flag, _lv, _rd in words if w is not None]
    level_words = [w for w, flag in emitted if flag == 0 and _s(w) == 1]
    assert [_e(w) for w in level_words] == [1], (
        "S=1 level words on the re-arm of an idle-high line must be exactly the "
        f"one readback of 1, got {[hex(w) for w in level_words]} "
        f"(all words: {[(hex(w), f) for w, f in emitted]})")
    flagged = [w & 0xFF for w, flag in emitted if flag == 1]
    assert flagged == [_LIFECYCLE_ENABLED, _LIFECYCLE_DISABLED, _LIFECYCLE_ENABLED], (
        f"expected ENABLED, DISABLED, ENABLED lifecycle frames, got {flagged}")
    level = None
    for w, flag in emitted:
        if flag:
            continue
        level = _e(w) if _s(w) else 1 - _e(w)
    assert level == 1, f"consumer-rebuilt CS level after re-arm is {level}, want 1"
    dut._log.info("DS-P1.71 sparse replay: only the true readback is an S=1 level word.")


if __name__ == "__main__":
    main()
