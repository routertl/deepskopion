# rr unknown-llm (AI-generated)
"""DS-P2.239: the STATUS frame a failed UART autoSync calibration now emits
(type 0b100, code 0x01 CALIBRATION_TIMEOUT) decodes as an error annotation.

The RTL side is pinned by
hw/sim/cocotb/tests/datalogger/uart/test_uart_calibration_fail_status_ds_p2_239.py.
Values are hard-coded (ROUTERTL-002): a moved code or type fails here.
"""
from __future__ import annotations

from sw.stream import decoders, zs_msg

STATUS_TYPE = 0b100
CALIBRATION_TIMEOUT = 0x01


def _status(code: int):
    hdr = zs_msg.make_header(datalogger_id=23, domain=zs_msg.DOMAIN_UART,
                             event_id=STATUS_TYPE, severity=zs_msg.SEV_INFO,
                             payload_len=4)
    return hdr, bytes([code, 0, 0, 0])


def test_a_failed_calibration_decodes_as_an_error_annotation():
    hdr, payload = _status(CALIBRATION_TIMEOUT)
    r = decoders.decode_uart(hdr, payload, 100_000_000)
    assert r.kind == "uart_calibration_timeout"
    assert r.is_error is True
    assert r.text == "CALIBRATION TIMEOUT"
    assert r.fields == {"status": "calibration_timeout"}


def test_the_lifecycle_frame_before_it_is_not_an_error():
    hdr, payload = _status(0x05)
    r = decoders.decode_uart(hdr, payload, 100_000_000)
    assert r.kind == "uart_enabled" and r.is_error is False
