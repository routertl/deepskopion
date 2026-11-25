# rr unknown-llm (AI-generated)
"""Contract-first NON-EDGE tag coverage for datalogger_gather (SGA-REQ-11/12/13).

The pre-existing suite (test_datalogger_gather.py) drives EDGE only, so the
datalogger_id/domain tag path for I2C/UART/SPI had ZERO coverage. These tests
drive each NON-EDGE datalogger with real pin traffic and assert the gather output
tag — and they pass GREEN: every datalogger embeds its device_id at its final
MSG_OUT stage (i2c_datalogger.vhd:421, uart_frame_gen.vhd:423, qspi_datalogger.vhd
:335, edge_datalogger.vhd:209), so the gather's <proto>Messages(i)[31:27]
extraction is correct for all four protocols.

  SGA-REQ-11 (I2C)  GREEN — I2C frames tag DOUT_DATALOGGER_ID==4, domain==i2c
  SGA-REQ-12 (UART) PARKED @undetermined → DS-T3.4 (needs a calibrating UART BFM;
                    bit-bang never reaches the datalogger's CALIBRATION_DONE)
  SGA-REQ-13 (SPI)  GREEN — id==8, domain==spi

HISTORY: DS-T2.19 alleged a tagging bug (i2c/uart not embedding device_id in
[31:27]) — this sim FALSIFIED it (I2C->id=4, SPI->id=8) and it was closed
not-a-bug. The mis-read came from an intermediate message stage, not the final
MSG_OUT prepend; the on-hardware "id=8" was a cache + commit-lag stale-read
artifact (deepskopion Haz.59), not a tag defect. These are the GREEN
regression lock for the correct behaviour.
"""

import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc  # noqa: F401
# SDK cocotb dir on the path so the proven calibrating UART master is importable
# (same insert the sibling test_datalogger_gather.py uses).
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
try:
    from engine.simulation import run_simulation
except ImportError:
    print("Error: import engine in the rr sim environment.")

from rr_cocotb_tb.drivers.uart_master import UartSource
from sdk.cocotb_helpers import requires  # noqa: F401  (undetermined dropped — SGA-REQ-12 now runnable)

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT / "hw" / "sim" / "cocotb"))   # ds_sim_sources
from ds_sim_sources import scoped_libraries   # noqa: E402

GENERICS = {"G_SYS_CLK": 100_000_000}
PERIOD_NS = 10

# CAPTURE_PINS bit indices (datalogger_config_pkg).
I2C0_SCL, I2C0_SDA = 12, 13
SPI0_CS, SPI0_MOSI, SPI0_MISO, SPI0_SCLK = 20, 21, 22, 23
UART0_TX = 36

# Device IDs (datalogger_config_pkg) + ZS domains (zs_msg_pkg).
I2C0_ID, SPI0_ID, UART0_ID = 4, 8, 12
DOM_UART, DOM_I2C, DOM_SPI = 1, 2, 3

IDLE = (
    (1 << 12) | (1 << 13) | (1 << 14) | (1 << 15)
    | (1 << 20) | (1 << 21) | (1 << 22) | (1 << 23)
    | (1 << 36) | (1 << 37)
)


def _set(pins, bit, val):
    return (pins | (1 << bit)) if val else (pins & ~(1 << bit))


async def reset_dut(dut):
    dut.RST.value = 1
    dut.DOUT_REQ.value = 0
    dut.CAPTURE_PINS.value = IDLE
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 32)


async def enable_datalogger(dut, dev_id):
    """config_control latches the slice whose CONFIG[15:11]==dev_id; enable=bit0."""
    dut.CONFIG.value = (dev_id << 11) | 0b01
    await ClockCycles(dut.CLK, 8)
    dut.CONFIG.value = 0
    await ClockCycles(dut.CLK, 8)


