# rr unknown-llm (AI-generated)
"""Unit tests for per-protocol payload decoders (DS-P1.12).

Round-trip a hand-packed zs_msg payload through the decoder and assert
the resulting :class:`DecodedFrame` matches the spec.  Decoder bugs
must never crash the drain loop — :func:`decode_frame` catches any
exception and surfaces it as an error annotation; the
``test_decoder_exception_surfaces_as_error_annotation`` case guards
that contract.
"""

from __future__ import annotations

import struct

import pytest

from sw.stream import decoders, zs_msg


from sw.stream import protocol_codes as _PC


@pytest.fixture(autouse=True)
def _fresh_i2c_phase():
    """DS-P3.209: decode_i2c keeps each datalogger's transfer phase; every
    test starts from an unknown phase."""
    decoders.reset_i2c_state()
    yield
    decoders.reset_i2c_state()


def _uart_frame(*, et: int, data: int, datalogger_id: int = 12):
    """A 1:1 UART frame: header.event_id = the message TYPE, payload[0] = the
    char/status byte (the actual gather framing — not the old 8-byte struct)."""
    hdr = zs_msg.make_header(datalogger_id=datalogger_id, domain=zs_msg.DOMAIN_UART,
                             event_id=et, severity=zs_msg.SEV_INFO, payload_len=4)
    return hdr, bytes([data, 0, 0, 0])


# ── UART decoder ──────────────────────────────────────────────────────


class TestDecodeUart:
    def test_printable_char(self):
        hdr, p = _uart_frame(et=_PC.UART_TYPES["payload"], data=0x55)
        r = decoders.decode_uart(hdr, p, 100_000_000)
        assert r.kind == "uart_byte" and "0x55" in r.text and "'U'" in r.text
        assert r.is_error is False

    def test_non_printable_char_no_glyph(self):
        hdr, p = _uart_frame(et=_PC.UART_TYPES["payload"], data=0x0A)
        r = decoders.decode_uart(hdr, p, 100_000_000)
        assert "0x0A" in r.text and "'" not in r.text

    def test_nine_bit_char(self):
        hdr, p = _uart_frame(et=_PC.UART_TYPES["payload_9bit"], data=0xA9)
        r = decoders.decode_uart(hdr, p, 100_000_000)
        assert r.kind == "uart_byte" and "9-bit" in r.text

    def test_status_parity_error(self):
        hdr, p = _uart_frame(et=_PC.UART_TYPES["status"], data=0x02)  # parity_error
        r = decoders.decode_uart(hdr, p, 100_000_000)
        assert r.kind == "uart_parity_error" and r.is_error is True
        assert "PARITY ERROR" in r.text and r.color == "#cc3333"

    def test_status_calibration_done(self):
        hdr, p = _uart_frame(et=_PC.UART_TYPES["status"], data=0x00)
        r = decoders.decode_uart(hdr, p, 100_000_000)
        assert r.kind == "uart_calibration_done" and r.is_error is False

    def test_short_payload_emits_error_not_crash(self):
        hdr, _ = _uart_frame(et=_PC.UART_TYPES["payload"], data=0x55)
        r = decoders.decode_uart(hdr, b"", sample_clk_hz=100_000_000)
        assert r.kind == "uart_short" and r.is_error is True


# ── I2C decoder ───────────────────────────────────────────────────────


def _i2c_byte(*, ack: bool, data: int, datalogger_id: int = 4):
    """An I2C payload frame. Per i2c_datalogger_protocol.yml the event_id low bit
    is the ack_nack field: 0 = ACK, 1 = NACK (type 0b00 << 1 | ack_nack)."""
    et = (_PC.I2C_TYPES["payload"] << 1) | (0 if ack else 1)
    hdr = zs_msg.make_header(datalogger_id=datalogger_id, domain=zs_msg.DOMAIN_I2C,
                             event_id=et, severity=zs_msg.SEV_INFO, payload_len=4)
    return hdr, bytes([data, 0, 0, 0])


