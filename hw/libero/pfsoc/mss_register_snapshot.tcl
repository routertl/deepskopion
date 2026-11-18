# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
#
# DS-P2.145 (PF-L0e) — MSS register snapshot for the differential-against-factory
# diff (OWNER DECISION). Reads the full MSS-init-relevant register set through a
# HALTED U54 (the E51 free-runs the eNVM firmware and is un-haltable; the U54s
# halt cleanly and share the same globally-mapped MSS SYSREG / peripheral space).
#
# Emits one `ADDR: VALUE` line per register (OpenOCD `mdw`), grep-friendly, so two
# snapshots (factory-running vs ours-running) diff cleanly.
#
# The register set (PolarFire SoC MSS memory map, UG0880 / mpfs_hal):
#   * SYSREG  base 0x20002000 — IOMUX0..5_CR, MSSIO bank cfg (voltage/drive/calib),
#     clock/subblock enables + soft-reset, DEVICE_VERSION, boot/status.
#   * MMUART0 base 0x20000000 — LCR/LSR/IIR/DLL/DLM (console TX/init state).
#   * L2 cache base 0x02010000 — Config (+0x00), WayEnable (+0x08).
#   * MSS I/O bank2 (the MMUART0 console pads live on BNK2).
#
# Usage (driver script sets the target current to a halted u54):
#   openocd ... -f this.tcl
# Requires: init already sensible; this file halts u54_1 then reads.

init
adapter speed 2000

# The E51 is expected un-haltable while running eNVM firmware; halt a U54 instead.
targets mpfs.u54_1
catch {halt}

proc rd {label addr} {
    if {[catch {set v [read_memory $addr 32 1]} e]} {
        echo [format "%-28s %s : READ_ERROR %s" $label $addr $e]
    } else {
        echo [format "%-28s %s : 0x%08x" $label $addr [lindex $v 0]]
    }
}

echo "=== DS-P2.145 MSS REGISTER SNAPSHOT (via halted u54_1) ==="

echo "--- SYSREG IOMUX (0x20002200..0x20002214) ---"
rd IOMUX0_CR   0x20002200
rd IOMUX1_CR   0x20002204
rd IOMUX2_CR   0x20002208
rd IOMUX3_CR   0x2000220c
rd IOMUX4_CR   0x20002210
rd IOMUX5_CR   0x20002214
rd IOMUX6_CR   0x20002218

echo "--- SYSREG MSSIO bank2/bank4 config (voltage/drive/calibration) ---"
rd MSSIO_BANK4_CFG_CR      0x20002238
rd MSSIO_BANK4_IO_CFG_0    0x2000223c
rd MSSIO_BANK4_IO_CFG_1    0x20002240
rd MSSIO_BANK2_CFG_CR      0x20002250
rd MSSIO_BANK2_IO_CFG_0    0x20002254
rd MSSIO_BANK2_IO_CFG_1    0x20002258
rd MSSIO_BANK2_IO_CFG_2    0x2000225c
rd MSSIO_BANK2_IO_CFG_3    0x20002260
rd MSSIO_BANK2_IO_CFG_4    0x20002264
rd MSSIO_BANK2_IO_CFG_5    0x20002268

echo "--- SYSREG clock / subblock enables + soft reset ---"
rd TEMPERATURE_SENSOR_CR   0x20002000
rd SOFT_RESET_CR           0x20002088
rd SUBBLK_CLOCK_CR         0x20002084
rd DEVICE_VERSION          0x20002020
rd MSSIO_CONTROL_CR        0x2000202c

echo "--- MSS PLL / MSSIO SCB peripheral clock area (best-effort) ---"
rd MSS_PLL_CTRL_or_periph  0x3e001000
rd MSS_SGMII_or_ddr        0x3e010000

echo "--- MMUART0 (0x20000000): DLAB=0 view (LCR/LSR/IIR) ---"
rd MMUART0_RBR_THR         0x20000000
rd MMUART0_IER             0x20000004
rd MMUART0_IIR_FCR         0x20000008
rd MMUART0_LCR             0x2000000c
rd MMUART0_MCR             0x20000010
rd MMUART0_LSR             0x20000014
rd MMUART0_MSR             0x20000018

echo "--- L2 cache controller (0x02010000) ---"
rd L2_CONFIG               0x02010000
rd L2_WAYENABLE            0x02010008

echo "--- MSSIO bank controller / IO calibration (physical pad enable layer) ---"
rd IOSCB_BANKCONT_DDR_SR    0x3e020000
rd IOSCB_BANKCONT_DDR_DPC   0x3e020004
rd IOSCB_IO_CALIB_DDR       0x3e040000
rd IOSCB_IO_CALIB_DDR_STAT  0x3e040004
rd IOSCB_IO_CALIB_SGMII     0x3e800000

# ---------------------------------------------------------------------------
# DS-P2.145 IOSCB ITERATION (OWNER DECISION step 1) — the SCB-space bank-2
# enable / calibration layer that gates the PHYSICAL MMUART0 pads. The prior
# REPLICATE-THE-FACTORY leg proved the IOMUX word (SYSREG 0x2000220x) is
# byte-identical to the factory yet the console is still silent, so the
# residual is DOWNSTREAM of mssio_setup() — in exactly this layer.
#
# Authoritative addresses derived from mpfs_hal on the built HSS clone:
#   SYSREGSCB_BASE = 0x20003000 (mss_ddr_sgmii_regs.h:5542).
#   mss_nwc_init.c writes SCB_REGS->MSSIO_CONTROL_CR (this SCB copy, NOT the
#   SYSREG 0x2000202c one) through the DCE/CORE_UP/FLASH_VALID/mss_io_en
#   ramp (bits 8..13) — THIS is the register that connects the MSS pads to
#   the outside world. flash_valid (bit12) + mss_io_en (bit13) must be 1 for
#   any MSSIO pad (incl. the MMUART0 TX pin on bank-2) to physically drive.
#   Struct offsets (mss_ddr_sgmii_regs.h g5_mss_top_scb_regs):
#     0x1a0 DEVICE_CONFIG_CR  0x1a8 ENVM_CR  0x1bc MSSIO_CONTROL_CR
#     0x1c0 MSS_IO_LOCKDOWN_CR  0x1c4 MSSIO_BANK2_CFG_CR  0x1c8 MSSIO_BANK4_CFG_CR
echo "--- SCB-space MSSIO bank enable / control (SYSREGSCB 0x20003000 + off) ---"
rd SCB_DEVICE_CONFIG_CR     0x200031a0
rd SCB_ENVM_CR              0x200031a8
rd SCB_MSSIO_CONTROL_CR     0x200031bc
rd SCB_MSS_IO_LOCKDOWN_CR   0x200031c0
rd SCB_MSSIO_BANK2_CFG_CR   0x200031c4
rd SCB_MSSIO_BANK4_CFG_CR   0x200031c8

# IO-calibration lane/status registers (IOC_REG0..6, base 0x3e040000). REG1/2
# report calibration status; a bank whose pads never leave tristate shows an
# incomplete calib here. These are the "calibration/lane registers" the OWNER
# DECISION named alongside the bank-2 enables + flash_valid.
echo "--- IO_CALIB_DDR lane/status registers (IOC_REG0..6 @ 0x3e040000) ---"
rd IOC_REG0                 0x3e040000
rd IOC_REG1                 0x3e040004
rd IOC_REG2                 0x3e040008
rd IOC_REG3                 0x3e04000c
rd IOC_REG4                 0x3e040010
rd IOC_REG5                 0x3e040014
rd IOC_REG6                 0x3e04001c

echo "=== END SNAPSHOT ==="
shutdown
