# rr unknown-llm (AI-generated)
# tb/datalogger/fields.py

# Bit positions (LSB = 0)
CFG_START_BIT        = 0
CFG_SHOW_SIG_BIT     = 1
CFG_UBS_EN_BIT       = 2
CFG_CHTMODE_EN_BIT     = 3
CFG_TOLERANCE_BIT    = 10


CFG_FS_LSB           = 4    # width 2: [5:4]
CFG_FS_WIDTH         = 2

CFG_AUTOSENSE_LSB    = 8    # width 2: [9:8]
CFG_AUTOSENSE_WIDTH  = 2

CFG_UBS_LINES_LSB    = 6    # width 2: [7:6]
CFG_UBS_LINES_WIDTH  = 2

CFG_ID_LSB           = 11   # width 5: [15:11]
CFG_ID_WIDTH         = 5

def mask(width: int, lsb: int) -> int:
    return ((1 << width) - 1) << lsb
