#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DE-C0 (DS-P1.44) — real-bus capture-correctness harness.

The DE-C0 done-conditions are:

  1. known UART/I2C/SPI/edge stimuli produce EXACT expected decoded payloads
     and ordering;
  2. the produced stream conforms to WS-C0 and the released viewer decodes it
     (RW-WS1's checker, not a human glance);
  3. a wrong expected payload fails the run;
  4. the capture is compared against the Xilinx baseline for the same stimuli,
     and any difference is explained rather than tolerated.

This module is the executable oracle for that chain. It closes a seam that was
un-bound end to end: the on-board self-test (``sw/stream/testbench/scenarios.py``,
DS-X5) proves the decoded payload is byte-exact in DDR, and DS-WS1
(``test_wave_stream_live_conformance.py``) proves the running daemon emits
contract-conformant frames given *some* DataSource — but nothing tied "a known
bus stimulus, decoded, produces THAT exact payload AND a viewer-conformant
stream carrying it, AND a wrong expectation fails". A decoder that emits the
right bytes on a wrong channel_id / role key would pass the payload check and
still ship a capture the viewer silently drops — the exact class of defect
WS-C0 was created to catch (D.14; the ``detail``/``message`` mislabel; the
unregistered enum members). DE-C0 binds all of it in one falsifiable oracle.

The binding that makes that true: ``run_case`` reads the exact decoded payload
back out of the CAPTURED WIRE FRAMES (the ANNOTATIONS/EDGE_EVENTS a subscriber
actually receives over the socket), NOT the in-process ``Batch``. So a decoded
event the producer's WS-C0 guard refuses to broadcast (wrong channel_id, a
renamed/absent role key, a dropped START/STOP) never reaches the wire and the
payload check fails closed — the same event that ACCEPTs an empty-but-valid
session now cannot satisfy the exact-payload condition. Payload and wire are one
check, not two independent ones.

WHAT THIS EXERCISES (all the code that runs on the board, minus the PL):

    known stimulus
      -> real zs_msg frames in a DDR ring (sw.stream.zs_msg builders)
      -> REAL sw.stream.data_source.DdrRingDataSource  (ring parse + resync)
      -> REAL sw.stream.decoders per-protocol decode  (byte -> Annotation.fields)
      -> REAL sw.stream.server.serve_one over a REAL socket  (the wire)
      -> vendored WS-C0 contracts/wave_stream_v1 check_session()  (the judge)

The ONLY substitution vs a booted board is the ring's byte source: a
``FakeDdrMmap`` holding frames this module built with the sanctioned
``zs_msg`` framing, instead of the DDR the PL writes. That substitution is the
same "provider seam" ``tools/golden_path/golden_path_runner.py`` already treats
as legitimate: proving the *PL* writes the right ring bytes is DS-X3's exact-
payload oracle and DE-R0's byte-identical altera/xilinx sim; proving the ring
bytes decode to the exact payload AND a conformant viewer stream is THIS ticket.

HONEST BOUNDARIES (see docs/GOLDEN_PATH.md "DE-C0"):

  * THIS HARNESS ITSELF STAYS BOARD-FREE BY CONSTRUCTION.  DE-B1's original
    "never booted to a shell" blocker (no host on this bench could write its
    SD card) was cleared 2026-08-23 (DS-P1.52/.55, boot17+), and DS-P2.101
    (2026-08-26) ran the live UART/I2C/SPI/edge witness this module's case
    definitions were built to support — see
    transcript-cited result and per-case verdicts. That witness drove the
    real regbank/DDR ring directly from an on-board Python driver (arming
    ``ds_stimulus_gen`` + decoding via the real
    ``DdrRingDataSource(RealDdrMmap, Regbank)``), not through this module —
    this module remains the board-free, socket-level oracle it always was,
    unchanged by that witness.
  * RW-WS1 IS EXTERNAL.  "The released viewer's checker" (RW-WS1, LT-P2.120) is
    RouteWave's C++ decoder in the logic-trace repo,
    outside this worktree. What binds the two products is the *vendored* WS-C0
    contract (``contracts/wave_stream_v1/``, owned by neither product, identity
    = digest): the released viewer is held to the same bytes ``check_session``
    is. So ACCEPT here is the judgement the viewer is bound to; running the C++
    RW-WS1 checker over these bytes is a cross-repo step named as a boundary,
    not something a check inside this repo can discharge.
  * #4 LAYERING.  The DE25 capture *software* (decoders + server) is vendor-
    neutral Python shared verbatim with the Xilinx target, so given identical
    ring bytes it decodes identically by construction. The vendor-specific half
    — the PL producing those ring bytes — is proven byte-identical altera vs
    xilinx by DE-R0 (queued sim, measured 2026-08-22) and its exact payloads by
    DS-X3. DE-C0's contribution to #4 is to pin, executably, that these cases
    reproduce the SAME decoded payloads the Xilinx sim oracle
    (``test_datalogger_to_ddr.py``) drives, so a drift in either side is caught
    (see ``test_capture_correctness.py``); it does NOT re-measure the PL.