class TestDecodeI2c:
    def test_ack_bit_zero_is_ack(self):
        """Contract: ack_nack=0 → ACK. Regression for DS-T2.32 — the decoder
        previously read the bit inverted, so every bus-ACK showed NACK."""
        hdr, p = _i2c_byte(ack=True, data=0xA0)   # addressed write, ACKed
        r = decoders.decode_i2c(hdr, p, 100_000_000)
        assert r.kind == "i2c_byte" and "ACK" in r.text and "NACK" not in r.text
        assert r.is_error is False

    def test_ack_bit_one_is_nack(self):
        """Contract: ack_nack=1 → NACK (e.g. the master's final read byte)."""
        hdr, p = _i2c_byte(ack=False, data=0xFF)
        r = decoders.decode_i2c(hdr, p, 100_000_000)
        assert r.kind == "i2c_byte" and "NACK" in r.text and r.is_error is True


# ── Dispatch ──────────────────────────────────────────────────────────


class TestDecodeFrameDispatch:
    def test_uart_domain_routes_to_decode_uart(self):
        hdr, payload = _uart_frame(et=_PC.UART_TYPES["payload"], data=0x55)
        result = decoders.decode_frame(hdr, payload, sample_clk_hz=100_000_000)
        assert result is not None
        assert result.kind == "uart_byte"

    def test_unregistered_domain_returns_none(self):
        """A domain with no decoder (I2S) — dispatch returns None so the caller
        falls back to the generic envelope text. (UART/I2C/SPI/JTAG/CAN ARE now
        registered — DS-P3.104 added CAN, so I2S is the unregistered example.)"""
        hdr = zs_msg.make_header(
            datalogger_id=0,
            domain=zs_msg.DOMAIN_I2S,
            event_id=0,
            payload_len=4,
        )
        result = decoders.decode_frame(hdr, b"\x00\x01\x02\x03",
                                       sample_clk_hz=100_000_000)
        assert result is None

    def test_decoder_exception_surfaces_as_error_annotation(self):
        """Decoder bug → safe error annotation, never an exception out of
        decode_frame.  Hard contract: the drain loop keeps streaming
        even when a single frame trips a parser edge case."""
        # Inject a broken decoder.
        sentinel_domain = 0xE  # reserved domain — not in zs_msg.DOMAIN_*
        broken_called = []

        def _broken(header, payload, sample_clk_hz):
            broken_called.append(True)
            raise RuntimeError("kaboom")

        original = decoders.DOMAIN_DECODERS.get(sentinel_domain)
        decoders.DOMAIN_DECODERS[sentinel_domain] = _broken
        try:
            hdr = zs_msg.make_header(
                datalogger_id=0,
                domain=sentinel_domain,
                event_id=0,
                payload_len=4,
            )
            result = decoders.decode_frame(hdr, b"\x00\x01\x02\x03",
                                           sample_clk_hz=100_000_000)
        finally:
            if original is None:
                del decoders.DOMAIN_DECODERS[sentinel_domain]
            else:
                decoders.DOMAIN_DECODERS[sentinel_domain] = original

        assert broken_called == [True]
        assert result is not None
        assert "decode_error" in result.kind
        assert "kaboom" in result.text
        assert result.is_error is True


# ── Baud helper ───────────────────────────────────────────────────────


class TestBaudFromOversample:
    # Floor-of-integer-division baud values for canonical UART rates.
    # Real oversample periods produced by the datalogger round to integer
    # cycles, so the derived baud is the floor — close to nominal but
    # not exactly equal.  Examples: oversample=10417 (closest integer
    # to 100MHz/9600) → 9599bd in the UI; oversample=5208 → 19201bd.
    @pytest.mark.parametrize("clk,oversample,expected", [
        (100_000_000, 10417, 9599),     # nominal 9600
        (100_000_000, 868,   115207),   # nominal 115200
        (100_000_000, 5208,  19201),    # nominal 19200
        (50_000_000,  5208,  9600),     # different clock, exact match
    ])
    def test_baud_compute(self, clk, oversample, expected):
        assert decoders._baud_from_oversample(oversample, clk) == expected

    def test_zero_period_returns_none(self):
        assert decoders._baud_from_oversample(0, 100_000_000) is None

    def test_negative_clock_returns_none(self):
        assert decoders._baud_from_oversample(10417, -1) is None


