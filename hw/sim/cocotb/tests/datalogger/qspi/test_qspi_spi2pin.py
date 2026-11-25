# rr unknown-llm (AI-generated)
"""DS-P3.23 — qspi_datalogger 2-data-pin (SPI/Dual) payload-format coverage.

test_qspi_capture exercises the QUAD build (G_QSPI_PINS=6). This file runs the
2-data-pin build (G_QSPI_PINS=4 → C_QSPI_DATA_PINS=2), which takes the OTHER
payload-build branch (qspi_datalogger.vhd:339-341, the `else generate`):

    payloadA = dev_id & "001" & TIMESTAMP[15:0] & IO0_byte   (type A = 001)
    payloadB = dev_id & "010" & TIMESTAMP[15:0] & IO1_byte   (type B = 010)

So unlike quad (both A/B = 001), here A and B are distinguishable by TYPE TAG,
and each carries the low 16 bits of TIMESTAMP instead of a second data byte.
This whole branch had zero functional coverage.

Reuses the inline flat-pin driver + monitor from test_qspi_capture (imported as
a dependency — cocotb only registers tests from the MODULE-named file, per the
test_i2c_replay sibling-import precedent).

Run:  RR_SKIP_CLOSURE_PREFLIGHT=1 rr sim run test_qspi_spi2pin
"""

import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from sdk.cocotb_helpers import requires  # noqa: E402

# sibling import (same dir) — driver/monitor/helpers, NOT its @cocotb.test set
from test_qspi_capture import (  # noqa: E402
    QspiDriver, FrameMon, decode, _bringup, _enable, DEV_ID,
    TYPE_EVENT, DEEPSKOPION_RTL,
)

TYPE_A = 0b001        # SPI payload A (IO0 / MOSI)
TYPE_B = 0b010        # SPI payload B (IO1 / MISO)

GENERICS = {
    "G_QSPI_ID_WIDTH": 5,
    "G_QSPI_DEV_ID": 28,
    "G_QSPI_PINS": 4,            # SPI: SCLK + CSn + IO0 + IO1 (2 data pins)
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_ENABLE_INFILTER": 0,
    "G_ILA_EN": 1,
}


def _by_type(mon, t):
    return [f for f in mon.frames if f["type"] == t]


@cocotb.test()
@requires("QSPI-REQ-10")
async def test_spi2pin_payloadA_io0(dut):
    """QSPI-REQ-10 (2-pin): MODE 0 byte on IO0 → a type-A (001) payload with the
    byte in [7:0]. Drive 0xA5 → payloadA.lo == 0xA5."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    await QspiDriver(dut, cpol=0, cpha=0).transfer({0: 0xA5})
    await ClockCycles(dut.CLK, 50)
    a = _by_type(mon, TYPE_A)
    assert a, f"no type-A(001) payload; frames={[hex(f['raw']) for f in mon.frames]}"
    assert a[0]["lo"] == 0xA5, f"2-pin A/IO0 got 0x{a[0]['lo']:02x} != 0xA5 (raw=0x{a[0]['raw']:08x})"
    assert a[0]["dev_id"] == DEV_ID, f"dev_id {a[0]['dev_id']} != {DEV_ID}"
    dut._log.info(f"2PIN payloadA OK raw=0x{a[0]['raw']:08x}")


@cocotb.test()
@requires("QSPI-REQ-10")
async def test_spi2pin_payloadB_io1(dut):
    """QSPI-REQ-10 (2-pin): a byte on IO1 (MISO) → a type-B (010) payload. In
    2-pin mode A and B are distinguishable by type tag (unlike quad). Drive
    0x5A on IO1 → payloadB.lo == 0x5A with type 010."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    await QspiDriver(dut, cpol=0, cpha=0).transfer({1: 0x5A})
    await ClockCycles(dut.CLK, 50)
    b = _by_type(mon, TYPE_B)
    assert b, f"no type-B(010) payload; frames={[hex(f['raw']) for f in mon.frames]}"
    assert b[0]["lo"] == 0x5A, f"2-pin B/IO1 got 0x{b[0]['lo']:02x} != 0x5A (raw=0x{b[0]['raw']:08x})"
    dut._log.info(f"2PIN payloadB OK raw=0x{b[0]['raw']:08x}")


