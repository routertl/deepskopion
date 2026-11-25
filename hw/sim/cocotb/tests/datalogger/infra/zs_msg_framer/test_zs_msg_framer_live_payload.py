# rr unknown-llm (AI-generated)
"""ZSF-REQ-50 / 51 / 52 — runtime PAYLOAD_WORDS_LIVE selects, defaults and
clamps the per-frame payload length (DS-P2.47 Phase B2).

Elaborated with G_PAYLOAD_WORDS=4. The pre-Phase-B2 suite
(test_zs_msg_framer.py) runs G_PAYLOAD_WORDS=1, where every live value clamps
to 1, so until DS-P2.187 nothing could fail if the live count were ignored,
mis-latched, mis-defaulted or unclamped: coverage-map reported
ZSF-REQ-50/51/52 missing.

Every expected length and payload_len below is a literal from the REQ text
(5 header words + n payload words; payload_len = 4n bytes), not derived from
the DUT (ROUTERTL-002). Each frame is also round-tripped through the PS-side
oracle sw.stream.zs_msg.parse_header.

The clamp test found a DUT defect on its first run: v_latched_words was
declared `natural range 1 to G_PAYLOAD_WORDS`, so a live count of 0 or
>G_PAYLOAD_WORDS was a range violation (nvc fatal) before the clamp could run.
Fixed in zs_msg_framer.vhd under DS-P2.187.

Run:  rr sim run test_zs_msg_framer_live_payload
"""

import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import routertl_core as _rrc

sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from engine.simulation import run_simulation  # noqa: E402
from sdk.cocotb_helpers import requires  # noqa: E402
from rr_cocotb_tb.env import TbEnv  # noqa: E402

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

sys.path.insert(0, str(DEEPSKOPION_ROOT))
from sw.stream import zs_msg as zsmsg  # noqa: E402

GENERICS = {"G_PAYLOAD_WORDS": 4}
PERIOD_NS = 10

# Four distinct, hard-coded payload words: PAYLOAD[31:0] is word 0.
PAYLOAD_WORDS = (0xCAFE0A00, 0xCAFE0B01, 0xCAFE0C02, 0xCAFE0D03)
PAYLOAD = sum(w << (32 * i) for i, w in enumerate(PAYLOAD_WORDS))

TUPLE = dict(
    datalogger_id=0x5C,
    msg_class=0x01,
    msg_type=0x1237,
    flags=0xA55A,
    timestamp=0x0123_4567_89AB_CDEF,
)


def custom_libraries():
    return {
        "tich": [
            f"{DEEPSKOPION_RTL}/pkg/zs_msg_pkg.vhd",
            f"{DEEPSKOPION_RTL}/datalogger/infra/zs_msg_framer/zs_msg_framer.vhd",
        ],
    }


async def reset_dut(dut):
    """Reset WITHOUT touching PAYLOAD_WORDS_LIVE (ZSF-REQ-51 needs it undriven)."""
    dut.RST.value = 1
    dut.IN_VALID.value = 0
    dut.OUT_READY.value = 1
    dut.DATALOGGER_ID.value = 0
    dut.MSG_CLASS.value = 0
    dut.MSG_TYPE.value = 0
    dut.FLAGS.value = 0
    dut.TIMESTAMP.value = 0
    dut.PAYLOAD.value = 0
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


def drive_tuple(dut):
    dut.DATALOGGER_ID.value = TUPLE["datalogger_id"]
    dut.MSG_CLASS.value = TUPLE["msg_class"]
    dut.MSG_TYPE.value = TUPLE["msg_type"]
    dut.FLAGS.value = TUPLE["flags"]
    dut.TIMESTAMP.value = TUPLE["timestamp"]
    dut.PAYLOAD.value = PAYLOAD
    dut.IN_VALID.value = 1