async def capture_bursts(dut, cycles):
    """Drain DOUT handshakes for `cycles`; return [(datalogger_id, msg_type)]."""
    out = []
    dut.DOUT_REQ.value = 1
    for _ in range(cycles):
        await RisingEdge(dut.CLK)
        if int(dut.DOUT_VALID.value) == 1:
            out.append((int(dut.DOUT_DATALOGGER_ID.value),
                        int(dut.DOUT_MSG_TYPE.value)))
    return out


# ── I2C bit-bang BFM (SDA=13, SCL=12). Generous timing to clear debounce. ──
P = 30   # cycles per half-phase


async def i2c_drive_byte(dut, byte):
    pins = IDLE
    # START: SDA high→low while SCL high
    pins = _set(pins, I2C0_SDA, 1); pins = _set(pins, I2C0_SCL, 1)
    dut.CAPTURE_PINS.value = pins; await ClockCycles(dut.CLK, P)
    pins = _set(pins, I2C0_SDA, 0); dut.CAPTURE_PINS.value = pins
    await ClockCycles(dut.CLK, P)
    # 8 data bits, MSB first, sampled on SCL rising
    for i in range(7, -1, -1):
        pins = _set(pins, I2C0_SCL, 0); dut.CAPTURE_PINS.value = pins
        await ClockCycles(dut.CLK, P)
        pins = _set(pins, I2C0_SDA, (byte >> i) & 1); dut.CAPTURE_PINS.value = pins
        await ClockCycles(dut.CLK, P // 2)
        pins = _set(pins, I2C0_SCL, 1); dut.CAPTURE_PINS.value = pins
        await ClockCycles(dut.CLK, P)
    # ACK clock
    pins = _set(pins, I2C0_SCL, 0); dut.CAPTURE_PINS.value = pins
    await ClockCycles(dut.CLK, P)
    pins = _set(pins, I2C0_SCL, 1); dut.CAPTURE_PINS.value = pins
    await ClockCycles(dut.CLK, P)
    # STOP: SDA low→high while SCL high
    pins = _set(pins, I2C0_SCL, 0); pins = _set(pins, I2C0_SDA, 0)
    dut.CAPTURE_PINS.value = pins; await ClockCycles(dut.CLK, P)
    pins = _set(pins, I2C0_SCL, 1); dut.CAPTURE_PINS.value = pins
    await ClockCycles(dut.CLK, P)
    pins = _set(pins, I2C0_SDA, 1); dut.CAPTURE_PINS.value = pins
    await ClockCycles(dut.CLK, P)


@cocotb.test()
@requires("SGA-REQ-11")
async def test_i2c_tag_routing(dut):
    """SGA-REQ-11: I2C frames must tag DOUT_DATALOGGER_ID==4 / domain==i2c.

    NOTE (DS-P3.55): this is GREEN, not RED. DS-T2.19 predicted the I2C path
    would mis-tag because it assumed `i2c message[31:27]` carries the message
    TYPE. RTL inspection falsifies that: i2c_datalogger assembles the word as
    `C_DEVICE_ID & payload` (i2c_datalogger.vhd:421-422), so [31:27] IS the
    device_id — the gather's `i2cMessages(i)(31 downto 27)` extracts it
    correctly. The sim confirms datalogger_id==4 / domain==i2c."""
    cocotb.start_soon(Clock(dut.CLK, PERIOD_NS, "ns").start())
    await reset_dut(dut)
    await enable_datalogger(dut, I2C0_ID)
    drv = cocotb.start_soon(i2c_drive_byte(dut, 0x50))
    events = await capture_bursts(dut, 8 * P * 6)
    await drv
    assert events, ("SGA-REQ-11: no DOUT events from the I2C datalogger — "
                    "BFM drove no detectable transaction (tune timing).")
    bad = [(sid, mt) for sid, mt in events if sid != I2C0_ID]
    doms = {(mt >> 8) & 0xF for _, mt in events}
    cocotb.log.info(f"SGA-REQ-11: {len(events)} I2C events; "
                    f"datalogger_ids={sorted({s for s,_ in events})} domains={sorted(doms)}")
    assert not bad, (
        f"SGA-REQ-11 violation (DS-T2.19): {len(bad)}/{len(events)} I2C events "
        f"tagged datalogger_id != {I2C0_ID}. Got ids={sorted({s for s,_ in events})}. "
        f"The gather derives datalogger_id from i2cMessages(i)[31:27] (= C_I2C_TYPE_*), "
        f"not the device_id constant C_I2C_IDS(i)=4.")
    assert doms == {DOM_I2C}, (
        f"SGA-REQ-11: I2C domain nibble != i2c({DOM_I2C}); got {sorted(doms)}.")


# ── UART calibrating BFM (TX=36) — the PROVEN rr_cocotb_tb UartSource ──
# The UART datalogger auto-calibrates (baud-detect → stop-bit-detect →
# CALIBRATION_DONE) over ~70 bytes before it emits anything (yml min_bytes:70),
# so a 5-byte bit-bang never produced a single gather burst (DS-T3.4). We reuse
# the same calibrating master the uart unit tests use, driven onto bit 36 of the
# CAPTURE_PINS bus via a thin shadow-word adapter (the other dataloggers' idle pins
# are preserved). 100 MHz / 460800 ≈ 217 cyc/bit — the proven unit-test rate.
BAUD = 460_800
CAL_BYTES = 150   # ~70 to lock + margin; matches uart_datalogger_test_utils


@cocotb.test()
@requires("SGA-REQ-12")
async def test_uart_tag_routing(dut):
    """SGA-REQ-12: UART frames must tag DOUT_DATALOGGER_ID==12 / domain==uart.

    NOTE (DS-P3.55): GREEN, not RED. DS-T2.19 predicted the UART path would
    mis-tag, assuming `uart message[31:27]` carries the TIMESTAMP and no
    device_id. RTL inspection falsifies that: uart_frame_gen.vhd:454-456
    assembles every UART word as `C_UART_DEV_ID_UNS & <type> & timestamp &
    payload`, so [31:27] IS the device_id. The gather extracts it correctly.

    DS-T3.4: previously @undetermined because a 5-byte bit-bang never reached the
    datalogger's CALIBRATION_DONE. Now driven by the calibrating UartSource over the
    full 150-byte stream — a real, runnable assertion."""
    cocotb.start_soon(Clock(dut.CLK, PERIOD_NS, "ns").start())
    await reset_dut(dut)
    # CONFIG-enable UART0 (id=12) with AutoSync (Enable=1 + AutoSync=1 = 0x03);
    # bit0-enable alone doesn't arm the calibration FSM (matches the unit tests).
    dut.CONFIG.value = ((UART0_ID & 0x1F) << 11) | 0x03
    await ClockCycles(dut.CLK, 64)
    dut.CONFIG.value = 0
    await ClockCycles(dut.CLK, 8)

    # The real bit handle, so the BFM strict protocol monitor can watch its edges.
    src = UartSource(dut.CAPTURE_PINS[UART0_TX], baud=BAUD, bits=8, stop_bits=1, parity="none")
    random.seed(0xC0FFEE)   # deterministic stream (DS-T3.14 lesson)
    data = [random.randint(0x00, 0xFF) for _ in range(CAL_BYTES)]

    # Collect DOUT handshakes in the background across the whole stream.
    events = []

    async def _collect():
        dut.DOUT_REQ.value = 1
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.DOUT_VALID.value) == 1:
                events.append((int(dut.DOUT_DATALOGGER_ID.value),
                               int(dut.DOUT_MSG_TYPE.value)))

    col = cocotb.start_soon(_collect())
    await src.write_bytes(data)
    await ClockCycles(dut.CLK, 4000)   # drain trailing frames
    col.kill()

    assert events, (
        "SGA-REQ-12: no DOUT events from the UART datalogger even with the "
        "calibrating UartSource over 150 bytes — CALIBRATION_DONE never reached. "
        "That would be a real DUT/BFM defect, not a tune-timing nit.")
    bad = [(sid, mt) for sid, mt in events if sid != UART0_ID]
    doms = {(mt >> 8) & 0xF for _, mt in events}
    cocotb.log.info(f"SGA-REQ-12: {len(events)} UART events; "
                    f"datalogger_ids={sorted({s for s, _ in events})} domains={sorted(doms)}")
    assert not bad, (
        f"SGA-REQ-12 violation: {len(bad)}/{len(events)} UART events tagged "
        f"datalogger_id != {UART0_ID}. Got ids={sorted({s for s, _ in events})}.")
    assert doms == {DOM_UART}, (
        f"SGA-REQ-12: UART domain nibble != uart({DOM_UART}); got {sorted(doms)}.")