Run (board-free):

    python3 -m sw.stream.testbench.capture_correctness

Exit code: 0 = every case PASS; 1 = any case FAIL (fail-closed, mirrors
``run_self_test``); 2 = harness/runtime error.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import struct
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

import msgpack

from sw.stream import channels as ch
from sw.stream import edge_capture, protocol_codes, zs_msg
from sw.stream import protocol as proto
from sw.stream.data_source import Annotation, Batch, DdrRingDataSource
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.regbank import FakeRegbank, Regs
from sw.stream.server import serve_one
from sw.stream.testbench.de_c0_stimulus_contract import (
    EDGE_ARM_DELAY_NS, EDGE_CHANNEL, EDGE_TRANSITIONS, I2C_ADDRESS_BYTE, I2C_DATA,
)

# ── the vendored WS-C0 contract (owned by neither product, identity = digest) ──
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_DIR = _REPO_ROOT / "contracts" / "wave_stream_v1"
if str(_CONTRACT_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTRACT_DIR))
import wave_stream_contract as C  # noqa: E402  (path set above)

logger = logging.getLogger(__name__)

_BYTES_PER_MSG = 4

# The stimulus payloads below are the SAME ones the Xilinx sim oracle
# (hw/sim/cocotb/tests/datalogger/infra/full_data_path/test_datalogger_to_ddr.py)
# drives — so the DE25 capture is compared against the Xilinx baseline for the
# same stimuli (DE-C0 done-condition #4). test_capture_correctness.py binds
# these to that file's literals, so a drift in either is a red test.
UART_MARKER = [0xA3, 0x5C, 0x71, 0x0E, 0xFF, 0x42]
UART_FLUSH = [0x00, 0xFF, 0x00, 0xFF]   # the oracle drives MARKER then FLUSH as
#                                         one CONTIGUOUS run; the harness drives
#                                         (and expects) the same MARKER + FLUSH.
I2C_ADDR = I2C_ADDRESS_BYTE  # 0x50 << 1 | W — must precede the data run (DS-X3)
SPI_MOSI = [0xDE, 0xAD, 0xBE, 0xEF]
# Edge: the finite production stimulus vector as (absolute_ns, level). Both
# supported sample clocks represent every timestamp exactly (20 ns grid).
# board-free source is 100 MHz => 10 ns/tick; the wire oracle below derives the
# actual period from the same HELO sample_clk_hz the source advertises.


# ── Regbank fake that positions the ring read head from RING_WRITE_PTR ─────────
class _MirrorRegbank(FakeRegbank):
    """Mirror RING_COMMIT_PTR <- RING_WRITE_PTR*4 so a test can place the write
    head by setting a message count. The daemon reads the committed byte offset
    from RING_COMMIT_PTR; the COMMIT-vs-WRITE divergence contract is DdrRing's
    own regression (test_ddr_ring_data_source.py) — here we only need a head."""

    def read_u32(self, offset: int) -> int:
        if offset == Regs.RING_COMMIT_PTR:
            return super().read_u32(Regs.RING_WRITE_PTR) * _BYTES_PER_MSG
        return super().read_u32(offset)


# ── zs_msg frame builders — the exact framing datalogger_gather emits ─────────────
def _uart_byte_frame(byte: int, ts: int) -> bytes:
    hdr = zs_msg.make_header(
        datalogger_id=12, domain=zs_msg.DOMAIN_UART,
        event_id=protocol_codes.UART_TYPES["payload"], severity=zs_msg.SEV_INFO,
        timestamp=ts, payload_len=4)
    return zs_msg.pack_header(hdr) + struct.pack("<I", byte & 0xFF)