class FrameCollector:
    """One OUT_DATA word per accepted (OUT_VALID && OUT_READY) handshake.

    Words are grouped into frames by OUT_VALID deassert: the framer has no
    tlast, so a frame ends on the first cycle OUT_VALID is low.
    """

    def __init__(self, dut):
        self.dut = dut
        self.words = []
        self.frames = []
        self._cur = []
        self._stop = False

    async def start(self):
        while not self._stop:
            await RisingEdge(self.dut.CLK)
            try:
                v = int(self.dut.OUT_VALID.value)
                r = int(self.dut.OUT_READY.value)
            except ValueError:
                continue
            if v == 1 and r == 1:
                w = int(self.dut.OUT_DATA.value)
                self.words.append(w)
                self._cur.append(w)
            elif v == 0 and self._cur:
                self.frames.append(self._cur)
                self._cur = []

    def stop(self):
        self._stop = True
        if self._cur:
            self.frames.append(self._cur)
            self._cur = []


def check_frame(words, n_words, payload_len):
    """Diff one collected frame against the hard-coded expectation.

    ``n_words`` / ``payload_len`` come from the caller's literal table, never
    from the DUT. Returns a list of mismatch strings (empty = clean).
    """
    diffs = []
    if len(words) != n_words:
        diffs.append(f"frame length {len(words)} words, want {n_words}")
    if len(words) < 5:
        return diffs + ["frame shorter than the 5-word header"]
    if words[4] & 0xFFFF != payload_len:
        diffs.append(f"word4 payload_len field {words[4] & 0xFFFF}, want {payload_len}")
    buf = b"".join(int(w).to_bytes(4, "little") for w in words)
    try:
        hdr = zsmsg.parse_header(buf, offset=0)
    except ValueError as e:
        return diffs + [f"parse_header failed: {e}"]
    for field in ("datalogger_id", "msg_class", "msg_type", "flags", "timestamp"):
        if getattr(hdr, field) != TUPLE[field]:
            diffs.append(f"{field} {getattr(hdr, field):#x}, want {TUPLE[field]:#x}")
    if hdr.payload_len != payload_len:
        diffs.append(f"parsed payload_len {hdr.payload_len}, want {payload_len}")
    want_payload = list(PAYLOAD_WORDS[: payload_len // 4])
    if words[5:] != want_payload:
        diffs.append(
            "payload words ["
            + ", ".join(f"{w:#010x}" for w in words[5:])
            + "], want ["
            + ", ".join(f"{w:#010x}" for w in want_payload)
            + "]"
        )
    return diffs


@cocotb.test()
@requires("ZSF-REQ-51")
async def test_undriven_live_count_emits_full_frame(dut):
    """ZSF-REQ-51: PAYLOAD_WORDS_LIVE never written -> 5 + G_PAYLOAD_WORDS = 9
    words per frame, payload_len 16, all four payload words in order.

    First in the file and never writes the port. rr runs each nvc testcase
    in its own simulator session; if a session were ever shared, a value an
    earlier test drove would persist, so the port is checked against its
    default (4) before the first frame and the test fails loudly instead of
    passing on a stale drive.
    """
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)
    assert int(dut.PAYLOAD_WORDS_LIVE.value) == 4, (
        "ZSF-REQ-51: PAYLOAD_WORDS_LIVE reads "
        f"{int(dut.PAYLOAD_WORDS_LIVE.value)} before any drive, want the port "
        "default G_PAYLOAD_WORDS=4 (or a drive left over from a shared session)"
    )
    collector = FrameCollector(dut)
    cocotb.start_soon(collector.start())
    for _ in range(2):
        drive_tuple(dut)
        await RisingEdge(dut.CLK)
        while int(dut.IN_READY.value) != 1:
            await RisingEdge(dut.CLK)
        dut.IN_VALID.value = 0
        await ClockCycles(dut.CLK, 30)
    collector.stop()
    assert len(collector.frames) == 2, (
        f"ZSF-REQ-51: {len(collector.frames)} frames, want 2: {collector.frames}"
    )
    failures = [d for f in collector.frames for d in check_frame(f, 9, 16)]
    assert not failures, (
        "ZSF-REQ-51: undriven PAYLOAD_WORDS_LIVE did not default to "
        f"G_PAYLOAD_WORDS=4: {failures}"
    )


# live value driven -> (frame words, payload_len bytes), from the REQ text.
SELECT_TABLE = [(1, 6, 4), (2, 7, 8), (3, 8, 12), (4, 9, 16)]
# ZSF-REQ-52: 0 and G+1 are out of range, 255 is the 8-bit extreme.
CLAMP_TABLE = [(0, 6, 4), (1, 6, 4), (4, 9, 16), (5, 9, 16), (255, 9, 16)]


async def _one_frame(dut, live):
    """Drive one tuple with PAYLOAD_WORDS_LIVE=live; return the frame."""
    collector = FrameCollector(dut)
    cocotb.start_soon(collector.start())
    dut.PAYLOAD_WORDS_LIVE.value = live
    drive_tuple(dut)
    await RisingEdge(dut.CLK)
    while int(dut.IN_READY.value) != 1:
        await RisingEdge(dut.CLK)
    dut.IN_VALID.value = 0
    await ClockCycles(dut.CLK, 30)
    collector.stop()
    return collector.frames


async def _run_table(dut, req, table):
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)
    failures = []
    for live, n_words, payload_len in table:
        frames = await _one_frame(dut, live)
        if len(frames) != 1:
            failures.append(f"live={live}: {len(frames)} frames, want 1: {frames}")
            continue
        diffs = check_frame(frames[0], n_words, payload_len)
        if diffs:
            failures.append(f"live={live}: {diffs}")
    assert not failures, (
        f"{req}: PAYLOAD_WORDS_LIVE handling wrong (G_PAYLOAD_WORDS=4):\n  "
        + "\n  ".join(failures)
    )


