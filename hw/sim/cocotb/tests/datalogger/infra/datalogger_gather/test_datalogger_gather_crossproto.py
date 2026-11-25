# rr unknown-llm (AI-generated)
"""Cross-protocol adversarial coverage for datalogger_gather (DS-P3.31).

The pre-existing suites attack the gather one protocol-domain at a time:

  * ``test_datalogger_gather.py``        — SGA-REQ-1..10, but every traffic test
                                        drives **EDGE only** (the EDGE-group
                                        4-way concurrent case for SGA-REQ-4,
                                        and the EDGE-only backpressure case for
                                        SGA-REQ-8).
  * ``test_datalogger_gather_nonedge.py``— SGA-REQ-11/12/13 tag correctness, but
                                        each drives a **single** non-EDGE
                                        protocol in isolation.

So the TOP burst arbiter's behaviour when **multiple protocol GROUPS present
valids at once** — the exact round-robin-across-groups + tagged-tuple-routing
surface the gather exists to serialise — has had ZERO coverage. The
requirements.yml says so explicitly: SGA-REQ-4 "cross-protocol (I2C + UART +
SPI + EDGE) is a follow-on with the same shape"; SGA-REQ-8 "hold DOUT_REQ low
while EVERY protocol generates traffic".

This file is that follow-on. It drives EDGE + I2C + SPI **simultaneously**
through the one shared CAPTURE_PINS bus and attacks the top arbiter:

  X1 (SGA-REQ-4)  concurrent tag-consistency + no-drop-vs-solo. The headline
                  invariant the single-domain tests CANNOT catch: every
                  captured event's (datalogger_id, domain) pair must stay matched
                  to the SAME source through the arbiter. A tag-scramble would
                  pair source A's payload with source B's msg_type — invisible
                  when only one domain is in flight.
  X2 (SGA-REQ-8)  cross-protocol backpressure: DOUT_REQ=0 while all three
                  stream; the burst FIFO must fill, then drain cleanly with no
                  alien IDs and every source represented.
  X3 (SGA-REQ-4)  EDGE-flood starvation: hammer EDGE0 at max rate while I2C0 +
                  SPI0 each send a few bytes; the slow protocols MUST still get
                  their full solo event count through — a priority-not-rotating
                  top arbiter would starve them behind the EDGE flood.

Bug-finding-is-success: a green run here is a yellow flag. Every surfaced
defect emits a T-ticket-ready diagnostic and is filed, never masked
(file-don't-fix).

UART is deliberately out of the concurrent mix: its datalogger needs ~70 bytes of
auto-calibration (~325k cycles) before it emits anything, so 4-way concurrency
is a heavy follow-on; the three bit-bang-fast protocols are the meaningful
attack on the arbiter today.
"""

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc  # noqa: F401
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
try:
    from engine.simulation import run_simulation
except ImportError:
    print("Error: import engine in the rr sim environment.")
    sys.exit(1)

from sdk.cocotb_helpers import requires

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT / "hw" / "sim" / "cocotb"))   # ds_sim_sources
from ds_sim_sources import scoped_libraries   # noqa: E402

GENERICS = {"G_SYS_CLK": 100_000_000}
PERIOD_NS = 10

# CAPTURE_PINS bit indices (datalogger_config_pkg) — same as the sibling suites.
I2C0_SCL, I2C0_SDA = 12, 13
SPI0_CS, SPI0_MOSI, SPI0_MISO, SPI0_SCLK = 20, 21, 22, 23
EDGE0_PIN = 42

# Device IDs (datalogger_config_pkg).
I2C0_ID, SPI0_ID, EDGE0_ID = 4, 8, 1
# DS-P1.104 default mix: no JTAG logger; UART0 and CAN0 are the other active families.
UART0_ID, CAN0_ID = 12, 20
# ZS domain nibbles (zs_msg_pkg) — msg_type = sev[15:12]|domain[11:8]|evt[7:0].
DOM_I2C, DOM_SPI, DOM_EDGE = 2, 3, 6