def _i2c_event_frame(event_name: str, ts: int) -> bytes:
    code = next(c for c, n in protocol_codes.I2C_EVENTS.items() if n == event_name)
    hdr = zs_msg.make_header(
        datalogger_id=4, domain=zs_msg.DOMAIN_I2C,
        event_id=protocol_codes.I2C_TYPES["event"], severity=zs_msg.SEV_INFO,
        timestamp=ts, payload_len=4)
    return zs_msg.pack_header(hdr) + bytes([code, 0, 0, 0])


def _i2c_byte_frame(data: int, ack: bool, ts: int) -> bytes:
    et = (protocol_codes.I2C_TYPES["payload"] << 1) | (0 if ack else 1)
    hdr = zs_msg.make_header(
        datalogger_id=4, domain=zs_msg.DOMAIN_I2C, event_id=et,
        severity=zs_msg.SEV_INFO, timestamp=ts, payload_len=4)
    return zs_msg.pack_header(hdr) + bytes([data & 0xFF, 0, 0, 0])


def _spi_mosi_frame(data: int, ts: int) -> bytes:
    hdr = zs_msg.make_header(
        datalogger_id=8, domain=zs_msg.DOMAIN_SPI,
        event_id=protocol_codes.SPI_TYPES["payload_a"], severity=zs_msg.SEV_INFO,
        timestamp=ts, payload_len=4)
    return zs_msg.pack_header(hdr) + bytes([data & 0xFF, 0, 0, 0])