# ── SPI bit-bang BFM (CS=20, SCLK=23, MOSI=21) — GREEN regression lock ──
@cocotb.test()
@requires("SGA-REQ-13")
async def test_spi_tag_routing(dut):
    """SGA-REQ-13: SPI/qspi tags id==8 / domain==spi. GREEN — locks the
    working device-id-at-[31:27] tagging path."""
    cocotb.start_soon(Clock(dut.CLK, PERIOD_NS, "ns").start())
    await reset_dut(dut)
    await enable_datalogger(dut, SPI0_ID)

    async def spi_drive(byte):
        pins = IDLE
        pins = _set(pins, SPI0_CS, 0); dut.CAPTURE_PINS.value = pins
        await ClockCycles(dut.CLK, P)
        for i in range(7, -1, -1):
            pins = _set(pins, SPI0_SCLK, 0)
            pins = _set(pins, SPI0_MOSI, (byte >> i) & 1)
            dut.CAPTURE_PINS.value = pins; await ClockCycles(dut.CLK, P)
            pins = _set(pins, SPI0_SCLK, 1); dut.CAPTURE_PINS.value = pins
            await ClockCycles(dut.CLK, P)
        pins = _set(pins, SPI0_SCLK, 0); dut.CAPTURE_PINS.value = pins
        await ClockCycles(dut.CLK, P)
        pins = _set(pins, SPI0_CS, 1); dut.CAPTURE_PINS.value = pins
        await ClockCycles(dut.CLK, P)

    drv = cocotb.start_soon(spi_drive(0xA5))
    events = await capture_bursts(dut, 8 * P * 6)
    await drv
    cocotb.log.info(f"SGA-REQ-13: {len(events)} SPI events; "
                    f"ids={sorted({s for s,_ in events})}")
    assert events, "SGA-REQ-13: no SPI events (tune BFM timing)."
    bad = [s for s, _ in events if s != SPI0_ID]
    assert not bad, f"SGA-REQ-13 REGRESSION: SPI events tagged != {SPI0_ID}: {bad}"


