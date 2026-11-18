# tb/datalogger/config.py
from dataclasses import dataclass
from .fields import *
from .codes import AutosenseConf, UserFs, ToleranceConf

@dataclass(frozen=True)
class DataloggerCfg:
    start: bool = False
    show_signaling: bool = False
    chtmode_enable: bool = False
    ubs_enable: bool = False
    autosense: AutosenseConf = AutosenseConf.AUTO
    tolerance : ToleranceConf = ToleranceConf.RELAX
    user_fs: UserFs = UserFs.FS_1X
    ubs_lines: int = 0            # 0..7 (must be <= project’s configured UBS wires)
    datalogger_id: int = 0           # 0..31

def _set_bit(v: int, bit: int, on: bool) -> int:
    return v | (1 << bit) if on else v & ~(1 << bit)

def _set_field(v: int, lsb: int, width: int, x: int) -> int:
    m = mask(width, lsb)
    return (v & ~m) | ((x << lsb) & m)

def pack_config(cfg: DataloggerCfg) -> int:
    v = 0
    # booleans
    v = _set_bit(v, CFG_START_BIT, cfg.start)
    v = _set_bit(v, CFG_SHOW_SIG_BIT, cfg.show_signaling)
    v = _set_bit(v, CFG_UBS_EN_BIT,  cfg.ubs_enable)
    v = _set_bit(v, CFG_CHTMODE_EN_BIT, cfg.chtmode_enable)
    v = _set_bit(v, CFG_TOLERANCE_BIT, cfg.tolerance)

    # fields
    if not (0 <= cfg.ubs_lines < (1 << CFG_UBS_LINES_WIDTH)):
        raise ValueError(f"ubs_lines out of range (0..{(1<<CFG_UBS_LINES_WIDTH)-1}): {cfg.ubs_lines}")
    if not (0 <= cfg.datalogger_id < (1 << CFG_ID_WIDTH)):
        raise ValueError(f"datalogger_id out of range (0..{(1<<CFG_ID_WIDTH)-1}): {cfg.datalogger_id}")
    if not (0 <= cfg.autosense < (1 << CFG_AUTOSENSE_WIDTH)):
        raise ValueError(f"autosense out of range (0..{(1<<CFG_AUTOSENSE_WIDTH)-1}): {cfg.autosense}")

    v = _set_field(v, CFG_FS_LSB,          CFG_FS_WIDTH,         int(cfg.user_fs))
    v = _set_field(v, CFG_UBS_LINES_LSB,   CFG_UBS_LINES_WIDTH,  cfg.ubs_lines)
    v = _set_field(v, CFG_ID_LSB,          CFG_ID_WIDTH,         cfg.datalogger_id)
    v = _set_field(v, CFG_AUTOSENSE_LSB,   CFG_AUTOSENSE_WIDTH,  cfg.autosense)

    return v & 0xFFFF

def unpack_config(v: int) -> DataloggerCfg:
    def bit(b): return (v >> b) & 1
    def fld(lsb, w): return (v >> lsb) & ((1 << w) - 1)

    return DataloggerCfg(
        start          = bool(bit(CFG_START_BIT)),
        show_signaling = bool(bit(CFG_SHOW_SIG_BIT)),
        chtmode_enable = bool(bit(CFG_CHTMODE_EN_BIT)),
        ubs_enable     = bool(bit(CFG_UBS_EN_BIT)),
        autosense      = AutosenseConf(fld(CFG_AUTOSENSE_LSB, CFG_AUTOSENSE_WIDTH)),
        tolerance      = ToleranceConf(bit(CFG_TOLERANCE_BIT)),
        user_fs        = UserFs(fld(CFG_FS_LSB, CFG_FS_WIDTH)),
        ubs_lines      = fld(CFG_UBS_LINES_LSB, CFG_UBS_LINES_WIDTH),
        datalogger_id     = fld(CFG_ID_LSB, CFG_ID_WIDTH),
    )
