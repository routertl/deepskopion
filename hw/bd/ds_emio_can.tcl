# ds_emio_can.tcl — enable CAN0 + CAN1 on EMIO (PL fabric). DS-P3.102.
#
# Project-feature delta (RTL-P2.618 ps7_hook), layered on the canonical Zybo
# Z7-20 board preset (routertl/boards/zybo-z7-20/ps7_preset.tcl) and listed in
# project.yml hardware.ps7_hooks. Runs AFTER the board preset and the other
# ds_*.tcl hooks. Sibling of ds_emio_uart_i2c_spi.tcl.
#
# Purpose
# -------
# Route BOTH Zynq-7 PS CAN controllers (CAN0 + CAN1) out of the MIO matrix and
# onto PL-fabric pins ("EMIO"), exposing the DIGITAL pre-transceiver CAN signals
# (PHY_TX = controller out, PHY_RX = controller in) to the fabric. deepskopion_top
# then ANDs the two TX legs into a single open-drain CAN bus (dominant '0' wins)
# and feeds it back to both RX legs + the passive can_datalogger (id 20) — a real
# 2-node CAN bus entirely in fabric, NO transceiver, NO external device.
#
# Why two controllers: a single CAN node cannot complete a frame — CAN requires
# at least one OTHER node to drive the ACK slot dominant, or it retransmits
# forever. Two controllers ACK each other, so frames complete and the datalogger
# sees real arbitration + real ACK. (Later: route PHY_TX/RX to PMOD pins + real
# transceivers for a physical bus — docs/BRING_UP_CAN.md phase 2.)
#
# Linux exposes them via the xilinx_can driver as SocketCAN can0 / can1
# (project.yml linux.dt_nodes). Bring-up uses 250 kbit/s — clean for the v0.1
# datalogger's fixed 16-TQ timing at 100 MHz clk_sys (prescaler = 25 exactly).
#
# PCW reference (processing_system7 v5.5), mirroring the UART/I2C/SPI hook:
#   PCW_CANx_PERIPHERAL_ENABLE   enable the controller block
#   PCW_CANx_CANx_IO             where its wires land — must be {EMIO}
#   PCW_EN_EMIO_CANx             explicit EMIO-pins export gate
# Both the _IO and the EN_EMIO_CANx flag must be set (the PCW back end uses the
# former for pinmux planning and the latter to actually instantiate the BD
# interface pin — setting one without the other silently drops the EMIO pin).
#
# NB (integrator-validated): this TCL only runs under Vivado, so the exact CAN
# clock property values are confirmed on-hardware per docs/BRING_UP_CAN.md, not
# in the unit suite.

puts "INFO: enabling EMIO for CAN0 + CAN1 (DS-P3.102 — 2-node fabric loopback)..."

set_property -dict [list \
    CONFIG.PCW_CAN0_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_CAN0_CAN0_IO           {EMIO} \
    CONFIG.PCW_EN_EMIO_CAN0           {1} \
    CONFIG.PCW_CAN1_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_CAN1_CAN1_IO           {EMIO} \
    CONFIG.PCW_EN_EMIO_CAN1           {1} \
] [get_bd_cells processing_system7_0]

# Do NOT set PCW_CAN_PERIPHERAL_CLKSRC / _FREQMHZ here — they are REDUNDANT.
# "IO PLL" / 100 MHz are already the PS7 IP's default CAN-branch values, so
# setting them changes nothing: the resulting deepskopion_bd.hwh is byte-identical
# with or without them, and ps7_init still writes the standard IO_PLL FDIV=30
# (1000 MHz), GEM0 DIV=8 (125 MHz), UART DIV=10 (100 MHz). The CAN controller
# takes the default ref clock; SocketCAN derives bit timing from it and the wire
# bitrate is set at runtime (`ip link set canX type can bitrate ...`).
#
# History (DS-T3.20, CLOSED-refuted): a CAN-enabled board once came up with a
# silent console + dead ethernet and this was MISdiagnosed as the override
# re-solving the shared IO PLL and skewing GEM/UART. That mechanism was refuted:
# the override is a proven no-op (above), GEM/UART clocks were standard in both
# builds. The real cause was a userspace brick — the can-utils/iproute2 rootfs
# rebuild clobbered /bin/busybox with the iproute2 `ip` binary, so /init panicked
# ("Attempted to kill init"). Fixed at the tool level by RTL-P2.717. The override
# is dropped purely as hygiene (don't set redundant clock params).
