# rr unknown-llm (AI-generated)
# tb/datalogger/uart/fields.py
#
# 16-bit UART-datalogger CONFIG word — the REAL decode map (uart_datalogger.vhd
# deviceLoadConfig, lines 357-361), NOT the old "RESERVED" mislabels that hid
# the autoSync bit and let pack_config silently emit explicit 5-bit (DS-T3.12):
#   [0]     enable / start
#   [1]     autoSync   : 1 = auto-detect baud + byte format from the line;
#                        0 = use the explicit [10:2] fields below.
#   [4:2]   dataBits   : (nbits - 5)  (0->5 .. 4->9 data bits)   [autoSync=0]
#   [5]     parityEn                                              [autoSync=0]
#   [6]     parityType : 0 even / 1 odd                           [autoSync=0]
#   [10:7]  stopBits   : bit7 = stopBitsDef(0) (1 -> 2 stop bits) [autoSync=0]
#   [15:11] datalogger_id : width 5
CFG_START_BIT        = 0
CFG_AUTOSYNC_BIT     = 1
CFG_DATABITS_LSB     = 2
CFG_DATABITS_WIDTH   = 3   # [4:2]
CFG_PARITY_EN_BIT    = 5
CFG_PARITY_TYPE_BIT  = 6
CFG_STOPBITS_LSB     = 7   # stopBitsDef(0); [10:8] unused
CFG_STOPBITS_WIDTH   = 4   # [10:7]

CFG_ID_LSB           = 11  # width 5: [15:11]
CFG_ID_WIDTH         = 5


def mask(width: int, lsb: int) -> int:
    return ((1 << width) - 1) << lsb
