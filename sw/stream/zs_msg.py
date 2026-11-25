# rr unknown-llm (AI-generated)
"""zs_msg_header_t parser — the common 20-byte header attached to every
PL-emitted protocol event in the DDR ring buffer.

Layout-of-record: docs/software/data_plane/message_formats/common_header.md.

C-struct (packed, little-endian on the PS):

    typedef struct __attribute__((packed)) {
        uint16_t magic;        // 'ZS' == 0x5A53
        uint8_t  version;      // 0x01 today
        uint8_t  header_len;   // 5 (= 5×32-bit words = 20 bytes)
        uint8_t  datalogger_id;   // dynamic instance ID
        uint8_t  msg_class;    // DATA=0, STATUS=1, TRIGGER=2
        uint16_t msg_type;     // Severity(15:12)|Domain(11:8)|EventID(7:0)
        uint64_t timestamp;    // 64-bit free-running counter (lower 48 used)
        uint16_t payload_len;  // bytes following the header
        uint16_t flags;        // generic flags (overflow, truncated, ...)
    } zs_msg_header_t;
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# Magic value as the LE-uint16 interpretation of bytes 'Z','S' on PS ARM.
# In memory: bytes [0x53, 0x5A]; as struct.unpack('<H', ...) → 0x5A53.
ZS_MAGIC = 0x5A53

HEADER_STRUCT = struct.Struct("<HBBBBHQHH")
HEADER_SIZE = HEADER_STRUCT.size  # 20
assert HEADER_SIZE == 20, "header layout drift — see common_header.md"


# msg_type bit slicing per event_and_datalogger_id_spec.md
def severity_of(msg_type: int) -> int:
    """[15:12] — INFO=0, WARN=1, ERROR=2, DEBUG=3, CONTROL=4."""
    return (msg_type >> 12) & 0xF


def domain_of(msg_type: int) -> int:
    """[11:8] — SYS=0, UART=1, I2C=2, SPI=3, I2S=4, QSPI=5, EDGE=6, TRIG=7."""
    return (msg_type >> 8) & 0xF


def event_id_of(msg_type: int) -> int:
    """[7:0] — domain-specific event index (UART_FRAME_OK=0x00, ...)."""
    return msg_type & 0xFF


# msg_class enum
CLASS_DATA    = 0
CLASS_STATUS  = 1
CLASS_TRIGGER = 2

# >>> GENERATED severity from system_ids.yml by id_gen.py (DS-P3.70) — DO NOT EDIT >>>
# Severity enum (msg_type[15:12]).
SEV_INFO    = 0x0
SEV_WARN    = 0x1
SEV_ERROR   = 0x2
SEV_DEBUG   = 0x3
SEV_CONTROL = 0x4
# <<< END GENERATED severity <<<

# >>> GENERATED error_events from system_ids.yml by id_gen.py (DS-P3.70) — DO NOT EDIT >>>
# Cross-domain error-provenance codes (event_id[7:0], reserved 0xF0-0xFF).
ERR_EVT_INTERNAL_FIFO_OVERFLOW = 0xF0
ERR_EVT_ARBITER_DROP           = 0xF1
ERR_EVT_RING_FULL              = 0xF2
ERR_EVT_DECODE_ERROR           = 0xF3
# <<< END GENERATED error_events <<<

# >>> GENERATED domains from system_ids.yml by id_gen.py (DS-P3.57) — DO NOT EDIT >>>
# Domain enum (msg_type[11:8])
DOMAIN_SYS     = 0x0
DOMAIN_UART    = 0x1
DOMAIN_I2C     = 0x2
DOMAIN_SPI     = 0x3
DOMAIN_I2S     = 0x4
DOMAIN_QSPI    = 0x5
DOMAIN_EDGE    = 0x6
DOMAIN_TRIG    = 0x7
DOMAIN_JTAG    = 0x8
DOMAIN_MDIO    = 0x9
DOMAIN_CAN     = 0xA
DOMAIN_ONEWIRE = 0xB
DOMAIN_LIN     = 0xC
DOMAIN_EYE     = 0xD

# Human-readable domain names (for annotation text).
DOMAIN_NAMES = {
    DOMAIN_SYS:     "sys",
    DOMAIN_UART:    "uart",
    DOMAIN_I2C:     "i2c",
    DOMAIN_SPI:     "spi",
    DOMAIN_I2S:     "i2s",
    DOMAIN_QSPI:    "qspi",
    DOMAIN_EDGE:    "edge",
    DOMAIN_TRIG:    "trig",
    DOMAIN_JTAG:    "jtag",
    DOMAIN_MDIO:    "mdio",
    DOMAIN_CAN:     "can",
    DOMAIN_ONEWIRE: "onewire",
    DOMAIN_LIN:     "lin",
    DOMAIN_EYE:     "eye",
}
# <<< END GENERATED domains <<<


@dataclass(frozen=True)
class ZsHeader:
    magic: int
    version: int
    header_len: int      # in 32-bit words (5 today)
    datalogger_id: int
    msg_class: int
    msg_type: int
    timestamp: int       # 64-bit (low 48 are the global timestamp)
    payload_len: int     # bytes following the header
    flags: int

    @property
    def severity(self) -> int:
        return severity_of(self.msg_type)

    @property
    def domain(self) -> int:
        return domain_of(self.msg_type)

    @property
    def event_id(self) -> int:
        return event_id_of(self.msg_type)

    @property
    def is_error(self) -> bool:
        return self.severity == SEV_ERROR

    @property
    def total_size(self) -> int:
        """Header + payload bytes — the consumer advances the read pointer by this much."""
        return HEADER_SIZE + self.payload_len


def parse_header(buf: bytes, offset: int = 0) -> ZsHeader:
    """Unpack a 20-byte zs_msg_header_t at `offset` in `buf`.

    Raises ValueError on magic mismatch — the consumer should treat that
    as a ring-buffer resync trigger (advance read pointer by 4 bytes and
    try again until magic re-aligns).
    """
    if len(buf) - offset < HEADER_SIZE:
        raise ValueError(f"short read: need {HEADER_SIZE}B at offset {offset}, got {len(buf) - offset}B")
    magic, version, header_len, datalogger_id, msg_class, msg_type, timestamp, payload_len, flags = \
        HEADER_STRUCT.unpack_from(buf, offset)
    if magic != ZS_MAGIC:
        raise ValueError(f"bad magic {magic:#06x} at offset {offset} (want {ZS_MAGIC:#06x})")
    return ZsHeader(
        magic=magic,
        version=version,
        header_len=header_len,
        datalogger_id=datalogger_id,
        msg_class=msg_class,
        msg_type=msg_type,
        timestamp=timestamp,
        payload_len=payload_len,
        flags=flags,
    )


def pack_header(header: ZsHeader) -> bytes:
    """Inverse of parse_header — used by tests + future PS-side fixtures."""
    return HEADER_STRUCT.pack(
        header.magic,
        header.version,
        header.header_len,
        header.datalogger_id,
        header.msg_class,
        header.msg_type,
        header.timestamp,
        header.payload_len,
        header.flags,
    )


def make_header(datalogger_id: int,
                domain: int,
                event_id: int = 0,
                severity: int = SEV_INFO,
                msg_class: int = CLASS_DATA,
                timestamp: int = 0,
                payload_len: int = 0,
                flags: int = 0,
                version: int = 1) -> ZsHeader:
    """Convenience builder — assembles msg_type from severity|domain|event_id."""
    if not (0 <= severity <= 0xF):
        raise ValueError(f"severity {severity} out of 4-bit range")
    if not (0 <= domain <= 0xF):
        raise ValueError(f"domain {domain} out of 4-bit range")
    if not (0 <= event_id <= 0xFF):
        raise ValueError(f"event_id {event_id} out of 8-bit range")
    msg_type = (severity << 12) | (domain << 8) | event_id
    return ZsHeader(
        magic=ZS_MAGIC,
        version=version,
        header_len=HEADER_SIZE // 4,
        datalogger_id=datalogger_id,
        msg_class=msg_class,
        msg_type=msg_type,
        timestamp=timestamp,
        payload_len=payload_len,
        flags=flags,
    )


# ─── EDGE_BATCH payload (DS-P2.47) ─────────────────────────────────────────
#
# One zs_msg whose msg_type = SEV_INFO|DOMAIN_EDGE|EVT_EDGE_BATCH carries N
# packed edge_event_t records in its payload. Layout per
# docs/software/data_plane/message_formats/payloads/edge_batch.md.

EVT_EDGE_EVENT = 0x00   # single-event payload (legacy default)
EVT_EDGE_BATCH = 0x01   # batched payload — N × edge_event_t
# DS-T2.84: an edge_datalogger ENABLED/DISABLED lifecycle frame (DS-T2.43), framed
# on its own (zs_msg_pkg.vhd C_ZS_EVT_EDGE_LIFECYCLE). Payload: the 32-bit
# lifecycle word, LE — byte 0 is the code below. It never rides an EDGE_BATCH: a
# batch entry keeps only S/F/E, and the lifecycle type "100" reads there as an
# S=1/E=0 "line is low" readback. 0x08 is outside the 0x00-0x07 S/F/E tags the
# gather puts on edge data words, which is what the fabric routes on.
EVT_EDGE_LIFECYCLE = 0x08
EDGE_LIFECYCLE_CODES = {0x03: "enabled", 0x00: "disabled"}
# DS-P2.16: one edge_datalogger Stream-RLE word, framed on its own
# (zs_msg_pkg.vhd C_ZS_EVT_EDGE_RLE). Payload: the 32-bit word, LE. Decoded by
# edge_capture.EdgeRleDecoder; its [26:24] is an RLE word type, not S/F/E.
EVT_EDGE_RLE = 0x09

EDGE_EVENT_STRUCT = struct.Struct("<HBB")   # ts_delta, channel, flags
EDGE_EVENT_SIZE   = EDGE_EVENT_STRUCT.size  # 4

EDGE_FLAG_E = 0x01      # edge polarity / level
EDGE_FLAG_F = 0x02      # FIFO pressure
EDGE_FLAG_S = 0x04      # send / read-event


@dataclass(frozen=True)
class EdgeEvent:
    """One entry inside an EDGE_BATCH payload.

    `ts_64b` is the absolute clk_sys-tick timestamp reconstructed from the
    batch anchor (header.timestamp) plus the entry's `ts_delta`. On the
    parse side it is always the absolute value; on the pack side, callers
    pass absolute timestamps and the packer computes deltas against the
    anchor.
    """
    ts_64b: int
    channel: int    # 0..255 (5b device_id zero-extended)
    s: bool = False
    f: bool = False
    e: bool = False

    @property
    def flags_byte(self) -> int:
        return ((self.s & 1) << 2) | ((self.f & 1) << 1) | (self.e & 1)


def pack_edge_batch(datalogger_id: int,
                    channel: int,
                    events: list[EdgeEvent],
                    anchor_ts: int | None = None,
                    flags: int = 0) -> bytes:
    """Pack `events` into one zs_msg with an EDGE_BATCH payload.

    Returns the full wire bytes — header + payload. Raises ValueError if:
      * events is empty (the framer never emits payload_len==0 batches),
      * any event's ts_delta from the anchor would overflow uint16,
      * any event.channel disagrees with the batch `channel` argument,
      * `channel` is out of byte range.

    If `anchor_ts` is None, the first event's `ts_64b` is used as anchor.
    """
    if not events:
        raise ValueError("EDGE_BATCH payload must carry at least 1 event")
    if not (0 <= channel <= 0xFF):
        raise ValueError(f"channel {channel} out of byte range")

    if anchor_ts is None:
        anchor_ts = events[0].ts_64b

    chunks = []
    for i, ev in enumerate(events):
        if ev.channel != channel:
            raise ValueError(
                f"event[{i}].channel={ev.channel} mismatches batch "
                f"channel={channel} — v1 requires single-channel batches")
        delta = ev.ts_64b - anchor_ts
        if not (0 <= delta <= 0xFFFF):
            raise ValueError(
                f"event[{i}] ts_delta={delta} out of uint16 range — "
                f"batch must close before this event (anchor={anchor_ts}, "
                f"ev_ts={ev.ts_64b})")
        chunks.append(EDGE_EVENT_STRUCT.pack(delta, channel, ev.flags_byte))

    payload = b"".join(chunks)
    header = make_header(
        datalogger_id=datalogger_id,
        domain=DOMAIN_EDGE,
        event_id=EVT_EDGE_BATCH,
        severity=SEV_INFO,
        msg_class=CLASS_DATA,
        timestamp=anchor_ts,
        payload_len=len(payload),
        flags=flags,
    )
    return pack_header(header) + payload


def parse_edge_batch(buf: bytes, offset: int = 0) -> tuple[ZsHeader, list[EdgeEvent]]:
    """Inverse of pack_edge_batch — returns (header, events).

    Validates: magic, msg_type matches EDGE_BATCH, payload_len is a
    positive multiple of 4, every entry's channel matches the header's
    datalogger_id. Raises ValueError on any structural mismatch.
    """
    header = parse_header(buf, offset)
    if header.domain != DOMAIN_EDGE or header.event_id != EVT_EDGE_BATCH:
        raise ValueError(
            f"not an EDGE_BATCH frame: msg_type={header.msg_type:#06x} "
            f"(want domain={DOMAIN_EDGE}, event_id={EVT_EDGE_BATCH:#04x})")
    if header.payload_len == 0:
        raise ValueError("EDGE_BATCH with payload_len=0 is invalid")
    if header.payload_len % EDGE_EVENT_SIZE != 0:
        raise ValueError(
            f"EDGE_BATCH payload_len={header.payload_len} is not a multiple "
            f"of {EDGE_EVENT_SIZE}")
    n = header.payload_len // EDGE_EVENT_SIZE

    payload_start = offset + HEADER_SIZE
    if len(buf) - payload_start < header.payload_len:
        raise ValueError(
            f"short read: need {header.payload_len}B of payload, got "
            f"{len(buf) - payload_start}B")

    events: list[EdgeEvent] = []
    for i in range(n):
        ts_delta, channel, flags = EDGE_EVENT_STRUCT.unpack_from(
            buf, payload_start + i * EDGE_EVENT_SIZE)
        if channel != header.datalogger_id:
            raise ValueError(
                f"event[{i}].channel={channel} mismatches header.datalogger_id="
                f"{header.datalogger_id} — v1 requires single-channel batches")
        events.append(EdgeEvent(
            ts_64b=header.timestamp + ts_delta,
            channel=channel,
            s=bool(flags & EDGE_FLAG_S),
            f=bool(flags & EDGE_FLAG_F),
            e=bool(flags & EDGE_FLAG_E),
        ))
    return header, events


# ─── EDGE_MAXFREQ payload (DS-P3.117) ──────────────────────────────────────
#
# One zs_msg whose msg_type = SEV_INFO|DOMAIN_EDGE|EVT_EDGE_MAXFREQ carries the
# FPGA-AUTHORITATIVE max-frequency datum for ONE edge-datalogger line: the smallest
# validated edge-to-edge interval (half-period) the edge_datalogger latched on that
# line since the last re-arm (edge_datalogger.vhd ED_MAX_INTERVAL / ED_MAX_NEW).
# This is the IN-BAND carrier — it replaces the reverted out-of-band
# MAXFREQ_IVAL_EDGE<k> regbank CSR slots — so RouteWave can correlate the max
# frequency in time with the live edges on the same stream.
#
# header.datalogger_id already carries the GLOBAL device id of the line (stamped by
# the emitter, mirroring every other edge frame). The payload is intentionally
# tiny: the interval plus the channel echoed in-payload (self-describing for a
# log replay that lost the header context) + the register width so the consumer
# applies the right all-ones sentinel mask.
#
# Payload (LE, 8 bytes):
#   offset 0  min_interval u32   latched smallest edge-to-edge interval, CLK
#                                 ticks; all-ones in `ival_width` bits = no edge
#                                 measured yet / just re-armed (sentinel).
#   offset 4  channel      u8    global edge device id (== header.datalogger_id)
#   offset 5  ival_width   u8    width (bits) of the RTL interval register, for
#                                 the sentinel mask (24 in this build)
#   offset 6  flags        u16   reserved (0)
EVT_EDGE_MAXFREQ = 0x02

EDGE_MAXFREQ_STRUCT = struct.Struct("<IBBH")   # min_interval, channel, ival_width, flags
EDGE_MAXFREQ_SIZE   = EDGE_MAXFREQ_STRUCT.size  # 8


@dataclass(frozen=True)
class EdgeMaxFreq:
    """One EVT_EDGE_MAXFREQ payload — the latched min interval for one line.

    `min_interval` is the raw register value (CLK ticks). `is_sentinel` is True
    when it is the all-ones "no edge measured yet" value for `ival_width` bits.
    """
    channel: int
    min_interval: int
    ival_width: int = 24

    @property
    def is_sentinel(self) -> bool:
        mask = (1 << self.ival_width) - 1
        return (self.min_interval & mask) == mask


def pack_edge_maxfreq(maxfreq: EdgeMaxFreq, datalogger_id: int | None = None,
                      timestamp: int = 0, flags: int = 0) -> bytes:
    """Pack an EdgeMaxFreq into a full DOMAIN_EDGE/EVT_EDGE_MAXFREQ zs_msg.

    `datalogger_id` defaults to `maxfreq.channel` (the global edge device id) so the
    header and payload agree by construction. Round-trips with parse_edge_maxfreq.
    """
    if datalogger_id is None:
        datalogger_id = maxfreq.channel
    if not (0 <= maxfreq.channel <= 0xFF):
        raise ValueError(f"channel {maxfreq.channel} out of byte range")
    if not (4 <= maxfreq.ival_width <= 32):
        raise ValueError(f"ival_width {maxfreq.ival_width} out of [4,32]")
    payload = EDGE_MAXFREQ_STRUCT.pack(
        maxfreq.min_interval & 0xFFFFFFFF,
        maxfreq.channel & 0xFF,
        maxfreq.ival_width & 0xFF,
        flags & 0xFFFF,
    )
    header = make_header(
        datalogger_id=datalogger_id,
        domain=DOMAIN_EDGE,
        event_id=EVT_EDGE_MAXFREQ,
        severity=SEV_INFO,
        msg_class=CLASS_DATA,
        timestamp=timestamp,
        payload_len=len(payload),
        flags=flags,
    )
    return pack_header(header) + payload


def parse_edge_maxfreq(buf: bytes, offset: int = 0) -> tuple[ZsHeader, EdgeMaxFreq]:
    """Inverse of pack_edge_maxfreq — returns (header, EdgeMaxFreq).

    Validates: magic, msg_type matches EDGE_MAXFREQ, payload_len == 8, and the
    in-payload channel matches header.datalogger_id (single source of truth).
    """
    header = parse_header(buf, offset)
    if header.domain != DOMAIN_EDGE or header.event_id != EVT_EDGE_MAXFREQ:
        raise ValueError(
            f"not an EDGE_MAXFREQ frame: msg_type={header.msg_type:#06x} "
            f"(want domain={DOMAIN_EDGE}, event_id={EVT_EDGE_MAXFREQ:#04x})")
    if header.payload_len != EDGE_MAXFREQ_SIZE:
        raise ValueError(
            f"EDGE_MAXFREQ payload_len={header.payload_len} must be "
            f"{EDGE_MAXFREQ_SIZE}")
    payload_start = offset + HEADER_SIZE
    if len(buf) - payload_start < EDGE_MAXFREQ_SIZE:
        raise ValueError(
            f"short read: need {EDGE_MAXFREQ_SIZE}B of payload, got "
            f"{len(buf) - payload_start}B")
    min_interval, channel, ival_width, _flags = \
        EDGE_MAXFREQ_STRUCT.unpack_from(buf, payload_start)
    if channel != header.datalogger_id:
        raise ValueError(
            f"payload channel={channel} mismatches header.datalogger_id="
            f"{header.datalogger_id}")
    return header, EdgeMaxFreq(
        channel=channel,
        min_interval=min_interval,
        ival_width=ival_width,
    )


# ─── EYE_POINT payload (DS-P2.66) ──────────────────────────────────────────
#
# One zs_msg whose msg_type = SEV_INFO|DOMAIN_EYE|EVT_EYE_POINT carries ONE
# measured cell of the PMOD eye-scan bathtub: the BER at a single
# (frequency, sample-phase) grid point. Unlike the passive dataloggers, these
# frames are PRODUCED BY THE PS (eye_sweep.py) — the PL measures one point on
# command (eye_engine), the PS reads the counters and frames the result into
# the SAME wave_stream path so RouteWave's BathtubAnalyzer (LT-P3.217) renders
# the eye live.
#
# Payload layout (LE, 28 bytes — v2 since DS-P3.118/LT-P3.219):
#   offset 0  freq_sel    u8    clk_drp frequency-profile index (eye Y axis)
#   offset 1  phase_sel   u8    idelay_scan tap 0..31           (fine/legacy axis)
#   offset 2  flags       u8    bit0 = locked (checker synced this point)
#   offset 3  reserved    u8
#   offset 4  freq_hz     u32   actual TX bit-clock for this profile (Hz)
#   offset 8  err_count   u64   post-lock detected bit errors  (BER numerator)
#   offset 16 bit_count   u64   post-lock measured bits        (BER denominator)
#   offset 24 phase_ps    i32   ABSOLUTE sample-phase offset in ps from nominal
#                               (DS-P3.118: MMCM fine-PS axis = ps_count*~17.86ps;
#                                IDELAY interim = tap*~78ps). The consumer's TRUE
#                                UI-fraction axis (LT-P3.219) = phase_ps*freq_hz/1e12
#                                — phase_sel alone is NOT a UI fraction (<1 UI above
#                                ~125MHz on the 32-tap line). Axis-agnostic by design.
#
# BER = err_count / bit_count (computed by the consumer). bit_count == 0 means
# the point never locked → "no measurement" (eye fully closed here), NOT BER=0.
# Backward compat: a 24-byte (v1, pre-phase_ps) payload still parses (phase_ps=0).
EVT_EYE_POINT = 0x00

EYE_POINT_STRUCT    = struct.Struct("<BBBBIQQ")   # v1 base
EYE_POINT_SIZE_V1   = EYE_POINT_STRUCT.size        # 24 (legacy, no phase_ps)
_EYE_POINT_PS       = struct.Struct("<i")          # v2 append: phase_ps (signed)
EYE_POINT_SIZE      = EYE_POINT_SIZE_V1 + _EYE_POINT_PS.size  # 28

EYE_FLAG_LOCKED = 0x01


@dataclass(frozen=True)
class EyePoint:
    """One measured (freq, phase) cell of the eye/bathtub."""
    freq_sel: int
    phase_sel: int
    err_count: int
    bit_count: int
    locked: bool
    freq_hz: int = 0
    phase_ps: int = 0      # absolute sample-phase offset (ps) — true-UI X axis

    @property
    def ber(self) -> float:
        """Bit-error ratio, or 1.0 when nothing was measured (unlocked/closed)."""
        return (self.err_count / self.bit_count) if self.bit_count else 1.0

    @property
    def phase_ui(self) -> float:
        """Sample-phase offset as a fraction of the unit interval (true-UI axis).
        0.0 when freq_hz is unknown."""
        return (self.phase_ps * self.freq_hz / 1e12) if self.freq_hz else 0.0


def pack_eye_point(point: EyePoint, datalogger_id: int = 0,
                   timestamp: int = 0) -> bytes:
    """Pack one EyePoint into a full DOMAIN_EYE zs_msg (header + 28B payload)."""
    payload = EYE_POINT_STRUCT.pack(
        point.freq_sel & 0xFF,
        point.phase_sel & 0xFF,
        (EYE_FLAG_LOCKED if point.locked else 0),
        0,
        point.freq_hz & 0xFFFFFFFF,
        point.err_count & 0xFFFFFFFFFFFFFFFF,
        point.bit_count & 0xFFFFFFFFFFFFFFFF,
    ) + _EYE_POINT_PS.pack(_clamp_i32(point.phase_ps))
    header = make_header(
        datalogger_id=datalogger_id,
        domain=DOMAIN_EYE,
        event_id=EVT_EYE_POINT,
        severity=SEV_INFO,
        timestamp=timestamp,
        payload_len=len(payload),
    )
    return pack_header(header) + payload


def parse_eye_point(payload: bytes) -> EyePoint:
    """Inverse of pack_eye_point's payload half — used by decode_eye + tests.
    Accepts both v2 (28B, with phase_ps) and legacy v1 (24B, phase_ps=0)."""
    if len(payload) < EYE_POINT_SIZE_V1:
        raise ValueError(
            f"EYE_POINT payload too short: need {EYE_POINT_SIZE_V1}B, got {len(payload)}B")
    freq_sel, phase_sel, flags, _rsvd, freq_hz, err, bits = \
        EYE_POINT_STRUCT.unpack_from(payload, 0)
    phase_ps = 0
    if len(payload) >= EYE_POINT_SIZE:
        (phase_ps,) = _EYE_POINT_PS.unpack_from(payload, EYE_POINT_SIZE_V1)
    return EyePoint(
        freq_sel=freq_sel,
        phase_sel=phase_sel,
        err_count=err,
        bit_count=bits,
        locked=bool(flags & EYE_FLAG_LOCKED),
        freq_hz=freq_hz,
        phase_ps=phase_ps,
    )


def _clamp_i32(v: int) -> int:
    return max(-0x80000000, min(0x7FFFFFFF, int(v)))


# ─── EYE_SUMMARY payload (DS-P3.119) ───────────────────────────────────────
#
# One zs_msg whose msg_type = SEV_INFO|DOMAIN_EYE|EVT_EYE_SUMMARY carries the
# daemon-AUTHORITATIVE link-margin verdict for a whole frequency sweep: the
# maximum usable bit rate plus the per-frequency eye-width curve. RouteWave's
# 'link margin' view (LT-P3.243) consumes this in PREFERENCE to recomputing the
# max rate host-side, and uses the per-freq partial-eye flag for its guard.
#
# Header (LE, 16 bytes):
#   offset 0  max_freq_hz   u32   highest freq with eye_width_ui >= margin (0=none)
#   offset 4  ber_threshold f32   the BER pass threshold these widths were judged at
#   offset 8  eye_margin_ui f32   the eye-width margin (UI) the pass test used
#   offset 12 n_freqs       u16   number of per-frequency records following
#   offset 14 flags         u16   reserved (0)
# Then n_freqs records (LE, 12 bytes each):
#   offset 0  freq_hz       u32   the candidate bit-clock (Hz)
#   offset 4  eye_width_ps  u32   open eye width at this freq (ps) — authoritative
#   offset 8  eye_width_mui u16   eye width in milli-UI (width_ui*1000, convenience)
#   offset 10 rec_flags     u8    bit0=passed (>=margin), bit1=partial_eye (<1 UI scan)
#   offset 11 reserved      u8
EVT_EYE_SUMMARY = 0x01

EYE_SUMMARY_HDR = struct.Struct("<IffHH")   # 16
EYE_SUMMARY_REC = struct.Struct("<IIHBB")   # 12

EYE_SREC_PASSED  = 0x01
EYE_SREC_PARTIAL = 0x02


@dataclass(frozen=True)
class EyeSummaryFreq:
    freq_hz: int
    eye_width_ps: int
    eye_width_ui: float
    passed: bool
    partial: bool = False


@dataclass(frozen=True)
class EyeSummary:
    max_freq_hz: int
    ber_threshold: float
    eye_margin_ui: float
    freqs: list           # List[EyeSummaryFreq]


def pack_eye_summary(summary: EyeSummary, datalogger_id: int = 0,
                     timestamp: int = 0) -> bytes:
    """Pack an EyeSummary into a full DOMAIN_EYE/EVT_EYE_SUMMARY zs_msg."""
    payload = EYE_SUMMARY_HDR.pack(
        summary.max_freq_hz & 0xFFFFFFFF,
        float(summary.ber_threshold),
        float(summary.eye_margin_ui),
        len(summary.freqs) & 0xFFFF,
        0,
    )
    for f in summary.freqs:
        rec_flags = (EYE_SREC_PASSED if f.passed else 0) | \
                    (EYE_SREC_PARTIAL if f.partial else 0)
        payload += EYE_SUMMARY_REC.pack(
            f.freq_hz & 0xFFFFFFFF,
            min(0xFFFFFFFF, max(0, int(round(f.eye_width_ps)))),
            min(0xFFFF, max(0, int(round(f.eye_width_ui * 1000)))),
            rec_flags,
            0,
        )
    header = make_header(
        datalogger_id=datalogger_id,
        domain=DOMAIN_EYE,
        event_id=EVT_EYE_SUMMARY,
        severity=SEV_INFO,
        timestamp=timestamp,
        payload_len=len(payload),
    )
    return pack_header(header) + payload


def parse_eye_summary(payload: bytes) -> EyeSummary:
    """Inverse of pack_eye_summary's payload half."""
    if len(payload) < EYE_SUMMARY_HDR.size:
        raise ValueError(
            f"EYE_SUMMARY payload too short: need {EYE_SUMMARY_HDR.size}B, "
            f"got {len(payload)}B")
    max_freq_hz, ber_threshold, eye_margin_ui, n_freqs, _flags = \
        EYE_SUMMARY_HDR.unpack_from(payload, 0)
    freqs = []
    off = EYE_SUMMARY_HDR.size
    for _ in range(n_freqs):
        if off + EYE_SUMMARY_REC.size > len(payload):
            raise ValueError("EYE_SUMMARY truncated: fewer records than n_freqs")
        freq_hz, width_ps, width_mui, rec_flags, _rsvd = \
            EYE_SUMMARY_REC.unpack_from(payload, off)
        freqs.append(EyeSummaryFreq(
            freq_hz=freq_hz,
            eye_width_ps=width_ps,
            eye_width_ui=width_mui / 1000.0,
            passed=bool(rec_flags & EYE_SREC_PASSED),
            partial=bool(rec_flags & EYE_SREC_PARTIAL),
        ))
        off += EYE_SUMMARY_REC.size
    return EyeSummary(
        max_freq_hz=max_freq_hz,
        ber_threshold=ber_threshold,
        eye_margin_ui=eye_margin_ui,
        freqs=freqs,
    )
