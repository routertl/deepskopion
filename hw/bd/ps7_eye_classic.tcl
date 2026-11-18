###############################################################################
# ps7_eye_classic.tcl — PS7 project deltas for the LEAN Classic eye target
# (DS-P3.115). Registered via targets/classic_eye.yml → hardware.ps7_hooks.
#
# Sourced by routertl's create_bd_cell wrapper (RTL-P3.557) AT PS-cell
# instantiation in classic_eye_bd.tcl, AFTER the board preset
# (routertl/boards/zybo/ps7_preset.tcl, auto-resolved from
# hardware.platform: zybo) has been applied. So this overlays the verified
# original-Zybo (PB200-279) PCW baseline.
#
# The lean eye target needs ONLY:
#   - M_AXI_GP0 (AXI-Lite master → regbank in classic_eye_top), FCLK0 100 MHz
# and explicitly DROPS everything the canfd/datalogger product BD carried:
#   - S_AXI_HP0/HP2 (no rr-ddr-stream DMA), M_AXI_GP1 (no CTU CAN FD),
#     IRQ_F2P (no fabric interrupts), the 2nd fabric clock.
# FCLK0 stays at the board-preset default 100 MHz (classic_eye_top's
# MMCME2_ADV assumes CLKIN1_PERIOD = 10.0), so no UART/SDIO divisor fixup
# is needed — we do not retune the PS clock tree.
###############################################################################

set_property -dict [list \
    CONFIG.PCW_USE_M_AXI_GP0            {1} \
    CONFIG.PCW_EN_CLK0_PORT             {1} \
    CONFIG.PCW_EN_RST0_PORT             {1} \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100} \
    CONFIG.PCW_USE_M_AXI_GP1            {0} \
    CONFIG.PCW_USE_S_AXI_HP0            {0} \
    CONFIG.PCW_USE_S_AXI_HP2            {0} \
    CONFIG.PCW_EN_CLK1_PORT             {0} \
    CONFIG.PCW_EN_RST1_PORT             {0} \
    CONFIG.PCW_IRQ_F2P_INTR             {0} \
    CONFIG.PCW_USE_FABRIC_INTERRUPT     {0} \
    CONFIG.PCW_EN_ENET0                 {1} \
    CONFIG.PCW_ENET0_PERIPHERAL_ENABLE  {1} \
    CONFIG.PCW_ENET0_GRP_MDIO_ENABLE    {1} \
    CONFIG.PCW_ENET0_RESET_ENABLE       {0} \
    CONFIG.PCW_EN_EMIO_ENET0            {0} \
    CONFIG.PCW_ENET0_ENET0_IO           {MIO 16 .. 27} \
    CONFIG.PCW_ENET0_GRP_MDIO_IO        {MIO 52 .. 53} \
    CONFIG.PCW_ENET0_PERIPHERAL_CLKSRC  {IO PLL} \
    CONFIG.PCW_ENET0_PERIPHERAL_FREQMHZ {1000 Mbps} \
    CONFIG.PCW_IO_IO_PLL_FREQMHZ        {1500.000} \
    CONFIG.PCW_GPIO_EMIO_GPIO_ENABLE    {1} \
    CONFIG.PCW_GPIO_EMIO_GPIO_IO        {1} \
] [get_bd_cells processing_system7_0]

# ROOT CAUSE of the "Could not attach PHY (-22)" / empty-advertisement / ghost-PHY
# failure (on-silicon 2026-06-19): the zybo board preset ships ENET0 DISABLED and
# assigns NONE of its MIO. Enabling ENET0 here WITHOUT pinning the IO let the PS7
# default BOTH ENET0_ENET0_IO and ENET0_GRP_MDIO_IO to *EMIO* (fabric) — so the
# GEM's RGMII+MDIO went to unconnected PL pins, never to the on-board RTL8211E
# which is hard-wired to MIO 16-27 (RGMII) + MIO 52-53 (MDIO). The GEM regs read
# fine (AXI), but MDIO reached no PHY → ghost at addr 0 with empty caps → -22, in
# BOTH the old static-reset and the gpio-reset builds. FIX: pin ENET0 to MIO
# 16-27 and MDIO to MIO 52-53 (free in the zybo preset), EMIO off. The PHY reset
# stays on EMIO GPIO[0]/E17 (a separate PL pin, unaffected by the MIO routing).
# PS ENET0 (onboard RTL8211E, RGMII on MIO 16-27 + MDIO 52-53) so eth0 streams
# the live bathtub to RouteWave. CLKSRC/freq come from the zybo preset (External,
# 1000 Mbps). MDIO on for PHY management. PS RESET stays OFF — the RTL8211E reset
# is a PL pin (eth_rst_b / E17), so it can't be a PS MIO reset. Instead we export an
# EMIO GPIO bit (width 1) so LINUX owns the reset as a proper DT-managed reset-gpios:
# EMIO GPIO bit 0 == PS GPIO controller line 54 (MIO 0-53, EMIO 54+), the same line
# canfd_demo uses for its SPI cs-gpios (DS-T2.16). classic_eye_top drives
# eth_rst_b <= GPIO_O(0); the mdio bus node's reset-gpios then pulses it cleanly at
# probe (assert-low → hold → deassert-high) so the PHY responds on MDIO. A static
# high never re-pulses a PHY that powered up before the fabric configured E17 — the
# "Could not attach PHY (-22)" failure. PHYAD is strapped on-board (0 or 1), so macb
# auto-scans the bus and finds it at whichever address (no phy@N pinned).
puts "INFO: ps7_eye_classic.tcl — lean PS7 deltas applied (M_AXI_GP0 + ENET0 + EMIO GPIO\[0\] for PHY reset; HP/GP1/IRQ/CLK1 off)."