def main():
    # DS-T3.22: resolve the compile set with the SAME autodiscovery the EDGE
    # suite uses (ds_sim_sources.scoped_libraries) instead of a hand-curated
    # list. The old hand list emitted ABSOLUTE paths; run_simulation's
    # locked-package injector folds in `infilter`/`infilter_array` with
    # PROJECT-RELATIVE paths, so its `if _f not in _existing` dedup missed the
    # already-listed infilter — analysing it twice into `tich` and desyncing
    # qspi_datalogger's checksum ("depends on an obsolete version of TICH.INFILTER").
    # scoped_libraries now emits project-relative paths, so the injector dedups
    # cleanly. This also kills the stale-drift footgun the ds_sim_sources
    # docstring warns about (a newly enabled datalogger / conditioner is picked up
    # automatically). XPM is handled by run_simulation's full-cache vendor path
    # (RTL-P3.703 _drop_scoped_vendor_libs), so no explicit xpm bucket is needed;
    # the pre_sim hook still pre-seeds TICH.ZS_MSG_PKG + TICH.INFILTER + the
    # RR.*_XPM_BODY units.
    run_simulation(
        top_level="tb_datalogger_gather",
        module=Path(__file__).stem,
        custom_libraries=scoped_libraries("tb_datalogger_gather"),
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
