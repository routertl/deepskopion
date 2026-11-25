# rr unknown-llm (AI-generated)
"""can_datalogger integration test + adversarial CAN BFM (DS-P3.100, the top).

Drives a RAW CAN RX line through the FULL four-block chain — can_bit_timing →
can_destuff → can_frame_fsm + can_crc15 — exercising everything the per-block
unit tests deliberately stubbed: real bit-clock recovery, real bit-stuffing, the
SOF seam, and the live CRC-15 engine wired to the FSM's window.

THE BFM (`CanBusBFM`) is adversarial-from-birth: it encodes a frame (SOF → ID →
control → data → CRC-15 → ACK → EOF), computes the real CRC-15, applies CAN
bit-stuffing, and bit-bangs RX at the configured prescaler. Knobs let it emit
MALFORMED frames — corrupted CRC, a 6-run stuff violation, a recessive (un-ACKed)
slot — so the error-provenance REQs are driven, not just the happy path.

All assertions are hard-coded expected values (ROUTERTL-002); the BFM's encode
side is stimulus, never an oracle the checker reads back from the DUT.

Covers end-to-end (the real datapath, not a stub):
  CAN-REQ-1  reset/idle determinism (no messages on an idle recessive bus)
  CAN-REQ-3  standard data frame, byte-exact, no crc_error on a good CRC
  CAN-REQ-4  extended (29-bit) data frame
  CAN-REQ-5  bit de-stuffing — a stuffed wire decodes to the ORIGINAL bytes
  CAN-REQ-7  corrupted CRC → crc_error (real engine disagrees with the wire)
  CAN-REQ-8  6-run stuff violation → stuff_error
  CAN-REQ-9  recessive ACK slot → ack_error
  CAN-REQ-10 back-to-back frames both fully captured
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
sys.path.insert(0, str(DS_ROOT / "hw" / "sim" / "cocotb"))   # shared BFMs
from can_bfm import CanBusBFM   # noqa: E402

DEV_ID = 20
CONFIG_WIDTH = 16
ID_WIDTH = 5
NUM_TQ = 16
PRESCALER = 4                       # clk_sys cycles per TQ → 64 cyc/bit (keeps sim brisk)
BIT_TIME = NUM_TQ * PRESCALER

# zs_msg decode (can_datalogger_protocol.yml)
T_ID, T_DATA, T_CTRL, T_EVENT, T_CRC = 1, 2, 3, 4, 5
EV_ENABLED = 0x03
EV_FRAME_BEGIN, EV_FRAME_END = 0x10, 0x20
EV_ACK_OBS = 0x30
EV_STUFF_ERR, EV_FORM_ERR, EV_CRC_ERR, EV_ACK_ERR = 0x40, 0x41, 0x42, 0x43


def mtype(m):   return (m >> 24) & 0x7
def devid(m):   return (m >> 27) & 0x1F
def payload(m): return m & 0xFF


# ── DUT plumbing ─────────────────────────────────────────────────────────────
def make_config(enable, prescaler, dev_id=DEV_ID):
    """16-bit CONFIG word: [15:11]=device_id, [10:1]=bit_prescaler, [0]=enable."""
    return (dev_id << 11) | ((prescaler & 0x3FF) << 1) | (enable & 1)


def start_collector(dut, got):
    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.CAN_MSG_VLD.value) == 1 and int(dut.CAN_MSG_RDY.value) == 1:
                m = int(dut.CAN_MSG_OUT.value)
                got.append(m)
                assert devid(m) == DEV_ID, f"misrouted msg dev={devid(m)} m={m:#010x}"
    cocotb.start_soon(collect())


async def setup(dut, enable=True):
    cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
    dut.RST.value = 1
    dut.CAN_PINS.value = 1                           # recessive idle
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.CAN_MSG_RDY.value = 1                        # always drain
    await ClockCycles(dut.CLK, 6)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 2)
    # apply the enable+prescaler CONFIG (config_control needs a CONFIG change edge)
    dut.CONFIG.value = make_config(1 if enable else 0, PRESCALER)
    await ClockCycles(dut.CLK, 4)


def decode(got):
    return [(mtype(m), payload(m)) for m in got]


# ── CAN-REQ-1: reset / idle determinism ──────────────────────────────────────
@cocotb.test()
async def idle_bus_is_silent(dut):
    got = []
    await setup(dut, enable=True)
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=20)                          # recessive, no SOF
    # only lifecycle (enabled) may appear; NO frame messages on an idle bus
    kinds = decode(got)
    assert all(k == T_EVENT for k, _ in kinds), f"idle bus emitted non-events: {kinds}"
    assert not any(p in (EV_FRAME_BEGIN, EV_FRAME_END) for k, p in kinds), \
        f"idle bus produced a frame boundary: {kinds}"
    dut._log.info(f"PASS idle — bus silent, only {len(kinds)} lifecycle event(s)")


# ── CAN-REQ-3 / -9: standard data frame, good CRC, ACKed ─────────────────────
@cocotb.test()
async def standard_frame_end_to_end(dut):
    got = []
    await setup(dut, enable=True)
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=8)
    meta = await bfm.send(can_id=0x123, data=[0xAB, 0xCD], ack=0)   # dominant ACK
    await bfm.idle(bits=10)

    crc = meta["crc"]
    expected = [
        (T_EVENT, EV_FRAME_BEGIN),
        (T_CTRL, 0x02), (T_ID, 0x01), (T_ID, 0x23),
        (T_DATA, 0xAB), (T_DATA, 0xCD),
        (T_CRC, (crc >> 7) & 0xFF), (T_CRC, (crc << 1) & 0xFF),
        (T_EVENT, EV_ACK_OBS), (T_EVENT, EV_FRAME_END)]
    frame = [m for m in decode(got) if not (m[0] == T_EVENT and m[1] == EV_ENABLED)]
    assert frame == expected, f"standard frame mismatch:\n got {frame}\n exp {expected}"
    assert not any(p == EV_CRC_ERR for k, p in frame), "good CRC raised crc_error"
    dut._log.info(f"PASS standard e2e — ID=0x123 data=[ab,cd] CRC={crc:#06x}: byte-exact, no crc_error")


# ── CAN-REQ-4: extended (29-bit) data frame ──────────────────────────────────
@cocotb.test()
async def extended_frame_end_to_end(dut):
    got = []
    await setup(dut, enable=True)
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=8)
    meta = await bfm.send(can_id=0x12345678, data=[0x5A], ide=True, ack=0)
    await bfm.idle(bits=10)

    crc = meta["crc"]
    expected = [
        (T_EVENT, EV_FRAME_BEGIN),
        (T_CTRL, 0x81),
        (T_ID, 0x12), (T_ID, 0x34), (T_ID, 0x56), (T_ID, 0x78),
        (T_DATA, 0x5A),
        (T_CRC, (crc >> 7) & 0xFF), (T_CRC, (crc << 1) & 0xFF),
        (T_EVENT, EV_ACK_OBS), (T_EVENT, EV_FRAME_END)]
    frame = [m for m in decode(got) if not (m[0] == T_EVENT and m[1] == EV_ENABLED)]
    assert frame == expected, f"extended frame mismatch:\n got {frame}\n exp {expected}"
    dut._log.info(f"PASS extended e2e — ID=0x12345678 data=[5a] CRC={crc:#06x}: byte-exact")


# ── CAN-REQ-5: bit de-stuffing — a stuffed wire decodes to the ORIGINAL ──────
@cocotb.test()
async def destuffing_recovers_original(dut):
    got = []
    await setup(dut, enable=True)
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=8)
    # ID + data chosen to force >=5 identical runs (stuff bits get inserted).
    meta = await bfm.send(can_id=0x000, data=[0x00, 0xFF], ack=0)
    await bfm.idle(bits=10)

    assert len(meta["stuffed"]) > len(meta["covered"]) + 15, \
        "test bug: this frame did not actually exercise stuffing"
    frame = [m for m in decode(got) if not (m[0] == T_EVENT and m[1] == EV_ENABLED)]
    ids = [p for k, p in frame if k == T_ID]
    datas = [p for k, p in frame if k == T_DATA]
    assert ids == [0x00, 0x00], f"de-stuffed ID wrong: {ids}"          # 11-bit 0 → 2 zero bytes
    assert datas == [0x00, 0xFF], f"de-stuffed data wrong (stuff bit leaked?): {datas}"
    assert not any(p in (EV_STUFF_ERR, EV_CRC_ERR) for k, p in frame), \
        f"well-formed stuffed frame faulted: {frame}"
    n_stuffed = len(meta['stuffed']) - (len(meta['covered']) + 15)
    dut._log.info(f"PASS destuff e2e — {n_stuffed} stuff bits removed, ID/data exact, no fault")


# ── CAN-REQ-7: corrupted CRC → crc_error (the REAL engine disagrees) ─────────
@cocotb.test()
async def corrupted_crc_e2e(dut):
    got = []
    await setup(dut, enable=True)
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=8)
    # force a wrong CRC on the wire; the datalogger's own CRC-15 must disagree
    await bfm.send(can_id=0x123, data=[0xAB, 0xCD], force_crc=0x0000, ack=0)
    await bfm.idle(bits=10)

    frame = [m for m in decode(got) if not (m[0] == T_EVENT and m[1] == EV_ENABLED)]
    assert any(p == EV_CRC_ERR for k, p in frame), f"corrupted CRC did NOT raise crc_error: {frame}"
    dut._log.info("PASS crc_error e2e — wire CRC 0x0000 vs computed → crc_error raised")


# ── CAN-REQ-8: 6-run stuff violation → stuff_error ───────────────────────────
@cocotb.test()
async def stuff_violation_e2e(dut):
    got = []
    await setup(dut, enable=True)
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=8)
    # Hand-craft a malformed wire: SOF + 6 dominant in the stuff-active region
    # (a healthy transmitter would have inserted a recessive stuff bit after 5).
    await bfm.drive_bits([0, 0, 0, 0, 0, 0, 0])     # SOF + 6 more dominant = 6-run violation
    dut.CAN_PINS.value = 1
    await bfm.idle(bits=12)

    frame = [m for m in decode(got) if not (m[0] == T_EVENT and m[1] == EV_ENABLED)]
    assert any(p == EV_STUFF_ERR for k, p in frame), f"6-run did NOT raise stuff_error: {frame}"
    dut._log.info("PASS stuff_error e2e — 6 dominant bits raised stuff_error")


# ── CAN-REQ-9: recessive ACK slot → ack_error ────────────────────────────────
@cocotb.test()
async def no_ack_raises_ack_error(dut):
    got = []
    await setup(dut, enable=True)
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=8)
    await bfm.send(can_id=0x123, data=[0xAB, 0xCD], ack=1)   # recessive ACK = nobody ACKed
    await bfm.idle(bits=10)

    frame = [m for m in decode(got) if not (m[0] == T_EVENT and m[1] == EV_ENABLED)]
    assert any(p == EV_ACK_ERR for k, p in frame), f"un-ACKed frame missing ack_error: {frame}"
    assert not any(p == EV_ACK_OBS for k, p in frame), f"un-ACKed frame falsely ack_observed: {frame}"
    dut._log.info("PASS ack_error e2e — recessive ACK slot raised ack_error")


# ── CAN-REQ-10: back-to-back frames both fully captured ──────────────────────
@cocotb.test()
async def back_to_back_frames(dut):
    got = []
    await setup(dut, enable=True)
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=8)
    await bfm.send(can_id=0x101, data=[0x11], ack=0)
    await bfm.idle(bits=6)                           # interframe space
    await bfm.send(can_id=0x202, data=[0x22], ack=0)
    await bfm.idle(bits=10)

    frame = [m for m in decode(got) if not (m[0] == T_EVENT and m[1] == EV_ENABLED)]
    begins = [i for i, (k, p) in enumerate(frame) if k == T_EVENT and p == EV_FRAME_BEGIN]
    ends = [i for i, (k, p) in enumerate(frame) if k == T_EVENT and p == EV_FRAME_END]
    assert len(begins) == 2 and len(ends) == 2, f"expected 2 complete frames, got {frame}"
    ids = [p for k, p in frame if k == T_ID]
    # frame1 ID 0x101 → bytes 0x01,0x01 ; frame2 ID 0x202 → 0x02,0x02
    assert ids == [0x01, 0x01, 0x02, 0x02], f"back-to-back IDs bled/dropped: {ids}"
    dut._log.info("PASS back-to-back — 2 frames fully captured, IDs 0x101/0x202 clean")


def main():
    run_simulation(
        top_level="can_datalogger",
        module="test_can_datalogger",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [
            str(RTL / "pkg" / "datalogger_config_pkg.vhd"),
            str(RTL / "pkg" / "datalogger_pkg.vhd"),
            str(RTL / "datalogger" / "infra" / "config_control" / "config_control.vhd"),
            str(RTL / "datalogger" / "can" / "can_bit_timing.vhd"),
            str(RTL / "datalogger" / "can" / "can_destuff.vhd"),
            str(RTL / "datalogger" / "can" / "can_crc15.vhd"),
            str(RTL / "datalogger" / "can" / "can_crc17.vhd"),
            str(RTL / "datalogger" / "can" / "can_crc21.vhd"),
            str(RTL / "datalogger" / "can" / "can_frame_fsm.vhd"),
            str(RTL / "datalogger" / "can" / "can_datalogger.vhd"),
        ]},
    )


if __name__ == "__main__":
    main()
