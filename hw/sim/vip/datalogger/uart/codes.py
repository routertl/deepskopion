# tb/datalogger/codes.py
from enum import IntEnum, IntFlag, auto


class MsgType(IntEnum):
    CONFIG  = 3
    STATUS  = 4
    PAYLOAD = 5
    ERROR   = 6
    UNKNOWN = 255


class EventStatus(IntEnum):
    CALIBRATION_DONE    = 0
    CALIBRATION_TIMEOUT = 1
    PARITY_ERROR        = 2
    START_BIT_ERROR     = 3
    STOP_BIT_ERROR      = 4
    ENABLED             = 5   # lifecycle frame (DS-P2.245)
    DISABLED            = 6   # lifecycle frame (DS-P2.245)
