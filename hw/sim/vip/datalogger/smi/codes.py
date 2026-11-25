# rr unknown-llm (AI-generated)
# tb/datalogger/codes.py
from enum import IntEnum

class UserFs(IntEnum):
    FS_1X = 0b00
    FS_2X = 0b01
    FS_4X = 0b10
    FS_8X = 0b11

class ToleranceConf(IntEnum):
    RELAX = 0b0
    STRICT  = 0b1

class AutosenseConf(IntEnum):
    AUTO = 0b00
    FORCE_32BIT = 0b01
    FORCE_48BIT = 0b10
