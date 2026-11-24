# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.315: arm_uart, arm_i2c and arm_spi must put their logger on the stimulus line before they enable it.

Silicon (DE25 C3 F2 and C4 A2-pos, 2026-11-15): the I2C positive leg carried a leading stop_error 1.04 ms after the
enable, at the switch from the header input to the internal stimulus. The production armers
(``stimulus_armer.arm_uart`` / ``arm_i2c`` / ``arm_spi``) enable the logger while EMIO_MUX_SEL still selects the
header pins, then switch the mux, the order DS-P2.313 fixed for arm_edge. Hypothesis, tested here: the logger reads
the header level, and the step to the stimulus line's idle level at the switch is decoded as bus activity.

Bench: hw/sim/tb_default_mix_core.vhd (the real deepskopion_core, default mix, G_STIM_DRIVES_EMIO true), as
DS-P2.313. The register writes are recorded from the production armer and replayed over AXI-Lite; its logger reset
sleep is replayed as 20 us and its inter-byte sleeps as 100 us (longer than one UART byte at UART_BAUD_DIV 434 on a
100 MHz clk_sys, 43.4 us). Each case starts from reset with the logger disabled and holds that protocol's header
inputs at one level for the whole case: low (0) and high (1, the pull-up idle).

  UART#12  TX jb(0)          I2C#4  SCL ja(0) SDA ja(1)          SPI#8  CS ja(4) MOSI ja(5) MISO ja(6) SCLK ja(7)

Verdict, per case (stated before the first run, corrected after it, see below):
  stimulus presence first: the generator output toggles after the arm (else VOID);
  UART: the logger's records after the arm are exactly enabled, one config record, then the 10 UART_PAYLOAD bytes.
        The config record is the "post-calibration configuration report" (uart_datalogger_protocol.yml): the UART
        logger always auto-discovers the baud, so it must report [23:8] = UART_BAUD_DIV 434 / the sampler prescaler 4
        (uart_datalogger.vhd C_PRESCALER, f_resolve_prescaler: 4 at 100 MHz) = 108.5, i.e. 108 +-1 ticks, and
        word_length [3:0] = 8, stop_bits [6] = 0, no parity;
  I2C:  exactly enabled, start, A0, AB, CD, 3E, stop (address byte 0x50 << 1, then I2C_DATA);
  SPI:  exactly start (0x03, logger enabled), cs_down_new_pol_l (0x11, the first CS fall reports CPOL 0), then per
        byte MOSI, MISO (the on-chip responder's payload A5 5A C3 3C, routed to the logger by DS-P2.343; hard-coded
        here, not imported), cs_up, with cs_down (0x09) opening bytes 2-4
        (qspi_datalogger_protocol.yml events);
  every protocol: no record with severity WARN or ERROR (msg_type[15:12] = 1 or 2).
  Prediction on today's armer order: the header-low cases RED (an extra or error record at the mux switch), the
  header-high cases GREEN (the header level equals the stimulus idle level).

Correction after the first run (job 5937d61e6fad, today's armer order, test not yet committed): the first version
expected [enabled] + bytes for UART and judged SPI by a cs_down/cs_up count. Both were wrong against the protocol
contracts: the UART config record is always sent after calibration (present at both header levels), and the SPI
logger's documented start and cs_down_new_pol_l events open every capture (identical at both header levels). The
expected lists above are the contract sequences. The SPI header-low and header-high records were identical in that
run, so a third SPI state was added: CS low with SCLK high (an idle mid-transfer bus); no prediction for it.
Second correction (job 466eacf93594): the config check expected 434 +-2 clk_sys cycles, as the yml field text says
("clock cycles per bit"); the logger reports sampler ticks (prescaler 4), 108 at header high. The expected value is
now 108 +-1 ticks.

Run (from the deepskopion dir): rr sim run test_arm_order_uart_i2c_spi_ds_p2_315
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Edge, RisingEdge, Timer
from cocotb.utils import get_sim_time
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

_HERE = Path(__file__).resolve()
_COCOTB = _HERE.parents[4]                          # hw/sim/cocotb
_ROOT = _HERE.parents[7]                            # deepskopion
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_COCOTB / "tests" / "stimulus" / "core_seam"))
from _core_seam_common import run_core_seam  # noqa: E402