# Canonical (datalogger_id -> expected domain) map. The whole point of X1/X2/X3:
# under concurrent multi-domain load these pairs must NEVER cross.
ID_TO_DOMAIN = {I2C0_ID: DOM_I2C, SPI0_ID: DOM_SPI, EDGE0_ID: DOM_EDGE}

# Bus-idle pin levels (I2C/UART/SPI high-Z pulled high; EDGE pins low).
IDLE = (
    (1 << 12) | (1 << 13) | (1 << 14) | (1 << 15)
    | (1 << 20) | (1 << 21) | (1 << 22) | (1 << 23)
    | (1 << 36) | (1 << 37)
)

P = 30   # cycles per I2C/SPI half-phase (clears the glitch_filter debounce)


# ── Shared bus model ────────────────────────────────────────────────────────
# The single-domain BFMs each rebuild the whole CAPTURE_PINS word from a local
# IDLE copy, so they CANNOT run concurrently (each clobbers the others' bits).
# This shared model keeps one live word; every driver flips only its own bit(s)
# and writes the whole word. Race-free under cocotb's cooperative scheduler —
# each set() is atomic between awaits.
class SharedBus:
    def __init__(self, sig, word):
        self._sig = sig
        self.word = word
        self._sig.value = word

    def set(self, bit, val):
        if val:
            self.word |= (1 << bit)
        else:
            self.word &= ~(1 << bit)
        self._sig.value = self.word


# ── Bus-aware BFMs ──────────────────────────────────────────────────────────
async def i2c_byte(bus, clk, byte):
    """One I2C transaction (START + 8 data bits + ACK clock + STOP)."""
    bus.set(I2C0_SDA, 1); bus.set(I2C0_SCL, 1)
    await ClockCycles(clk, P)
    bus.set(I2C0_SDA, 0)                       # START
    await ClockCycles(clk, P)
    for i in range(7, -1, -1):
        bus.set(I2C0_SCL, 0)
        await ClockCycles(clk, P)
        bus.set(I2C0_SDA, (byte >> i) & 1)
        await ClockCycles(clk, P // 2)
        bus.set(I2C0_SCL, 1)
        await ClockCycles(clk, P)
    bus.set(I2C0_SCL, 0)                        # ACK clock
    await ClockCycles(clk, P)
    bus.set(I2C0_SCL, 1)
    await ClockCycles(clk, P)
    bus.set(I2C0_SCL, 0); bus.set(I2C0_SDA, 0)  # STOP
    await ClockCycles(clk, P)
    bus.set(I2C0_SCL, 1)
    await ClockCycles(clk, P)
    bus.set(I2C0_SDA, 1)
    await ClockCycles(clk, P)


async def spi_byte(bus, clk, byte):
    """One CS-framed SPI byte (mode-0 style: sample on SCLK rising)."""
    bus.set(SPI0_CS, 0)
    await ClockCycles(clk, P)
    for i in range(7, -1, -1):
        bus.set(SPI0_SCLK, 0)
        bus.set(SPI0_MOSI, (byte >> i) & 1)
        await ClockCycles(clk, P)
        bus.set(SPI0_SCLK, 1)
        await ClockCycles(clk, P)
    bus.set(SPI0_SCLK, 0)
    await ClockCycles(clk, P)
    bus.set(SPI0_CS, 1)
    await ClockCycles(clk, P)


async def edge_toggles(bus, clk, n, hold):
    """Drive n transitions on EDGE0, `hold` cycles per level."""
    state = 0
    for _ in range(n):
        state ^= 1
        bus.set(EDGE0_PIN, state)
        await ClockCycles(clk, hold)


# ── Common helpers ──────────────────────────────────────────────────────────
async def reset_dut(bus, dut):
    dut.RST.value = 1
    dut.DOUT_REQ.value = 0
    bus.word = IDLE
    dut.CAPTURE_PINS.value = IDLE
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 32)


