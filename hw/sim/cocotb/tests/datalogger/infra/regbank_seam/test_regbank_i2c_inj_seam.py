"""
test_regbank_i2c_inj_seam — D.11 prime-suspect diagnostic.

D.11 (deepskopion, 2026-05-25) concluded the EMIO-loop fabric is correct in
sim (tb_emio_loop 3/3 PASS) but REQ-6 still fails on HW. The PRIME SUSPECT
for REQ-6 is the untested seam between the regbank's AXI-Lite write and the
i2c_injector's DATAIN_VLD pulse: existing tb_emio_loop drives DATAIN_VLD as
a TB port (bypassing the regbank entirely), so it only proves "given a
correct VLD pulse, injector+mux work" — it never asks "does an AXI write to
0x34 actually PRODUCE that pulse?"

This test wires the REAL inner regbank (tich.system_regbank) to the REAL
i2c_injector — mirroring deepskopion_top.vhd's slot fan-out / telemetry
fan-in — then drives AXI-Lite writes from cocotbext.axi.AxiLiteMaster and
observes whether the i2c_injector's TX FIFO actually fills (via its STATUS
read-back AND a direct OBS_I2C_INJ_DATAIN_VLD tap).

Hard-coded expected values (ROUTERTL-002):
  * After CONTROL[0]=1 + four writes of bytes DE / AD / BE / EF to DATAIN,
    STATUS reads back with fifo_empty='0' and count=4.

Outcomes:
  PASS → seam is OK in sim. The HW REQ-6 failure has another cause (PS-side
         AXI delivery, BD addr decode, or a HW-specific timing issue).
         File-don't-fix.
  FAIL → seam reproduces in sim. The regbank → DATAIN_VLD path is broken
         and that's the actual REQ-6 root cause. Pinpointed without a HW
         cycle.

Run:  rr sim run test_regbank_i2c_inj_seam   (from the deepskopion dir)
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer
from cocotbext.axi import AxiLiteBus, AxiLiteMaster


# Routertl SDK cocotb path (matches every sibling deepskopion test).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from sdk.cocotb_helpers import requires   # noqa: E402


# Regbank byte-offset slots (mirrors system_regbank_pkg constants).
SLOT_I2C_INJ_CONTROL = 0x30        # word 12
SLOT_I2C_INJ_DATAIN  = 0x34        # word 13
SLOT_I2C_INJ_STATUS  = 0x38        # word 14

# i2c_injector CONTROL layout (i2c_injector.vhd header).
CTRL_ENABLE = 0x1                  # bit[0]
SLAVE_ADDR  = 0x50                 # bits[7:1]
CTRL_FIFO_RESET = 1 << 9           # bit[9], self-clearing

# i2c_injector STATUS layout (i2c_injector.vhd header lines 37-41).
STATUS_FIFO_EMPTY_BIT = 0
STATUS_FIFO_PROG_FULL_BIT = 1
# [G_FIFO_DEPTH+3-1 downto 3] = data count. With G_FIFO_DEPTH=4, that's
# bits [6:3] — a 4-bit count covering 0..15 (FIFO is 16 deep — 2**4).
G_FIFO_DEPTH = 4
STATUS_COUNT_LSB = 3
STATUS_COUNT_WIDTH = G_FIFO_DEPTH + 1   # +1: matches 2**DEPTH-1 max + the prog_full margin

GENERICS = {"G_FIFO_DEPTH": G_FIFO_DEPTH}

# Hard-coded payload (ROUTERTL-002 — never derived at runtime).
PAYLOAD = [0xDE, 0xAD, 0xBE, 0xEF]

CLK_PERIOD_NS = 10                  # 100 MHz clk_sys


def status_count(status_word: int) -> int:
    """Decode the FIFO data-count slice from the i2c_injector STATUS word."""
    mask = (1 << STATUS_COUNT_WIDTH) - 1
    return (status_word >> STATUS_COUNT_LSB) & mask


def make_i2c_control(enable: bool = True, addr: int = SLAVE_ADDR) -> int:
    val = 0
    if enable:
        val |= CTRL_ENABLE
    val |= (addr & 0x7F) << 1
    return val


async def reset(dut):
    dut.RST.value = 1
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 4)


async def make_axi_master(dut):
    """Bind cocotbext.axi.AxiLiteMaster to the flat S_AXI_* ports."""
    bus = AxiLiteBus.from_prefix(dut, "S_AXI")
    return AxiLiteMaster(bus, dut.CLK, dut.RST)


# ═════════════════════════════════════════════════════════════════════════
# Test 1 — direct seam verification (does the AXI write reach the FIFO?)
# ═════════════════════════════════════════════════════════════════════════

@cocotb.test()
@requires("REGBANK-SEAM-REQ-1")
async def test_axi_write_to_datain_fills_fifo(dut):
    """
    Drive AXI-Lite writes to I2C_INJ_CONTROL (enable) then four writes to
    I2C_INJ_DATAIN with bytes DE/AD/BE/EF. After each DATAIN write, read
    I2C_INJ_STATUS and assert the FIFO count advanced.

    If the regbank → DATAIN_VLD pulse never lands, fifo_empty stays '1'
    and count stays 0 — that's the D.11 seam defect.
    """
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await reset(dut)
    axi = await make_axi_master(dut)

    # ── 1. Initial STATUS — FIFO must be empty after reset. ─────────────
    s0 = (await axi.read(SLOT_I2C_INJ_STATUS, 4)).data
    s0_w = int.from_bytes(s0, "little")
    dut._log.info(f"[init] STATUS=0x{s0_w:08X} (fifo_empty={s0_w & 1}, count={status_count(s0_w)})")
    assert (s0_w & 1) == 1, f"FIFO not empty after reset: STATUS=0x{s0_w:08X}"
    assert status_count(s0_w) == 0, f"FIFO count non-zero after reset: {status_count(s0_w)}"

    # ── 2. Enable the injector via CONTROL[0]. ──────────────────────────
    ctrl = make_i2c_control(enable=True, addr=SLAVE_ADDR)
    await axi.write(SLOT_I2C_INJ_CONTROL, ctrl.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)
    obs_ctrl = int(dut.OBS_I2C_INJ_CONTROL.value)
    dut._log.info(f"[ctrl] wrote 0x{ctrl:08X}; OBS_I2C_INJ_CONTROL=0x{obs_ctrl:08X}")
    assert obs_ctrl == ctrl, (
        f"CONTROL fan-out broken: wrote 0x{ctrl:08X}, regbank fanned out 0x{obs_ctrl:08X}"
    )

    # ── 3. Push each payload byte via AXI to DATAIN; check count grows. ─
    saw_vld_pulse = False

    async def vld_watcher():
        """Catch the one-cycle DATAIN_VLD pulse — required for FIFO write."""
        nonlocal saw_vld_pulse
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.OBS_I2C_INJ_DATAIN_VLD.value) == 1:
                saw_vld_pulse = True

    cocotb.start_soon(vld_watcher())

    for i, byte in enumerate(PAYLOAD, start=1):
        await axi.write(SLOT_I2C_INJ_DATAIN, byte.to_bytes(4, "little"))
        await ClockCycles(dut.CLK, 4)
        sw = int.from_bytes((await axi.read(SLOT_I2C_INJ_STATUS, 4)).data, "little")
        obs_din = int(dut.OBS_I2C_INJ_DATAIN.value) & 0xFF
        dut._log.info(
            f"[push {i}] wrote 0x{byte:02X} → DATAIN; "
            f"OBS_I2C_INJ_DATAIN low byte=0x{obs_din:02X}; "
            f"STATUS=0x{sw:08X} (empty={sw & 1}, count={status_count(sw)})"
        )

        assert (sw & 1) == 0, (
            f"[push {i}] FIFO STILL EMPTY after AXI write — seam is broken "
            f"(D.11 confirmed): STATUS=0x{sw:08X}"
        )
        assert status_count(sw) == i, (
            f"[push {i}] FIFO count={status_count(sw)}, expected {i} — "
            f"partial DATAIN_VLD pulses landing (DS-T3.2 race?): STATUS=0x{sw:08X}"
        )

    assert saw_vld_pulse, (
        "OBS_I2C_INJ_DATAIN_VLD never pulsed — regbank wrote the slot but its "
        "valid pulse never fanned out. D.11 seam defect confirmed."
    )

    dut._log.info(
        f"✓ REGBANK-SEAM-REQ-1 PASS — 4 AXI writes to slot 0x{SLOT_I2C_INJ_DATAIN:02X} "
        f"each produced a DATAIN_VLD pulse and the FIFO count reached 4"
    )


# ═════════════════════════════════════════════════════════════════════════
# Test 2 — discriminator: does CONTROL[9] fifo-reset actually drain the FIFO?
# Catches DS-T3.2 collateral if it ever fires from this seam.
# ═════════════════════════════════════════════════════════════════════════

@cocotb.test()
@requires("REGBANK-SEAM-REQ-2")
async def test_ctrl_fifo_reset_drains(dut):
    """
    After preloading, write CONTROL with the fifo_reset bit (self-clearing).
    STATUS must return fifo_empty='1' and count=0 within a few cycles.

    This is the canonical 'preload-after-reset' sequence DS-T3.2 calls out as
    racy (RST_BUSY tied open) — if the reset doesn't drain, that's confirmed
    here too.
    """
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await reset(dut)
    axi = await make_axi_master(dut)

    # Enable + preload one byte.
    ctrl_enable = make_i2c_control(enable=True, addr=SLAVE_ADDR)
    await axi.write(SLOT_I2C_INJ_CONTROL, ctrl_enable.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)
    await axi.write(SLOT_I2C_INJ_DATAIN, (0xAA).to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)

    sw = int.from_bytes((await axi.read(SLOT_I2C_INJ_STATUS, 4)).data, "little")
    assert status_count(sw) == 1, (
        f"precondition FAIL: count must be 1 before reset, got {status_count(sw)} "
        f"(STATUS=0x{sw:08X})"
    )

    # Pulse fifo_reset via CONTROL[9] (self-clearing).
    await axi.write(
        SLOT_I2C_INJ_CONTROL, (ctrl_enable | CTRL_FIFO_RESET).to_bytes(4, "little"),
    )
    # The bit is "self-clearing" per the header — write enable-only back to
    # release it cleanly. Wait for the FIFO to actually drain (DS-T3.2 races
    # may surface as long-tail-empty).
    await ClockCycles(dut.CLK, 4)
    await axi.write(SLOT_I2C_INJ_CONTROL, ctrl_enable.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 16)

    sw_post = int.from_bytes((await axi.read(SLOT_I2C_INJ_STATUS, 4)).data, "little")
    dut._log.info(
        f"[reset] post-reset STATUS=0x{sw_post:08X} "
        f"(empty={sw_post & 1}, count={status_count(sw_post)})"
    )
    assert (sw_post & 1) == 1, (
        f"FIFO not empty after CONTROL[9] reset: STATUS=0x{sw_post:08X} — "
        f"DS-T3.2 RST_BUSY collateral in this seam?"
    )
    assert status_count(sw_post) == 0, (
        f"FIFO count={status_count(sw_post)} after reset, expected 0"
    )

    dut._log.info("✓ REGBANK-SEAM-REQ-2 PASS — CONTROL[9] reset drained the FIFO via AXI")


# ═════════════════════════════════════════════════════════════════════════
# Test 3 — DS-T3.2 RST_BUSY race via the regbank front-door
# Mirrors the canonical "preload immediately after reset" PS firmware
# pattern. With i2c_injector.vhd:147 tying RST_BUSY => open, writes
# landing inside the FIFO's wr_rst_busy window are silently dropped.
# ═════════════════════════════════════════════════════════════════════════

@cocotb.test()
@requires("REGBANK-SEAM-REQ-3")
async def test_reset_then_immediate_preload_race(dut):
    """
    Worst-case PS sequence:
      1. preload 4 bytes (precondition: count=4)
      2. issue CONTROL[9] fifo-reset
      3. IMMEDIATELY (back-to-back AXI, no idle delay) preload 4 new bytes
      4. assert FIFO count = 4

    If wr_rst_busy is still asserted when any of the four new DATAIN writes
    arrive, that write's fifo_wr_en pulse is dropped on the floor (because
    the FIFO boundary ignores writes during reset). The injector exposes no
    busy bit, so the host has no way to know — exactly the DS-T3.2 defect.

    file-don't-fix: if this reproduces, do NOT pad the sequence with idle
    cycles or read STATUS first to "settle". Either confirms DS-T3.2 from
    the regbank front-door (no sim workaround) or rules it out.
    """
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await reset(dut)
    axi = await make_axi_master(dut)

    ctrl_en = make_i2c_control(enable=True, addr=SLAVE_ADDR)

    # ── Precondition: enable + preload 4 bytes 0x11 / 0x22 / 0x33 / 0x44.
    await axi.write(SLOT_I2C_INJ_CONTROL, ctrl_en.to_bytes(4, "little"))
    for b in [0x11, 0x22, 0x33, 0x44]:
        await axi.write(SLOT_I2C_INJ_DATAIN, b.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)
    sw_pre = int.from_bytes((await axi.read(SLOT_I2C_INJ_STATUS, 4)).data, "little")
    assert status_count(sw_pre) == 4, (
        f"precondition FAIL: count must be 4 pre-reset, got {status_count(sw_pre)} "
        f"(STATUS=0x{sw_pre:08X})"
    )

    # ── The race: reset + 4 new DATAIN writes, back-to-back. ──────────────
    # CONTROL[9] is self-clearing per the injector header, so we don't
    # need to write enable-only again before pushing DATAIN. cocotbext.axi
    # issues the next write as soon as the slave's BVALID acknowledges
    # the previous — no Python-side idle.
    await axi.write(
        SLOT_I2C_INJ_CONTROL, (ctrl_en | CTRL_FIFO_RESET).to_bytes(4, "little"),
    )
    for b in PAYLOAD:                       # [0xDE, 0xAD, 0xBE, 0xEF]
        await axi.write(SLOT_I2C_INJ_DATAIN, b.to_bytes(4, "little"))

    # Let the FIFO state settle before observing — this is OBSERVATION
    # latency, NOT a workaround for the race (the race already happened
    # or didn't happen during the back-to-back burst above).
    await ClockCycles(dut.CLK, 32)

    sw_post = int.from_bytes((await axi.read(SLOT_I2C_INJ_STATUS, 4)).data, "little")
    cnt = status_count(sw_post)
    dut._log.info(
        f"[race] post-reset+preload STATUS=0x{sw_post:08X} "
        f"(empty={sw_post & 1}, count={cnt}) — expect 4"
    )

    assert cnt == 4, (
        f"DS-T3.2 reproduced via regbank front-door: count={cnt} after "
        f"reset+4xpreload (expected 4). FIFO wr_rst_busy was still asserted "
        f"when {4 - cnt} DATAIN write(s) arrived; the injector's RST_BUSY "
        f"is tied open so they were silently dropped. This is the prime "
        f"suspect for the HW REQ-6 'master reads partial / zeros' symptom."
    )

    dut._log.info(
        "✓ REGBANK-SEAM-REQ-3 PASS — back-to-back reset+preload through the "
        "regbank kept all 4 bytes. DS-T3.2 does NOT reproduce at this AXI "
        "cadence (cocotbext.axi). HW cadence may still trigger it."
    )


# ═════════════════════════════════════════════════════════════════════════
# Test 4 — RTL-P2.651 / DS-T2.15 — AR single-outstanding gate
# ═════════════════════════════════════════════════════════════════════════

@cocotb.test()
@requires("REGBANK-SEAM-REQ-4")
async def test_ar_single_outstanding_held_valid(dut):
    """
    RTL-P2.651 / DS-T2.15: the regbank AXI-Lite slave must accept exactly
    ONE AR handshake per intended read, even when a master holds ARVALID
    high across an un-ack'd RVALID (the Cadence PS-AXI / Xilinx smartconnect
    burst-collapse pattern).

    Buggy regbank (P_AR re-arm not gated on r_valid_r='0') toggles
    ar_ready_r 1-on / 1-off while ARVALID is held and RREADY is low —
    ar_addr_r is silently re-latched each accept, and a master that
    pipelines ARADDR before the previous RREADY drains will have its
    second read silently merged into the first response (wrong RDATA
    back to SW).

    Fix: gate the ar_ready_r re-arm on r_valid_r='0' (mirror of the
    RTL-P2.650 b_valid_r='0' gate on the write side). Applied in
    routertl's sdk/generators/regbank/generate_axilite.py P_AR block.

    Hard-coded test (ROUTERTL-002):
      - Drive ARADDR = SLOT_I2C_INJ_CONTROL (0x30), ARVALID = 1, RREADY = 0.
      - Count S_AXI_ARREADY assertions over 24 sys-clk cycles.
      - Assert count == 1.

    Today (no gate): saw ~12 accepts. After fix: exactly 1.
    """
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await reset(dut)

    # Quiesce every channel except AR — we want to isolate the read path.
    dut.S_AXI_AWADDR.value  = 0
    dut.S_AXI_AWPROT.value  = 0
    dut.S_AXI_AWVALID.value = 0
    dut.S_AXI_WDATA.value   = 0
    dut.S_AXI_WSTRB.value   = 0
    dut.S_AXI_WVALID.value  = 0
    dut.S_AXI_BREADY.value  = 0
    dut.S_AXI_ARADDR.value  = SLOT_I2C_INJ_CONTROL
    dut.S_AXI_ARPROT.value  = 0
    dut.S_AXI_ARVALID.value = 0
    dut.S_AXI_RREADY.value  = 0
    await ClockCycles(dut.CLK, 2)

    # Begin a single logical read with held ARVALID and RREADY pinned LOW.
    # The slave will raise ARREADY exactly once if the AR gate is in place;
    # without the gate, ar_ready_r toggles 1-on/1-off and we count multiple
    # accepts.
    dut.S_AXI_ARVALID.value = 1

    arready_accepts = 0
    window = 24
    for _ in range(window):
        await RisingEdge(dut.CLK)
        if int(dut.S_AXI_ARREADY.value) == 1:
            arready_accepts += 1

    # Drain so the bench finishes cleanly.
    dut.S_AXI_ARVALID.value = 0
    dut.S_AXI_RREADY.value  = 1
    await ClockCycles(dut.CLK, 4)
    dut.S_AXI_RREADY.value  = 0

    dut._log.info(
        f"[RTL-P2.651] held ARVALID over {window} cycles, RREADY=0, "
        f"saw {arready_accepts} ARREADY accept(s); expected exactly 1"
    )
    assert arready_accepts == 1, (
        f"RTL-P2.651: regbank AR handshake re-fired {arready_accepts} times "
        f"under held ARVALID with un-ack'd RVALID — AXI-Lite single-outstanding "
        f"violation. ar_ready_r is not gated on r_valid_r='0'. Apply the "
        f"one-line P_AR gate in routertl's sdk/generators/regbank/"
        f"generate_axilite.py and regenerate."
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
        top_level="tb_regbank_i2c_inj_seam",
        module="test_regbank_i2c_inj_seam",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                # SDK shared units (i2c_slave dep via i2c_injector)
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                # Deepskopion packages (regbank pkg + i2c_slave deps)
                f"{DEEPSKOPION_RTL}/pkg/rom_info_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/system_regbank_pkg.vhd",
                # FIFO primitive (injector dep)
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                # I2C responder chain (slave + injector)
                f"{DEEPSKOPION_RTL}/datalogger/i2c/i2c_slave.vhd",
                f"{DEEPSKOPION_RTL}/responder/i2c_injector.vhd",
                # Regbank (inner only — wrapper's ROMs aren't relevant)
                f"{DEEPSKOPION_RTL}/regbank/system_regbank.vhd",
                # Sim wrapper (DUT)
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_regbank_i2c_inj_seam.vhd",
            ],
        },
    )