TOP = "tb_default_mix_core"
CLK_PERIOD_NS = 10

# Header idle levels of the DS-P1.104 default-mix test; each case overrides its own protocol's pins.
IDLE = {
    "JA_0": 1, "JA_1": 1, "JA_2": 1, "JA_3": 1,
    "JA_4": 1, "JA_5": 0, "JA_6": 0, "JA_7": 0,
    "JB_0": 1, "JB_1": 1, "JB_2": 0, "JB_3": 0, "JB_4": 0,
    "JB_5": 1, "JB_6": 0, "JB_7": 0,
}
HEADER_PINS = {"uart": ("JB_0",), "i2c": ("JA_0", "JA_1"), "spi": ("JA_4", "JA_5", "JA_6", "JA_7")}
SPI_MID = {"JA_4": 0, "JA_5": 0, "JA_6": 0, "JA_7": 1}   # CS asserted, SCLK high: an idle mid-transfer bus
LOGGER_ID = {"uart": 12, "i2c": 4, "spi": 8}
STIM_OUT = {"uart": ("stim_uart_tx_w",), "i2c": ("stim_i2c_scl_o_w", "stim_i2c_scl_t_w"),
            "spi": ("stim_spi_sclk_w",)}

# Hard-coded records (msg_type[11:0], payload[7:0]); msg_type = severity[15:12] | domain[11:8] | event[7:0].
UART_STATUS, UART_BYTE, UART_CONFIG = 0x104, 0x102, 0x101
UART_BAUD_TICKS, UART_BAUD_TOL, UART_CONFIG_LOW = 108, 1, 0x08   # 434 cycles / prescaler 4
UART_ENABLED = 0x05
I2C_EV, I2C_BYTE = 0x204, 0x200
I2C_ENABLED, I2C_START, I2C_STOP = 0x03, 0x09, 0x0A
SPI_MOSI, SPI_MISO, SPI_EV = 0x301, 0x302, 0x304
SPI_CS_DOWN, SPI_CS_UP = 0x09, 0x0A
EXPECTED = {
    "uart": [(UART_STATUS, UART_ENABLED), (UART_CONFIG, UART_CONFIG_LOW)] + [(UART_BYTE, b) for b in
                                             (0xA3, 0x5C, 0x71, 0x0E, 0xFF, 0x42, 0x00, 0xFF, 0x00, 0xFF)],
    "i2c": [(I2C_EV, I2C_ENABLED), (I2C_EV, I2C_START), (I2C_BYTE, 0xA0), (I2C_BYTE, 0xAB), (I2C_BYTE, 0xCD),
            (I2C_BYTE, 0x3E), (I2C_EV, I2C_STOP)],
}
SPI_MOSI_BYTES = [0xDE, 0xAD, 0xBE, 0xEF]
SPI_MISO_BYTES = [0xA5, 0x5A, 0xC3, 0x3C]   # the responder's payload, one byte per transfer (DS-P2.386)
SPI_START, SPI_CS_DOWN_NEW_POL_L = 0x03, 0x11
EXPECTED["spi"] = [(SPI_EV, SPI_START), (SPI_EV, SPI_CS_DOWN_NEW_POL_L)]
for _i, (_b, _m) in enumerate(zip(SPI_MOSI_BYTES, SPI_MISO_BYTES)):
    EXPECTED["spi"] += ([] if _i == 0 else [(SPI_EV, SPI_CS_DOWN)]) + [(SPI_MOSI, _b), (SPI_MISO, _m), (SPI_EV, SPI_CS_UP)]

SLEEP_RESET_NS, SLEEP_BYTE_NS = 20_000, 100_000


def _armer_ops(protocol: str) -> list:
    """The register writes the production armer makes, in order, with ("sleep", ns) where it sleeps."""
    from sw.stream import stimulus_armer

    ops: list = []

    class _Recorder:
        def write_u32(self, off, val):
            ops.append((int(off), int(val)))

    def _sleep(s):
        ops.append(("sleep", SLEEP_RESET_NS if s == stimulus_armer._DATALOGGER_RESET_S else SLEEP_BYTE_NS))

    real_sleep = stimulus_armer.time.sleep
    stimulus_armer.time.sleep = _sleep
    try:
        stimulus_armer.ARMERS[protocol](_Recorder())
    finally:
        stimulus_armer.time.sleep = real_sleep
    return ops


