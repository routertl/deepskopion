# rr unknown-llm (AI-generated)
# tb/datalogger/config.py
from dataclasses import dataclass
from .fields import *

@dataclass(frozen=True)
class DataloggerCfg:
    start: bool = False
    datalogger_id: int = 0           # 0..31
    # DS-T3.12: autoSync (CONFIG bit 1) was NEVER packed — pack_config silently
    # emitted explicit mode with dataBits=0 => 5-bit decode, so every DataloggerCfg-
    # driven UART test mis-decoded an 8-bit stream. Default True = auto-detect the
    # baud + byte format from the line (the common, correct case; matches the
    # uart_datalogger_test_utils.apply_config 0x03 path). Set auto_sync=False to use
    # the explicit format fields below.
    auto_sync: bool = True
    data_bits: int = 8            # 5..9   (explicit mode only)
    parity_en: bool = False       # explicit mode only
    parity_type: int = 0          # 0 even / 1 odd  (explicit mode only)
    two_stop: bool = False        # True -> 2 stop bits  (explicit mode only)

def _set_bit(v: int, bit: int, on: bool) -> int:
    return v | (1 << bit) if on else v & ~(1 << bit)

def _set_field(v: int, lsb: int, width: int, x: int) -> int:
    m = mask(width, lsb)
    return (v & ~m) | ((x << lsb) & m)

def pack_config(cfg: DataloggerCfg) -> int:
    if not (0 <= cfg.datalogger_id < (1 << CFG_ID_WIDTH)):
        raise ValueError(f"datalogger_id out of range (0..{(1<<CFG_ID_WIDTH)-1}): {cfg.datalogger_id}")
    if not (5 <= cfg.data_bits <= 9):
        raise ValueError(f"data_bits out of range (5..9): {cfg.data_bits}")

    v = 0
    v = _set_bit(v, CFG_START_BIT,       cfg.start)
    v = _set_bit(v, CFG_AUTOSYNC_BIT,    cfg.auto_sync)
    # Explicit-format fields — the RTL ignores them when autoSync=1, but pack them
    # always so the word is well-defined and auto_sync=False just works.
    v = _set_field(v, CFG_DATABITS_LSB,  CFG_DATABITS_WIDTH, cfg.data_bits - 5)
    v = _set_bit(v, CFG_PARITY_EN_BIT,   cfg.parity_en)
    v = _set_bit(v, CFG_PARITY_TYPE_BIT, bool(cfg.parity_type & 1))
    v = _set_bit(v, CFG_STOPBITS_LSB,    cfg.two_stop)   # stopBitsDef(0)
    v = _set_field(v, CFG_ID_LSB,        CFG_ID_WIDTH,   cfg.datalogger_id)
    return v & 0xFFFF

def unpack_config(v: int) -> DataloggerCfg:
    def bit(b): return (v >> b) & 1
    def fld(lsb, w): return (v >> lsb) & ((1 << w) - 1)

    return DataloggerCfg(
        start       = bool(bit(CFG_START_BIT)),
        datalogger_id  = fld(CFG_ID_LSB, CFG_ID_WIDTH),
        auto_sync   = bool(bit(CFG_AUTOSYNC_BIT)),
        data_bits   = fld(CFG_DATABITS_LSB, CFG_DATABITS_WIDTH) + 5,
        parity_en   = bool(bit(CFG_PARITY_EN_BIT)),
        parity_type = bit(CFG_PARITY_TYPE_BIT),
        two_stop    = bool(bit(CFG_STOPBITS_LSB)),
    )