# ── JTAG decoder (Domain 0x8, DS-P3.56 P2) ────────────────────────────
#
# The gather frames each 32-bit datalogger word 1:1: header.event_id = the
# 3-bit TYPE field (word[26:24]); payload = the full 32-bit word LE, so
# payload[0] = word[7:0] = the data/event byte. These tests hand-pack
# that exact wire shape and assert decode_jtag's text (hard-coded
# expecteds — never derived from the decoder itself).


def _jtag_payload(*, dev: int = 16, mtype: int = decoders.JTAG_T_EVENT,
                  timestamp: int = 0, data: int = 0x03) -> bytes:
    """The 4-byte LE datalogger word as datalogger_gather stores it in the frame."""
    word = (((dev & 0x1F) << 27) | ((mtype & 0x7) << 24)
            | ((timestamp & 0xFFFF) << 8) | (data & 0xFF))
    return struct.pack("<I", word)


def _jtag_header(*, event_id: int, datalogger_id: int = 16) -> zs_msg.ZsHeader:
    return zs_msg.make_header(
        datalogger_id=datalogger_id,
        domain=zs_msg.DOMAIN_JTAG,
        event_id=event_id,          # the TYPE field, per the gather
        severity=zs_msg.SEV_INFO,
        payload_len=4,
    )


