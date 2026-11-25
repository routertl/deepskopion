# rr unknown-llm (AI-generated)

from typing import Callable, Optional
from .decode import decode_uart_msg
from .formatter import describe_uart_msg

from rr_cocotb_tb.monitors import bv_to_int, StreamMonitor

def _decode_ascii(b: int) -> str:
    if 32 <= b <= 126:
        return f"byte=0x{b:02X} | '{chr(b)}'"
    return f"byte=0x{b:02X}"

class UartMonitor(StreamMonitor):
    def __init__(
        self,
        env,
        inst,
        *,
        valid_name: str = "UART_RX_VLD",
        data_name: str = "UART_RX_DATA",
        name: str = "UART RX",
        decode_fn=lambda w: f"word=0x{int(w):08X} | {describe_uart_msg(decode_uart_msg(int(w)))}",
    ):
        valid_sig = getattr(inst, valid_name)
        data_sig  = getattr(inst, data_name)
        super().__init__(
            env,
            clk=env.clk,
            valid_fn=lambda: bv_to_int(valid_sig.value),
            data_fn =lambda: bv_to_int(data_sig.value),
            name=name,
            decode_fn=lambda w: f"word=0x{int(w):08X} | {describe_uart_msg(decode_uart_msg(int(w)))}",
        )