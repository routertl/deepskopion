"""DS-P3.93 — qspi_datalogger input glitch-filter (G_ENABLE_INFILTER) coverage.

The G_ENABLE_INFILTER=1 generate branch (qspi_datalogger.vhd:169-181) wraps every
QSPI_PINS bit in a `tich.infilter` 2-cycle agreement debouncer (PULSE_OUT only
follows once all G_DEB_CYCLES samples agree → sub-2-cycle transients are
rejected). It had zero coverage. This build turns it on.

Two attacks:
  1. clean capture still works THROUGH the filter (it only adds ~2 cycles of
     matched latency on SCLK + data, so sampling stays aligned);
  2. a 1-cycle spurious SCLK pulse mid-byte is REJECTED — without the filter that
     glitch is a spurious rising edge → an extra qspiCE → a 9th sample →
     mis-aligned/wrong byte; with the filter the byte is captured intact.

Reuses the monitor/bring-up from test_qspi_capture (sibling import; cocotb only
registers tests from the MODULE-named file).

Run:  RR_SKIP_CLOSURE_PREFLIGHT=1 rr sim run test_qspi_infilter
"""

import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from sdk.cocotb_helpers import requires  # noqa: E402

from test_qspi_capture import (  # noqa: E402
    QspiDriver, FrameMon, _bringup, _enable,
    P_SCLK, P_CSN, P_IO, DEEPSKOPION_RTL,
)

GENERICS = {
    "G_QSPI_ID_WIDTH": 5,
    "G_QSPI_DEV_ID": 28,
    "G_QSPI_PINS": 6,
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_ENABLE_INFILTER": 1,     # ← the branch under test
    "G_ILA_EN": 1,
}

HALF = 12   # CLK cycles per SCLK half-period (>> the 2-cycle filter latency)


def _vec(sclk: int, csn: int, io0: int) -> int:
    return (sclk << P_SCLK) | (csn << P_CSN) | (io0 << P_IO[0])


async def _transfer_mode0(dut, byte: int, glitch_bit=None):
    """MODE-0 single-byte transfer on IO0, cycle-precise. If glitch_bit is set,
    inject a 1-cycle spurious SCLK-high pulse during that bit's low phase."""
    dut.QSPI_PINS.value = _vec(0, 1, 0)             # idle: SCLK low, CS high
    await ClockCycles(dut.CLK, HALF)
    dut.QSPI_PINS.value = _vec(0, 0, 0)             # assert CS
    await ClockCycles(dut.CLK, 2 * HALF)            # setup

    for i in range(7, -1, -1):
        bit = (byte >> i) & 1
        dut.QSPI_PINS.value = _vec(0, 0, bit)       # present data, SCLK low
        await ClockCycles(dut.CLK, HALF // 2)
        if glitch_bit is not None and i == glitch_bit:
            dut.QSPI_PINS.value = _vec(1, 0, bit)   # 1-cycle spurious SCLK high
            await ClockCycles(dut.CLK, 1)
            dut.QSPI_PINS.value = _vec(0, 0, bit)   # back low
            await ClockCycles(dut.CLK, HALF - HALF // 2 - 1)
        else:
            await ClockCycles(dut.CLK, HALF - HALF // 2)
        dut.QSPI_PINS.value = _vec(1, 0, bit)       # real sampling edge (rising)
        await ClockCycles(dut.CLK, HALF)
        dut.QSPI_PINS.value = _vec(0, 0, bit)       # SCLK low

    await ClockCycles(dut.CLK, HALF)
    dut.QSPI_PINS.value = _vec(0, 1, 0)             # deassert CS
    await ClockCycles(dut.CLK, HALF)


@cocotb.test()
@requires("QSPI-REQ-41")
async def test_infilter_clean_capture(dut):
    """QSPI-REQ-41: with the input filter ON, a clean MODE-0 byte is still
    captured correctly (the filter only adds matched SCLK/data latency)."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    await QspiDriver(dut, cpol=0, cpha=0).transfer({0: 0xA5})
    await ClockCycles(dut.CLK, 60)
    pays = mon.payloads()
    assert pays, f"filter ON broke clean capture; frames={[hex(f['raw']) for f in mon.frames]}"
    assert pays[0]["lo"] == 0xA5, (
        f"filter ON corrupted clean byte: got 0x{pays[0]['lo']:02x} != 0xA5 "
        f"(raw=0x{pays[0]['raw']:08x})")
    dut._log.info(f"INFILTER clean OK raw=0x{pays[0]['raw']:08x}")


@cocotb.test()
@requires("QSPI-REQ-41")
async def test_infilter_rejects_sclk_glitch(dut):
    """QSPI-REQ-41: a 1-cycle spurious SCLK pulse during bit 4's low phase must
    be REJECTED by the 2-cycle debouncer — the byte 0x6C is captured intact. An
    unfiltered path would see the glitch as a 9th sampling edge and mis-align."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    await _transfer_mode0(dut, 0x6C, glitch_bit=4)
    await ClockCycles(dut.CLK, 60)
    pays = mon.payloads()
    assert pays, f"no payload after glitched transfer; frames={[hex(f['raw']) for f in mon.frames]}"
    assert pays[0]["lo"] == 0x6C, (
        f"filter did NOT reject the SCLK glitch: got Q0=0x{pays[0]['lo']:02x} != 0x6C "
        f"(raw=0x{pays[0]['raw']:08x}) — a spurious edge corrupted the byte")
    dut._log.info(f"INFILTER glitch-reject OK raw=0x{pays[0]['raw']:08x}")


def main():
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH

    run_simulation(
        top_level="qspi_datalogger",
        module="test_qspi_infilter",
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
