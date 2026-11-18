# ds_emio_uart_i2c_spi.tcl — enable UART0 / I2C0 / SPI0 on EMIO (PL fabric).
#
# DS-P2.51 Phase 1. Project-feature delta (RTL-P2.618 ps7_hook).
# Layered on top of the canonical Zybo Z7-20 board preset
# (routertl/boards/zybo-z7-20/ps7_preset.tcl). Listed in project.yml
# hardware.ps7_hooks; runs AFTER the board preset and the other
# ds_*.tcl hooks.
#
# Purpose
# -------
# Re-route three PS-side controllers (UART0, I2C0, SPI0) out of the
# MIO pin matrix and into PL-fabric pins ("EMIO"). The PS Linux still
# exposes them as /dev/ttyPS1 (after /dev/ttyPS0 = MIO UART1, the
# console), /dev/i2c-0, and /dev/spidev0. Their wires land inside the
# fabric where deepskopion_top routes them directly into the datalogger
# input matrix — no PMOD jumper, no external dongle. The whole
# stimulus + observation loop is internal to the chip.
#
# PS-side bring-up (post-bitstream load):
#   * /dev/ttyPS1     — pyserial Serial(...) opens this, write bytes,
#                       datalogger renders zs_msg UART frames.
#   * /dev/i2c-0      — i2c-tools / smbus2 drives transactions; the
#                       datalogger sees SCL/SDA edges. Without a PL slave
#                       (Phase 2 — DS-P2.51 work item #3) the master
#                       will NAK its address but the START + addr byte
#                       still emit valid datalogger events.
#   * /dev/spidev0    — spidev drives MOSI/SCLK/CS; datalogger captures.
#                       MISO is tied low in Phase 1 (no echo slave yet).
#
# UART1 (MIO) stays the kernel console (ttyPS0). The board preset has
# it on MIO[10:11] per the Zybo Z7-20 platform definition. UART0 here
# is the *second* UART, fully under userspace control.
#
# PCW property reference (Vivado 2024.1 processing_system7_0 v5.5):
#   PCW_<periph>_PERIPHERAL_ENABLE  enable the controller block
#   PCW_<periph>_<inst>_IO          where its wires land — MIO/EMIO/<bank>
#   PCW_EN_EMIO_<periph><inst>      explicit EMIO-pins export gate
#
# Both _<inst>_IO and the EN_EMIO_<periph><inst> flag must be set —
# the PCW back end uses the former for pinmux planning and the latter
# to actually instantiate the BD interface pin. Setting one without
# the other has bitten previous bring-ups (silent: design synthesises
# but the EMIO pin is absent from the PS7 cell's port list).

puts "INFO: enabling EMIO for UART0 + I2C0 + SPI0 + GPIO (DS-P2.51 / DS-T2.16)..."

# DS-T2.16: EMIO GPIO bit 0 is the fabric-driven SPI chip-select. The Zynq
# PS-SPI controller asserts its native CS internally (SSCTRL) but the EMIO
# SPI0_SS_O pin never carries it out (SCLK/MOSI do) — so the SPI injector
# never sees a transaction start (DS-T7/T8 ETIMEDOUT). The fix: drive CS from
# an EMIO GPIO bit, coupled to spidev via `cs-gpios` in the DT (project.yml
# linux.dt_nodes). The Linux SPI core toggles this GPIO around each transfer.
# EMIO GPIO bit 0 == PS GPIO controller line 54 (MIO 0-53, EMIO 54+).
set_property -dict [list \
    CONFIG.PCW_UART0_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_UART0_UART0_IO          {EMIO} \
    CONFIG.PCW_EN_EMIO_UART0           {1} \
    CONFIG.PCW_I2C0_PERIPHERAL_ENABLE  {1} \
    CONFIG.PCW_I2C0_I2C0_IO            {EMIO} \
    CONFIG.PCW_EN_EMIO_I2C0            {1} \
    CONFIG.PCW_SPI0_PERIPHERAL_ENABLE  {1} \
    CONFIG.PCW_SPI0_SPI0_IO            {EMIO} \
    CONFIG.PCW_EN_EMIO_SPI0            {1} \
    CONFIG.PCW_GPIO_EMIO_GPIO_ENABLE   {1} \
    CONFIG.PCW_GPIO_EMIO_GPIO_IO       {1} \
] [get_bd_cells processing_system7_0]