async def enable_datalogger(dut, dev_id):
    """config_control latches the slice whose CONFIG[15:11]==dev_id; enable=bit0."""
    dut.CONFIG.value = (dev_id << 11) | 0b01
    await ClockCycles(dut.CLK, 8)
    dut.CONFIG.value = 0
    await ClockCycles(dut.CLK, 8)


async def drain_to_empty(dut, bus, max_cycles=4096):
    """Consume queued events until the burst FIFO is empty for a sustained
    window, returning the count drained. Every datalogger emits a one-time
    enable-lifecycle frame (DS-P3.90 uniform datalogger_lifecycle) when its CONFIG
    enable is latched; those transients must be flushed BEFORE any baseline
    measurement or they contaminate the first window measured (proven by
    test_gather_diag — i2c enable emits 1 lifecycle frame). Also makes each
    test self-isolating against residue from a prior test (shared sim DUT)."""
    bus.word = IDLE
    dut.CAPTURE_PINS.value = IDLE
    dut.DOUT_REQ.value = 1
    drained = quiet = waited = 0
    while waited < max_cycles:
        await RisingEdge(dut.CLK)
        waited += 1
        try:
            if int(dut.DOUT_VALID.value) == 1:
                drained += 1
        except ValueError:
            pass
        if int(dut.FIFO_BURST_EMPTY.value) == 1:
            quiet += 1
            if quiet >= 64:
                break
        else:
            quiet = 0
    return drained


def make_collector(dut, captured):
    """Background coroutine: append (datalogger_id, domain, payload_dev) on every
    DOUT_VALID handshake. Asserts DOUT_REQ is the caller's responsibility."""
    async def _collect():
        while True:
            await RisingEdge(dut.CLK)
            try:
                if int(dut.DOUT_VALID.value) == 1:
                    payload = int(dut.DOUT_PAYLOAD.value)
                    sid = int(dut.DOUT_DATALOGGER_ID.value)
                    mty = int(dut.DOUT_MSG_TYPE.value)
                    captured.append((sid, (mty >> 8) & 0xF, (payload >> 27) & 0x1F))
            except ValueError:
                continue
    return _collect


def audit_tag_consistency(captured, allowed_ids, where):
    """Every event: datalogger_id in allowed_ids, domain matches ID_TO_DOMAIN,
    and the payload's [31:27] device_id equals the tagged datalogger_id.
    Returns per-id counts. Raises AssertionError (file-don't-fix) on any
    scramble."""
    alien = [(sid, dom, pdev) for sid, dom, pdev in captured if sid not in allowed_ids]
    assert not alien, (
        f"{where}: {len(alien)} events with alien datalogger_id ∉ "
        f"{sorted(allowed_ids)}. First few: {alien[:5]}. The top arbiter "
        f"mux-selected a source that wasn't driven, or the tagged tuple is "
        f"corrupt under cross-protocol contention. File T-tier."
    )
    scramble = [(sid, dom, pdev) for sid, dom, pdev in captured
                if dom != ID_TO_DOMAIN[sid]]
    assert not scramble, (
        f"{where}: {len(scramble)} events where the domain nibble does NOT "
        f"match the datalogger_id. First few (sid,dom,payload_dev): "
        f"{scramble[:5]}. Expected map {ID_TO_DOMAIN}. The arbiter paired one "
        f"source's payload with another's msg_type — a tagged-tuple scramble "
        f"across protocol groups. THIS is the cross-domain bug the single-"
        f"protocol suites cannot see. File T-tier."
    )
    divergence = [(sid, pdev) for sid, dom, pdev in captured if sid != pdev]
    assert not divergence, (
        f"{where}: {len(divergence)} events where DOUT_DATALOGGER_ID != "
        f"payload[31:27]. First few: {divergence[:5]}. The tagged-tuple chain "
        f"diverges from the payload device_id under contention. File T-tier."
    )
    counts = {}
    for sid, _dom, _pdev in captured:
        counts[sid] = counts.get(sid, 0) + 1
    return counts


