"""DS-P2.257: arm_uart_breaks programs the UART datalogger for autoSync and the
stimulus generator's break pattern, in that order, with hard-coded words."""
import pytest

from sw.stream import stimulus_armer as A


class _Rec:
    def __init__(self):
        self.writes = []

    def write_u32(self, off, val):
        self.writes.append((off, val))


def test_arm_uart_breaks_write_sequence(monkeypatch):
    monkeypatch.setattr(A.time, "sleep", lambda _s: None)
    monkeypatch.setattr(A, "emio_mux_word", lambda proto: {"uart": 0x2}[proto])
    rb = _Rec()
    A.arm_uart_breaks(rb, low_bits=13, count=80, baud_div=100)
    assert rb.writes == [
        (0x24, 0x2),                       # EMIO_MUX_SEL: UART, before the logger (DS-P2.318)
        (0x20, 12 << 11),                  # datalogger 12 disable
        (0x20, (12 << 11) | 0x03),         # enable | autoSync
        (0x224, 100),                      # STIM_UART_BAUD_DIV
        (0x220, 0x0D03),                   # enable | break | 13 low bit-times
        (0x228, 80),                       # 80 breaks
    ]


@pytest.mark.parametrize("kw", [dict(low_bits=0), dict(low_bits=256), dict(count=0),
                                dict(count=0x10000), dict(baud_div=1)])
def test_arm_uart_breaks_rejects_before_any_write(monkeypatch, kw):
    monkeypatch.setattr(A.time, "sleep", lambda _s: None)
    rb = _Rec()
    with pytest.raises(ValueError):
        A.arm_uart_breaks(rb, **kw)
    assert rb.writes == []
