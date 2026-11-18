# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.104 C4: the default data logger mix, end to end through the real deepskopion_core.

Bench: hw/sim/tb_default_mix_core.vhd. The test drives the core's header inputs
(ja/jb, the ports every product top wires to its own header), arms each data
logger only through the core's AXI-Lite regbank port, and reads the records at
the generated gather's output inside the core.

The default mix (owner ruling 2026-11-13, DS-D.53), with the header pin each
input sits on:

  I2C#4  SCL ja(0) SDA ja(1)      I2C#5  SCL ja(2) SDA ja(3)
  SPI#8  CS ja(4) MOSI ja(5) MISO ja(6) SCLK ja(7)
  UART#12 TX jb(0)                UART#13 TX jb(1)
  EDGE#1 jb(2)  EDGE#2 jb(3)  EDGE#3 jb(4)
  CAN#20 RX jb(5)

Every expected record below is a hard-coded value. Each instance of a protocol
gets a different stimulus (EDGE 1/2/3 pulse counts 1/2/3, distinct I2C
addresses and bytes, distinct UART bytes), so a swapped pin or id shows up as a
wrong value, not only as a missing one. Each input has its own check; the test
reports every failing check together.

Records: msg_type = severity[15:12] | domain[11:8] | event[7:0]; payload[7:0] is
the data byte or event code (zs_msg_pkg, the per-protocol logger RTL).

Not covered: the product tops. No simulation elaborates deepskopion_top,
deepskopion_de25_top or deepskopion_pfsoc_top around their vendor blocks
(DS-P3.267). The header-port-to-ja/jb step is held line-exact by
tools/tests/test_default_mix_ds_p1_104.py (TOP_WIRING).

Run (from the deepskopion dir): rr sim run test_default_mix_core_ds_p1_104
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

_HERE = Path(__file__).resolve()
_COCOTB = _HERE.parents[4]                          # hw/sim/cocotb
sys.path.insert(0, str(_COCOTB))                                    # can_bfm
sys.path.insert(0, str(_COCOTB / "tests" / "stimulus" / "core_seam"))  # run_core_seam
from can_bfm import CanBusBFM  # noqa: E402
from _core_seam_common import run_core_seam  # noqa: E402

TOP = "tb_default_mix_core"
CLK_PERIOD_NS = 10                 # clk_sys 100 MHz, as on the Zybo
SLOT_DATALOGGER_CONFIG = 0x20

# ── Arming words: (id << 11) | per-logger config, written after an (id << 11)
# priming write so the CONFIG value changes (config_control latches on change).
ARM = {
    1: 0x0801,    # EDGE#1, both edges
    2: 0x1001,    # EDGE#2
    3: 0x1801,    # EDGE#3
    4: 0x2001,    # I2C#4, enable
    5: 0x2801,    # I2C#5
    8: 0x4001,    # SPI#8, mode 0, 4-wire
    12: 0x600D,   # UART#12, explicit 8N1
    13: 0x680D,   # UART#13, explicit 8N1
    20: 0xA009,   # CAN#20, prescaler 4 (100 MHz / (16*4) = 1.5625 Mbit/s)
}

# ── Expected records per input (hard-coded) ──────────────────────────────────
EDGE_RISE, EDGE_FALL = 0x0600, 0x0601
EDGE_PULSES = {1: 1, 2: 2, 3: 3}
I2C_EV, I2C_ACK = 0x0204, 0x0200
I2C_START, I2C_STOP = 0x09, 0x0A
SPI_MOSI, SPI_MISO = 0x0301, 0x0302
UART_BYTE = 0x0102
CAN_ID, CAN_DATA, CAN_CTRL = 0x0A01, 0x0A02, 0x0A03

