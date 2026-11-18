"""DS-T3.6 drift-gate: the I2C VIP decoder's hardcoded TYPE constants must match
the single-sourced protocol_codes.I2C_TYPES (generated from
i2c_datalogger_protocol.yml, the same table the RTL i2c_datalogger.vhd and the daemon
sw/stream/decoders.py decode against).

Motivation: the I2C VIP type constants (TYPE_EVENT / TYPE_PAYLOAD_ACK /
TYPE_PAYLOAD_NACK) are hand-typed, not sourced from protocol_codes — the exact
shape that let the UART VIP decoder silently rot (DS-T3.15: it read PAYLOAD=0b000
and mis-decoded every real 0b010 payload as ERROR). The I2C codes are currently
correct; this gate keeps them that way. I2C uses a 2-bit base type (payload=0b00)
with the ACK/NACK indicator in bit[24] above it (0b000=ACK, 0b001=NACK); event is
the full 3-bit 0b100.
"""
from hw.sim.vip.datalogger.i2c import decoder as D
from sw.stream import protocol_codes

I = protocol_codes.I2C_TYPES


def _word(dev: int, type3: int, ts: int = 0x00D8, data: int = 0x55) -> int:
    """Assemble a 32-bit I2C datalogger message: [31:27]dev [26:24]type [23:8]ts [7:0]data."""
    return ((dev & 0x1F) << 27) | ((type3 & 0x7) << 24) | ((ts & 0xFFFF) << 8) | (data & 0xFF)


def test_i2c_vip_type_constants_match_yml_single_source():
    assert D.TYPE_EVENT == I["event"]                  # 0b100
    # payload is the 2-bit base (0b00); ACK/NACK lives in bit[24] above it.
    assert (D.TYPE_PAYLOAD_ACK >> 1) == I["payload"]   # 0b000 >> 1 == 0b00
    assert (D.TYPE_PAYLOAD_NACK >> 1) == I["payload"]  # 0b001 >> 1 == 0b00
    assert D.TYPE_PAYLOAD_ACK & 1 == 0                 # ACK  -> bit[24] clear
    assert D.TYPE_PAYLOAD_NACK & 1 == 1                # NACK -> bit[24] set


def test_i2c_event_decodes():
    m = D.I2cMsgDecoder(device_id=2).decode(_word(2, I["event"], data=D.I2cEvent.START))
    assert m.is_event and not m.is_payload
    assert m.event_name == "START"


def test_i2c_payload_ack_decodes():
    m = D.I2cMsgDecoder(device_id=2).decode(_word(2, D.TYPE_PAYLOAD_ACK, data=0x55))
    assert m.is_payload and not m.is_event
    assert m.ack is True
    assert m.data == 0x55


def test_i2c_payload_nack_decodes():
    m = D.I2cMsgDecoder(device_id=2).decode(_word(2, D.TYPE_PAYLOAD_NACK, data=0x55))
    assert m.is_payload
    assert m.ack is False
