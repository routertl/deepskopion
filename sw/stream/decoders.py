"""Per-protocol payload decoders for zs_msg frames (DS-P1.12).

A decoder turns the raw payload bytes of one zs_msg frame into the
annotation kwargs that RouteWave renders as a human-readable label.
DdrRingDataSource calls :func:`decode_frame` for every parsed header;
when a domain decoder is registered the result lands in the
``Annotation`` payload (kind, text, color, is_error), otherwise the
caller falls back to the generic ``evt=0x.. payload=N B`` text.

Decoder protocol::

    def decode(header, payload, sample_clk_hz) -> DecodedFrame

Registered per domain in :data:`DOMAIN_DECODERS`.  Add a new entry to
land another protocol — see :func:`decode_uart` for the canonical shape.

Decoder exceptions are caught and surfaced as ``<domain>_decode_error``
annotations so a decoder bug never crashes the drain loop.

Spec references:
  * ``docs/software/data_plane/message_formats/event_and_datalogger_id_spec.md``
    — domain + event-ID enums (UART §0x1, I2C §0x2, …)
  * ``docs/software/data_plane/message_formats/payloads/uart.yaml``
    — UART 8-byte payload layout
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from . import zs_msg
from . import protocol_codes   # GENERATED from *_datalogger_protocol.yml (DS-P3.59)


def _ev_code(events: Dict[int, str], name: str) -> int:
    """Reverse-lookup a single-sourced event code by its YAML name (so the
    decoders reference the contract by meaning, not a hand-typed hex)."""
    for code, n in events.items():
        if n == name:
            return code
    raise KeyError(f"event {name!r} absent from generated protocol_codes")


# ── UART (Domain 0x1) ─────────────────────────────────────────────────
#
# msg_type ladder per event_and_datalogger_id_spec.md §UART:
#   0x0100  UART_FRAME_OK         INFO    Valid character received.
#   0x0102  UART_BREAK_DETECTED   INFO    Line held low > frame time.
#   0x2100  UART_PARITY_ERROR     ERROR   Parity calculation mismatch.
#   0x2101  UART_FRAMING_ERROR    ERROR   Missing stop bit.
#   0x2102  UART_OVERRUN_ERROR    ERROR   Internal datalogger buffer full.
#
# header.event_id is the low byte of msg_type (parse_header in
# zs_msg.py masks with 0xFF), so the per-event constants below ignore
# severity — header.is_error already tells us if we're on the ERROR
# ladder.

UART_EVT_FRAME_OK        = 0x00  # 0x0100 (severity=INFO)  / 0x2100 (severity=ERROR / parity)
UART_EVT_FRAMING_ERROR   = 0x01  # 0x2101 (severity=ERROR / framing)
UART_EVT_BREAK_DETECTED  = 0x02  # 0x0102 (severity=INFO)  / 0x2102 (severity=ERROR / overrun)

# UART payload layout (8 bytes LE, per uart.yaml):
#   offset 0  uart_char          u8
#   offset 1  parity_error       u8  (bool)
#   offset 2  frame_error        u8  (bool)
#   offset 3  reserved           u8
#   offset 4  oversample_period  u16 (bit duration in clk_sys cycles)
#   offset 6  reserved           u16
_UART_PAYLOAD_STRUCT = struct.Struct("<BBBBHH")
_UART_PAYLOAD_SIZE = _UART_PAYLOAD_STRUCT.size  # 8


@dataclass(frozen=True)
class DecodedFrame:
    """Decoder output, folded into :class:`data_source.Annotation` by the
    caller.

    Attributes
    ----------
    kind
        Short event tag (e.g. ``"uart_byte"``, ``"uart_err_parity"``).
        Distinguishes per-protocol event kinds within one channel lane.
    text
        Full annotation label — what RouteWave displays.
    color
        Optional hex color override.  ``None`` → caller uses the
        severity-derived default (green for OK, red for error).
    is_error
        Optional explicit error flag.  ``None`` → caller falls back to
        ``header.is_error`` (severity-derived).
    fields
        DS-P3.88: structured, role-tagged decoded fields — the same data the
        ``text`` label renders, but machine-readable so the consumer can show
        them as structured cells (RouteWave per-protocol packet-table columns,
        LT-P3.168) and an agent can read packet contents without parsing
        ``text``.  ``None`` → no structured payload for this frame.

        Values are typed (ints for bytes, bools for ACK, strings for event
        names) — the consumer formats them for display (e.g. ``0x{:02X}``).
        Role keys are the cross-repo contract; keep them stable.  Per-protocol
        key sets (emitted by the decoders below):

          UART byte    {"byte": int, "ascii": str?, "nine_bit": True?}
          UART status  {"status": str}          UART config {"config": int}
          I2C byte     {"data": int, "ack": bool, "datalogger_id": int}
          I2C event    {"event": str, "datalogger_id": int}
          SPI          {"mosi": int} | {"miso": int} | {"event": str}
          JTAG         {"tdi": int} | {"tdo": int} | {"event": str, "bits": int?}

        Carried on the wire under the ANNOTATIONS ``payload`` key (the consumer
        already reserves it; see protocol.py ``_annotations_payload``).
    """
    kind: str
    text: str
    color: Optional[str] = None
    is_error: Optional[bool] = None
    fields: Optional[Dict[str, object]] = None


def _baud_from_oversample(oversample_period: int, clk_hz: int) -> Optional[int]:
    """oversample_period is bit duration in clk_sys cycles.

    ``baud = clk_hz / oversample_period``.  Returns ``None`` when the
    period is 0 (uninitialised — datalogger hasn't measured a bit yet)
    or when the result is nonsensical (negative clk_hz, etc.).
    """
    if oversample_period <= 0 or clk_hz <= 0:
        return None
    return clk_hz // oversample_period


def _printable_glyph(byte_val: int) -> str:
    """Render *byte_val* as a quoted printable char, else empty string.

    Used inline as the suffix to a hex rendering — ``0x55 'U'`` reads
    nicely; ``0x0A`` (newline) just shows the hex.
    """
    if 0x20 <= byte_val < 0x7F:
        return f"'{chr(byte_val)}'"
    return ""


def decode_uart(header: zs_msg.ZsHeader,
                payload: bytes,
                sample_clk_hz: int) -> DecodedFrame:
    """UART: per-char bytes + calibration/status events (1:1 framing).

    Like every other Burst datalogger the gather frames the UART datalogger 1:1, so
    header.event_id is the 3-bit message TYPE and payload[0] the data/status
    byte. Types (single-sourced, uart_datalogger_protocol.yml): payload=0b010,
    payload_9bit=0b011, status=0b100, config=0b001.

    Output text shape::

        0x48 'H'              -- a received character
        0x1A9 (9-bit)         -- a 9-bit character (bit8=1)
        PARITY ERROR          -- a status/error event
    """
    if len(payload) < 1:
        return DecodedFrame("uart_short", "short payload", "#cc3333", True)
    data = payload[0]
    et = header.event_id
    if et in (protocol_codes.UART_TYPES["payload"],
              protocol_codes.UART_TYPES["payload_9bit"]):
        nine = et == protocol_codes.UART_TYPES["payload_9bit"]
        glyph = _printable_glyph(data)
        text = f"0x{data:02X}" + (f" {glyph}" if glyph else "")
        fields: Dict[str, object] = {"byte": data}
        if 0x20 <= data < 0x7F:
            fields["ascii"] = chr(data)
        if nine:
            fields["nine_bit"] = True
        return DecodedFrame("uart_byte", text + (" (9-bit)" if nine else ""),
                            "#4a8c2a", False, fields=fields)
    if et == protocol_codes.UART_TYPES["status"]:
        name = protocol_codes.UART_EVENTS.get(data, f"status0x{data:02X}")
        err = name.endswith("_error") or name.endswith("_timeout")
        return DecodedFrame(f"uart_{name}", name.upper().replace("_", " "),
                            "#cc3333" if err else "#cc8833", err,
                            fields={"status": name})
    if et == protocol_codes.UART_TYPES["config"]:
        return DecodedFrame("uart_config", f"config 0x{data:02X}", "#cc8833", False,
                            fields={"config": data})
    return DecodedFrame("uart_unknown", f"type=0x{et:X} 0x{data:02X}",
                        "#cc8833", False)


# ── JTAG / IEEE-1149.1 (Domain 0x8) ───────────────────────────────────
#
# The jtag_datalogger emits a 32-bit datalogger word that datalogger_gather frames
# 1:1 into a zs_msg (DS-P3.29 pack-at-source, datalogger_gather.vhd:597):
#   * datalogger_id   = device_id  (word[31:27], here 16)
#   * msg_type     = make_msg_type(SEV_INFO, DOMAIN_JTAG, event_id)
#                    where event_id = the 3-bit TYPE field word[26:24]
#   * payload      = the FULL 32-bit word, LE (4 bytes) — so payload[0]
#                    is word[7:0], the message's data/event byte.
#
# header.event_id therefore carries the message TYPE, and payload[0] the
# 8-bit data byte (TDI byte / TDO byte / event code).  Layout owned by
# hw/rtl/datalogger/jtag/jtag_datalogger_protocol.yml (DS-P3.56).

# Type + event codes are SINGLE-SOURCED from jtag_datalogger_protocol.yml via
# protocol_codes_gen.py (DS-P3.59) — never hand-edit; change the YAML contract.
JTAG_T_TDI       = protocol_codes.JTAG_TYPES["payload_tdi"]   # 001 host->device byte
JTAG_T_TDO       = protocol_codes.JTAG_TYPES["payload_tdo"]   # 010 device->host byte
JTAG_T_TAP_STATE = protocol_codes.JTAG_TYPES["tap_state"]     # 011 verbose TAP-FSM state (DS-T3.16)
JTAG_T_EVENT     = protocol_codes.JTAG_TYPES["event"]         # 100 lifecycle / event

# TAP-FSM state names indexed by the 4-bit code in payload[3:0]. Ordering is
# single-sourced from jtag_datalogger_protocol.yml `tap_states` (= the RTL
# jtag_datalogger.vhd StateType'pos): test_logic_reset=0x0 .. update_ir=0xF.
_JTAG_TAP_STATES = (
    "TEST-LOGIC-RESET", "RUN-TEST-IDLE", "SELECT-DR", "CAPTURE-DR", "SHIFT-DR",
    "EXIT1-DR", "PAUSE-DR", "EXIT2-DR", "UPDATE-DR", "SELECT-IR", "CAPTURE-IR",
    "SHIFT-IR", "EXIT1-IR", "PAUSE-IR", "EXIT2-IR", "UPDATE-IR",
)

# The *_END events OR the count of valid bits in the final (sub-)byte into the
# low nibble; 0 == the last byte was a full 8 bits (JTAG-REQ-5).
JTAG_EV_DISABLED  = _ev_code(protocol_codes.JTAG_EVENTS, "disabled")
JTAG_EV_ENABLED   = _ev_code(protocol_codes.JTAG_EVENTS, "enabled")
JTAG_EV_TAP_RESET = _ev_code(protocol_codes.JTAG_EVENTS, "tap_reset")
JTAG_EV_IR_BEGIN  = _ev_code(protocol_codes.JTAG_EVENTS, "ir_shift_begin")
JTAG_EV_IR_END    = _ev_code(protocol_codes.JTAG_EVENTS, "ir_shift_end")
JTAG_EV_DR_BEGIN  = _ev_code(protocol_codes.JTAG_EVENTS, "dr_shift_begin")
JTAG_EV_DR_END    = _ev_code(protocol_codes.JTAG_EVENTS, "dr_shift_end")

_JTAG_EVENT_COLOR  = "#7a5cc8"   # purple — TAP lifecycle / shift bracket
_JTAG_TDI_COLOR    = "#4a8c2a"   # green  — host->device
_JTAG_TDO_COLOR    = "#3a7ca8"   # blue   — device->host
_JTAG_NOTICE_COLOR = "#cc8833"   # amber  — reset / disabled


def _jtag_bits_label(nibble: int) -> str:
    """Final-(sub-)byte valid-bit count → label. 0 means byte-aligned."""
    return "8b" if nibble == 0 else f"{nibble}b"


def _decode_jtag_event(code: int) -> DecodedFrame:
    hi = code & 0xF0
    if code == JTAG_EV_ENABLED:
        return DecodedFrame("jtag_enabled", "ENABLED", _JTAG_TDI_COLOR, False,
                            fields={"event": "enabled"})
    if code == JTAG_EV_DISABLED:
        return DecodedFrame("jtag_disabled", "DISABLED", "#888888", False,
                            fields={"event": "disabled"})
    if code == JTAG_EV_TAP_RESET:
        return DecodedFrame("jtag_tap_reset", "TAP-RESET", _JTAG_NOTICE_COLOR, False,
                            fields={"event": "tap_reset"})
    if hi == JTAG_EV_IR_BEGIN:
        return DecodedFrame("jtag_ir_begin", "IR-SHIFT [", _JTAG_EVENT_COLOR, False,
                            fields={"event": "ir_shift_begin"})
    if hi == JTAG_EV_IR_END:
        return DecodedFrame("jtag_ir_end",
                            f"] IR-SHIFT ({_jtag_bits_label(code & 0x0F)})",
                            _JTAG_EVENT_COLOR, False,
                            fields={"event": "ir_shift_end", "bits": (code & 0x0F) or 8})
    if hi == JTAG_EV_DR_BEGIN:
        return DecodedFrame("jtag_dr_begin", "DR-SHIFT [", _JTAG_EVENT_COLOR, False,
                            fields={"event": "dr_shift_begin"})
    if hi == JTAG_EV_DR_END:
        return DecodedFrame("jtag_dr_end",
                            f"] DR-SHIFT ({_jtag_bits_label(code & 0x0F)})",
                            _JTAG_EVENT_COLOR, False,
                            fields={"event": "dr_shift_end", "bits": (code & 0x0F) or 8})
    return DecodedFrame("jtag_event_unknown", f"event 0x{code:02X}",
                        _JTAG_NOTICE_COLOR, False, fields={"event": f"unknown_0x{code:02X}"})


def decode_jtag(header: zs_msg.ZsHeader,
                payload: bytes,
                sample_clk_hz: int) -> DecodedFrame:
    """Decode one JTAG zs_msg frame into a TAP-lane annotation.

    Output text shape::

        TDI 0xA5            -- host->device shift byte
        TDO 0x3C            -- device->host shift byte
        ENABLED             -- datalogger enabled (lifecycle)
        DR-SHIFT [          -- shift-DR bracket open
        ] DR-SHIFT (5b)     -- shift-DR bracket close, 5 valid bits in last byte
        TAP-RESET           -- TEST-LOGIC-RESET reached
    """
    if len(payload) < 1:
        return DecodedFrame(
            kind="jtag_short_payload",
            text=f"short payload: {len(payload)}B (need >=1)",
            color="#cc3333",
            is_error=True,
        )
    data = payload[0]                 # word[7:0] — data/event byte
    et = header.event_id              # word[26:24] — message TYPE
    if et == JTAG_T_TDI:
        return DecodedFrame("jtag_tdi", f"TDI 0x{data:02X}", _JTAG_TDI_COLOR, False,
                            fields={"tdi": data})
    if et == JTAG_T_TDO:
        return DecodedFrame("jtag_tdo", f"TDO 0x{data:02X}", _JTAG_TDO_COLOR, False,
                            fields={"tdo": data})
    if et == JTAG_T_TAP_STATE:
        s = data & 0xF
        name = _JTAG_TAP_STATES[s]
        return DecodedFrame("jtag_tap_state", f"TAP={name}", _JTAG_EVENT_COLOR, False,
                            fields={"tap_state": s, "state": name})
    if et == JTAG_T_EVENT:
        return _decode_jtag_event(data)
    return DecodedFrame("jtag_unknown_type",
                        f"type=0x{et:X} data=0x{data:02X}",
                        _JTAG_NOTICE_COLOR, False)


# ── I2C (Domain 0x2) — codes single-sourced from i2c_datalogger_protocol.yml ──
# Same 1:1 framing as JTAG: header.event_id = the 3-bit type field, payload[0]
# = the data/event byte. type 0b100=event; 0b00a=payload (a = the ACK bit).
I2C_T_EVENT   = protocol_codes.I2C_TYPES["event"]     # 0b100
I2C_T_PAYLOAD = protocol_codes.I2C_TYPES["payload"]   # 0b00 (+ack bit -> event_id)
# Display labels (presentation) keyed by the YAML event name; codes come from
# the generated table, names map to pretty text here.
_I2C_LABEL = {"start": "START", "stop": "STOP", "repeated_start": "REP-START",
              "enabled": "ENABLED", "disabled": "DISABLED"}


# DS-P3.209: per I2C datalogger, where the decoder is in the current transfer.
# "addr" = the next byte is the address byte (after START / REP-START);
# "read" / "write" = data bytes of a transfer whose address byte had R/W = 1 / 0;
# absent = unknown (stream start, after STOP, or after a lost START).
_I2C_PHASE: Dict[int, str] = {}


def reset_i2c_state() -> None:
    """Forget every datalogger's transfer phase (a new stream, or a test)."""
    _I2C_PHASE.clear()


def decode_i2c(header: zs_msg.ZsHeader, payload: bytes,
               sample_clk_hz: int) -> DecodedFrame:
    """I2C: START/STOP/REP-START events + per-byte ACK/NACK.

        START · 0xA0 ACK · 0x55 ACK · STOP

    DS-P3.209: in a READ the ACK bit after each data byte is the master's, and
    a master NACKs the last byte it wants: that NACK ends the read and is not
    an error. The decoder tracks each datalogger's phase from START / REP-START
    and the address byte's R/W bit, so ``START · 0xA1 ACK · 0xFF NACK · STOP``
    (an i2cget) carries no error. An address NACK (no device), a NACK of a
    written byte, and any NACK whose phase is unknown stay errors."""
    if len(payload) < 1:
        return DecodedFrame("i2c_short", "short payload", "#cc3333", True)
    data = payload[0]
    et = header.event_id
    dlid = header.datalogger_id
    if et == I2C_T_EVENT:
        name = protocol_codes.I2C_EVENTS.get(data, f"evt0x{data:02X}")
        err = name.endswith("_error")
        if name in ("start", "repeated_start"):
            _I2C_PHASE[dlid] = "addr"
        elif name == "stop" or err:
            _I2C_PHASE.pop(dlid, None)
        # DS-P3.155: carry originating datalogger_id so multi-instance
        # I2C (e.g. cam A #5 vs cam B #4) is distinguishable in RouteWave/consumers.
        return DecodedFrame(f"i2c_{name}", _I2C_LABEL.get(name, name.upper()),
                            "#cc3333" if err else "#cc8833", err,
                            fields={"event": name,
                                    "datalogger_id": header.datalogger_id})
    if (et >> 1) == I2C_T_PAYLOAD:                       # payload byte (+ACK bit)
        ack = (et & 1) == 0
        phase = _I2C_PHASE.get(dlid)
        if phase == "addr":
            _I2C_PHASE[dlid] = "read" if data & 1 else "write"
        # DS-P3.209: a NACK in a read's data phase is the master ending the read.
        is_error = (not ack) and phase != "read"
        # DS-P3.155: carry originating datalogger_id so multi-instance
        # I2C (e.g. cam A #5 vs cam B #4) is distinguishable in RouteWave/consumers.
        return DecodedFrame("i2c_byte", f"0x{data:02X} {'ACK' if ack else 'NACK'}",
                            "#4a8c2a" if ack else "#cc8833", is_error,
                            fields={"data": data, "ack": ack,
                                    "datalogger_id": header.datalogger_id})
    return DecodedFrame("i2c_unknown", f"type=0x{et:X} 0x{data:02X}", "#cc8833", False)


# ── SPI (Domain 0x3, via the qspi datalogger in 4-pin mode) — codes single-
# sourced from qspi_datalogger_protocol.yml ──────────────────────────────
SPI_T_MOSI  = protocol_codes.SPI_TYPES["payload_a"]   # 0b001 MOSI / IO0
SPI_T_MISO  = protocol_codes.SPI_TYPES["payload_b"]   # 0b010 MISO / IO1
SPI_T_EVENT = protocol_codes.SPI_TYPES["event"]       # 0b100
_SPI_LABEL = {"cs_down": "CS↓", "cs_up": "CS↑", "cs_up_incomplete": "CS↑(incomplete)",
              "cs_down_new_pol_h": "CS↓(CPOL=1)", "cs_down_new_pol_l": "CS↓(CPOL=0)",
              "start": "ENABLED", "stop": "DISABLED", "timeout": "timeout"}


def decode_spi(header: zs_msg.ZsHeader, payload: bytes,
               sample_clk_hz: int) -> DecodedFrame:
    """SPI: CS down/up events + per-byte MOSI / MISO.

        CS↓ · MOSI 0x9F · MISO 0xEF · CS↑
    """
    if len(payload) < 1:
        return DecodedFrame("spi_short", "short payload", "#cc3333", True)
    data = payload[0]
    et = header.event_id
    if et == SPI_T_MOSI:
        return DecodedFrame("spi_mosi", f"MOSI 0x{data:02X}", "#4a8c2a", False,
                            fields={"mosi": data})
    if et == SPI_T_MISO:
        return DecodedFrame("spi_miso", f"MISO 0x{data:02X}", "#3a7ca8", False,
                            fields={"miso": data})
    if et == SPI_T_EVENT:
        name = protocol_codes.SPI_EVENTS.get(data, f"evt0x{data:02X}")
        err = name == "timeout"
        return DecodedFrame("spi_event", _SPI_LABEL.get(name, name.upper()),
                            "#cc3333" if err else "#cc8833", err,
                            fields={"event": name})
    return DecodedFrame("spi_unknown", f"type=0x{et:X} 0x{data:02X}",
                        "#cc8833", False)


# ── CAN / classic 2.0A/2.0B (Domain 0xA) — codes single-sourced from
# can_datalogger_protocol.yml ────────────────────────────────────────────
# Same 1:1 framing as JTAG: header.event_id = the 3-bit type field, payload[0]
# = the data/event byte. Types: 001 id, 010 data, 011 control, 100 event,
# 101 crc. The cross-frame grouping (frame_begin..frame_end → one CAN frame
# summary) lives in can_reassembler.py — this is the per-message lane.
CAN_T_ID      = protocol_codes.CAN_TYPES["id_payload"]     # 001 arbitration-ID byte
CAN_T_DATA    = protocol_codes.CAN_TYPES["data_payload"]   # 010 data byte
CAN_T_CONTROL = protocol_codes.CAN_TYPES["control"]        # 011 {IDE,RTR,rsvd,DLC}
CAN_T_EVENT   = protocol_codes.CAN_TYPES["event"]          # 100 lifecycle / boundary / error
CAN_T_CRC     = protocol_codes.CAN_TYPES["crc_payload"]    # 101 CRC byte (15 classic / 17|21 FD)
CAN_T_FD_CTRL = protocol_codes.CAN_TYPES["fd_control"]     # 110 CAN-FD {IDE,BRS,ESI,0,DLC}
CAN_T_FD_AUX  = protocol_codes.CAN_TYPES["fd_aux"]         # 111 CAN-FD stuff-count {parity_ok,0000,gray}

CAN_EV_DISABLED    = _ev_code(protocol_codes.CAN_EVENTS, "disabled")
CAN_EV_ENABLED     = _ev_code(protocol_codes.CAN_EVENTS, "enabled")
CAN_EV_FRAME_BEGIN = _ev_code(protocol_codes.CAN_EVENTS, "frame_begin")
CAN_EV_FRAME_END   = _ev_code(protocol_codes.CAN_EVENTS, "frame_end")
CAN_EV_ACK_OBS     = _ev_code(protocol_codes.CAN_EVENTS, "ack_observed")
CAN_EV_STUFF_ERR   = _ev_code(protocol_codes.CAN_EVENTS, "stuff_error")
CAN_EV_FORM_ERR    = _ev_code(protocol_codes.CAN_EVENTS, "form_error")
CAN_EV_CRC_ERR     = _ev_code(protocol_codes.CAN_EVENTS, "crc_error")
CAN_EV_ACK_ERR     = _ev_code(protocol_codes.CAN_EVENTS, "ack_error")
CAN_EV_STUFF_CNT_ERR = _ev_code(protocol_codes.CAN_EVENTS, "stuff_count_error")

# Events that mark a corrupt/abnormal frame (red + is_error). overload_marker
# and the lifecycle/boundary events are notices, not errors.
_CAN_ERR_EVENTS = {"stuff_error", "form_error", "crc_error", "ack_error",
                   "overflow_error"}
_CAN_EVENT_LABEL = {
    "enabled": "ENABLED", "disabled": "DISABLED",
    "frame_begin": "SOF [", "frame_end": "] EOF", "ack_observed": "ACK",
    "stuff_error": "STUFF-ERR", "form_error": "FORM-ERR", "crc_error": "CRC-ERR",
    "ack_error": "ACK-ERR", "overload_marker": "OVERLOAD",
    "overflow_error": "FIFO-OVF",
}
_CAN_EVENT_COLOR  = "#7a5cc8"   # purple — lifecycle / frame boundary
_CAN_ID_COLOR     = "#3a7ca8"   # blue   — arbitration ID
_CAN_DATA_COLOR   = "#4a8c2a"   # green  — data
_CAN_CRC_COLOR    = "#cc8833"   # amber  — CRC
_CAN_ERR_COLOR    = "#cc3333"   # red    — fault events


def _decode_can_control(b: int) -> DecodedFrame:
    """Control byte: {IDE[7], RTR[6], rsvd[5:4], DLC[3:0]}."""
    ide = bool(b & 0x80)
    rtr = bool(b & 0x40)
    dlc = b & 0x0F
    text = f"CTRL {'EXT' if ide else 'STD'}{' RTR' if rtr else ''} DLC={dlc}"
    return DecodedFrame("can_control", text, _CAN_EVENT_COLOR, False,
                        fields={"ide": ide, "rtr": rtr, "dlc": dlc})


def _decode_can_event(code: int) -> DecodedFrame:
    name = protocol_codes.CAN_EVENTS.get(code, f"evt0x{code:02X}")
    err = name in _CAN_ERR_EVENTS
    label = _CAN_EVENT_LABEL.get(name, name.upper())
    return DecodedFrame(f"can_{name}", label,
                        _CAN_ERR_COLOR if err else _CAN_EVENT_COLOR, err,
                        fields={"event": name})


def decode_can(header: zs_msg.ZsHeader, payload: bytes,
               sample_clk_hz: int) -> DecodedFrame:
    """CAN: per-message annotation — ID byte, control(IDE/RTR/DLC), data byte,
    CRC byte, and lifecycle/boundary/error events.

        SOF [ · CTRL STD DLC=2 · ID 0x01 · ID 0x23 · 0xAB · 0xCD · CRC 0xAA · ACK · ] EOF
    """
    if len(payload) < 1:
        return DecodedFrame("can_short", "short payload", "#cc3333", True)
    data = payload[0]
    et = header.event_id
    if et == CAN_T_ID:
        return DecodedFrame("can_id", f"ID 0x{data:02X}", _CAN_ID_COLOR, False,
                            fields={"id_byte": data})
    if et == CAN_T_DATA:
        return DecodedFrame("can_data", f"0x{data:02X}", _CAN_DATA_COLOR, False,
                            fields={"data": data})
    if et == CAN_T_CONTROL:
        return _decode_can_control(data)
    if et == CAN_T_CRC:
        return DecodedFrame("can_crc", f"CRC 0x{data:02X}", _CAN_CRC_COLOR, False,
                            fields={"crc_byte": data})
    if et == CAN_T_EVENT:
        return _decode_can_event(data)
    return DecodedFrame("can_unknown", f"type=0x{et:X} 0x{data:02X}",
                        _CAN_EVENT_COLOR, False)


# ── EYE (Domain 0xD) ──────────────────────────────────────────────────
#
# A DOMAIN_EYE frame is one measured cell of the PMOD eye-scan bathtub:
# (freq_sel, phase_sel, err_count, bit_count, locked). Unlike the passive
# dataloggers these are PRODUCED by the PS (eye_sweep.py); the decoder just turns
# the 24-byte EYE_POINT payload into the structured fields RouteWave's
# BathtubAnalyzer (LT-P3.217) plots — phase on X, frequency on Y, BER as colour.

_EYE_OK_COLOR     = "#33aa55"   # locked, measured
_EYE_CLOSED_COLOR = "#cc3333"   # never locked → eye closed at this cell


def decode_eye(header: zs_msg.ZsHeader, payload: bytes,
               sample_clk_hz: int) -> DecodedFrame:
    """A DOMAIN_EYE frame → structured fields. Sub-dispatches on the event id:
    EVT_EYE_POINT = one bathtub cell, EVT_EYE_SUMMARY = the link-margin verdict."""
    if zs_msg.event_id_of(header.msg_type) == zs_msg.EVT_EYE_SUMMARY:
        return _decode_eye_summary(payload)

    pt = zs_msg.parse_eye_point(payload)
    if not pt.locked or pt.bit_count == 0:
        # Eye closed at this cell — distinct from a measured BER of 0.
        return DecodedFrame(
            kind="eye_closed",
            text=f"eye[f{pt.freq_sel} φ{pt.phase_sel}] CLOSED (no lock)",
            color=_EYE_CLOSED_COLOR,
            is_error=False,
            fields={
                "freq_sel": pt.freq_sel, "phase_sel": pt.phase_sel,
                "phase_ps": pt.phase_ps, "phase_ui": pt.phase_ui,
                "freq_hz": pt.freq_hz, "locked": False,
                "err_count": pt.err_count, "bit_count": pt.bit_count,
                "ber": None,
            },
        )
    return DecodedFrame(
        kind="eye_point",
        text=f"eye[f{pt.freq_sel} φ{pt.phase_sel}] BER={pt.ber:.2e} "
             f"({pt.err_count}/{pt.bit_count})",
        color=_EYE_OK_COLOR,
        is_error=False,
        fields={
            "freq_sel": pt.freq_sel, "phase_sel": pt.phase_sel,
            "phase_ps": pt.phase_ps, "phase_ui": pt.phase_ui,
            "freq_hz": pt.freq_hz, "locked": True,
            "err_count": pt.err_count, "bit_count": pt.bit_count,
            "ber": pt.ber,
        },
    )


def _decode_eye_summary(payload: bytes) -> DecodedFrame:
    """The DS-P3.119 link-margin verdict → 'MAX RATE: X MHz @ BER<=thr' + the
    per-frequency eye-width curve (the LT-P3.243 view's authoritative input)."""
    s = zs_msg.parse_eye_summary(payload)
    if s.max_freq_hz > 0:
        text = (f"MAX RATE {s.max_freq_hz/1e6:.1f} MHz @ BER<={s.ber_threshold:.0e} "
                f"(margin {s.eye_margin_ui:.2f} UI)")
        color = _EYE_OK_COLOR
    else:
        text = (f"LINK FAILS — no rate holds {s.eye_margin_ui:.2f} UI "
                f"@ BER<={s.ber_threshold:.0e}")
        color = _EYE_CLOSED_COLOR
    return DecodedFrame(
        kind="eye_summary",
        text=text,
        color=color,
        is_error=(s.max_freq_hz == 0),
        fields={
            "max_freq_hz": s.max_freq_hz,
            "ber_threshold": s.ber_threshold,
            "eye_margin_ui": s.eye_margin_ui,
            "curve": [
                {"freq_hz": f.freq_hz, "eye_width_ps": f.eye_width_ps,
                 "eye_width_ui": f.eye_width_ui, "passed": f.passed,
                 "partial": f.partial}
                for f in s.freqs
            ],
        },
    )


_EDGE_MAXFREQ_OK_COLOR   = "#2a8c6a"
_EDGE_MAXFREQ_NONE_COLOR = "#888888"
_EDGE_LIFECYCLE_COLOR    = "#7a5cc8"   # purple — lifecycle, as JTAG/CAN events
_EDGE_RLE_COLOR          = "#3a7bd5"
_EDGE_RLE_NAMES = {0b000: "SYNC_LO", 0b001: "SYNC_HI", 0b101: "PACK",
                   0b110: "LONG", 0b111: "IDLE"}


def decode_edge(header: zs_msg.ZsHeader, payload: bytes,
                sample_clk_hz: int) -> DecodedFrame:
    """A DOMAIN_EDGE frame → structured fields, sub-dispatched on event id.

    DS-P3.117: EVT_EDGE_MAXFREQ (0x02) is the FPGA-authoritative per-line max
    frequency (latched smallest validated edge-to-edge interval). Decoded to a
    'edge_maxfreq' annotation with {channel, min_interval_ticks, freq_hz};
    freq_hz is None for the all-ones 'no edge yet' sentinel. The interval→Hz math
    is single-sourced from edge_capture.maxfreq_interval_to_hz so the in-band
    frame and the RTL/SW contract agree.

    EVT_EDGE_EVENT (0x00) / EVT_EDGE_BATCH (0x01) keep their existing 1:N
    parse_edge_batch oracle (per-event annotation expansion lands in a follow-on
    ticket — it is a different, multi-record shape from this 1:1 decoder).

    DS-T2.84: EVT_EDGE_LIFECYCLE (0x08) is a datalogger being armed or disarmed.
    It becomes an 'edge_lifecycle' annotation {channel, state, code} and is never
    an EDGE_EVENTS word, so it cannot move a lane's level.

    DS-P2.16: EVT_EDGE_RLE (0x09) is one Stream-RLE word → an 'edge_rle'
    annotation {channel, word, word_type, payload, sync}.
    """
    from sw.stream.edge_capture import maxfreq_interval_to_hz

    et = zs_msg.event_id_of(header.msg_type)
    if et == zs_msg.EVT_EDGE_LIFECYCLE:
        if len(payload) < 1:
            return DecodedFrame("edge_lifecycle_short", "short payload",
                                "#cc3333", True)
        code = payload[0]
        state = zs_msg.EDGE_LIFECYCLE_CODES.get(code, f"code0x{code:02X}")
        return DecodedFrame(
            kind="edge_lifecycle",
            text=f"edge ch{header.datalogger_id} {state.upper()}",
            color=_EDGE_LIFECYCLE_COLOR,
            is_error=False,
            fields={"channel": header.datalogger_id, "state": state, "code": code},
        )
    if et == zs_msg.EVT_EDGE_RLE:
        # DS-P2.16: one Stream-RLE word. The annotation carries the word; the
        # waveform comes from edge_capture.EdgeRleDecoder over the word stream.
        from sw.stream.edge_capture import RLE_SYNC_HI, RLE_SYNC_LO
        if len(payload) < 4:
            return DecodedFrame("edge_rle_short", "short payload", "#cc3333", True)
        word = int.from_bytes(payload[:4], "little")
        kind = (word >> 24) & 0x7
        name = _EDGE_RLE_NAMES.get(kind, f"type{kind:03b}")
        return DecodedFrame(
            kind="edge_rle",
            text=f"edge ch{header.datalogger_id} RLE {name}",
            color=_EDGE_RLE_COLOR,
            is_error=name.startswith("type"),
            fields={"channel": header.datalogger_id, "word": word,
                    "word_type": name, "payload": word & 0xFFFFFF,
                    "sync": kind in (RLE_SYNC_LO, RLE_SYNC_HI)},
        )
    if et == zs_msg.EVT_EDGE_MAXFREQ:
        _h, mf = zs_msg.parse_edge_maxfreq(
            zs_msg.pack_header(header) + payload, 0)
        freq_hz = maxfreq_interval_to_hz(
            mf.min_interval, sample_clk_hz, mf.ival_width)
        if freq_hz is None:
            return DecodedFrame(
                kind="edge_maxfreq",
                text=f"max-freq ch{mf.channel}: no edge yet",
                color=_EDGE_MAXFREQ_NONE_COLOR,
                is_error=False,
                fields={"channel": mf.channel,
                        "min_interval_ticks": mf.min_interval,
                        "freq_hz": None},
            )
        return DecodedFrame(
            kind="edge_maxfreq",
            text=f"max-freq ch{mf.channel}: {freq_hz/1e6:.3f} MHz "
                 f"(min ival {mf.min_interval} tk)",
            color=_EDGE_MAXFREQ_OK_COLOR,
            is_error=False,
            fields={"channel": mf.channel,
                    "min_interval_ticks": mf.min_interval,
                    "freq_hz": freq_hz},
        )

    return DecodedFrame(
        kind="edge_unknown",
        text=f"edge event_id=0x{et:02X} (no 1:1 decoder)",
        color="#cc8833",
        is_error=False,
    )


# ── Domain → decoder dispatch ─────────────────────────────────────────


DecoderFn = Callable[[zs_msg.ZsHeader, bytes, int], DecodedFrame]


DOMAIN_DECODERS: Dict[int, DecoderFn] = {
    zs_msg.DOMAIN_UART: decode_uart,
    zs_msg.DOMAIN_I2C:  decode_i2c,
    zs_msg.DOMAIN_SPI:  decode_spi,
    zs_msg.DOMAIN_JTAG: decode_jtag,
    zs_msg.DOMAIN_CAN:  decode_can,    # DS-P3.104
    zs_msg.DOMAIN_EYE:  decode_eye,    # DS-P2.66
    zs_msg.DOMAIN_EDGE: decode_edge,   # DS-P3.117 (EVT_EDGE_MAXFREQ; batch=1:N TBD)
    # EVT_EDGE_EVENT / EVT_EDGE_BATCH 1:N per-event annotation expansion lands
    # in a follow-on ticket (distinct from this 1:1 decoder shape) — the
    # parse_edge_batch oracle from DS-P2.47 already exists for it.
}


# ── DS-P3.70: shared cross-domain error-provenance events (event_id 0xF0..0xFF) ──
# Two parallel maps: a STABLE snake_case CODE (the cross-repo contract field
# `loss_stage`, == the system_ids.yml error_events key) keyed on by RouteWave's
# per-stage provenance render, and a HUMAN string for the annotation text.
_ERR_EVT_CODES = {
    zs_msg.ERR_EVT_INTERNAL_FIFO_OVERFLOW: "internal_fifo_overflow",
    zs_msg.ERR_EVT_ARBITER_DROP:           "arbiter_drop",
    zs_msg.ERR_EVT_RING_FULL:              "ring_full",
    zs_msg.ERR_EVT_DECODE_ERROR:           "decode_error",
}
_ERR_EVT_NAMES = {
    zs_msg.ERR_EVT_INTERNAL_FIFO_OVERFLOW: "internal FIFO overflow",
    zs_msg.ERR_EVT_ARBITER_DROP:           "arbiter drop",
    zs_msg.ERR_EVT_RING_FULL:              "ring full",
    zs_msg.ERR_EVT_DECODE_ERROR:           "decode error",
}
_ERR_EVT_MIN = 0xF0


def error_stage_code(event_id: int) -> str:
    """Stable snake_case loss-stage code for a provenance event_id (the
    cross-repo `loss_stage` field). Single source for the in-band decode AND the
    out-of-band counter watcher (data_source.LossProvenanceWatcher)."""
    return _ERR_EVT_CODES.get(event_id, f"err_{event_id:#x}")


def error_stage_human(event_id: int) -> str:
    """Human label for a provenance event_id (the annotation text)."""
    return _ERR_EVT_NAMES.get(event_id, error_stage_code(event_id))


def _decode_error_event(header: zs_msg.ZsHeader, payload: bytes) -> DecodedFrame:
    """An in-band SEV_ERROR loss-provenance frame (DS-P3.70 piece 3). payload[0:4]
    = lost_count (LE). Rendered as a red marker naming the source datalogger + the
    stage that lost data — so RouteWave shows '\u26a0 N edges lost here'.

    DS-T3.18 (wave_stream loss v0.11): the frame's ``datalogger_id`` names WHICH
    channel overflowed (per-channel error_event_bank). Carried into the loss
    payload so RouteWave buckets/labels the loss per channel, not per domain."""
    lost = int.from_bytes(payload[0:4], "little") if len(payload) >= 4 else 0
    src = zs_msg.DOMAIN_NAMES.get(header.domain, f"dom{header.domain:x}")
    return DecodedFrame(
        kind="loss",
        # DS-P3.127(b): ASCII-only marker \u2014 U+26A0 doesn't render in the
        # RouteWave font atlas, which would hide this loss evidence on-canvas.
        text=f"! {lost} {src} event(s) lost "
             f"({error_stage_human(header.event_id)}) on ch {header.datalogger_id}",
        color="#cc3333",
        is_error=True,
        fields={
            "loss_stage": error_stage_code(header.event_id),
            "provenance_code": header.event_id,
            "lost_count": lost,
            "domain": src,
            "datalogger_id": header.datalogger_id,   # DS-T3.18 attribution; renamed in contract 2.0.0 (DS-P2.111)
            "in_band": True,
        },
    )


def decode_frame(header: zs_msg.ZsHeader,
                 payload: bytes,
                 sample_clk_hz: int) -> Optional[DecodedFrame]:
    """Dispatch to the registered per-domain decoder.

    Returns the :class:`DecodedFrame` on hit, ``None`` when no decoder
    is registered for *header.domain*.  Caller (DdrRingDataSource)
    falls back to the generic ``evt=0x.. payload=N B`` annotation
    text in that case.

    Decoder exceptions are caught and surfaced as a
    ``<domain>_decode_error`` annotation so a decoder bug never
    crashes the drain loop — the daemon must keep streaming even when
    a single frame's payload trips a parser edge case.
    """
    # Shared error-provenance frame (any domain): SEV_ERROR + event_id in the
    # reserved 0xF0..0xFF range = a loss report, decoded uniformly (DS-P3.70).
    if header.is_error and header.event_id >= _ERR_EVT_MIN:
        return _decode_error_event(header, payload)

    fn = DOMAIN_DECODERS.get(header.domain)
    if fn is None:
        return None
    try:
        return fn(header, payload, sample_clk_hz)
    except Exception as exc:
        domain_name = zs_msg.DOMAIN_NAMES.get(header.domain,
                                              f"dom{header.domain:x}")
        return DecodedFrame(
            kind=f"{domain_name}_decode_error",
            text=f"decoder raised {type(exc).__name__}: {exc}",
            color="#cc3333",
            is_error=True,
        )
