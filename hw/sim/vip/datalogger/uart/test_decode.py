"""DS-T3.15 drift-gate: the VIP UART decoder's wire type codes [26:24] must match
the single-sourced protocol_codes.UART_TYPES — generated from
uart_datalogger_protocol.yml (DS-P3.59) and already drift-gated, the same table the
RTL (uart_frame_gen.vhd) and the daemon (sw/stream/decoders.py) decode against.

The bug this guards: decode.py was stale from before commit 48663d8 ("Align
uart_frame_gen with YAML 2.0 protocol", payload 000->010). It read PAYLOAD=0b000
and invented ERROR=0b010, so it mis-decoded EVERY real payload (0b010) as an
ERROR. There is no 0b000 type and no distinct error type — line errors are STATUS
(0b100) events.
"""
from hw.sim.vip.datalogger.uart import decode as D
from hw.sim.vip.datalogger.uart.codes import MsgType
from sw.stream import protocol_codes

U = protocol_codes.UART_TYPES


def _word(dev: int, type3: int, ts: int = 0x00D8, data: int = 0x55) -> int:
    """Assemble a 32-bit UART datalogger message: [31:27]dev [26:24]type [23:8]ts [7:0]data."""
    return ((dev & 0x1F) << 27) | ((type3 & 0x7) << 24) | ((ts & 0xFFFF) << 8) | (data & 0xFF)


def test_decode_constants_match_yml_single_source():
    # decode.py wire codes must equal the generated/gated protocol_codes table.
    assert D.CONFIG_TYPE == U["config"]
    assert D.PAYLOAD_TYPE == U["payload"]
    assert D.PAYLOAD_9BIT_TYPE == U["payload_9bit"]
    assert D.STATUS_TYPE == U["status"]


def test_normal_payload_decodes_as_payload_not_error():
    # DS-T3.15 regression: type 0b010 is a PAYLOAD, never an ERROR.
    m = D.decode_uart_msg(_word(23, U["payload"], data=0x55))
    assert m.kind == MsgType.PAYLOAD
    assert m.raw & 0xFF == 0x55


def test_9bit_payload_decodes_as_payload():
    m = D.decode_uart_msg(_word(23, U["payload_9bit"], data=0xAA))
    assert m.kind == MsgType.PAYLOAD


def test_config_decodes_as_config():
    assert D.decode_uart_msg(_word(23, U["config"])).kind == MsgType.CONFIG


def test_status_decodes_with_event():
    # low nibble 0x02 = parity_error
    m = D.decode_uart_msg(_word(23, U["status"], data=0x02))
    assert m.kind == MsgType.STATUS
    assert m.event is not None