class TestDecodeJtag:
    def test_tdi_byte(self):
        hdr = _jtag_header(event_id=decoders.JTAG_T_TDI)
        payload = _jtag_payload(mtype=decoders.JTAG_T_TDI, data=0xA5)
        r = decoders.decode_jtag(hdr, payload, sample_clk_hz=100_000_000)
        assert r.kind == "jtag_tdi"
        assert r.text == "TDI 0xA5"
        assert r.is_error is False

    def test_tdo_byte(self):
        hdr = _jtag_header(event_id=decoders.JTAG_T_TDO)
        payload = _jtag_payload(mtype=decoders.JTAG_T_TDO, data=0x3C)
        r = decoders.decode_jtag(hdr, payload, sample_clk_hz=100_000_000)
        assert r.kind == "jtag_tdo"
        assert r.text == "TDO 0x3C"

    def test_enabled_event(self):
        hdr = _jtag_header(event_id=decoders.JTAG_T_EVENT)
        payload = _jtag_payload(data=decoders.JTAG_EV_ENABLED)
        r = decoders.decode_jtag(hdr, payload, sample_clk_hz=100_000_000)
        assert r.kind == "jtag_enabled"
        assert r.text == "ENABLED"

    def test_disabled_event(self):
        hdr = _jtag_header(event_id=decoders.JTAG_T_EVENT)
        payload = _jtag_payload(data=decoders.JTAG_EV_DISABLED)
        r = decoders.decode_jtag(hdr, payload, sample_clk_hz=100_000_000)
        assert r.kind == "jtag_disabled"
        assert r.text == "DISABLED"

    def test_tap_reset_event(self):
        hdr = _jtag_header(event_id=decoders.JTAG_T_EVENT)
        payload = _jtag_payload(data=decoders.JTAG_EV_TAP_RESET)
        r = decoders.decode_jtag(hdr, payload, sample_clk_hz=100_000_000)
        assert r.kind == "jtag_tap_reset"
        assert r.text == "TAP-RESET"

    def test_tap_state_decodes_name(self):
        # DS-T3.16: type 0b011, payload[3:0] = TAP state code.
        hdr = _jtag_header(event_id=decoders.JTAG_T_TAP_STATE)
        r = decoders.decode_jtag(
            hdr, _jtag_payload(mtype=decoders.JTAG_T_TAP_STATE, data=0x4), 100_000_000)
        assert r.kind == "jtag_tap_state"
        assert r.text == "TAP=SHIFT-DR"
        assert r.fields == {"tap_state": 0x4, "state": "SHIFT-DR"}
        assert r.is_error is False

    def test_tap_state_all_16_codes_map(self):
        # Every 4-bit code resolves to the yml-ordered name; reset=0x0, update_ir=0xF.
        hdr = _jtag_header(event_id=decoders.JTAG_T_TAP_STATE)
        names = [decoders.decode_jtag(
            hdr, _jtag_payload(mtype=decoders.JTAG_T_TAP_STATE, data=s), 100_000_000
        ).fields["state"] for s in range(16)]
        assert names[0x0] == "TEST-LOGIC-RESET"
        assert names[0xF] == "UPDATE-IR"
        assert len(set(names)) == 16   # all distinct

    def test_dr_begin_and_full_byte_end(self):
        hdr = _jtag_header(event_id=decoders.JTAG_T_EVENT)
        begin = decoders.decode_jtag(
            hdr, _jtag_payload(data=decoders.JTAG_EV_DR_BEGIN), 100_000_000)
        end = decoders.decode_jtag(
            hdr, _jtag_payload(data=decoders.JTAG_EV_DR_END), 100_000_000)
        assert begin.kind == "jtag_dr_begin" and begin.text == "DR-SHIFT ["
        # low nibble 0 -> last byte was a full 8 bits
        assert end.kind == "jtag_dr_end" and end.text == "] DR-SHIFT (8b)"

    def test_dr_end_subbyte_nibble(self):
        hdr = _jtag_header(event_id=decoders.JTAG_T_EVENT)
        # 0x45 = DR_END | 5 valid bits in the final byte (JTAG-REQ-5)
        r = decoders.decode_jtag(hdr, _jtag_payload(data=0x45), 100_000_000)
        assert r.kind == "jtag_dr_end"
        assert r.text == "] DR-SHIFT (5b)"

    def test_ir_begin_and_subbyte_end(self):
        hdr = _jtag_header(event_id=decoders.JTAG_T_EVENT)
        begin = decoders.decode_jtag(
            hdr, _jtag_payload(data=decoders.JTAG_EV_IR_BEGIN), 100_000_000)
        end = decoders.decode_jtag(
            hdr, _jtag_payload(data=0x23), 100_000_000)   # IR_END | 3 bits
        assert begin.kind == "jtag_ir_begin" and begin.text == "IR-SHIFT ["
        assert end.kind == "jtag_ir_end" and end.text == "] IR-SHIFT (3b)"

    def test_short_payload_is_error(self):
        hdr = _jtag_header(event_id=decoders.JTAG_T_EVENT)
        r = decoders.decode_jtag(hdr, b"", sample_clk_hz=100_000_000)
        assert r.kind == "jtag_short_payload"
        assert r.is_error is True

    def test_dispatch_via_decode_frame(self):
        # decode_frame must route DOMAIN_JTAG to decode_jtag.
        hdr = _jtag_header(event_id=decoders.JTAG_T_TDI)
        payload = _jtag_payload(mtype=decoders.JTAG_T_TDI, data=0x5A)
        r = decoders.decode_frame(hdr, payload, sample_clk_hz=100_000_000)
        assert r is not None and r.kind == "jtag_tdi" and r.text == "TDI 0x5A"

    def test_registered_in_dispatch_table(self):
        assert zs_msg.DOMAIN_JTAG in decoders.DOMAIN_DECODERS
        assert decoders.DOMAIN_DECODERS[zs_msg.DOMAIN_JTAG] is decoders.decode_jtag


