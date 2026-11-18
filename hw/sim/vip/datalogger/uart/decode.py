from dataclasses import dataclass
from typing import Optional, Tuple
from .codes import MsgType, EventStatus

TYPE_SHIFT       = 24
TYPE_MASK        = 0b111 << TYPE_SHIFT   # bits 26:24

LOW_NIBBLE_MASK  = 0xF
# Wire type codes [26:24] — single-sourced in uart_datalogger_protocol.yml; the RTL
# (uart_frame_gen.vhd) and the daemon (sw/stream/decoders.py) both already agree.
# DS-T3.15: this decoder was stale from before commit 48663d8 ("Align
# uart_frame_gen with YAML 2.0 protocol", which moved payload 000→010). It read
# PAYLOAD=0b000 and invented ERROR=0b010, so it mis-decoded EVERY real payload
# (0b010) as an ERROR. There is NO 0b000 type and NO distinct error type — line
# errors are STATUS (0b100) events (PARITY/START/STOP) decoded from the low nibble.
PAYLOAD_TYPE      = 0b010   # normal data (<=8-bit, or 9-bit with bit8=0)
PAYLOAD_9BIT_TYPE = 0b011   # 9-bit data with bit8=1
STATUS_TYPE       = 0b100
CONFIG_TYPE       = 0b001

@dataclass(frozen=True)
class DecodedMsg:
    kind: MsgType
    raw: int
    event: Optional[EventStatus] = None
    errors: Tuple[str, ...] = ()

_EVENT_NIBBLE_TO_STATUS = {
    0x0: EventStatus.CALIBRATION_DONE,
    0x1: EventStatus.CALIBRATION_TIMEOUT,
    0x2: EventStatus.PARITY_ERROR,
    0x3: EventStatus.START_BIT_ERROR,
    0x4: EventStatus.STOP_BIT_ERROR,
    0x5: EventStatus.ENABLED,
    0x6: EventStatus.DISABLED,
}

def _event_status_from_low_nibble(n: int) -> Optional[EventStatus]:
    return _EVENT_NIBBLE_TO_STATUS.get(n)

def decode_uart_msg(value: int) -> DecodedMsg:
    value = int(value)  # be explicit
    type_field = (value & TYPE_MASK) >> TYPE_SHIFT

    if type_field == STATUS_TYPE:
        code = value & LOW_NIBBLE_MASK
        st = _event_status_from_low_nibble(code)
        if st is None:
            return DecodedMsg(kind=MsgType.UNKNOWN, raw=value,
                              errors=(f"Unknown event code 0x{code:X}",))
        return DecodedMsg(kind=MsgType.STATUS, raw=value, event=st)

    if type_field == CONFIG_TYPE:
        return DecodedMsg(kind=MsgType.CONFIG, raw=value)

    # Both normal (0b010) and 9-bit-MSB-set (0b011) payloads are PAYLOAD.
    if type_field in (PAYLOAD_TYPE, PAYLOAD_9BIT_TYPE):
        return DecodedMsg(kind=MsgType.PAYLOAD, raw=value)

    return DecodedMsg(kind=MsgType.UNKNOWN, raw=value,
                      errors=("Unrecognized type field",))

def decode_config_msg(value: int) -> str:
    """Decode the raw config message value into a human-readable string."""
    nb_bits = value & 0x0F
    has_parity  = (value >> 4) & 0x01
    parity_type = (value >> 5) & 0x01
    stop_bits   = (value >> 6) & 0x01
    br_cycles   = (value >> 8) & 0xFFFF

    parity_str = "None"
    if has_parity:
        parity_str = "Even" if parity_type == 0 else "Odd"

    stop_bits_str = {
        0: "1+",
        1: "1",
    }.get(stop_bits, "Unknown")

    clock_period_ns = 10       # 100 MHz
    fpga_shift = 8             # br_cycles units = 8 FPGA clocks

    def baud_from_br_cycles(br_cycles: int) -> float:
        bit_time_ns = clock_period_ns * br_cycles * fpga_shift
        return 1e9 / bit_time_ns

    baud = baud_from_br_cycles(br_cycles)

    return (f"Config: {nb_bits} bits, Parity: {parity_str}, "
            f"Stop bits: {stop_bits_str}, Baud rate: {baud:.0f} bps")