# ── X1: concurrent tag-consistency + no-drop-vs-solo ────────────────────────
@cocotb.test()
@requires("SGA-REQ-4")
async def test_crossproto_tag_consistency_concurrent(dut):
    """X1 — drive EDGE0 + I2C0 + SPI0 concurrently through the top arbiter.

    Step 1: measure each protocol's SOLO event count (uncontended baseline).
    Step 2: drive all three concurrently with the same stimulus; assert
            * tag consistency (datalogger_id <-> domain <-> payload device_id),
            * each protocol's concurrent count == its solo count (the top
              arbiter dropped nothing under contention),
            * no tail-spurious events after settle.

    A concurrent count < solo means the top arbiter dropped that protocol's
    events under cross-group contention; > solo means a double-handshake or a
    stale-FIFO leak. Either is T-tier.
    """
    cocotb.start_soon(Clock(dut.CLK, PERIOD_NS, "ns").start())
    bus = SharedBus(dut.CAPTURE_PINS, IDLE)
    await reset_dut(bus, dut)
    for dev in (EDGE0_ID, I2C0_ID, SPI0_ID):
        await enable_datalogger(dut, dev)
    dut.DOUT_REQ.value = 1
    # Flush enable-lifecycle transients so the solo baselines are clean.
    n_trans = await drain_to_empty(dut, bus)
    dut._log.info(f"X1 flushed {n_trans} enable-lifecycle transient(s)")

    N_EDGE, HOLD = 12, 4
    I2C_BYTES = [0x50, 0xA3, 0x11]
    SPI_BYTES = [0xA5, 0x3C, 0xF0]

    # ── Step 1: solo baselines ──
    async def solo(driver_coro):
        cap = []
        col = cocotb.start_soon(make_collector(dut, cap)())
        await driver_coro()
        await ClockCycles(dut.CLK, 256)
        col.kill()
        return cap

    edge_solo = await solo(lambda: edge_toggles(bus, dut.CLK, N_EDGE, HOLD))
    bus.word = IDLE; dut.CAPTURE_PINS.value = IDLE
    await ClockCycles(dut.CLK, 64)

    async def _i2c_stream():
        for b in I2C_BYTES:
            await i2c_byte(bus, dut.CLK, b)
    i2c_solo = await solo(_i2c_stream)
    bus.word = IDLE; dut.CAPTURE_PINS.value = IDLE
    await ClockCycles(dut.CLK, 64)

    async def _spi_stream():
        for b in SPI_BYTES:
            await spi_byte(bus, dut.CLK, b)
    spi_solo = await solo(_spi_stream)
    bus.word = IDLE; dut.CAPTURE_PINS.value = IDLE
    await ClockCycles(dut.CLK, 64)

    solo_counts = {
        EDGE0_ID: len(edge_solo),
        I2C0_ID: len(i2c_solo),
        SPI0_ID: len(spi_solo),
    }
    dut._log.info(f"X1 solo baselines: {solo_counts}")
    assert all(v > 0 for v in solo_counts.values()), (
        f"X1 pre-condition: a protocol produced ZERO solo events — can't "
        f"measure cross-protocol drop without a baseline. solo={solo_counts}. "
        f"Tune BFM timing."
    )

    # ── Step 2: concurrent drive ──
    captured = []
    col = cocotb.start_soon(make_collector(dut, captured)())
    drivers = [
        cocotb.start_soon(edge_toggles(bus, dut.CLK, N_EDGE, HOLD)),
        cocotb.start_soon(_i2c_stream()),
        cocotb.start_soon(_spi_stream()),
    ]
    for d in drivers:
        await d
    await ClockCycles(dut.CLK, 512)
    settle_baseline = len(captured)
    bus.word = IDLE; dut.CAPTURE_PINS.value = IDLE
    await ClockCycles(dut.CLK, 512)
    tail_extras = len(captured) - settle_baseline
    col.kill()

    counts = audit_tag_consistency(
        captured, set(ID_TO_DOMAIN), "X1 (concurrent EDGE+I2C+SPI)")

    for dev_id, solo_n in solo_counts.items():
        conc_n = counts.get(dev_id, 0)
        assert conc_n == solo_n, (
            f"X1 violation (cross-protocol drop/extra): device_id={dev_id} "
            f"(domain={ID_TO_DOMAIN[dev_id]}) produced {conc_n} events "
            f"concurrently but {solo_n} solo. "
            + ("The top arbiter DROPPED this source's events under cross-group "
               "contention." if conc_n < solo_n
               else "The top arbiter emitted spurious extras for this source.")
            + f" all concurrent counts={counts}, solo={solo_counts}. File T-tier."
        )

    assert tail_extras == 0, (
        f"X1 violation (tail): {tail_extras} extra DOUT_VALID pulses after "
        f"pins-idle settle. Phantom events from stale FIFO state. File T-tier."
    )
    dut._log.info(
        f"X1: concurrent counts={counts} == solo={solo_counts}; tag "
        f"consistency held across all 3 domains; no tail.")