# ── CAN (Domain 0xA, DS-P3.104) — 1:1 per-message decode ────────────────────
# Same gather framing: header.event_id = the 3-bit TYPE, payload[0] = the byte.
# Hard-coded expected texts/fields (ROUTERTL-002 — never derived from decode_can).
def _can_header(*, event_id: int, datalogger_id: int = 20) -> zs_msg.ZsHeader:
    return zs_msg.make_header(datalogger_id=datalogger_id, domain=zs_msg.DOMAIN_CAN,
                              event_id=event_id, severity=zs_msg.SEV_INFO, payload_len=4)


def _can_payload(*, data: int) -> bytes:
    return bytes([data & 0xFF, 0, 0, 0])     # payload[0] = the data/event byte


class TestDecodeCan:
    def test_id_byte(self):
        r = decoders.decode_can(_can_header(event_id=decoders.CAN_T_ID),
                                _can_payload(data=0x23), 100_000_000)
        assert r.kind == "can_id" and r.text == "ID 0x23"
        assert r.fields == {"id_byte": 0x23}

    def test_data_byte(self):
        r = decoders.decode_can(_can_header(event_id=decoders.CAN_T_DATA),
                                _can_payload(data=0xAB), 100_000_000)
        assert r.kind == "can_data" and r.text == "0xAB"
        assert r.fields == {"data": 0xAB}

    def test_control_std(self):
        # IDE=0 RTR=0 DLC=2 → 0x02
        r = decoders.decode_can(_can_header(event_id=decoders.CAN_T_CONTROL),
                                _can_payload(data=0x02), 100_000_000)
        assert r.kind == "can_control" and r.text == "CTRL STD DLC=2"
        assert r.fields == {"ide": False, "rtr": False, "dlc": 2}

    def test_control_ext_rtr(self):
        # IDE=1 RTR=1 DLC=1 → 0x80|0x40|0x01 = 0xC1
        r = decoders.decode_can(_can_header(event_id=decoders.CAN_T_CONTROL),
                                _can_payload(data=0xC1), 100_000_000)
        assert r.text == "CTRL EXT RTR DLC=1"
        assert r.fields == {"ide": True, "rtr": True, "dlc": 1}

    def test_crc_byte(self):
        r = decoders.decode_can(_can_header(event_id=decoders.CAN_T_CRC),
                                _can_payload(data=0xAA), 100_000_000)
        assert r.kind == "can_crc" and r.text == "CRC 0xAA"
        assert r.fields == {"crc_byte": 0xAA}

    def test_frame_begin_end(self):
        hdr = _can_header(event_id=decoders.CAN_T_EVENT)
        b = decoders.decode_can(hdr, _can_payload(data=decoders.CAN_EV_FRAME_BEGIN), 100_000_000)
        e = decoders.decode_can(hdr, _can_payload(data=decoders.CAN_EV_FRAME_END), 100_000_000)
        assert b.kind == "can_frame_begin" and b.text == "SOF [" and b.is_error is False
        assert e.kind == "can_frame_end" and e.text == "] EOF"

    def test_ack_observed(self):
        r = decoders.decode_can(_can_header(event_id=decoders.CAN_T_EVENT),
                                _can_payload(data=decoders.CAN_EV_ACK_OBS), 100_000_000)
        assert r.kind == "can_ack_observed" and r.text == "ACK" and r.is_error is False

    def test_crc_error_is_error(self):
        r = decoders.decode_can(_can_header(event_id=decoders.CAN_T_EVENT),
                                _can_payload(data=decoders.CAN_EV_CRC_ERR), 100_000_000)
        assert r.kind == "can_crc_error" and r.text == "CRC-ERR" and r.is_error is True

    def test_ack_error_is_error(self):
        r = decoders.decode_can(_can_header(event_id=decoders.CAN_T_EVENT),
                                _can_payload(data=decoders.CAN_EV_ACK_ERR), 100_000_000)
        assert r.kind == "can_ack_error" and r.text == "ACK-ERR" and r.is_error is True

    def test_dispatch_via_decode_frame(self):
        r = decoders.decode_frame(_can_header(event_id=decoders.CAN_T_ID),
                                  _can_payload(data=0x01), sample_clk_hz=100_000_000)
        assert r is not None and r.kind == "can_id" and r.text == "ID 0x01"

    def test_registered_in_dispatch_table(self):
        assert zs_msg.DOMAIN_CAN in decoders.DOMAIN_DECODERS
        assert decoders.DOMAIN_DECODERS[zs_msg.DOMAIN_CAN] is decoders.decode_can


