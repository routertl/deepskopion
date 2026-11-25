# rr unknown-llm (AI-generated)
"""EdgeReplaySource — drive a cocotb DUT pin from captured Edge Detector edges
(DS-P2.60).

This is the replay half of "dogfood a captured real signal": an edge capture
recorded on silicon (or synthesised) is decoded into absolute (t_ns, level)
transitions and played back onto a DUT input, so a datalogger testbench sees the
exact waveform that was on the wire.

    src = EdgeReplaySource(dut.UART_IN, clk_mhz=100)
    await src.replay_capture("capture.jsonl", instance=1)   # EDGE0 → UART_IN

The codec lives in sw/stream/edge_capture.py (single source of truth, shared
with the daemon's capture path).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional, Tuple, Union

from cocotb.triggers import Timer

# Import the shared codec from the daemon package (sw/stream).
_DS_ROOT = Path(__file__).resolve().parents[3]  # deepskopion/
sys.path.insert(0, str(_DS_ROOT / "sw" / "stream"))
from edge_capture import EdgeCapture, words_to_transitions  # noqa: E402


class EdgeReplaySource:
    """Replays captured edges onto ``pin`` (a cocotb handle)."""

    def __init__(self, pin, clk_mhz: int = 100):
        self._pin = pin
        self._clk_mhz = clk_mhz

    async def replay_transitions(self, transitions: List[Tuple[int, int]],
                                 idle_level: int = 1,
                                 settle_ns: int = 0) -> None:
        """Drive ``pin`` through an absolute (t_ns, level) transition list.

        The pin is held at ``idle_level`` until the first transition; times are
        taken relative to the first transition so a capture that starts at an
        arbitrary absolute ns replays immediately."""
        self._pin.value = idle_level
        if not transitions:
            if settle_ns:
                await Timer(settle_ns, units="ns")
            return
        t0 = transitions[0][0]
        prev = 0
        for t_ns, level in transitions:
            rel = t_ns - t0
            dt = rel - prev
            if dt > 0:
                await Timer(dt, units="ns")
                prev = rel
            self._pin.value = level & 1
        if settle_ns:
            await Timer(settle_ns, units="ns")

    async def replay_capture(self, capture: Union[str, EdgeCapture],
                             instance: Optional[int] = None,
                             idle_level: int = 1,
                             settle_ns: int = 0) -> int:
        """Load a capture (path or :class:`EdgeCapture`) and replay one
        instance's edges. Returns the number of transitions played."""
        cap = EdgeCapture.read(capture) if isinstance(capture, str) else capture
        tr = cap.transitions(instance=instance)
        await self.replay_transitions(tr, idle_level=idle_level, settle_ns=settle_ns)
        return len(tr)

    async def replay_words(self, words: List[int], instance: Optional[int] = None,
                           idle_level: int = 1, settle_ns: int = 0) -> int:
        """Replay a raw EDGE_EVENT word list (e.g. straight off the wire)."""
        tr = words_to_transitions(words, self._clk_mhz, instance=instance)
        await self.replay_transitions(tr, idle_level=idle_level, settle_ns=settle_ns)
        return len(tr)


def uart_waveform_words(payload: bytes, baud: int, *, instance: int = 1,
                        clk_mhz: int = 100, bits: int = 8, stop_bits: int = 1,
                        start_ns: int = 5_000) -> List[int]:
    """Synthesise EDGE_EVENT words for a clean UART 8N1 waveform of ``payload``.

    Useful to build a deterministic capture fixture for the replay round-trip
    without needing live silicon. Idle = high; each byte = start(0) + LSB-first
    data + stop(1). Only level CHANGES are emitted (as the real edge datalogger
    does). The encoded edge timing carries the baud, so an autoSync=0 explicit
    datalogger recovers it."""
    from edge_capture import encode_edge_word
    bit_ns = round(1_000_000_000 / baud)
    words: List[int] = []
    level = 1                      # idle high
    t = start_ns
    for byte in payload:
        frame = [0] + [(byte >> i) & 1 for i in range(bits)] + [1] * stop_bits
        for b in frame:
            if b != level:
                words.append(encode_edge_word(instance, t, b, clk_mhz))
                level = b
            t += bit_ns
        # inter-byte idle gap (line already high after stop)
        t += bit_ns
    return words