def _edge_batch_frame(channel: int,
                      transitions: List[Tuple[int, int]],
                      clk_mhz: int = 100) -> bytes:
    """One EDGE_BATCH frame reproducing ``transitions`` = [(abs_ns, level)].

    The decoder reconstructs the post-edge level as ``1 - e`` (for a normal
    edge, s=0), so ``e = 1 - level``; ts is an absolute clk tick (ns / (1000 /
    clk_mhz)). anchor = the first tick."""
    ns_per_tick = 1000 // clk_mhz
    ticks = [ns // ns_per_tick for ns, _ in transitions]
    anchor = ticks[0]
    events = [
        zs_msg.EdgeEvent(ts_64b=t, channel=channel, s=False, f=False,
                         e=bool(1 - level))
        for t, (_, level) in zip(ticks, transitions)
    ]
    return zs_msg.pack_edge_batch(datalogger_id=channel, channel=channel,
                                  events=events, anchor_ts=anchor)


# ── the case set ──────────────────────────────────────────────────────────────
#: one captured wire frame as check_session sees it: (tag, payload dict).
WireFrame = Tuple[int, dict]


@dataclass
class CaptureCase:
    name: str
    protocol: str
    #: builds the ring blob (concatenated real zs_msg frames) for the stimulus
    build_frames: Callable[[], bytes]
    #: the exact expected decoded payload/ordering — see per-protocol extractors
    expected: Any
    #: pulls the observed decoded payload out of the CAPTURED WIRE FRAMES (the
    #: ANNOTATIONS/EDGE_EVENTS a subscriber actually received), in wire order —
    #: NOT out of the in-process Batch, so a frame the WS-C0 guard refused to
    #: broadcast simply is not here and the exact-payload check fails closed.
    extract: Callable[[List[WireFrame], int, int], Any]
    channel_id: int
    #: how a LIVE (on-silicon) observation is compared to ``expected``.
    #: Default ``None`` == exact equality, which is what uart/i2c/spi use: their
    #: payload is a byte/event sequence with no timebase in it. ``edge_scl``
    #: overrides it because its payload IS timestamps, and on silicon those come
    #: from a free-running counter with no epoch — see
    #: :func:`compare_edge_arm_anchored`. The BOARD-FREE oracle always uses exact
    #: equality (its frames are synthesised at the canonical absolute times), so
    #: this seam only relaxes the epoch, never the vector.
    compare_live: Optional[Callable[[Any, Any], Tuple[bool, str]]] = None


def _uart_frames() -> bytes:
    run = UART_MARKER + UART_FLUSH   # the oracle's contiguous MARKER+FLUSH run
    return b"".join(_uart_byte_frame(b, ts=100 * (i + 1))
                    for i, b in enumerate(run))


def _i2c_frames() -> bytes:
    ts = 0

    def nxt() -> int:
        nonlocal ts
        ts += 100
        return ts

    parts = [_i2c_event_frame("start", nxt())]
    parts.append(_i2c_byte_frame(I2C_ADDR, ack=True, ts=nxt()))
    parts += [_i2c_byte_frame(d, ack=True, ts=nxt()) for d in I2C_DATA]
    parts.append(_i2c_event_frame("stop", nxt()))
    return b"".join(parts)


def _spi_frames() -> bytes:
    return b"".join(_spi_mosi_frame(b, ts=100 * (i + 1))
                    for i, b in enumerate(SPI_MOSI))


# ── wire readers: pull decoded payload back out of the captured wire frames ────
def _wire_annotations(captured: List[WireFrame], channel_id: int) -> List[dict]:
    """Every ANNOTATIONS-frame annotation on ``channel_id``, in wire order.

    Filtering on channel_id is itself part of the binding: an annotation the
    decoder stamped on the wrong lane (the D.14 / undeclared-channel defect) is
    either refused by the producer's WS-C0 guard (never on the wire) or lands on
    a different channel_id here — either way it is absent from this case's
    observed payload, so the exact-payload check fails closed."""
    out: List[dict] = []
    for tag, payload in captured:
        if tag != proto.TAG_ANNOTATIONS:
            continue
        for a in payload.get("annotations", []):
            if a.get("channel_id") == channel_id:
                out.append(a)
    return out


def _extract_uart(captured: List[WireFrame], channel_id: int,
                  _sample_clk_hz: int) -> List[int]:
    return [a["payload"]["byte"]
            for a in _wire_annotations(captured, channel_id)
            if a.get("kind") == "uart_byte" and "payload" in a]


#: I2C datalogger LIFECYCLE control annotations — emitted when the datalogger is
#: enabled/disabled (the shared armer's disable→enable), NOT part of an I2C bus
#: transaction. Excluded from the decoded transaction, mirroring how the SPI/UART
#: extractors keep only bus-payload kinds.
_I2C_LIFECYCLE_KINDS = frozenset({"i2c_enabled", "i2c_disabled"})


def _extract_i2c(captured: List[WireFrame], channel_id: int,
                 _sample_clk_hz: int) -> List[Any]:
    """Ordered I2C decoded stream: START/STOP events as their name string,
    payload bytes as (data, ack). Binding events AND ordering — not just the
    byte values — is what makes dropping/swapping START/STOP a RED run (the
    weakness the copied DdrRing test, which requires START…bytes…STOP, does not
    have when only i2c_byte.data is kept)."""
    seq: List[Any] = []
    for a in _wire_annotations(captured, channel_id):
        kind = a.get("kind", "")
        pay = a.get("payload", {})
        if kind == "i2c_byte":
            seq.append((pay.get("data"), pay.get("ack")))
        elif kind in _I2C_LIFECYCLE_KINDS:
            # Datalogger LIFECYCLE control, not a bus event: the shared production
            # armer (de_c0_production_armer.arm_i2c) disables→enables the datalogger
            # before the transaction, so a daemon-live capture carries a leading
            # ``i2c_enabled`` control annotation ahead of ``start`` (DS-P1.59
            # silicon witness). The sibling extractors already exclude their own
            # control events — ``_extract_spi`` keeps only ``spi_mosi`` and drops
            # the ``spi_event``/``spi_miso`` a live SPI capture also carries — so
            # the I2C decode must likewise scope to the BUS transaction. This is
            # not a loosening: START/STOP/repeated-start, byte values, ACK bits,
            # and every I2C *error* event stay in ``seq`` (an unexpected error is
            # still an extra element that fails the exact match).
            continue
        elif kind.startswith("i2c_") and "event" in pay:
            seq.append(pay["event"])
    return seq


def _extract_spi(captured: List[WireFrame], channel_id: int,
                 _sample_clk_hz: int) -> List[int]:
    return [a["payload"]["mosi"]
            for a in _wire_annotations(captured, channel_id)
            if a.get("kind") == "spi_mosi" and "payload" in a]


def _extract_edge(captured: List[WireFrame], channel_id: int,
                  sample_clk_hz: int) -> List[Tuple[int, int]]:
    """Decode at the clock the producer advertised on this same wire session.

    ``DdrRingDataSource`` creates EDGE_EVENT words using its sample clock and
    ``serve_one`` publishes that source clock in HELO.  Reading HELO here keeps
    the oracle independent of board defaults without giving it a second
    constant that can silently match a wrong daemon constant (DS-T2.73).
    """
    words: List[int] = []
    for tag, payload in captured:
        if tag == proto.TAG_EDGE_EVENTS:
            words += list(payload.get("words", []))
    clk_mhz = max(1, sample_clk_hz // 1_000_000)
    return edge_capture.words_to_transitions(
        words, clk_mhz=clk_mhz, instance=channel_id)


#: the ordered I2C decoded stream the oracle stimulus must reproduce on the wire
_I2C_EXPECTED: List[Any] = [
    "start",
    (I2C_ADDR, True), *[(d, True) for d in I2C_DATA],
    "stop",
]


def compare_edge_arm_anchored(observed: Any, expected: Any) -> Tuple[bool, str]:
    """Compare a LIVE edge observation to the canonical vector, ARM-ANCHORED.

    WHY THIS EXISTS (DS-P1.44 design decision; the DE-C0 clause-3 edge gap).
    ``expected`` is ``[(10000, 1), (10200, 0), (10520, 1), (10800, 0)]`` —
    absolute ns measured FROM THE ARM WRITE. In the board-free oracle those are
    also the literal wire timestamps, because the harness synthesises the
    EDGE_BATCH frames itself, so exact equality is the right judge there and is
    unchanged. On silicon they cannot be: the edge datalogger stamps every
    transition from a FREE-RUNNING 1 µs counter whose epoch is board power-on and
    which is never exposed to the host (there is no timestamp register in
    ``sw/stream/regbank.py``, and the wrap has no epoch at all — the ts_us field
    is 16 bits). The committed DS-P1.59 capture reads ~54.19 ms, not 10 µs.

    So the previous judge — exact equality against those literals on a live
    capture — was UNSATISFIABLE BY CONSTRUCTION: no correct DUT, at any moment,
    could ever pass it. A test no correct implementation can pass is not a strict
    test, it is a broken one; it reported a harness epoch mismatch as a silicon
    defect and gated ``edge_scl`` on it.

    The alternative fix — change ``ds_stimulus_gen.vhd`` to emit an arm-anchored
    marker edge so the enable instant becomes observable in the datalogger's own
    timebase — is the only way to recover a literally-exact absolute compare. It
    is deliberately NOT taken here: it mutates the canonical vector that the
    Xilinx sim baseline (``test_edge_to_ddr``), the board-free oracle and the
    contract are all bound to, and needs a full Agilex re-synth to prove. Filed
    as the follow-up; this comparator is the documented judge decision instead.

    WHAT IS STILL ASSERTED — everything except the unobservable epoch:

    1. The decoded edge timeline after the arm boundary is monotonic (a backwards
       timeline is a decoder defect — exactly the S=1 readback bug DS-P1.44 fixed
       in ``edge_capture``). Pre-arm transitions fall outside it per clause 2.
    2. The ARM BOUNDARY is located mechanically, not chosen: the last inter-
       transition gap >= ``EDGE_ARM_DELAY_NS``. The RTL guarantees such a gap
       (p_edge step 0 holds the line low for 10 us after enable) and guarantees
       the vector's own gaps are far smaller (200/320/280 ns), so this locates
       the vector start uniquely. Pre-arm transitions (datalogger-enable and
       EMIO_MUX_SEL settling) fall outside it and are excluded BECAUSE they are
       pre-stimulus, not because they are inconvenient.
    3. The post-arm window holds EXACTLY ``len(expected)`` transitions — not
       "at least", and not "the last four of however many". The RTL emits four
       and then holds forever, so a free-running toggle source, a re-triggered
       vector, or a truncated burst all FAIL here.
    4. Levels match the baseline EXACTLY, in order.
    5. Inter-transition deltas match the baseline EXACTLY, to the nanosecond —
       200/320/280 ns. This is the full timing content of the vector; only the
       shared additive epoch is dropped.
    6. The line was LOW immediately before the vector (the RTL holds it low
       through the arm delay), when a pre-arm transition exists to say so.

    A disconnected DUT yields no transitions and FAILs (1)/(3) — the vacuity
    control the falsifiable-test-harness rule demands. Mutating one expected
    transition time or level, capture unchanged, FAILs (4)/(5): clause 6 keeps
    its teeth.

    Returns ``(ok, detail)``.
    """
    exp = [(int(t), int(v)) for t, v in expected]
    obs = [(int(t), int(v)) for t, v in observed]
    n = len(exp)
    if n == 0:
        return False, "empty expected edge vector"
    if not obs:
        return False, ("no edge transitions decoded off the wire (nothing was "
                       "captured) — the arm-anchored compare cannot pass vacuously")
    # (2) arm boundary = the last gap >= the RTL's guaranteed arm delay.
    start = 0
    for i in range(1, len(obs)):
        if obs[i][0] - obs[i - 1][0] >= EDGE_ARM_DELAY_NS:
            start = i
    post = obs[start:]

    # (1) decoded edge timeline after the arm boundary is monotonic.
    if any(b[0] < a[0] for a, b in zip(post, post[1:])):
        return False, f"decoded edge timeline is NOT monotonic: {post}"

    # (3) exactly n transitions after the arm boundary.
    if len(post) != n:
        return False, (f"{len(post)} transition(s) after the arm boundary, "
                       f"expected exactly {n}: {post}")

    # (4) levels exact, in order.
    obs_levels = [v for _, v in post]
    exp_levels = [v for _, v in exp]
    if obs_levels != exp_levels:
        return False, f"edge levels {obs_levels} != baseline {exp_levels}"

    # (5) inter-transition deltas exact, to the ns.
    obs_deltas = [b[0] - a[0] for a, b in zip(post, post[1:])]
    exp_deltas = [b[0] - a[0] for a, b in zip(exp, exp[1:])]
    if obs_deltas != exp_deltas:
        return False, f"edge deltas {obs_deltas} ns != baseline {exp_deltas} ns"

    # (6) the line was held LOW through the arm delay, when observable.
    if start > 0 and obs[start - 1][1] != 0:
        return False, (f"line was HIGH ({obs[start - 1]}) through the arm delay "
                       f"before the vector; the RTL holds it low")

    gap = (post[0][0] - obs[start - 1][0]) if start > 0 else None
    return True, (f"{n} transition(s) exact arm-anchored: levels {obs_levels}, "
                  f"deltas {obs_deltas} ns, epoch +{post[0][0] - exp[0][0]} ns "
                  f"(free-running datalogger counter), "
                  + (f"quiet-low arm gap {gap} ns >= {EDGE_ARM_DELAY_NS} ns"
                     if gap is not None
                     else "no pre-arm transition in the capture window"))


def known_cases() -> List[CaptureCase]:
    """The DE-C0 case set: one per protocol, using the Xilinx-baseline stimuli."""
    return [
        CaptureCase("uart_marker", "uart", _uart_frames,
                    UART_MARKER + UART_FLUSH, _extract_uart, 100),
        CaptureCase("i2c_write", "i2c", _i2c_frames,
                    list(_I2C_EXPECTED), _extract_i2c, 101),
        CaptureCase("spi_mosi", "spi", _spi_frames,
                    list(SPI_MOSI), _extract_spi, 102),
        CaptureCase("edge_scl", "edge",
                    lambda: _edge_batch_frame(EDGE_CHANNEL, EDGE_TRANSITIONS),
                    list(EDGE_TRANSITIONS), _extract_edge, EDGE_CHANNEL,
                    compare_live=compare_edge_arm_anchored),
    ]


# ── decode: drive the REAL DdrRingDataSource over a synthetic ring ─────────────
def decode_case(case: CaptureCase) -> Batch:
    """Decode ``case``'s stimulus through the real on-board decode path.

    Board-free: a ``FakeDdrMmap`` holds the frames; every other line is the
    exact code a booted board runs. ``poll_period_s=0`` + a constant clock ⇒ no
    wall-clock sleeping and a zero-width poll window (so annotation ts_end ==
    ts_start, which the contract accepts — it only rejects ts_end < ts_start)."""
    blob = case.build_frames()
    assert len(blob) % _BYTES_PER_MSG == 0, "frames must be word-aligned"
    ddr = FakeDdrMmap(max(4096, len(blob) * 4))
    rb = _MirrorRegbank()
    src = DdrRingDataSource(regbank=rb, ddr=ddr, sample_clk_hz=100_000_000,
                            poll_period_s=0.0, clock=lambda: 1000.0)
    try:
        src.next_batch()  # seed the read pointer without replaying history
        ddr.write_bytes(0, blob)
        rb.set(Regs.RING_WRITE_PTR, len(blob) // _BYTES_PER_MSG)
        return src.next_batch()
    finally:
        src.close()


# ── conformance: drive the REAL server over a REAL socket, judge the wire ──────
class _ReplayDataSource:
    """Yields a fixed list of Batches, one per next_batch(), then clean EOF —
    the same shape DS-WS1's live-conformance test uses to drive serve_one."""

    def __init__(self, batches: List[Batch], sample_clk_hz: int = 100_000_000):
        self._batches = list(batches)
        self._i = 0
        self._sample_clk_hz = sample_clk_hz

    def sample_clk_hz(self) -> int:
        return self._sample_clk_hz

    def apply_config(self, datalogger_id, cfg_word, op="set_config"):
        return None

    def read_stats(self):
        return None

    def next_batch(self) -> Optional[Batch]:
        if self._i >= len(self._batches):
            return None
        b = self._batches[self._i]
        self._i += 1
        return b


_HELO_OK_PAYLOAD = {"consumer": "de-c0-capture-correctness"}


def _connect_with_retry(sock_path: Path, attempts: int = 40,
                        backoff_s: float = 0.05) -> socket.socket:
    last = None
    for _ in range(attempts):
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock_path))
            return c
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last = exc
            time.sleep(backoff_s)
    raise RuntimeError(f"could not connect to {sock_path}: {last}")


def _read_one_frame(unp: "msgpack.Unpacker", conn: socket.socket,
                    deadline_s: float = 2.0):
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        for obj in unp:
            assert isinstance(obj, list) and len(obj) == 2
            return obj[0], obj[1]
        conn.settimeout(max(0.01, end - time.monotonic()))
        try:
            data = conn.recv(8192)
        except socket.timeout:
            continue
        if not data:
            raise RuntimeError("connection closed before frame received")
        unp.feed(data)
    raise TimeoutError("timed out waiting for the next frame")


def capture_session(batches: List[Batch], sample_clk_hz: int = 100_000_000):
    """Drive serve_one() with ``batches`` over a real AF_UNIX socket; return the
    full BIDIRECTIONAL session as check_session() wants it:
    ``(helo, captured, session_frames, server_rc)`` where ``session_frames`` is
    ``[(direction, tag, payload), ...]`` in wire order (HELO, HELO_OK, data…)."""
    source = _ReplayDataSource(batches, sample_clk_hz)
    with tempfile.TemporaryDirectory() as td:
        sock_path = Path(td) / "de_c0.sock"
        rc: list = []
        th = threading.Thread(
            target=lambda: rc.append(
                serve_one(sock_path, source, connect_timeout_s=3.0)),
            daemon=True)
        th.start()
        conn = _connect_with_retry(sock_path)
        try:
            unp = msgpack.Unpacker(raw=False)
            tag, helo = _read_one_frame(unp, conn)
            assert tag == proto.TAG_HELO, f"expected HELO, got tag {tag}"
            conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, _HELO_OK_PAYLOAD))
            captured = []
            for _ in range(500):
                tag, payload = _read_one_frame(unp, conn)
                if tag == proto.TAG_EOF:
                    break
                captured.append((tag, payload))
        finally:
            try:
                conn.close()
            except OSError:
                pass
        th.join(timeout=3.0)
    session = [("p2c", proto.TAG_HELO, helo),
               ("c2p", proto.TAG_HELO_OK, _HELO_OK_PAYLOAD)]
    session += [("p2c", t, p) for t, p in captured]
    return helo, captured, session, rc