# ── DS-P3.70 piece 3: in-band error-provenance decode ───────────────────────
def test_decode_error_event_internal_fifo_overflow():
    """A SEV_ERROR | DOMAIN_EDGE | ERR_EVT_INTERNAL_FIFO_OVERFLOW frame decodes to
    a red loss marker naming the source + stage + lost_count (regardless of the
    per-domain decoder)."""
    import struct
    from sw.stream import decoders, zs_msg
    hdr = zs_msg.make_header(0, zs_msg.DOMAIN_EDGE,
                             event_id=zs_msg.ERR_EVT_INTERNAL_FIFO_OVERFLOW,
                             severity=zs_msg.SEV_ERROR)
    out = decoders.decode_frame(hdr, struct.pack("<I", 37), 100_000_000)
    assert out is not None and out.is_error
    assert out.kind == "loss"
    assert "37" in out.text and "edge" in out.text and "overflow" in out.text
    # DS-P3.127(b): ASCII-only so the loss marker actually renders in RouteWave
    # (the U+26A0 warning glyph the marker used to start with is unrenderable).
    assert out.text.isascii(), f"non-ASCII glyph in loss marker: {out.text!r}"


def test_error_provenance_takes_priority_over_domain_decoder():
    """The shared error path fires even for a domain that HAS a decoder (e.g.
    UART) when severity=ERROR + event_id is in the reserved 0xF0..0xFF range."""
    import struct
    from sw.stream import decoders, zs_msg
    hdr = zs_msg.make_header(0, zs_msg.DOMAIN_UART,
                             event_id=zs_msg.ERR_EVT_RING_FULL,
                             severity=zs_msg.SEV_ERROR)
    out = decoders.decode_frame(hdr, struct.pack("<I", 9), 100_000_000)
    assert out.kind == "loss" and "ring full" in out.text and "9" in out.text


def test_decode_error_event_carries_per_channel_datalogger_id():
    """DS-T3.18 (wave_stream loss v0.11): the in-band loss frame's datalogger_id —
    WHICH channel overflowed — is carried into the loss payload AND named in the
    text. Regression-guards the per-channel error_event_bank attribution."""
    import struct
    from sw.stream import decoders, zs_msg
    hdr = zs_msg.make_header(3, zs_msg.DOMAIN_EDGE,         # edge channel device id 3
                             event_id=zs_msg.ERR_EVT_INTERNAL_FIFO_OVERFLOW,
                             severity=zs_msg.SEV_ERROR)
    out = decoders.decode_frame(hdr, struct.pack("<I", 21), 100_000_000)
    assert out.kind == "loss"
    assert out.fields["datalogger_id"] == 3, f"datalogger_id not carried: {out.fields}"
    assert "ch 3" in out.text, f"channel not named in text: {out.text!r}"
    assert out.fields["in_band"] is True


# ── DS-P3.88: structured, role-tagged decoded fields ──────────────────
#
# Each decoder carries machine-readable fields alongside the display text, so
# the consumer can render per-protocol columns (LT-P3.168) and an agent can read
# packet contents without parsing the label. The role KEYS are a cross-repo
# contract — these tests pin them. Values are typed (int byte / bool ack / str
# event name); the consumer formats for display.


def _spi_frame(*, et: int, data: int, datalogger_id: int = 3):
    hdr = zs_msg.make_header(datalogger_id=datalogger_id, domain=zs_msg.DOMAIN_SPI,
                             event_id=et, severity=zs_msg.SEV_INFO, payload_len=4)
    return hdr, bytes([data, 0, 0, 0])


