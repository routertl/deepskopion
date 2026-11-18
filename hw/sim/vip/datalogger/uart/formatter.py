# Protocol datalogger formatters
from .codes import MsgType, EventStatus
from .decode import DecodedMsg, decode_uart_msg, decode_config_msg


def describe_uart_msg(msg: DecodedMsg) -> str:
    if msg.kind == MsgType.STATUS:
        label = {
            EventStatus.CALIBRATION_DONE:    "CALIBRATION DONE",
            EventStatus.CALIBRATION_TIMEOUT: "CALIBRATION TIMEOUT",
            EventStatus.PARITY_ERROR:        "PARITY ERROR",
            EventStatus.START_BIT_ERROR:     "START BIT ERROR",
            EventStatus.STOP_BIT_ERROR:      "STOP BIT ERROR",
            EventStatus.ENABLED:             "ENABLED",
            EventStatus.DISABLED:            "DISABLED",
        }.get(msg.event, "UART event")
        return f"Event message: {label}"

    if msg.kind == MsgType.PAYLOAD:
        text = "Payload message"
        text += f" = {byte_to_ascii(msg.raw & 0xFF)}"
        return text

    if msg.kind == MsgType.CONFIG:
        text = "Configuration message"
        text += f" = {decode_config_msg(msg.raw)}"
        return text

    if msg.kind == MsgType.ERROR:
        return "Type error message"

    return text + f" [ERROR: Unrecognized UART message type]"


def byte_to_ascii(b: int) -> str:
    if 32 <= b <= 126:
        return f"0x{b:02X} ('{chr(b)}')"
    return f"0x{b:02X}"

# Backwards-compatible one-liner
def analyze_uart_msg(value: int) -> str:
    return describe_uart_msg(decode_uart_msg(value))