# ── X2: cross-protocol backpressure drain (SGA-REQ-8) ───────────────────────
@cocotb.test()
@requires("SGA-REQ-8")
async def test_crossproto_backpressure_drain(dut):
    """X2 — hold DOUT_REQ=0 while EDGE0 + I2C0 + SPI0 all stream; the burst
    FIFO must fill (FIFO_BURST_FULL rises), then release and drain.

    Asserts under back-pressure across DIFFERENT domains:
        * FIFO_BURST_FULL rises within the produce window (the consumer-overload
          signal is alive when 3 groups pile in, not just EDGE),
        * the drained stream is tag-consistent (no cross-domain scramble while
          events sat queued together in the arbiter FIFOs),
        * every driven source is represented after drain (no group fully
          starved/dropped under contention),
        * FIFO_BURST_LEVEL returns to 0 / EMPTY high at quiescent.
    """
    cocotb.start_soon(Clock(dut.CLK, PERIOD_NS, "ns").start())
    bus = SharedBus(dut.CAPTURE_PINS, IDLE)
    await reset_dut(bus, dut)
    for dev in (EDGE0_ID, I2C0_ID, SPI0_ID):
        await enable_datalogger(dut, dev)
    # Flush enable-lifecycle transients so the FIFO is genuinely empty before
    # we assert the back-pressure precondition.
    n_trans = await drain_to_empty(dut, bus)
    dut._log.info(f"X2 flushed {n_trans} enable-lifecycle transient(s)")
    dut.DOUT_REQ.value = 0     # back-pressure ON
    await ClockCycles(dut.CLK, 4)

    assert int(dut.FIFO_BURST_EMPTY.value) == 1, (
        "X2 pre-condition: FIFO_BURST_EMPTY low at idle. File T-tier.")
    assert int(dut.FIFO_BURST_FULL.value) == 0, (
        "X2 pre-condition: FIFO_BURST_FULL high at idle. File T-tier.")

    # ── Produce with no drain: EDGE flood + repeated I2C/SPI bytes ──
    full_seen = False

    async def _edge_flood():
        nonlocal full_seen
        state = 0
        for _ in range(4000):
            state ^= 1
            bus.set(EDGE0_PIN, state)
            await ClockCycles(dut.CLK, 2)
            if int(dut.FIFO_BURST_FULL.value) == 1:
                full_seen = True

    async def _i2c_flood():
        for _ in range(6):
            await i2c_byte(bus, dut.CLK, 0x5A)

    async def _spi_flood():
        for _ in range(6):
            await spi_byte(bus, dut.CLK, 0xC3)

    producers = [
        cocotb.start_soon(_edge_flood()),
        cocotb.start_soon(_i2c_flood()),
        cocotb.start_soon(_spi_flood()),
    ]
    for p in producers:
        await p

    bus.word = IDLE; dut.CAPTURE_PINS.value = IDLE
    await ClockCycles(dut.CLK, 64)

    assert full_seen or int(dut.FIFO_BURST_FULL.value) == 1, (
        "X2 violation (back-pressure invisibility): 3 protocols streamed with "
        "DOUT_REQ=0 but FIFO_BURST_FULL never rose. The internal FIFO is "
        "draining without DOUT_REQ, silently dropping upstream, or the full "
        "status is wired wrong. File T-tier.")

    # ── Release back-pressure, drain, audit ──
    captured = []
    col = cocotb.start_soon(make_collector(dut, captured)())
    dut.DOUT_REQ.value = 1

    # Drain to TRUE quiescence — FIFO_BURST_LEVEL==0 (per-input FIFOs empty) AND
    # the output empty. Keying on FIFO_BURST_EMPTY (output-FIFO empty) alone is
    # wrong: it can read 1 while the per-input FIFOs still hold data, breaking
    # the drain check prematurely. LEVEL==0 is the correct quiescence signal.
    QUIESCENT, MAX_DRAIN, waited, quiet = 256, 32768, 0, 0
    while waited < MAX_DRAIN:
        await ClockCycles(dut.CLK, 64)
        waited += 64
        if int(dut.FIFO_BURST_LEVEL.value) == 0 and int(dut.FIFO_BURST_EMPTY.value) == 1:
            quiet += 64
            if quiet >= QUIESCENT:
                break
        else:
            quiet = 0
    col.kill()

    assert int(dut.FIFO_BURST_LEVEL.value) == 0, (
        f"X2 violation (incomplete drain): FIFO_BURST_LEVEL="
        f"{int(dut.FIFO_BURST_LEVEL.value)} after {waited} drain cycles. "
        f"Drain stalled despite consumer ready. File T-tier.")
    assert int(dut.FIFO_BURST_EMPTY.value) == 1, (
        "X2 violation: FIFO_BURST_EMPTY low at quiescent. File T-tier.")

    counts = audit_tag_consistency(
        captured, set(ID_TO_DOMAIN), "X2 (backpressured cross-protocol drain)")
    starved = [d for d in ID_TO_DOMAIN if counts.get(d, 0) == 0]
    assert not starved, (
        f"X2 violation (source starvation under back-pressure): device_ids "
        f"{starved} produced ZERO drained events despite being driven. The "
        f"FIFO/arbiter dropped a whole group under contention. counts={counts}. "
        f"File T-tier.")
    dut._log.info(
        f"X2: FIFO_BURST_FULL rose under 3-protocol back-pressure; drained "
        f"{len(captured)} events in {waited} cycles; counts={counts}; tag "
        f"consistency held; no group starved.")


