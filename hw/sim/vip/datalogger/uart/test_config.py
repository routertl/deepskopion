"""DS-T3.12 regression guard: pack_config must set the autoSync bit.

The bug: DataloggerCfg/pack_config never packed CONFIG bit 1 (autoSync), so every
DataloggerCfg-driven UART test silently ran EXPLICIT mode with dataBits=0 => 5-bit
decode. These unit checks pin the CONFIG-word encoding to the uart_datalogger.vhd
decode map (and the proven inline cfg_word from test_uart_explicit_config)."""
from hw.sim.vip.datalogger.uart.config import DataloggerCfg, pack_config, unpack_config


def _ref(dev, *, enable, auto_sync, nbits, parity_en, parity_type, stop2):
    p = ((enable & 1) | ((auto_sync & 1) << 1) | (((nbits - 5) & 0x7) << 2)
         | ((parity_en & 1) << 5) | ((parity_type & 1) << 6) | ((1 if stop2 else 0) << 7))
    return ((dev & 0x1F) << 11) | (p & 0x7FF)


def test_default_sets_autosync():
    # The exact call that was silently mis-decoding (test_uart_datalogger.py).
    w = pack_config(DataloggerCfg(start=True, datalogger_id=12))
    assert (w >> 1) & 1 == 1, "autoSync (CONFIG bit 1) must be set by default"
    assert w == _ref(12, enable=1, auto_sync=1, nbits=8, parity_en=0, parity_type=0, stop2=False)


def test_explicit_8n1_encoding():
    w = pack_config(DataloggerCfg(start=True, datalogger_id=4, auto_sync=False, data_bits=8))
    assert (w >> 1) & 1 == 0
    assert w == _ref(4, enable=1, auto_sync=0, nbits=8, parity_en=0, parity_type=0, stop2=False)


def test_explicit_7e2_encoding():
    w = pack_config(DataloggerCfg(start=True, datalogger_id=1, auto_sync=False,
                               data_bits=7, parity_en=True, parity_type=0, two_stop=True))
    assert w == _ref(1, enable=1, auto_sync=0, nbits=7, parity_en=1, parity_type=0, stop2=True)


def test_roundtrip():
    for c in (DataloggerCfg(start=True, datalogger_id=9),
              DataloggerCfg(start=True, datalogger_id=6, auto_sync=False, data_bits=6,
                         parity_en=True, parity_type=1, two_stop=True)):
        assert unpack_config(pack_config(c)) == c