EXPECTED = {
    "EDGE#1 jb(2)": [EDGE_RISE, EDGE_FALL],
    "EDGE#2 jb(3)": [EDGE_RISE, EDGE_FALL] * 2,
    "EDGE#3 jb(4)": [EDGE_RISE, EDGE_FALL] * 3,
    "I2C#4 ja(0)/ja(1)": [(I2C_EV, I2C_START), (I2C_ACK, 0xA0), (I2C_ACK, 0xAB), (I2C_EV, I2C_STOP)],
    "I2C#5 ja(2)/ja(3)": [(I2C_EV, I2C_START), (I2C_ACK, 0xA2), (I2C_ACK, 0xCD), (I2C_EV, I2C_STOP)],
    "SPI#8 ja(4..7)": [(SPI_MOSI, 0xA1), (SPI_MISO, 0x3C)],
    "UART#12 jb(0)": [(UART_BYTE, 0x5A)],
    "UART#13 jb(1)": [(UART_BYTE, 0xC3)],
    "CAN#20 jb(5)": [(CAN_CTRL, 0x02), (CAN_ID, 0x01), (CAN_ID, 0x23), (CAN_DATA, 0xAB), (CAN_DATA, 0xCD)],
}
DEFAULT_IDS = {1, 2, 3, 4, 5, 8, 12, 13, 20}

# Header-pin idle levels: I2C high, SPI CS high and SCLK low, UART high,
# EDGE low, CAN recessive high.
IDLE = {
    "JA_0": 1, "JA_1": 1, "JA_2": 1, "JA_3": 1,
    "JA_4": 1, "JA_5": 0, "JA_6": 0, "JA_7": 0,
    "JB_0": 1, "JB_1": 1, "JB_2": 0, "JB_3": 0, "JB_4": 0,
    "JB_5": 1, "JB_6": 0, "JB_7": 0,
}


def _w(v: int) -> bytes:
    return (v & 0xFFFFFFFF).to_bytes(4, "little")


async def _edge_pulses(pin, n: int) -> None:
    for _ in range(n):
        pin.value = 1
        await Timer(500, unit="ns")
        pin.value = 0
        await Timer(500, unit="ns")


async def _i2c_write(scl, sda, addr7: int, data: list[int], half_ns: int = 1250) -> None:
    """Bit-bang a whole I2C write (master and an ACKing slave on one bus)."""
    sda.value = 0                      # START: SDA falls while SCL is high
    await Timer(half_ns, unit="ns")
    scl.value = 0
    await Timer(half_ns, unit="ns")
    for byte in [addr7 << 1] + data:
        for i in range(7, -1, -1):
            sda.value = (byte >> i) & 1
            await Timer(half_ns, unit="ns")
            scl.value = 1
            await Timer(half_ns, unit="ns")
            scl.value = 0
        sda.value = 0                  # ACK on the 9th clock
        await Timer(half_ns, unit="ns")
        scl.value = 1
        await Timer(half_ns, unit="ns")
        scl.value = 0
    sda.value = 0                      # STOP: SDA rises while SCL is high
    await Timer(half_ns, unit="ns")
    scl.value = 1
    await Timer(half_ns, unit="ns")
    sda.value = 1
    await Timer(4 * half_ns, unit="ns")


async def _spi_byte(cs, mosi, miso, sclk, mosi_byte: int, miso_byte: int,
                    half_ns: int = 500) -> None:
    """One mode-0 transfer, MSB first, both data lines driven by the test."""
    cs.value = 0
    await Timer(2 * half_ns, unit="ns")
    for i in range(7, -1, -1):
        mosi.value = (mosi_byte >> i) & 1
        miso.value = (miso_byte >> i) & 1
        await Timer(half_ns, unit="ns")
        sclk.value = 1
        await Timer(half_ns, unit="ns")
        sclk.value = 0
    await Timer(2 * half_ns, unit="ns")
    cs.value = 1
    await Timer(4 * half_ns, unit="ns")


async def _uart_byte(tx, byte: int, bit_ns: int = 8680) -> None:
    """One 8N1 frame at 115200 baud, LSB first."""
    for level in [0] + [(byte >> i) & 1 for i in range(8)] + [1]:
        tx.value = level
        await Timer(bit_ns, unit="ns")