async def _case(dut, protocol: str, level) -> None:
    levels = dict(IDLE)
    if level == "mid":
        levels.update(SPI_MID)
    else:
        for pin in HEADER_PINS[protocol]:
            levels[pin] = level
    for name, lv in levels.items():
        getattr(dut, name).value = lv
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, unit="ns").start())
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 16)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 16)
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)

    records: list = []          # (sim ns, datalogger id, msg_type, payload)

    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.PROBE_DOUT_VALID.value) == 1 and int(dut.PROBE_DOUT_REQ.value) == 1:
                records.append((get_sim_time("ns"), int(dut.PROBE_DOUT_DATALOGGER_ID.value),
                                int(dut.PROBE_DOUT_MSG_TYPE.value), int(dut.PROBE_DOUT_PAYLOAD.value)))

    toggles: list = []

    async def watch(name):
        sig = getattr(dut.u_core, name)
        while True:
            await Edge(sig)
            toggles.append((get_sim_time("ns"), name, int(sig.value)))

    cocotb.start_soon(collect())
    for name in STIM_OUT[protocol]:
        cocotb.start_soon(watch(name))

    t_arm = get_sim_time("ns")
    ops = _armer_ops(protocol)
    dut._log.info(f"{protocol} header={level}: armer writes "
                  f"{[op if op[0] == 'sleep' else (hex(op[0]), hex(op[1])) for op in ops]}")
    for op in ops:
        if op[0] == "sleep":
            await Timer(op[1], unit="ns")
        else:
            await axi.write(op[0], op[1].to_bytes(4, "little"))
    await Timer(1, unit="ms")

    mine = [(t, mt, p) for t, i, mt, p in records if i == LOGGER_ID[protocol] and t >= t_arm]
    got = [(mt & 0xFFF, p & 0xFF) for _, mt, p in mine]
    severe = [(t, hex(mt), hex(p & 0xFF)) for t, mt, p in mine if (mt >> 12) in (1, 2)]
    dut._log.info(f"{protocol} header={level}: stimulus toggles {len(toggles)}, first at "
                  f"{toggles[0][0] if toggles else None} ns")
    dut._log.info(f"{protocol} header={level}: records {[(t, hex(mt), hex(p)) for t, mt, p in mine]}")

    assert toggles, f"VOID: the {protocol} stimulus generator never toggled after the arm"
    assert not severe, f"{protocol} header={level}: WARN/ERROR records {severe}"
    want = EXPECTED[protocol]
    assert got == want, (f"{protocol} header={level}: records {[(hex(a), hex(b)) for a, b in got]}, "
                         f"expected {[(hex(a), hex(b)) for a, b in want]}")
    if protocol == "uart":
        baud = [(p >> 8) & 0xFFFF for _, mt, p in mine if mt & 0xFFF == UART_CONFIG]
        assert len(baud) == 1 and abs(baud[0] - UART_BAUD_TICKS) <= UART_BAUD_TOL, (
            f"uart header={level}: config baud [23:8] {baud} ticks, expected {UART_BAUD_TICKS} +-{UART_BAUD_TOL}")


@cocotb.test()
async def test_arm_uart_header_low(dut):
    await _case(dut, "uart", 0)


@cocotb.test()
async def test_arm_uart_header_high(dut):
    await _case(dut, "uart", 1)


@cocotb.test()
async def test_arm_i2c_header_low(dut):
    await _case(dut, "i2c", 0)


@cocotb.test()
async def test_arm_i2c_header_high(dut):
    await _case(dut, "i2c", 1)


@cocotb.test()
async def test_arm_spi_header_low(dut):
    await _case(dut, "spi", 0)


@cocotb.test()
async def test_arm_spi_header_high(dut):
    await _case(dut, "spi", 1)


@cocotb.test()
async def test_arm_spi_header_mid_transfer(dut):
    await _case(dut, "spi", "mid")


if __name__ == "__main__":
    run_core_seam("test_arm_order_uart_i2c_spi_ds_p2_315", top_level=TOP)