@cocotb.test()
@requires("ZSF-REQ-50")
async def test_live_count_selects_frame_length(dut):
    """ZSF-REQ-50: live count n in [1, 4] -> exactly 5+n words, payload_len 4n."""
    await _run_table(dut, "ZSF-REQ-50", SELECT_TABLE)


@cocotb.test()
@requires("ZSF-REQ-50")
async def test_live_count_latched_on_handshake(dut):
    """ZSF-REQ-50: the count is latched on the input handshake.

    Drive live=3, then change the port to 1 on the cycle after the
    handshake. The in-flight frame must still carry 3 payload words.
    """
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)
    collector = FrameCollector(dut)
    cocotb.start_soon(collector.start())
    dut.PAYLOAD_WORDS_LIVE.value = 3
    drive_tuple(dut)
    await RisingEdge(dut.CLK)
    while int(dut.IN_READY.value) != 1:
        await RisingEdge(dut.CLK)
    dut.IN_VALID.value = 0
    dut.PAYLOAD_WORDS_LIVE.value = 1
    await ClockCycles(dut.CLK, 30)
    collector.stop()
    assert len(collector.frames) == 1, (
        f"ZSF-REQ-50: {len(collector.frames)} frames, want 1: {collector.frames}"
    )
    diffs = check_frame(collector.frames[0], 8, 12)
    assert not diffs, (
        "ZSF-REQ-50: live count not held from the handshake (drove 3, then "
        f"changed the port to 1): {diffs}"
    )


@cocotb.test()
@requires("ZSF-REQ-52")
async def test_live_count_clamps_to_port_capacity(dut):
    """ZSF-REQ-52: 0 latches as 1, >G_PAYLOAD_WORDS latches as G_PAYLOAD_WORDS.

    Never a zero-payload frame, never a payload beyond the 4-word port.
    """
    await _run_table(dut, "ZSF-REQ-52", CLAMP_TABLE)


def main():
    run_simulation(
        top_level="zs_msg_framer",
        module=Path(__file__).stem,
        custom_libraries=custom_libraries(),
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