class TestStructuredFields:
    def test_uart_byte_fields(self):
        hdr, p = _uart_frame(et=_PC.UART_TYPES["payload"], data=0x55)
        r = decoders.decode_uart(hdr, p, 100_000_000)
        assert r.fields == {"byte": 0x55, "ascii": "U"}

    def test_uart_nonprintable_omits_ascii(self):
        hdr, p = _uart_frame(et=_PC.UART_TYPES["payload"], data=0x0A)
        r = decoders.decode_uart(hdr, p, 100_000_000)
        assert r.fields == {"byte": 0x0A}            # no ascii key for non-printable

    def test_uart_nine_bit_flag(self):
        hdr, p = _uart_frame(et=_PC.UART_TYPES["payload_9bit"], data=0xA9)
        r = decoders.decode_uart(hdr, p, 100_000_000)
        assert r.fields.get("nine_bit") is True and r.fields["byte"] == 0xA9

    def test_uart_status_field(self):
        hdr, p = _uart_frame(et=_PC.UART_TYPES["status"], data=0x02)  # parity_error
        r = decoders.decode_uart(hdr, p, 100_000_000)
        assert r.fields == {"status": "parity_error"}

    def test_i2c_byte_ack_fields(self):
        hdr, p = _i2c_byte(ack=True, data=0xA0)
        r = decoders.decode_i2c(hdr, p, 100_000_000)
        assert r.fields == {"data": 0xA0, "ack": True, "datalogger_id": 4}

    def test_i2c_byte_nack_fields(self):
        hdr, p = _i2c_byte(ack=False, data=0xFF)
        r = decoders.decode_i2c(hdr, p, 100_000_000)
        assert r.fields == {"data": 0xFF, "ack": False, "datalogger_id": 4}

    def test_i2c_byte_fields_carry_originating_ids(self):
        """DS-P3.155: decode_i2c carries datalogger_id into fields
        so multi-instance I2C (e.g. cam B #4 vs cam A #5) is distinguishable."""
        hdr_b, p_b = _i2c_byte(ack=True, data=0x3C, datalogger_id=4)
        r_b = decoders.decode_i2c(hdr_b, p_b, 100_000_000)
        assert r_b.fields == {"data": 0x3C, "ack": True, "datalogger_id": 4}

        hdr_a, p_a = _i2c_byte(ack=True, data=0x3C, datalogger_id=5)
        r_a = decoders.decode_i2c(hdr_a, p_a, 100_000_000)
        assert r_a.fields == {"data": 0x3C, "ack": True, "datalogger_id": 5}

    def test_i2c_event_fields_carry_originating_ids(self):
        """DS-P3.155: I2C event decode (e.g. START) carries datalogger_id."""
        code = next(c for c, n in _PC.I2C_EVENTS.items() if n == "start")
        hdr = zs_msg.make_header(datalogger_id=5, domain=zs_msg.DOMAIN_I2C,
                                 event_id=decoders.I2C_T_EVENT, severity=zs_msg.SEV_INFO,
                                 payload_len=4)
        r = decoders.decode_i2c(hdr, bytes([code, 0, 0, 0]), 100_000_000)
        assert r.fields == {"event": "start", "datalogger_id": 5}

    def test_spi_mosi_field(self):
        hdr, p = _spi_frame(et=decoders.SPI_T_MOSI, data=0x9F)
        r = decoders.decode_spi(hdr, p, 100_000_000)
        assert r.kind == "spi_mosi" and r.fields == {"mosi": 0x9F}

    def test_spi_miso_field(self):
        hdr, p = _spi_frame(et=decoders.SPI_T_MISO, data=0xEF)
        r = decoders.decode_spi(hdr, p, 100_000_000)
        assert r.kind == "spi_miso" and r.fields == {"miso": 0xEF}

    def test_spi_event_field(self):
        hdr, p = _spi_frame(et=decoders.SPI_T_EVENT, data=9)  # cs_down
        r = decoders.decode_spi(hdr, p, 100_000_000)
        assert r.fields == {"event": "cs_down"}

    def test_jtag_tdi_field(self):
        hdr = _jtag_header(event_id=decoders.JTAG_T_TDI)
        payload = _jtag_payload(mtype=decoders.JTAG_T_TDI, data=0xA5)
        r = decoders.decode_jtag(hdr, payload, sample_clk_hz=100_000_000)
        assert r.fields == {"tdi": 0xA5}

    def test_jtag_event_field(self):
        hdr = _jtag_header(event_id=decoders.JTAG_T_EVENT)
        payload = _jtag_payload(data=decoders.JTAG_EV_ENABLED)
        r = decoders.decode_jtag(hdr, payload, sample_clk_hz=100_000_000)
        assert r.fields == {"event": "enabled"}

    def test_fields_msgpack_roundtrip(self):
        """The structured payload must survive a msgpack round-trip (the wire
        encoding) with typed values intact — the consumer reads it back."""
        import msgpack
        hdr, p = _i2c_byte(ack=True, data=0x48)
        r = decoders.decode_i2c(hdr, p, 100_000_000)
        back = msgpack.unpackb(msgpack.packb(r.fields, use_bin_type=True), raw=False)
        assert back == {"data": 0x48, "ack": True, "datalogger_id": 4}