# ── X3: EDGE-flood starvation of the slow protocols (SGA-REQ-4 fairness) ────
@cocotb.test()
@requires("SGA-REQ-4")
async def test_crossproto_edge_flood_no_starvation(dut):
    """X3 — the sharpest top-arbiter fairness attack. Hammer EDGE0 at max rate
    (toggle every 2 cycles) for the whole test while I2C0 + SPI0 each send a
    few bytes. The slow protocols MUST still deliver their FULL solo event
    count — a top arbiter that doesn't rotate priority (or favours a fixed
    input index) would starve I2C/SPI behind the relentless EDGE valids.

    Measure I2C/SPI solo counts first (no EDGE), then re-drive them UNDER the
    EDGE flood and assert equality. A shortfall is round-robin-fairness broken
    at the top arbiter — the classic 'grant never rotates' defect. File T-tier.
    """
    cocotb.start_soon(Clock(dut.CLK, PERIOD_NS, "ns").start())
    bus = SharedBus(dut.CAPTURE_PINS, IDLE)
    await reset_dut(bus, dut)
    for dev in (EDGE0_ID, I2C0_ID, SPI0_ID):
        await enable_datalogger(dut, dev)
    dut.DOUT_REQ.value = 1
    n_trans = await drain_to_empty(dut, bus)
    dut._log.info(f"X3 flushed {n_trans} enable-lifecycle transient(s)")

    I2C_BYTES = [0x50, 0xA3, 0x11, 0x7E]
    SPI_BYTES = [0xA5, 0x3C, 0xF0, 0x0F]

    async def _i2c_stream():
        for b in I2C_BYTES:
            await i2c_byte(bus, dut.CLK, b)

    async def _spi_stream():
        for b in SPI_BYTES:
            await spi_byte(bus, dut.CLK, b)

    # ── Solo baselines (no EDGE) ──
    async def solo(driver_coro):
        cap = []
        col = cocotb.start_soon(make_collector(dut, cap)())
        await driver_coro()
        await ClockCycles(dut.CLK, 256)
        col.kill()
        return len(cap)

    i2c_solo = await solo(_i2c_stream)
    bus.word = IDLE; dut.CAPTURE_PINS.value = IDLE
    await ClockCycles(dut.CLK, 64)
    spi_solo = await solo(_spi_stream)
    bus.word = IDLE; dut.CAPTURE_PINS.value = IDLE
    await ClockCycles(dut.CLK, 64)
    assert i2c_solo > 0 and spi_solo > 0, (
        f"X3 pre-condition: solo counts i2c={i2c_solo} spi={spi_solo}; need a "
        f"baseline. Tune BFM timing.")
    dut._log.info(f"X3 solo baselines: i2c={i2c_solo} spi={spi_solo}")

    # ── Under EDGE flood ──
    flood_on = True

    async def _edge_flood():
        state = 0
        while flood_on:
            state ^= 1
            bus.set(EDGE0_PIN, state)
            await ClockCycles(dut.CLK, 2)

    captured = []
    col = cocotb.start_soon(make_collector(dut, captured)())
    flood = cocotb.start_soon(_edge_flood())
    # Drive I2C + SPI concurrently, both buried under the EDGE flood.
    drivers = [cocotb.start_soon(_i2c_stream()), cocotb.start_soon(_spi_stream())]
    for d in drivers:
        await d
    await ClockCycles(dut.CLK, 512)
    flood_on = False
    flood.kill()
    bus.set(EDGE0_PIN, 0)
    await ClockCycles(dut.CLK, 256)
    col.kill()

    counts = audit_tag_consistency(
        captured, set(ID_TO_DOMAIN), "X3 (EDGE-flood)")
    i2c_under = counts.get(I2C0_ID, 0)
    spi_under = counts.get(SPI0_ID, 0)
    edge_under = counts.get(EDGE0_ID, 0)
    dut._log.info(
        f"X3 under flood: i2c={i2c_under} spi={spi_under} edge={edge_under}")

    assert edge_under > 0, (
        "X3 sanity: EDGE produced zero events under its own flood — BFM/enable "
        "broken. File T-tier.")
    assert i2c_under == i2c_solo, (
        f"X3 violation (I2C starved by EDGE flood): I2C delivered {i2c_under} "
        f"events under the EDGE flood vs {i2c_solo} solo. The top arbiter is "
        f"starving the I2C group behind the relentless EDGE valids — round-"
        f"robin priority is not rotating. File T-tier.")
    assert spi_under == spi_solo, (
        f"X3 violation (SPI starved by EDGE flood): SPI delivered {spi_under} "
        f"events under the EDGE flood vs {spi_solo} solo. Top-arbiter fairness "
        f"broken — the slow group is locked out behind EDGE. File T-tier.")
    dut._log.info(
        f"X3: under a max-rate EDGE flood, I2C ({i2c_under}) and SPI "
        f"({spi_under}) kept their full solo counts — top arbiter fairness "
        f"holds across protocol groups.")