def _collect(dut, got: list) -> None:
    async def run():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.PROBE_DOUT_VALID.value) == 1 and int(dut.PROBE_DOUT_REQ.value) == 1:
                got.append((int(dut.PROBE_DOUT_DATALOGGER_ID.value),
                            int(dut.PROBE_DOUT_MSG_TYPE.value),
                            int(dut.PROBE_DOUT_PAYLOAD.value)))
    cocotb.start_soon(run())


def _observed(got: list) -> dict:
    """Per check, the records it judges, in arrival order."""
    def of(dev_id, types, with_byte=True):
        return [((t, p & 0xFF) if with_byte else t)
                for i, t, p in got if i == dev_id and t in types]
    return {
        "EDGE#1 jb(2)": of(1, (EDGE_RISE, EDGE_FALL), with_byte=False),
        "EDGE#2 jb(3)": of(2, (EDGE_RISE, EDGE_FALL), with_byte=False),
        "EDGE#3 jb(4)": of(3, (EDGE_RISE, EDGE_FALL), with_byte=False),
        "I2C#4 ja(0)/ja(1)": [r for r in of(4, (I2C_EV, I2C_ACK, 0x0201))
                              if r[0] != I2C_EV or r[1] in (I2C_START, I2C_STOP)],
        "I2C#5 ja(2)/ja(3)": [r for r in of(5, (I2C_EV, I2C_ACK, 0x0201))
                              if r[0] != I2C_EV or r[1] in (I2C_START, I2C_STOP)],
        "SPI#8 ja(4..7)": of(8, (SPI_MOSI, SPI_MISO)),
        "UART#12 jb(0)": of(12, (UART_BYTE,)),
        "UART#13 jb(1)": of(13, (UART_BYTE,)),
        "CAN#20 jb(5)": of(20, (CAN_ID, CAN_DATA, CAN_CTRL)),
    }


@cocotb.test()
async def test_default_mix_reaches_its_records(dut):
    for name, level in IDLE.items():
        getattr(dut, name).value = level
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 16)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 16)
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)

    got: list = []
    _collect(dut, got)

    for dev_id, word in ARM.items():
        await axi.write(SLOT_DATALOGGER_CONFIG, _w(dev_id << 11))
        await ClockCycles(dut.CLK, 20)
        await axi.write(SLOT_DATALOGGER_CONFIG, _w(word))
        await ClockCycles(dut.CLK, 20)
    await Timer(5, unit="us")

    await _edge_pulses(dut.JB_2, EDGE_PULSES[1])
    await _edge_pulses(dut.JB_3, EDGE_PULSES[2])
    await _edge_pulses(dut.JB_4, EDGE_PULSES[3])
    await Timer(5, unit="us")
    await _i2c_write(dut.JA_0, dut.JA_1, 0x50, [0xAB])
    await _i2c_write(dut.JA_2, dut.JA_3, 0x51, [0xCD])
    await _spi_byte(dut.JA_4, dut.JA_5, dut.JA_6, dut.JA_7, 0xA1, 0x3C)
    await _uart_byte(dut.JB_0, 0x5A)
    await Timer(500, unit="us")
    await _uart_byte(dut.JB_1, 0xC3)
    await Timer(500, unit="us")
    bfm = CanBusBFM(dut.JB_5, dut.CLK, prescaler=4)
    await bfm.idle(bits=8)
    await bfm.send(can_id=0x123, data=[0xAB, 0xCD], ack=0)
    await bfm.idle(bits=10)
    await Timer(50, unit="us")

    seen = _observed(got)
    failures = [f"{name}: got {seen[name]}, expected {want}"
                for name, want in EXPECTED.items() if seen[name] != want]
    alien = sorted({i for i, _, _ in got} - DEFAULT_IDS)
    if alien:
        failures.append(f"records from ids outside the default mix: {alien}")
    dut._log.info(f"{len(got)} records; DDR bursts {int(dut.DDR_BURSTS.value)}")
    for name in EXPECTED:
        dut._log.info(f"{name}: {seen[name]}")
    assert not failures, "default mix checks failed:\n  " + "\n  ".join(failures)


if __name__ == "__main__":
    run_core_seam("test_default_mix_core_ds_p1_104", top_level=TOP)
