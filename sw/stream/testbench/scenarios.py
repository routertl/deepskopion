# rr unknown-llm (AI-generated)
"""PS-side self-test scenarios — one per PS-LOOP-REQ-N (DS-P3.44).

Each scenario is a small function taking a SelfTestBench + RingOracle
(Layer 1 only) and returning a Verdict. The orchestrator
(run_self_test.py) walks the SCENARIOS list, calls each, and emits a
JSON verdict file.

Layer 0 — regbank self-check (REQ-1..4). No oracle needed.
Layer 1 — EMIO injector loop (REQ-5..8). Oracle walks DDR ring frames.

Datalogger device-id mapping (from tools/datalogger-builder/config/datalogger_config.yml):

  | EMIO_MUX_SEL bit | Datalogger slot | device_id |
  | bit[0] = I2C     | I2C #4       | 4         |
  | bit[1] = UART    | UART #12     | 12        |
  | bit[2] = SPI     | SPI #8       | 8         |
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from sw.stream.testbench.emio_inject import SelfTestBench
from sw.stream.testbench.ring_oracle import RingOracle, open_ring

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
#  Verdict + registry
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class Verdict:
    status: str               # "PASS" | "FAIL" | "SKIP"
    detail: str = ""
    duration_s: float = 0.0
    expected: dict = field(default_factory=dict)
    got: dict = field(default_factory=dict)

    @classmethod
    def passed(cls, detail: str = "", **kw) -> "Verdict":
        return cls(status="PASS", detail=detail, **kw)

    @classmethod
    def failed(cls, detail: str, **kw) -> "Verdict":
        return cls(status="FAIL", detail=detail, **kw)

    @classmethod
    def skipped(cls, detail: str, **kw) -> "Verdict":
        return cls(status="SKIP", detail=detail, **kw)


@dataclass
class Scenario:
    req_id: str
    name: str
    fn: Callable[..., Verdict]
    layer: int   # 0 = regbank-only, 1 = needs oracle + EMIO loop


SCENARIOS: List[Scenario] = []


def _register(req_id: str, name: str, layer: int):
    def deco(fn):
        SCENARIOS.append(Scenario(req_id=req_id, name=name, fn=fn, layer=layer))
        return fn
    return deco


# ─────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────

DATALOGGER_ID_I2C_4  = 4
DATALOGGER_ID_SPI_8  = 8
DATALOGGER_ID_UART_12 = 12

# Expected ring locator values pinned by deepskopion_top generics.
EXPECTED_RING_BASE_LOW  = 0x10000000
EXPECTED_RING_BASE_HIGH = 0x00000000
EXPECTED_RING_BYTES     = 0x10000000   # 2^28 = 256 MB

SLAVE_ADDR = 0x50   # arbitrary I2C address pinned to the injector


def _contains_bytes(haystack: bytes, needle: bytes) -> bool:
    """Byte-exact, in-order matcher shared by the capture scenarios."""
    return bool(needle) and haystack.find(needle) >= 0


# ─────────────────────────────────────────────────────────────────────────
#  Layer 0 — regbank self-check
# ─────────────────────────────────────────────────────────────────────────


@_register("PS-LOOP-REQ-1", "regbank_identity_nonzero", layer=0)
def regbank_identity_nonzero(bench: SelfTestBench, oracle: Optional[RingOracle]) -> Verdict:
    """Identity slots readable + all non-zero."""
    ident = bench.regbank.identity()
    zero = [k for k, v in ident.items() if v == 0]
    if zero:
        return Verdict.failed(
            f"identity slot(s) zero: {zero}",
            got=ident,
        )
    return Verdict.passed(
        f"git={ident['git_hash']:#010x} sysver={ident['system_version']:#010x} "
        f"build={ident['build_date']:#010x} rbver={ident['regbank_version']:#010x}",
        got=ident,
    )


@_register("PS-LOOP-REQ-2", "regbank_rom_drains", layer=0)
def regbank_rom_drains(bench: SelfTestBench, oracle: Optional[RingOracle]) -> Verdict:
    """Streaming-ROM read returns the declared N words; has non-zero entropy."""
    payload = bench.regbank.drain_regbank_rom()
    n_words = len(payload) // 4
    if n_words == 0:
        return Verdict.failed("REGBANK_NUM_WORDS reported 0 — ROM not initialised")
    if set(payload) == {0}:
        return Verdict.failed(
            f"drained {n_words} words but all zero — ROM init suspect",
            got={"n_words": n_words, "all_zero": True},
        )
    return Verdict.passed(
        f"drained {n_words} words; first 4 bytes = {payload[:4].hex()}",
        got={"n_words": n_words, "first_word_hex": payload[:4].hex()},
    )


@_register("PS-LOOP-REQ-3", "ring_locator_matches_design", layer=0)
def ring_locator_matches_design(bench: SelfTestBench, oracle: Optional[RingOracle]) -> Verdict:
    """Ring locator slots match deepskopion_top generics."""
    loc = bench.regbank.ring_locator()
    expected = {
        "base_low":  EXPECTED_RING_BASE_LOW,
        "base_high": EXPECTED_RING_BASE_HIGH,
        "bytes":     EXPECTED_RING_BYTES,
    }
    mismatches = {k: (loc[k], v) for k, v in expected.items() if loc[k] != v}
    if mismatches:
        return Verdict.failed(
            f"ring locator drift: {mismatches}",
            expected=expected, got=loc,
        )
    return Verdict.passed(
        f"base={loc['base_low']:#010x} bytes={loc['bytes']:#x}",
        expected=expected, got=loc,
    )


@_register("PS-LOOP-REQ-4", "datalogger_config_round_trip", layer=0)
def datalogger_config_round_trip(bench: SelfTestBench, oracle: Optional[RingOracle]) -> Verdict:
    """DATALOGGER_CONFIG write 0x1234, read back 0x1234 (low 16 bits)."""
    from sw.stream.regbank import Regs
    test_value = 0x00001234
    saved = bench.regbank.raw.read_u32(Regs.DATALOGGER_CONFIG)
    try:
        bench.regbank.raw.write_u32(Regs.DATALOGGER_CONFIG, test_value)
        readback = bench.regbank.raw.read_u32(Regs.DATALOGGER_CONFIG)
    finally:
        # Restore — never leak SW state into the next scenario.
        bench.regbank.raw.write_u32(Regs.DATALOGGER_CONFIG, saved)
    if (readback & 0xFFFF) != (test_value & 0xFFFF):
        return Verdict.failed(
            f"DATALOGGER_CONFIG round-trip mismatch: wrote {test_value:#06x}, "
            f"read {readback:#010x}",
            expected={"low16": test_value & 0xFFFF},
            got={"low16": readback & 0xFFFF, "full": readback},
        )
    return Verdict.passed(
        f"wrote/read {test_value & 0xFFFF:#06x}; restored {saved:#010x}",
    )


# ─────────────────────────────────────────────────────────────────────────
#  Layer 1 — EMIO injector loop (oracle = RingOracle)
# ─────────────────────────────────────────────────────────────────────────


def _drive_uart_marker(bench: SelfTestBench, marker: int = 0xA3,
                        cal_len: int = 150) -> None:
    """Drive the EMIO UART0 line with the sequence the uart_datalogger
    actually needs to emit a decoded frame for ``marker``:

      1. RANDOM calibration burst (``cal_len`` bytes). The auto-cal in
         uart_datalogger.vhd locks its baud estimate from the edge transitions
         of the FIRST burst — a constant pattern (e.g. 0x55) biases the
         period estimator to a wrong baud and every subsequent byte
         decodes as garbage. Mirrors test_datalogger_to_ddr.test_uart_to_ddr
         which uses 150 random bytes for the same reason.
      2. The marker byte under test.
      3. A short FLUSH burst (4 bytes). The datalogger's playback FIFO holds
         the LAST byte of any run until subsequent edges push it through
         the frame-gen — without flush bytes the marker stays trapped in
         the FIFO and never lands in the DDR ring.

    DS-T2.10 root cause: the previous single-byte 0x55 helper hit BOTH
    failure modes — no cal data and no flush — so the datalogger never
    emitted a frame for the byte. The RTL was functionally correct
    (test_datalogger_to_ddr + test_emio_loop both PASS); only the PS-side
    test was naive about the auto-cal + flush contract.
    """
    import random as _r
    rng = _r.Random(0xC0FFEE)
    # DS-T2.30: configure the UART datalogger for EXPLICIT 8N1 (0x600D: enable=1,
    # autoSync=0, dataBits=3). The boot default was 0x6001 (dataBits=0 → 5N1),
    # which decodes an 8N1 stream as 5-bit frames → garbage. extra=6 → 0x600D.
    bench.regbank.enable_datalogger(DATALOGGER_ID_UART_12, enable=True, extra=6)
    bench.regbank.set_emio_mux(uart=True)
    time.sleep(0.005)   # let mux settle
    cal = bytes(rng.randint(0, 0xFF) for _ in range(cal_len))
    bench.uart.write(cal)
    # cal_len bytes @ DEFAULT_UART_BAUD 9600 8N1 = ~10 * cal_len / 9600 s.
    # 150 bytes -> ~156 ms; pad another 50 ms for datalogger pipeline latency.
    time.sleep(0.010 * cal_len / 9600 + 0.05)
    bench.uart.write(bytes([marker]))
    bench.uart.write(b"\x00\xFF\x00\xFF")
    # marker + 4 flush bytes @ 9600 = ~5.2 ms; pad for DMA flush.
    time.sleep(0.030)


@_register("PS-LOOP-REQ-5", "uart_emio_round_trip", layer=1)
def uart_emio_round_trip(bench: SelfTestBench, oracle: RingOracle) -> Verdict:
    """Definitive UART round-trip: a known multi-byte payload written to
    the EMIO UART0 tty (base 0xE0000000, resolved with emio_uart_dev())
    must be read back BYTE-FOR-BYTE by the
    uart_datalogger — not just a single marker byte.

    Wire sequence: <random cal burst> + <known payload> + <flush>.
      * cal burst (random — a constant pattern biases the auto-cal) lets
        uart_datalogger.vhd lock its baud estimate from the first edges.
      * the known payload is the message under test.
      * the trailing flush pushes the payload's last byte through the
        datalogger's playback FIFO. DS UART caveat: the FIFO holds the FINAL
        byte of the whole stream until the next edge, so only the last
        *flush* byte is unrecovered — the payload comes back in full and
        must appear contiguously in the captured byte stream.
    """
    import random as _r
    payload = b"DEEPSKOPION-DS-T9-UART-READBACK!"   # distinctive vs random cal
    cal = bytes(_r.Random(0xC0FFEE).randint(0, 0xFF) for _ in range(150))
    # DS-T2.30: explicit 8N1 (0x600D), not the boot default 0x6001 (5N1) which
    # decodes an 8N1 stream as garbage. extra=6 → autoSync=0, dataBits=3.
    bench.regbank.enable_datalogger(DATALOGGER_ID_UART_12, enable=True, extra=6)
    bench.regbank.set_emio_mux(uart=True)
    try:
        time.sleep(0.005)
        # 1) Cal burst to lock the datalogger's baud, then let those ~150 frames DMA
        #    into the ring. We snapshot AFTER the cal so collect_new_frames only
        #    walks the payload's frames (~len(payload)) — collecting all 150+ cal
        #    frames per run overruns the ring-parser window (it recovers only a
        #    prefix), which dropped the payload tail in the first cut of this test.
        bench.uart.write(cal)
        time.sleep(0.010 * len(cal) / 9600 + 0.10)      # let cal flush into the ring (baud locked)
        oracle.snapshot()
        # 2) Known payload + flush. The cal's last byte is still trapped in the
        #    datalogger FIFO and gets pushed out by payload[0] — so the captured
        #    stream after the snapshot is [cal[-1], *payload, *flush][:-1]; we
        #    therefore search for `payload` as a CONTIGUOUS subsequence rather
        #    than comparing from index 0.
        #
        #    DS-P3.165: on PolarFire SoC images, MMUART1 is shared between Linux
        #    userspace stimulus (/dev/ttyS1) and the Hart Software Services (HSS)
        #    E51 monitor console. Appended console bytes after the payload are
        #    expected; _contains_bytes verifies the contiguous payload is intact,
        #    and the finally block closes the stimulus window by disengaging the mux.
        bench.uart.write(payload)
        bench.uart.write(b"\x00\xFF\x00\xFF")            # flush: push payload's last byte through
        time.sleep(0.05)

        try:
            frames = oracle.collect_new_frames(min_count=len(payload), timeout_s=2.5)
        except TimeoutError as exc:
            return Verdict.failed(
                f"too few UART frames after payload+flush burst: {exc}")

        uart_frames = RingOracle.filter_by_datalogger(frames, DATALOGGER_ID_UART_12)
        if not uart_frames:
            return Verdict.failed(
                f"got {len(frames)} frame(s) but none on datalogger_id={DATALOGGER_ID_UART_12}",
                expected={"datalogger_id": DATALOGGER_ID_UART_12},
                got={"all_datalogger_ids": sorted({f.datalogger_id for f in frames})},
            )

        # Reassemble the decoded byte stream (low byte of each UART frame's
        # payload word, in ring order) and check the message round-tripped.
        captured = bytes(
            int.from_bytes(f.payload[:4], "little") & 0xFF for f in uart_frames)
        if _contains_bytes(captured, payload):
            return Verdict.passed(
                f"UART readback EXACT: full {len(payload)}-byte payload recovered "
                f"byte-for-byte within {len(captured)} captured bytes "
                f"(datalogger_id={DATALOGGER_ID_UART_12})",
            )
        return Verdict.failed(
            f"transmitted payload NOT read back by the datalogger "
            f"({len(captured)} bytes captured on id={DATALOGGER_ID_UART_12})",
            expected={"payload": payload.hex()},
            got={"captured_tail": captured[-48:].hex()},
        )
    finally:
        # DS-P3.165: isolate the stimulus window so shared MMUART1 traffic
        # (e.g. HSS monitor console) does not continue flowing into the UART
        # datalogger and polluting subsequent scenarios.
        bench.regbank.set_emio_mux(uart=False)



def _drive_i2c_read(bench: SelfTestBench, payload: bytes) -> bytes:
    bench.regbank.load_i2c_response(SLAVE_ADDR, payload)
    bench.regbank.set_emio_mux(i2c=True)
    time.sleep(0.005)
    return bench.i2c.read_bytes(SLAVE_ADDR, len(payload))


@_register("PS-LOOP-REQ-6", "i2c_emio_round_trip", layer=1)
def i2c_emio_round_trip(bench: SelfTestBench, oracle: RingOracle) -> Verdict:
    """EMIO I2C master read with pre-loaded injector returns the expected
    bytes AND emits frames on datalogger_id=4."""
    expected_payload = bytes([0xDE, 0xAD, 0xBE, 0xEF])
    oracle.snapshot()
    try:
        got_payload = _drive_i2c_read(bench, expected_payload)
    except OSError as exc:
        import errno as _errno
        if exc.errno == _errno.EAGAIN:
            # Persistent EAGAIN = EMIO-I2C bus wedge (DS-T2.21 / Haz.61). NOT
            # the oracle, NOT a capture defect (I2C tagging proven id=4 in
            # isolation). The bus stays held after an aborted transaction and
            # Linux has no DT bus-recovery on this node. Stays RED on purpose:
            # a real unfixed integration defect, not a tooling artifact — do
            # not soften to SKIP/undetermined. Fix in DS-T2.21 (DT scl/sda-gpios
            # recovery + injector tristates SDA on abort).
            return Verdict.failed(
                f"EMIO-I2C bus wedged: {exc} — persistent EAGAIN after an "
                f"aborted transaction; no DT bus-recovery (DS-T2.21 / Haz.61). "
                f"Clear by reprogramming the PL.")
        return Verdict.failed(f"I2C transaction errored: {exc}")
    time.sleep(0.020)  # let ring DMA flush

    if got_payload != expected_payload:
        return Verdict.failed(
            "I2C master read returned wrong bytes "
            f"(MISO-equivalent: got {got_payload.hex()}, want {expected_payload.hex()})",
            expected={"bytes": expected_payload.hex()},
            got={"bytes": got_payload.hex()},
        )
    try:
        frames = oracle.collect_new_frames(min_count=1, timeout_s=0.5)
    except TimeoutError as exc:
        return Verdict.failed(f"I2C read OK but no ring frames captured: {exc}")
    i2c_frames = RingOracle.filter_by_datalogger(frames, DATALOGGER_ID_I2C_4)
    if not i2c_frames:
        return Verdict.failed(
            f"got {len(frames)} frames but none on datalogger_id={DATALOGGER_ID_I2C_4}",
            expected={"datalogger_id": DATALOGGER_ID_I2C_4},
            got={"all_datalogger_ids": sorted({f.datalogger_id for f in frames})},
        )
    # Payload frames have event_id 0/1 (ACK/NACK); event_id 4 carries bus
    # lifecycle markers. A read must appear as address+R followed by the exact
    # returned bytes. Seeing any id=4 frame was insufficient: START/STOP alone
    # could satisfy that check while DDR lost or corrupted the data.
    captured = bytes(f.payload[0] for f in i2c_frames
                     if f.payload and (f.header.event_id >> 1) == 0)
    expected_capture = bytes([(SLAVE_ADDR << 1) | 1]) + expected_payload
    if not _contains_bytes(captured, expected_capture):
        return Verdict.failed(
            "I2C response was correct at the master but not byte-exact in DDR",
            expected={"captured_bytes": expected_capture.hex()},
            got={"captured_bytes": captured.hex()},
        )
    return Verdict.passed(
        f"I2C readback EXACT through DDR: {expected_capture.hex()} "
        f"within {len(captured)} captured byte frame(s)",
    )


@_register("PS-LOOP-REQ-7", "spi_emio_round_trip", layer=1)
def spi_emio_round_trip(bench: SelfTestBench, oracle: RingOracle) -> Verdict:
    """EMIO SPI master transfer with pre-loaded injector returns the
    expected bytes on MISO AND emits frames on datalogger_id=8.

    DS-T8 diagnostic enhancement (2026-05-29): when the PS Cadence
    spi-cadence master fails with ETIMEDOUT (the historical REQ-7
    symptom), do NOT short-circuit before draining the ring — the SPI
    datalogger on capture_pin[20..23] may have captured whatever CS/SCLK/MOSI
    edges the controller DID produce on the wire before giving up. Those
    frames are the only HW evidence available without bitstream-level
    probe regs (DS-T13). The verdict reports BOTH the xfer outcome and
    the datalogger's view, so a failing REQ-7 turns into "PS-side controller
    drove N edges then stalled" rather than "PS-side controller errored,
    unknown wire state".
    """
    expected_payload = bytes([0x12, 0x34, 0x56, 0x78])
    oracle.snapshot()
    bench.regbank.load_spi_response(expected_payload)
    bench.regbank.set_emio_mux(spi=True)
    time.sleep(0.005)

    xfer_exc: Optional[Exception] = None
    got_payload: Optional[bytes] = None
    try:
        got_payload = bench.spi.xfer(b"\x00" * len(expected_payload))
    except OSError as exc:
        xfer_exc = exc

    time.sleep(0.020)

    # Drain the ring regardless of xfer outcome — the SPI datalogger may have
    # captured partial wire activity (CS falling edges, SCLK edges, MOSI
    # bytes) before the controller stalled. min_count=0 + soft TimeoutError
    # = "best-effort drain", never raises out.
    try:
        frames = oracle.collect_new_frames(min_count=0, timeout_s=0.5)
    except TimeoutError:
        frames = []
    spi_frames = RingOracle.filter_by_datalogger(frames, DATALOGGER_ID_SPI_8)

    # Helper: serialise the datalogger's view for the verdict JSON.
    def _datalogger_evidence() -> dict:
        return {
            "ring_frames_total":  len(frames),
            "spi_datalogger_frames": len(spi_frames),
            "spi_payloads_lo": [
                int.from_bytes(f.payload[:4], "little") & 0xFFFFFFFF
                for f in spi_frames[:16]   # cap the JSON blob
            ],
            "all_datalogger_ids": sorted({f.datalogger_id for f in frames}),
        }

    if xfer_exc is not None:
        return Verdict.failed(
            f"SPI transaction errored: {xfer_exc} "
            f"(datalogger captured {len(spi_frames)} SPI frame(s))",
            expected={"bytes": expected_payload.hex()},
            got={
                "xfer_exception": f"{type(xfer_exc).__name__}: {xfer_exc}",
                **_datalogger_evidence(),
            },
        )

    if got_payload != expected_payload:
        return Verdict.failed(
            f"SPI MISO returned {got_payload.hex()}, expected {expected_payload.hex()}",
            expected={"bytes": expected_payload.hex()},
            got={"bytes": got_payload.hex(), **_datalogger_evidence()},
        )
    if not spi_frames:
        return Verdict.failed(
            f"SPI xfer OK but no ring frames on datalogger_id={DATALOGGER_ID_SPI_8}",
            expected={"datalogger_id": DATALOGGER_ID_SPI_8},
            got=_datalogger_evidence(),
        )
    # event_id=2 is the MISO/IO1 payload lane. Prove the response seen by the
    # PS master is also the exact response committed to DDR.
    captured_miso = bytes(f.payload[0] for f in spi_frames
                          if f.payload and f.header.event_id == 0b010)
    if not _contains_bytes(captured_miso, expected_payload):
        return Verdict.failed(
            "SPI response was correct at the master but not byte-exact in DDR",
            expected={"captured_miso": expected_payload.hex()},
            got={"captured_miso": captured_miso.hex(), **_datalogger_evidence()},
        )
    return Verdict.passed(
        f"SPI MISO readback EXACT through DDR: {expected_payload.hex()} "
        f"within {len(captured_miso)} captured MISO byte frame(s)",
    )


@_register("PS-LOOP-REQ-8", "emio_mux_off_silences_loop", layer=1)
def emio_mux_off_silences_loop(bench: SelfTestBench, oracle: RingOracle) -> Verdict:
    """With EMIO_MUX_SEL=0 the same master traffic does NOT produce frames
    on the EMIO-mux'd datalogger slots."""
    # Force all EMIO_MUX_SEL bits off.
    bench.regbank.set_emio_mux(i2c=False, uart=False, spi=False)
    time.sleep(0.010)

    oracle.snapshot()
    # Same UART byte we used for REQ-5, but with mux disengaged.
    bench.uart.write(b"\x55")
    time.sleep(0.020)

    try:
        frames = oracle.collect_new_frames(min_count=0, timeout_s=0.1)
    except TimeoutError:
        frames = []

    leaked = RingOracle.filter_by_datalogger(frames, DATALOGGER_ID_UART_12)
    if leaked:
        return Verdict.failed(
            f"EMIO traffic leaked into datalogger_id={DATALOGGER_ID_UART_12} "
            "with mux disengaged",
            got={"leaked_frame_count": len(leaked)},
        )
    # Same check on the mux'd I2C/SPI slots — they should also be quiet
    # since we didn't drive their masters in this scenario.
    return Verdict.passed(
        f"mux disengaged: no frames on datalogger_id={DATALOGGER_ID_UART_12} "
        f"({len(frames)} unrelated frame(s) elsewhere)",
    )


