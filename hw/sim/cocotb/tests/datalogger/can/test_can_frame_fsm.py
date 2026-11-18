"""can_frame_fsm unit test (DS-P3.100, Block 3).

Isolates the FRAME WALK: drives the DE-STUFFED field-bit stream directly (plus the
single raw SOF strobe that arms IDLE), so stuffing/CRC-recovery are out of scope
here (proven in Block 1/2, re-proven at integration with the CAN BFM). Asserts the
emitted 32-bit zs_msg stream BYTE-EXACT against hard-coded expected frames
(ROUTERTL-002) — the FSM never gets to "decide" what's correct.

Covers:
  CAN-REQ-2  lifecycle (enabled event on the enable edge)
  CAN-REQ-3  standard data frame, end-to-end, byte-exact
  CAN-REQ-4  extended (29-bit) data frame → control(IDE=1) + 4 id bytes
  CAN-REQ-6  remote frame (RTR=1) → control(RTR=1), NO data_payload messages
  CAN-REQ-7  crc_error verdict — the FSM emits crc_error iff its CRC_COMPUTED
             input disagrees with the captured wire CRC (the compare logic; the
             CRC-15 *math* is proven in test_can_crc15, and the real engine wired
             to the real window is proven end-to-end in test_can_datalogger)
  CAN-REQ-9  ack_observed on a dominant ACK slot

NOTE on CRC_COMPUTED: this unit isolates the frame walk + the crc_error COMPARE.
can_crc15 is a sibling block wired in the top, so here we drive CRC_COMPUTED
directly: a good frame sets it equal to the wire CRC (no crc_error), the
corrupted test sets it different (crc_error). The wire CRC values below are
arbitrary field contents — their CRC-15 correctness is not under test here.
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

DS_ROOT = Path(__file__).resolve().parents[6]   # deepskopion/
RTL = DS_ROOT / "hw" / "rtl"

DEV_ID = 20

# zs_msg type codes (can_datalogger_protocol.yml)
T_ID, T_DATA, T_CTRL, T_EVENT, T_CRC = 1, 2, 3, 4, 5
EV_ENABLED = 0x03
EV_FRAME_BEGIN, EV_FRAME_END = 0x10, 0x20
EV_ACK_OBS = 0x30
EV_CRC_ERR = 0x42


def mtype(m):   return (m >> 24) & 0x7
def devid(m):   return (m >> 27) & 0x1F
def payload(m): return m & 0xFF


def bits_msb(value, width):
    """MSB-first list of `width` bits of `value` (the CAN wire order)."""
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


# ── stimulus helpers ────────────────────────────────────────────────────────
# Inter-strobe gap. The FSM stages up to 5 messages on one bit (extended frame:
# control + 4 id bytes) and drains one/clk while the capture branch pauses — so a
# DS strobe must not arrive until the burst has drained. The real datapath
# guarantees this structurally (can_bit_timing emits <=1 strobe per CAN bit, i.e.
# >=16 clk_sys cycles apart, vs a max 5-cycle burst); we mirror that invariant
# here with a gap comfortably above the 5-msg burst rather than an unrealistically
# tight cadence the hardware can never produce.
DS_GAP = 8

async def ds_bit(dut, b):
    """Strobe one de-stuffed field bit into the FSM (paced per the drain invariant)."""
    dut.DS_BIT.value = b
    dut.DS_VALID.value = 1
    await RisingEdge(dut.CLK)
    dut.DS_VALID.value = 0
    await ClockCycles(dut.CLK, DS_GAP)


async def raw_sof(dut):
    """One raw sample strobe of a dominant bit — the SOF that arms IDLE."""
    dut.BIT_VALUE.value = 0
    dut.BIT_VALID.value = 1
    await RisingEdge(dut.CLK)
    dut.BIT_VALID.value = 0
    dut.BIT_VALUE.value = 1
    await ClockCycles(dut.CLK, 3)


async def reset(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
    dut.RST.value = 1
    dut.ENABLE.value = 0
    dut.BIT_VALID.value = 0
    dut.BIT_VALUE.value = 1      # recessive idle
    dut.DS_VALID.value = 0
    dut.DS_BIT.value = 1
    dut.STUFF_ERROR.value = 0
    dut.CAN_MSG_RDY.value = 1    # always drain
    dut.TIMESTAMP.value = 0
    dut.CRC_COMPUTED.value = 0   # sibling can_crc15 result (driven per-frame below)
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


def start_collector(dut, got):
    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.CAN_MSG_VLD.value) == 1 and int(dut.CAN_MSG_RDY.value) == 1:
                m = int(dut.CAN_MSG_OUT.value)
                got.append(m)
                assert devid(m) == DEV_ID, f"misrouted msg dev={devid(m)} m={m:#010x}"
    cocotb.start_soon(collect())


async def run_frame(dut, ds_stream, crc_computed=0):
    """Enable, drive SOF + the de-stuffed stream, return the decoded messages.

    `crc_computed` is the value the sibling can_crc15 would present on CRC_COMPUTED
    when the CRC field completes; the FSM emits crc_error iff it != the wire CRC.
    Held stable for the whole frame (the FSM only samples it at the CRC-field end)."""
    got = []
    start_collector(dut, got)
    dut.CRC_COMPUTED.value = crc_computed
    dut.ENABLE.value = 1
    await ClockCycles(dut.CLK, 6)
    await raw_sof(dut)
    for b in ds_stream:
        await ds_bit(dut, b)
    await ClockCycles(dut.CLK, 10)
    return [(mtype(m), payload(m)) for m in got]


# ── CAN-REQ-3 / -9: standard data frame ─────────────────────────────────────
# ID=0x123, RTR=0, IDE=0, DLC=2, data=[0xAB,0xCD], CRC=0x5555, ACK dominant.
#   id byte0 = 00000|id[10:8]=001 = 0x01 ; byte1 = id[7:0] = 0x23
#   control  = IDE0 RTR0 00 DLC0010 = 0x02
#   crc byte0= CRC[14:7]=0xAA ; byte1 = CRC[6:0]<<1 = 0xAA
@cocotb.test()
async def standard_frame_byte_exact(dut):
    await reset(dut)
    ds = (
        [0]                                          # SOF echo (swallowed)
        + bits_msb(0x123, 11) + [0] + [0] + [0]      # ID, RTR=0, IDE=0, r0
        + bits_msb(0x2, 4)                           # DLC=2
        + bits_msb(0xAB, 8) + bits_msb(0xCD, 8)      # data
        + bits_msb(0x5555, 15)                       # CRC-15
        + [1] + [0] + [1]                            # CRC-delim, ACK(dom), ACK-delim
        + [1, 1, 1, 1, 1, 1, 1])                     # EOF
    expected = [
        (T_EVENT, EV_ENABLED), (T_EVENT, EV_FRAME_BEGIN),
        (T_CTRL, 0x02), (T_ID, 0x01), (T_ID, 0x23),
        (T_DATA, 0xAB), (T_DATA, 0xCD), (T_CRC, 0xAA), (T_CRC, 0xAA),
        (T_EVENT, EV_ACK_OBS), (T_EVENT, EV_FRAME_END)]
    got = await run_frame(dut, ds, crc_computed=0x5555)   # matches wire → no crc_error
    assert got == expected, _diff(got, expected)
    dut._log.info(f"PASS standard — ID=0x123 DLC=2 data=[ab,cd] CRC=0x5555: {len(got)} msgs byte-exact")


# ── CAN-REQ-4: extended (29-bit) data frame ─────────────────────────────────
# FullID=0x12345678, IDE=1, RTR=0, DLC=1, data=[0x5A], CRC=0x1234.
#   base=ID[28:18]=0x48D, ext=ID[17:0]=0x5678
#   id bytes = 000|ID[28:24]=0x12, ID[23:16]=0x34, ID[15:8]=0x56, ID[7:0]=0x78
#   control = IDE1 RTR0 00 DLC0001 = 0x81
#   crc byte0 = 0x1234[14:7]=0x24 ; byte1 = 0x1234[6:0]<<1 = 0x68
@cocotb.test()
async def extended_frame_byte_exact(dut):
    await reset(dut)
    base, ext = (0x12345678 >> 18) & 0x7FF, 0x12345678 & 0x3FFFF
    ds = (
        [0]
        + bits_msb(base, 11) + [1] + [1]             # base ID, SRR=1, IDE=1
        + bits_msb(ext, 18) + [0] + [0] + [0]        # ext ID, RTR=0, r1, r0
        + bits_msb(0x1, 4)                           # DLC=1
        + bits_msb(0x5A, 8)                          # data
        + bits_msb(0x1234, 15)                       # CRC-15
        + [1] + [0] + [1]
        + [1, 1, 1, 1, 1, 1, 1])
    expected = [
        (T_EVENT, EV_ENABLED), (T_EVENT, EV_FRAME_BEGIN),
        (T_CTRL, 0x81),
        (T_ID, 0x12), (T_ID, 0x34), (T_ID, 0x56), (T_ID, 0x78),
        (T_DATA, 0x5A), (T_CRC, 0x24), (T_CRC, 0x68),
        (T_EVENT, EV_ACK_OBS), (T_EVENT, EV_FRAME_END)]
    got = await run_frame(dut, ds, crc_computed=0x1234)   # matches wire → no crc_error
    assert got == expected, _diff(got, expected)
    dut._log.info(f"PASS extended — ID=0x12345678 DLC=1 data=[5a] CRC=0x1234: {len(got)} msgs byte-exact")


# ── CAN-REQ-6: remote frame (RTR=1) — NO data bytes despite DLC>0 ────────────
# ID=0x7A0, RTR=1, IDE=0, DLC=2 (but a remote frame carries no data), CRC=0x0F0F.
#   id bytes = 0x07, 0xA0 ; control = IDE0 RTR1 00 DLC0010 = 0x42
#   crc byte0 = 0x0F0F[14:7]=0x1E ; byte1 = 0x0F0F[6:0]<<1 = 0x1E
@cocotb.test()
async def remote_frame_no_data(dut):
    await reset(dut)
    ds = (
        [0]
        + bits_msb(0x7A0, 11) + [1] + [0] + [0]      # ID, RTR=1, IDE=0, r0
        + bits_msb(0x2, 4)                           # DLC=2 (no data follows)
        + bits_msb(0x0F0F, 15)                       # CRC-15 (data field skipped)
        + [1] + [0] + [1]
        + [1, 1, 1, 1, 1, 1, 1])
    expected = [
        (T_EVENT, EV_ENABLED), (T_EVENT, EV_FRAME_BEGIN),
        (T_CTRL, 0x42), (T_ID, 0x07), (T_ID, 0xA0),
        (T_CRC, 0x1E), (T_CRC, 0x1E),                # no T_DATA between id and crc
        (T_EVENT, EV_ACK_OBS), (T_EVENT, EV_FRAME_END)]
    got = await run_frame(dut, ds, crc_computed=0x0F0F)   # matches wire → no crc_error
    assert got == expected, _diff(got, expected)
    assert not any(t == T_DATA for t, _ in got), f"remote frame emitted data: {got}"
    dut._log.info(f"PASS remote — ID=0x7A0 RTR=1 DLC=2 (no data) CRC=0x0F0F: {len(got)} msgs byte-exact")


# ── CAN-REQ-7: corrupted CRC → crc_error ─────────────────────────────────────
# Same standard frame as above, but the datalogger's own CRC-15 (CRC_COMPUTED)
# disagrees with the captured wire CRC → the FSM MUST emit a crc_error event
# (0x42) right after the 2 crc_payload bytes. The good-CRC twin (above) proves
# the verdict does NOT fire on a match — together they pin the compare both ways.
@cocotb.test()
async def corrupted_crc_raises_crc_error(dut):
    await reset(dut)
    ds = (
        [0]
        + bits_msb(0x123, 11) + [0] + [0] + [0]
        + bits_msb(0x2, 4)
        + bits_msb(0xAB, 8) + bits_msb(0xCD, 8)
        + bits_msb(0x5555, 15)                       # wire CRC = 0x5555
        + [1] + [0] + [1]
        + [1, 1, 1, 1, 1, 1, 1])
    expected = [
        (T_EVENT, EV_ENABLED), (T_EVENT, EV_FRAME_BEGIN),
        (T_CTRL, 0x02), (T_ID, 0x01), (T_ID, 0x23),
        (T_DATA, 0xAB), (T_DATA, 0xCD), (T_CRC, 0xAA), (T_CRC, 0xAA),
        (T_EVENT, EV_CRC_ERR),                        # ← engine disagrees with wire
        (T_EVENT, EV_ACK_OBS), (T_EVENT, EV_FRAME_END)]
    # CRC_COMPUTED = 0x7F3C (the genuine CRC-15 of this frame, per test_can_crc15)
    # != the corrupted wire CRC 0x5555 → crc_error.
    got = await run_frame(dut, ds, crc_computed=0x7F3C)
    assert got == expected, _diff(got, expected)
    assert (T_EVENT, EV_CRC_ERR) in got, f"corrupted CRC did not raise crc_error: {got}"
    dut._log.info(f"PASS crc_error — wire 0x5555 vs computed 0x7F3C raised crc_error (0x42): {len(got)} msgs")


def _diff(got, expected):
    return ("frame decode mismatch:\n"
            f"  got     : {[(t, hex(p)) for t, p in got]}\n"
            f"  expected: {[(t, hex(p)) for t, p in expected]}")


def main():
    run_simulation(
        top_level="can_frame_fsm",
        module="test_can_frame_fsm",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [
            str(RTL / "pkg" / "datalogger_config_pkg.vhd"),
            str(RTL / "pkg" / "datalogger_pkg.vhd"),
            str(RTL / "datalogger" / "can" / "can_frame_fsm.vhd"),
        ]},
    )


if __name__ == "__main__":
    main()
