###############################################################################
# classic_eye_bd.tcl — LEAN Zynq-7 PS subsystem for the Classic eye target
# (DS-P3.115). BD-creation hook, sourced in-session by routertl's
# gen_project.tcl via targets/classic_eye.yml → tcl_hooks.gen_project.
#
# This is a STRIPPED sibling of deepskopion_bd.tcl. The product BD carries a
# 2nd fabric clock, S_AXI_HP0 (rr-ddr-stream DMA), M_AXI_GP1 → 2× APB → CTU
# CAN FD, EMIO UART/I2C/SPI/CAN, and a 3-bit IRQ_F2P concat — NONE of which
# fit (or are needed) on the ~half-size xc7z010 for a PMOD eye-scan. The eye
# instrument is polled over a single AXI-Lite regbank, so this BD exposes only:
#
#   - DDR_* / FIXED_IO_*    (PS pins, inout, propagate from PS7)
#   - M_AXI_GP0  (AXI-Lite master, post-smartconnect) → regbank in the top
#   - fclk_clk0  (100 MHz fabric clock out)
#   - fclk_aresetn (active-low reset out, synchronous to fclk_clk0)
#
# PS7 PCW config is owned by routertl: the original-Zybo board preset
# (routertl/boards/zybo/ps7_preset.tcl, from hardware.platform: zybo) plus the
# ps7_eye_classic.tcl delta hook (hardware.ps7_hooks) auto-apply at the
# create_bd_cell below (RTL-P3.557). So M_AXI_GP0 + FCLK0/RST0 exist by the
# time we wire them; HP0/GP1/IRQ are off.
###############################################################################

puts "INFO: Building classic_eye_bd (lean Zynq-7 PS subsystem, xc7z010)..."

create_bd_design classic_eye_bd
current_bd_design [get_bd_designs classic_eye_bd]

# ── Processing System 7 ──────────────────────────────────────────────────────
# create_bd_cell triggers routertl's wrapper: applies the zybo board preset +
# ps7_eye_classic.tcl. After this, M_AXI_GP0 / FCLK_CLK0 / FCLK_RESET0_N exist.
set processing_system7_0 [create_bd_cell -type ip \
    -vlnv xilinx.com:ip:processing_system7:5.5 processing_system7_0]

# ── GP0 smartconnect (1×1) — PS7 AXI3 GP0 → AXI4-Lite external port ──────────
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:smartconnect:1.0 axi_smartconnect_gp0
set_property -dict [list \
    CONFIG.NUM_SI {1} \
    CONFIG.NUM_MI {1} \
] [get_bd_cells axi_smartconnect_gp0]

connect_bd_intf_net \
    [get_bd_intf_pins processing_system7_0/M_AXI_GP0] \
    [get_bd_intf_pins axi_smartconnect_gp0/S00_AXI]

# ── Reset ────────────────────────────────────────────────────────────────────
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_ps7_100M

# ── External ports ───────────────────────────────────────────────────────────
# PS dedicated pins.
make_bd_intf_pins_external [get_bd_intf_pins processing_system7_0/DDR]
make_bd_intf_pins_external [get_bd_intf_pins processing_system7_0/FIXED_IO]
set_property name DDR      [get_bd_intf_ports DDR_0]
set_property name FIXED_IO [get_bd_intf_ports FIXED_IO_0]

# EMIO GPIO (RTL8211E PHY reset) — width-1 bank exported by ps7_eye_classic.tcl
# (PCW_GPIO_EMIO_GPIO_ENABLE/IO). Flatten O/I/T to top-level BD ports like the
# canfd_demo SPI-CS pattern (DS-T2.16). classic_eye_top uses GPIO_O(0) as the
# Linux-driven eth_rst_b (PS GPIO line 54 = EMIO bit 0); GPIO_I/GPIO_T are tied
# off / ignored in the top. Linux toggles GPIO_O(0) via the mdio bus reset-gpios.
foreach pin_name {GPIO_O GPIO_I GPIO_T} {
    make_bd_pins_external \
        -name $pin_name \
        [get_bd_pins processing_system7_0/$pin_name]
}

# AXI-Lite master out → regbank slave in classic_eye_top. The BD wrapper
# expands this aximm_rtl interface to flat M_AXI_GP0_* ports.
set m_axi_gp0_port [create_bd_intf_port -mode Master \
    -vlnv xilinx.com:interface:aximm_rtl:1.0 M_AXI_GP0]
set_property -dict [list \
    CONFIG.PROTOCOL    {AXI4LITE} \
    CONFIG.ADDR_WIDTH  {32} \
    CONFIG.DATA_WIDTH  {32} \
    CONFIG.HAS_BURST   {0} \
    CONFIG.HAS_CACHE   {0} \
    CONFIG.HAS_LOCK    {0} \
    CONFIG.HAS_QOS     {0} \
    CONFIG.HAS_REGION  {0} \
] $m_axi_gp0_port
connect_bd_intf_net $m_axi_gp0_port \
    [get_bd_intf_pins axi_smartconnect_gp0/M00_AXI]

# Clock + reset out ports.
set fclk_clk0_port [create_bd_port -dir O -type clk fclk_clk0]
set_property -dict [list \
    CONFIG.FREQ_HZ           {100000000} \
    CONFIG.ASSOCIATED_BUSIF  {M_AXI_GP0} \
    CONFIG.ASSOCIATED_RESET  {fclk_aresetn} \
] $fclk_clk0_port
set fclk_aresetn_port [create_bd_port -dir O -type rst fclk_aresetn]
set_property CONFIG.POLARITY ACTIVE_LOW $fclk_aresetn_port

# ── Clock + reset distribution (single domain, FCLK_CLK0 @ 100 MHz) ──────────
connect_bd_net \
    [get_bd_pins processing_system7_0/FCLK_CLK0] \
    [get_bd_pins rst_ps7_100M/slowest_sync_clk] \
    [get_bd_pins axi_smartconnect_gp0/aclk] \
    [get_bd_pins processing_system7_0/M_AXI_GP0_ACLK] \
    [get_bd_ports fclk_clk0]

connect_bd_net \
    [get_bd_pins processing_system7_0/FCLK_RESET0_N] \
    [get_bd_pins rst_ps7_100M/ext_reset_in]

connect_bd_net \
    [get_bd_pins rst_ps7_100M/peripheral_aresetn] \
    [get_bd_pins axi_smartconnect_gp0/aresetn] \
    [get_bd_ports fclk_aresetn]

# ── Address map — regbank at 0x4000_0000, 4 KB ───────────────────────────────
assign_bd_address -offset 0x40000000 -range 0x00001000 \
    -target_address_space [get_bd_addr_spaces processing_system7_0/Data] \
    [get_bd_addr_segs M_AXI_GP0/Reg]

# ── Validate & save ──────────────────────────────────────────────────────────
regenerate_bd_layout
validate_bd_design
save_bd_design

puts "INFO: classic_eye_bd build complete — PS7 + 1x1 GP0 smartconnect + proc_sys_reset, M_AXI_GP0/DDR/FIXED_IO/fclk_clk0/fclk_aresetn exposed."