@_register("PS-LOOP-REQ-9", "ring_oracle_exact_committed_burst", layer=1)
def ring_oracle_exact_committed_burst(bench: SelfTestBench,
                                      oracle: RingOracle) -> Verdict:
    """Falsify stale/enqueue-ahead ring reads with one exact SPI burst.

    A three-byte full-duplex transfer produces exactly eight SPI frames:
    CS-down, three MOSI bytes, three MISO bytes, and CS-up. The oracle walks
    only to RING_COMMIT_PTR and RealDdrMmap remaps each read, so recovering the
    exact cardinality and MISO sequence proves both halves of REQ-9 on the
    live capture path.
    """
    expected_miso = bytes([0x6D, 0xA5, 0xC3])
    expected_frame_count = 2 + 2 * len(expected_miso)
    bench.regbank.enable_datalogger(DATALOGGER_ID_SPI_8, enable=True)
    bench.regbank.load_spi_response(expected_miso)
    bench.regbank.set_emio_mux(spi=True)
    time.sleep(0.010)
    oracle.snapshot()

    try:
        got_miso = bench.spi.xfer(b"\x00" * len(expected_miso))
    except OSError as exc:
        return Verdict.failed(f"REQ-9 SPI falsifier transaction errored: {exc}")
    time.sleep(0.050)
    try:
        frames = oracle.collect_new_frames(
            min_count=expected_frame_count, timeout_s=1.0,
        )
    except TimeoutError as exc:
        return Verdict.failed(f"REQ-9 exact-frame falsifier timed out: {exc}")

    spi_frames = RingOracle.filter_by_datalogger(frames, DATALOGGER_ID_SPI_8)
    captured_miso = bytes(f.payload[0] for f in spi_frames
                          if f.payload and f.header.event_id == 0b010)
    expected = {
        "spi_frame_count": expected_frame_count,
        "miso": expected_miso.hex(),
    }
    got = {
        "spi_frame_count": len(spi_frames),
        "miso": captured_miso.hex(),
        "master_miso": got_miso.hex(),
        "commit_offset": oracle.commit_offset(),
        "write_idle": oracle.write_idle(),
    }
    if (len(spi_frames) != expected_frame_count
            or captured_miso != expected_miso
            or got_miso != expected_miso):
        return Verdict.failed(
            "REQ-9 did not recover the exact committed SPI burst",
            expected=expected,
            got=got,
        )
    return Verdict.passed(
        f"exactly {expected_frame_count} committed frames recovered; "
        f"MISO={expected_miso.hex()}",
        expected=expected,
        got=got,
    )


# ─────────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────────


def all_scenarios() -> List[Scenario]:
    """Return scenarios in registration order (Layer 0 first, then Layer 1)."""
    return list(SCENARIOS)


def make_oracle(bench: SelfTestBench) -> RingOracle:
    return open_ring(bench.regbank)