# ── the run: exact payload + conformance per case, fail-closed ─────────────────
@dataclass
class CaseVerdict:
    name: str
    protocol: str
    status: str                       # "PASS" | "FAIL"
    detail: str = ""
    expected: Any = None
    observed: Any = None
    conformance: str = ""             # "ACCEPT" | "REJECT: <fields>"
    fields: List[str] = field(default_factory=list)


def run_case(case: CaptureCase) -> CaseVerdict:
    """Decode the stimulus, drive the real server over a real socket, then judge
    ONE captured session two ways that cannot be satisfied independently:

      (1) the vendored WS-C0 checker ACCEPTs the wire, AND
      (2) the exact decoded payload/ordering read back OUT OF THE CAPTURED WIRE
          FRAMES matches ``case.expected``.

    Both read the same ``captured`` frames, so a decoded event the producer's
    WS-C0 guard refused to broadcast (wrong channel_id, renamed/absent role key,
    dropped START/STOP) is absent from the wire and fails (2) — it can no longer
    slip through by matching an in-process Batch the socket never carried."""
    batch = decode_case(case)
    if batch is None:
        return CaseVerdict(case.name, case.protocol, "FAIL",
                           "decode produced no batch")

    # Drive the real server/socket ONCE; everything below judges these frames.
    helo, captured, session, rc = capture_session([batch])

    # (2a) the produced stream conforms to WS-C0 (the judgement the released
    #      viewer is bound to; RW-WS1's own C++ checker is external — see module
    #      docstring). A non-conformant batch is refused by the producer, so this
    #      ACCEPT is over exactly the frames a subscriber received.
    result = C.check_session(session)
    if not result.accepted:
        return CaseVerdict(case.name, case.protocol, "FAIL",
                           "capture stream is NOT WS-C0 conformant",
                           expected=case.expected, observed=None,
                           conformance="REJECT: " + "; ".join(map(str, result.failures)),
                           fields=list(result.fields))
    if rc and rc[0] != 0:
        return CaseVerdict(case.name, case.protocol, "FAIL",
                           f"server exited non-zero ({rc[0]})",
                           expected=case.expected, observed=None)

    # (2b) EXACT decoded payload + ordering, READ FROM THE WIRE. Fail closed when
    #      the expected data frames never reached the socket (guard-dropped): an
    #      empty/short observed cannot be rounded up to a pass.
    # The oracle consumes the source clock from the HELO a real subscriber saw,
    # not an independent board constant.  If producer and deployment disagree,
    # HELO exposes that disagreement; the oracle cannot conceal it by carrying
    # the same stale constant in a second code path.
    observed = case.extract(captured, case.channel_id, helo["sample_clk_hz"])
    if observed != case.expected:
        detail = ("no expected data frames reached the wire (guard-dropped?)"
                  if not observed else
                  "decoded wire payload does not match expected, in order")
        return CaseVerdict(case.name, case.protocol, "FAIL", detail,
                           expected=case.expected, observed=observed,
                           conformance="ACCEPT")

    return CaseVerdict(case.name, case.protocol, "PASS",
                       f"{len(observed)} decoded wire element(s) exact + WS-C0 ACCEPT",
                       expected=case.expected, observed=observed,
                       conformance="ACCEPT")