# ── X4: per-datalogger enable/disable lifecycle UNIFORMITY (DS-T?.? anchor) ────
@cocotb.test()
@requires("SGA-REQ-14")
async def test_lifecycle_enable_disable_uniformity(dut):
    """X4 — uniform enable/disable lifecycle across ALL active dataloggers.

    DS-P3.90 made the per-datalogger enable/disable marker (ENABLED 0x03 /
    DISABLED 0x00) a uniform in-band frame the host uses to observe each passive
    channel being armed/disarmed. This test enables then disables each ACTIVE
    datalogger in isolation and asserts the gather emits at least one frame on the
    enable edge and one on the disable edge, tagged with that datalogger's
    device_id.

    GREEN since DS-T2.43 (deepskopion) wired tich.datalogger_lifecycle into
    edge_datalogger — previously this was a RED expect_fail anchor because
    edge_datalogger emitted NEITHER frame (no datalogger_lifecycle instance, no inline
    event), unlike i2c/uart (shared emitter) and qspi/jtag (bespoke START/STOP /
    EvEnabled). This is the regression guard that keeps the lifecycle uniform.
    The DS-P1.104 default mix has no JTAG logger, so the probe covers its five
    protocol families: I2C, SPI, UART, CAN FD and EDGE.
    """
    cocotb.start_soon(Clock(dut.CLK, PERIOD_NS, "ns").start())
    bus = SharedBus(dut.CAPTURE_PINS, IDLE)

    async def probe(dev_id, name):
        await reset_dut(bus, dut)
        bus.word = IDLE; dut.CAPTURE_PINS.value = IDLE
        dut.DOUT_REQ.value = 1
        en = []
        col = cocotb.start_soon(make_collector(dut, en)())
        dut.CONFIG.value = (dev_id << 11) | 0b01          # enable edge
        await ClockCycles(dut.CLK, 200)
        col.kill()
        dis = []
        col = cocotb.start_soon(make_collector(dut, dis)())
        dut.CONFIG.value = (dev_id << 11) | 0b00          # disable edge
        await ClockCycles(dut.CLK, 200)
        col.kill()
        en_ids = {s for s, _, _ in en}
        dis_ids = {s for s, _, _ in dis}
        dut._log.info(
            f"X4 lifecycle[{name} id={dev_id}]: enable-frames={len(en)} "
            f"ids={sorted(en_ids)} disable-frames={len(dis)} ids={sorted(dis_ids)}")
        assert len(en) >= 1 and dev_id in en_ids, (
            f"X4 (DS-T2.43): {name} (id={dev_id}) emitted NO lifecycle frame on "
            f"the ENABLE edge — host cannot observe this channel being armed. "
            f"DS-P3.90 claims a uniform ENABLED marker across all dataloggers; "
            f"edge_datalogger has neither the shared datalogger_lifecycle nor an "
            f"inline enable event. File/track T-tier.")
        assert len(dis) >= 1 and dev_id in dis_ids, (
            f"X4 (DS-T2.43): {name} (id={dev_id}) emitted NO lifecycle frame on "
            f"the DISABLE edge — host cannot observe this channel being "
            f"disarmed. File/track T-tier.")

    for dev_id, name in ((I2C0_ID, "I2C0"), (SPI0_ID, "SPI0"), (UART0_ID, "UART0"),
                         (CAN0_ID, "CAN0"), (EDGE0_ID, "EDGE0")):
        await probe(dev_id, name)


# ── Simulation entry point ──────────────────────────────────────────────────
def main():
    # DS-T3.22: resolve the compile set with autodiscovery (scoped_libraries)
    # instead of a hand-curated list. The old hand list was BOTH stale (missing
    # can_datalogger, which the gather instantiates → "CAN_DATALOGGER not found") and
    # emitted absolute paths that defeated run_simulation's relative-path
    # locked-package injector dedup (double-analysed infilter → qspi obsolete).
    # scoped_libraries fixes both: it walks the DUT closure (so can_datalogger is
    # picked up) and emits project-relative paths (so the injector dedups). XPM is
    # supplied by run_simulation's full vendor cache (RTL-P3.703).
    run_simulation(
        top_level="tb_datalogger_gather",
        module=Path(__file__).stem,
        custom_libraries=scoped_libraries("tb_datalogger_gather"),
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
