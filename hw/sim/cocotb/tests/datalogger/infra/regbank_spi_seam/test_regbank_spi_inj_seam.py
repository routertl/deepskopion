# rr unknown-llm (AI-generated)
"""
test_regbank_spi_inj_seam — DS-T12 / DS-T3.2 mirror for the SPI side.

Same shape as test_regbank_i2c_inj_seam: wires the REAL inner regbank
(tich.system_regbank) to the REAL spi_injector exactly as deepskopion_top
does (slot fan-out + telemetry fan-in), then drives AXI-Lite writes from
cocotbext.axi.AxiLiteMaster and observes the FIFO via STATUS reads and
the OBS_SPI_INJ_* taps.

DS-T12 is the SPI counterpart to DS-T3.2: spi_injector had the same
compound defect i2c_injector did — level-sampled CONTROL[9] +
RST_BUSY=>open on the XPM FIFO. Through the regbank front-door the
canonical "reset, then preload" PS firmware burst would lose every byte.

Three tests mirror the i2c suite:
  REQ-1 — AXI writes to DATAIN advance the FIFO count to 4.
  REQ-2 — CONTROL[9] fifo-reset drains the FIFO via AXI.
  REQ-3 — Reset-then-immediate-preload race. With the DS-T12 fix in
          spi_injector.vhd (edge-detect CONTROL[9] + drain mode), all
          4 bytes survive. Without the fix this hangs at count=0/4.

Run:  rr sim run test_regbank_spi_inj_seam   (from the deepskopion dir)
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from sdk.cocotb_helpers import requires   # noqa: E402


# Regbank byte-offset slots (mirrors system_regbank_pkg constants).
SLOT_SPI_INJ_CONTROL = 0x40        # word 16
SLOT_SPI_INJ_DATAIN  = 0x44        # word 17
SLOT_SPI_INJ_STATUS  = 0x48        # word 18

# spi_injector CONTROL layout (spi_injector.vhd header).
CTRL_ENABLE     = 0x1              # bit[0]
CTRL_FIFO_RESET = 1 << 9           # bit[9], self-clearing (DS-T12 edge-detect)

# spi_injector STATUS layout — same as i2c_injector for symmetry.
STATUS_FIFO_EMPTY_BIT = 0
G_FIFO_DEPTH = 4
STATUS_COUNT_LSB = 3
STATUS_COUNT_WIDTH = G_FIFO_DEPTH + 1
STATUS_DRAIN_BUSY_BIT = 15        # DS-T12 / DS-T3.2

GENERICS = {"G_FIFO_DEPTH": G_FIFO_DEPTH}

# Hard-coded payload (ROUTERTL-002 — never derived at runtime).
PAYLOAD = [0xDE, 0xAD, 0xBE, 0xEF]

CLK_PERIOD_NS = 10


def status_count(status_word: int) -> int:
    mask = (1 << STATUS_COUNT_WIDTH) - 1
    return (status_word >> STATUS_COUNT_LSB) & mask


async def reset(dut):
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 4)


async def make_axi_master(dut):
    bus = AxiLiteBus.from_prefix(dut, "S_AXI")
    return AxiLiteMaster(bus, dut.CLK, dut.RST)


# ═════════════════════════════════════════════════════════════════════════
# Test 1 — direct seam verification
# ═════════════════════════════════════════════════════════════════════════

@cocotb.test()
@requires("REGBANK-SPI-SEAM-REQ-1")
async def test_axi_write_to_datain_fills_fifo(dut):
    """AXI writes to slot 0x44 must advance the spi_injector FIFO count."""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await reset(dut)
    axi = await make_axi_master(dut)

    s0 = int.from_bytes((await axi.read(SLOT_SPI_INJ_STATUS, 4)).data, "little")
    dut._log.info(f"[init] STATUS=0x{s0:08X}")
    assert (s0 & 1) == 1 and status_count(s0) == 0, (
        f"FIFO must start empty: STATUS=0x{s0:08X}"
    )

    await axi.write(SLOT_SPI_INJ_CONTROL, CTRL_ENABLE.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)
    assert int(dut.OBS_SPI_INJ_CONTROL.value) == CTRL_ENABLE, (
        f"CONTROL fan-out broken: regbank fanned out "
        f"0x{int(dut.OBS_SPI_INJ_CONTROL.value):08X}"
    )

    saw_vld_pulse = False

    async def vld_watcher():
        nonlocal saw_vld_pulse
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.OBS_SPI_INJ_DATAIN_VLD.value) == 1:
                saw_vld_pulse = True

    cocotb.start_soon(vld_watcher())

    for i, byte in enumerate(PAYLOAD, start=1):
        await axi.write(SLOT_SPI_INJ_DATAIN, byte.to_bytes(4, "little"))
        await ClockCycles(dut.CLK, 4)
        sw = int.from_bytes((await axi.read(SLOT_SPI_INJ_STATUS, 4)).data, "little")
        dut._log.info(
            f"[push {i}] wrote 0x{byte:02X}; STATUS=0x{sw:08X} "
            f"(empty={sw & 1}, count={status_count(sw)})"
        )
        assert (sw & 1) == 0, (
            f"[push {i}] FIFO empty after AXI write — regbank/seam broken: "
            f"STATUS=0x{sw:08X}"
        )
        assert status_count(sw) == i, (
            f"[push {i}] count={status_count(sw)}, expected {i}: STATUS=0x{sw:08X}"
        )

    assert saw_vld_pulse, "OBS_SPI_INJ_DATAIN_VLD never pulsed"
    dut._log.info(
        f"✓ REGBANK-SPI-SEAM-REQ-1 PASS — 4 AXI writes to slot 0x{SLOT_SPI_INJ_DATAIN:02X} "
        f"reached the FIFO via the DATAIN_VLD pulse"
    )


# ═════════════════════════════════════════════════════════════════════════
# Test 2 — CONTROL[9] drains the FIFO via AXI
# ═════════════════════════════════════════════════════════════════════════

@cocotb.test()
@requires("REGBANK-SPI-SEAM-REQ-2")
async def test_ctrl_fifo_reset_drains(dut):
    """CONTROL[9] (self-clearing edge) drains a populated FIFO via AXI."""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await reset(dut)
    axi = await make_axi_master(dut)

    await axi.write(SLOT_SPI_INJ_CONTROL, CTRL_ENABLE.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)
    await axi.write(SLOT_SPI_INJ_DATAIN, (0x55).to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)
    sw = int.from_bytes((await axi.read(SLOT_SPI_INJ_STATUS, 4)).data, "little")
    assert status_count(sw) == 1, (
        f"precondition FAIL: count must be 1 before reset, got "
        f"{status_count(sw)} (STATUS=0x{sw:08X})"
    )

    await axi.write(
        SLOT_SPI_INJ_CONTROL,
        (CTRL_ENABLE | CTRL_FIFO_RESET).to_bytes(4, "little"),
    )
    await ClockCycles(dut.CLK, 4)
    await axi.write(SLOT_SPI_INJ_CONTROL, CTRL_ENABLE.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 16)

    sw_post = int.from_bytes((await axi.read(SLOT_SPI_INJ_STATUS, 4)).data, "little")
    dut._log.info(
        f"[reset] post-reset STATUS=0x{sw_post:08X} "
        f"(empty={sw_post & 1}, count={status_count(sw_post)})"
    )
    assert (sw_post & 1) == 1 and status_count(sw_post) == 0, (
        f"FIFO not empty after CONTROL[9] reset: STATUS=0x{sw_post:08X}"
    )
    dut._log.info("✓ REGBANK-SPI-SEAM-REQ-2 PASS — CONTROL[9] drained the FIFO via AXI")


# ═════════════════════════════════════════════════════════════════════════
# Test 3 — DS-T12 reset-then-immediate-preload race (mirror of DS-T3.2 REQ-3)
# ═════════════════════════════════════════════════════════════════════════

@cocotb.test()
@requires("REGBANK-SPI-SEAM-REQ-3")
async def test_reset_then_immediate_preload_race(dut):
    """
    Worst-case PS sequence:
      1. preload 4 bytes (precondition: count=4)
      2. issue CONTROL[9] fifo-reset
      3. IMMEDIATELY (back-to-back AXI, no idle delay) preload 4 new bytes
      4. assert FIFO count = 4

    Without the DS-T12 fix (level-sampled CONTROL[9] + RST_BUSY=>open) the
    new writes are silently dropped while the XPM FIFO is in wr_rst_busy.
    With the fix (edge-detect + drain mode) the FIFO is never in XPM reset
    and the writes land normally.
    """
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await reset(dut)
    axi = await make_axi_master(dut)

    # ── Precondition: enable + preload 4 bytes. ─────────────────────────
    await axi.write(SLOT_SPI_INJ_CONTROL, CTRL_ENABLE.to_bytes(4, "little"))
    for b in [0x11, 0x22, 0x33, 0x44]:
        await axi.write(SLOT_SPI_INJ_DATAIN, b.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)
    sw_pre = int.from_bytes((await axi.read(SLOT_SPI_INJ_STATUS, 4)).data, "little")
    assert status_count(sw_pre) == 4, (
        f"precondition FAIL: count must be 4 pre-reset, got "
        f"{status_count(sw_pre)} (STATUS=0x{sw_pre:08X})"
    )

    # ── The race: reset + 4 new DATAIN writes, back-to-back. ────────────
    await axi.write(
        SLOT_SPI_INJ_CONTROL,
        (CTRL_ENABLE | CTRL_FIFO_RESET).to_bytes(4, "little"),
    )
    for b in PAYLOAD:                       # [0xDE, 0xAD, 0xBE, 0xEF]
        await axi.write(SLOT_SPI_INJ_DATAIN, b.to_bytes(4, "little"))

    # Observation latency, NOT a workaround — the race resolved (or didn't)
    # during the back-to-back burst above.
    await ClockCycles(dut.CLK, 32)

    sw_post = int.from_bytes((await axi.read(SLOT_SPI_INJ_STATUS, 4)).data, "little")
    cnt = status_count(sw_post)
    dut._log.info(
        f"[race] post-reset+preload STATUS=0x{sw_post:08X} "
        f"(empty={sw_post & 1}, count={cnt}) — expect 4"
    )

    assert cnt == 4, (
        f"DS-T12 race reproduced: count={cnt} after reset+4xpreload "
        f"(expected 4). The XPM FIFO wr_rst_busy window swallowed "
        f"{4 - cnt} DATAIN write(s). spi_injector needs the DS-T3.2 fix "
        f"pattern (edge-detect CONTROL[9] + drain mode)."
    )

    dut._log.info(
        "✓ REGBANK-SPI-SEAM-REQ-3 PASS — back-to-back reset+preload through "
        "the regbank kept all 4 bytes. DS-T12 fix verified."
    )


# ─────────────────────────────────────────────────────────────────────────
#  Standalone Execution (ROUTERTL-001 — engine.simulation.run_simulation only)
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from engine.simulation import run_simulation
    from engine.paths import SRC_PATH, SDK_SRC_PATH

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
    DEEPSKOPION_RTL  = DEEPSKOPION_ROOT / "hw" / "rtl"
    from hw.sim.portable_fifo_sources import olo_fifo_sources

    run_simulation(
        top_level="tb_regbank_spi_inj_seam",
        module="test_regbank_spi_inj_seam",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                # Deepskopion packages
                f"{DEEPSKOPION_RTL}/pkg/rom_info_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/system_regbank_pkg.vhd",
                # FIFO primitive (injector dep)
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                # SPI responder chain
                f"{DEEPSKOPION_RTL}/responder/spi_slave.vhd",
                f"{DEEPSKOPION_RTL}/responder/spi_injector.vhd",
                # Regbank (inner only)
                f"{DEEPSKOPION_RTL}/regbank/system_regbank.vhd",
                # Sim wrapper (DUT)
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_regbank_spi_inj_seam.vhd",
            ],
        },
    )