@cocotb.test()
@requires("QSPI-REQ-22")
async def test_spi2pin_both_lines_distinct(dut):
    """QSPI-REQ-22 (2-pin): drive IO0=0x12 and IO1=0x34 in one transfer →
    payloadA(001) carries 0x12 and payloadB(010) carries 0x34, distinguished by
    type tag."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    await QspiDriver(dut, cpol=0, cpha=0).transfer({0: 0x12, 1: 0x34})
    await ClockCycles(dut.CLK, 50)
    a = _by_type(mon, TYPE_A)
    b = _by_type(mon, TYPE_B)
    assert a and b, f"missing A or B; A={[hex(f['raw']) for f in a]} B={[hex(f['raw']) for f in b]}"
    assert a[0]["lo"] == 0x12, f"A/IO0 got 0x{a[0]['lo']:02x} != 0x12"
    assert b[0]["lo"] == 0x34, f"B/IO1 got 0x{b[0]['lo']:02x} != 0x34"
    dut._log.info(f"2PIN A+B OK A=0x{a[0]['raw']:08x} B=0x{b[0]['raw']:08x}")


@cocotb.test()
@requires("QSPI-REQ-12")
async def test_spi2pin_timestamp_embedded(dut):
    """QSPI-REQ-12 (2-pin): the 2-pin payload embeds the low 16 bits of
    TIMESTAMP in [23:8]. Hold TIMESTAMP=0xBEEF, drive 0x77 on IO0 → payloadA
    must carry byte 0x77 AND ts16==0xBEEF."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    dut.TIMESTAMP.value = 0xBEEF
    await QspiDriver(dut, cpol=0, cpha=0).transfer({0: 0x77})
    await ClockCycles(dut.CLK, 50)
    a = _by_type(mon, TYPE_A)
    assert a, f"no type-A payload; frames={[hex(f['raw']) for f in mon.frames]}"
    assert a[0]["lo"] == 0x77, f"2-pin byte got 0x{a[0]['lo']:02x} != 0x77"
    assert a[0]["ts16"] == 0xBEEF, (
        f"embedded timestamp got 0x{a[0]['ts16']:04x} != 0xBEEF (raw=0x{a[0]['raw']:08x})")
    dut._log.info(f"2PIN timestamp OK raw=0x{a[0]['raw']:08x}")


@cocotb.test()
@requires("QSPI-REQ-12")
async def test_timestamp_is_capture_not_send_time(dut):
    """QSPI-REQ-12 (DS-P4.2): the embedded TIMESTAMP must mark when the byte was
    CAPTURED, not when the packet was DELIVERED. Discriminator: hold TIMESTAMP=V
    while the byte is sampled under backpressure (RDY=0 so it can't deliver),
    then change TIMESTAMP=W before releasing RDY. A capture-time stamp carries V;
    a (buggy) send-time stamp carries the later W — and under real backpressure
    that error is unbounded, destroying the timing the datalogger exists to record.
    """
    V, W = 0x1111, 0x2222
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    dut.TIMESTAMP.value = V
    dut.QSPI_MSG_RDY.value = 0                       # backpressure from the start
    await QspiDriver(dut, cpol=0, cpha=0).transfer({0: 0x77})
    # byte is captured (bufferPayload fired) but stuck undelivered (RDY=0).
    dut.TIMESTAMP.value = W                          # change AFTER capture
    await ClockCycles(dut.CLK, 20)
    dut.QSPI_MSG_RDY.value = 1                       # now let it deliver
    await ClockCycles(dut.CLK, 50)
    a = _by_type(mon, TYPE_A)
    assert a, f"no type-A payload; frames={[hex(f['raw']) for f in mon.frames]}"
    assert a[0]["lo"] == 0x77, f"byte corrupted: 0x{a[0]['lo']:02x} != 0x77"
    assert a[0]["ts16"] == V, (
        f"timestamp is SEND-time, not CAPTURE-time: embedded 0x{a[0]['ts16']:04x}, "
        f"but the byte was captured at TIMESTAMP=0x{V:04x}; it reflects the "
        f"post-capture value 0x{W:04x} read combinationally at delivery — under "
        f"backpressure this lag is unbounded (DS-P4.2).")
    dut._log.info(f"TIMESTAMP capture-time OK raw=0x{a[0]['raw']:08x}")


@cocotb.test()
@requires("QSPI-REQ-11")
async def test_event_timestamp_is_capture_not_send_time(dut):
    """QSPI-REQ-11 (DS-T3.10): event packets must stamp the event instant, not
    delivery. Hold TIMESTAMP=V, assert CS (queues a CS-down event) under
    backpressure, change TIMESTAMP=W, then release — the event must carry V."""
    V, W = 0x0AAA, 0x0BBB
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    dut.TIMESTAMP.value = V
    dut.QSPI_MSG_RDY.value = 0
    drv = QspiDriver(dut, cpol=0, cpha=0)
    await drv._assert_cs()                          # CS-down event queued at ts=V
    await ClockCycles(dut.CLK, 20)
    dut.TIMESTAMP.value = W
    dut.QSPI_MSG_RDY.value = 1
    await ClockCycles(dut.CLK, 50)
    evs = [e for e in mon.events() if e["code"] in (0x09, 0x10, 0x11)]
    assert evs, f"no CS-down event; events={[(hex(e['code']), hex(e['ts16'])) for e in mon.events()]}"
    assert evs[0]["ts16"] == V, (
        f"event timestamp is SEND-time: 0x{evs[0]['ts16']:04x} != capture 0x{V:04x} "
        f"(reflects post-capture 0x{W:04x}) — DS-T3.10")
    dut._log.info(f"EVENT timestamp capture-time OK code=0x{evs[0]['code']:02x} ts=0x{evs[0]['ts16']:04x}")


def main():
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH

    run_simulation(
        top_level="qspi_datalogger",
        module="test_qspi_spi2pin",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{SDK_SRC_PATH}/units/infilter.vhd",
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/qspi/qspi_datalogger.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
    )


if __name__ == "__main__":
    main()