def run_all(cases: Optional[List[CaptureCase]] = None) -> Tuple[List[CaseVerdict], int]:
    """Run every case; return (verdicts, exit_code). Fail-closed: any non-PASS
    verdict makes the exit code 1 (mirrors run_self_test — a capture check that
    could not confirm is missing evidence, not success)."""
    cases = cases if cases is not None else known_cases()
    verdicts = [run_case(c) for c in cases]
    code = 0 if all(v.status == "PASS" for v in verdicts) and verdicts else 1
    return verdicts, code


def _verdict_json(verdicts: List[CaseVerdict]) -> dict:
    counts = {"PASS": 0, "FAIL": 0}
    for v in verdicts:
        counts[v.status] = counts.get(v.status, 0) + 1
    return {
        "harness": "de_c0_capture_correctness",
        "board_free": True,
        "contract": "wave_stream_v1",
        "summary": counts,
        "cases": [
            {
                "name": v.name, "protocol": v.protocol, "status": v.status,
                "detail": v.detail, "expected": v.expected, "observed": v.observed,
                "conformance": v.conformance, "fields": v.fields,
            }
            for v in verdicts
        ],
    }


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="DE-C0 real-bus capture-correctness harness (board-free)")
    p.add_argument("--out", type=Path, default=None,
                   help="Write the verdict JSON to this path.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-5s %(name)s %(message)s")

    try:
        verdicts, code = run_all()
    except Exception as exc:  # noqa: BLE001 — distinguish "harness broken" (2)
        logging.getLogger("de_c0").error("harness error: %s", exc)
        import traceback
        traceback.print_exc()
        return 2

    doc = _verdict_json(verdicts)
    if args.out:
        args.out.write_text(json.dumps(doc, indent=2))

    print("=== DE-C0 capture correctness (board-free oracle; live DE25 witness: DS-P2.101) ===")
    for v in verdicts:
        print(f"  [{v.status:<4}] {v.protocol:<5} {v.name:<14} {v.detail}")
        if v.status != "PASS":
            print(f"          expected={v.expected!r}")
            print(f"          observed={v.observed!r}")
            if v.conformance:
                print(f"          {v.conformance}")
    print(f"  PASS={doc['summary']['PASS']} FAIL={doc['summary']['FAIL']} "
          f"total={len(verdicts)}")
    return code


if __name__ == "__main__":
    sys.exit(main())