# ── DS-P3.117: EVT_EDGE_MAXFREQ in-band decode ─────────────────────────────


def _edge_maxfreq_wire(channel: int, min_interval: int, ival_width: int = 24):
    """Build a full EVT_EDGE_MAXFREQ frame and split it into (header, payload)
    the way decode_frame's caller does."""
    wire = zs_msg.pack_edge_maxfreq(
        zs_msg.EdgeMaxFreq(channel=channel, min_interval=min_interval,
                           ival_width=ival_width))
    hdr = zs_msg.parse_header(wire)
    return hdr, wire[zs_msg.HEADER_SIZE:]


class TestDecodeEdgeMaxFreq:
    def test_registered_in_dispatch_table(self):
        assert zs_msg.DOMAIN_EDGE in decoders.DOMAIN_DECODERS
        assert decoders.DOMAIN_DECODERS[zs_msg.DOMAIN_EDGE] is decoders.decode_edge

    def test_measured_value_to_hz(self):
        """min_interval=50 @ 100 MHz → 1.0 MHz (clk/(2*ival)). Hard-coded
        (ROUTERTL-002)."""
        hdr, payload = _edge_maxfreq_wire(channel=3, min_interval=50)
        r = decoders.decode_frame(hdr, payload, sample_clk_hz=100_000_000)
        assert r is not None
        assert r.kind == "edge_maxfreq"
        assert r.is_error is False
        assert r.fields == {"channel": 3, "min_interval_ticks": 50,
                            "freq_hz": 1_000_000.0}

    def test_measured_value_to_hz_8tick(self):
        """min_interval=8 @ 100 MHz → 6.25 MHz."""
        hdr, payload = _edge_maxfreq_wire(channel=6, min_interval=8)
        r = decoders.decode_frame(hdr, payload, sample_clk_hz=100_000_000)
        assert r.fields["freq_hz"] == 6_250_000.0

    def test_sentinel_freq_is_none(self):
        """All-ones interval → 'no edge yet' → freq_hz None (not a false 0)."""
        hdr, payload = _edge_maxfreq_wire(channel=1, min_interval=0x00FFFFFF)
        r = decoders.decode_frame(hdr, payload, sample_clk_hz=100_000_000)
        assert r.kind == "edge_maxfreq"
        assert r.fields["freq_hz"] is None
        assert r.fields["channel"] == 1

    def test_fields_msgpack_roundtrip(self):
        import msgpack
        hdr, payload = _edge_maxfreq_wire(channel=2, min_interval=100)
        r = decoders.decode_edge(hdr, payload, 100_000_000)
        back = msgpack.unpackb(msgpack.packb(r.fields, use_bin_type=True), raw=False)
        assert back == {"channel": 2, "min_interval_ticks": 100,
                        "freq_hz": 500_000.0}
