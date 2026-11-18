###############################################################################
# deepskopion_bd.tcl — Zynq-7 PS subsystem (BD-creation hook).
#
# Sourced in-session by routertl/tcl/xilinx/gen_project.tcl via the
# project.yml tcl_hooks.gen_project list (canonical form post-
# RTL-P3.506; gen_bd.tcl was retired). The hook is classified as a
# BD hook (it contains create_bd_cell), runs through the N-pass
# healer + VLNV-upgrade pre-pass, and saves into Vivado's project.
#
# This BD is a pure PS subsystem — PS7, two AXI smartconnects, a
# proc_sys_reset, all wired internally. It exposes its three useful
# surfaces to the parent RTL:
#
#   - DDR_* / FIXED_IO_*       (PS pins, inout, propagate from PS7)
#   - M_AXI_GP0  (master)      AXI-Lite, post-smartconnect
#   - S_AXI_HP0  (slave)       AXI4, pre-smartconnect, 128-bit
#   - fclk_clk0  (clock out)   100 MHz fabric clock
#   - fclk_aresetn (reset out) active-low, synchronized to fclk_clk0
#
# The user-authored top — hw/rtl/deepskopion_top.vhd — declares this
# BD as a VHDL `component` and instantiates it alongside datalogger_gather,
# rr-ddr-stream, and the regbank. Same architectural pattern as the
# linux_zybo reference (stereoVision_top.vhd line 90+ declares
# component StereoVisionBD, line 329 instantiates it).
#
# Topology (BD-internal):
#
#                                          ┌─────────────────────┐
#                                          │  axi_smartconnect_  │  M00_AXI
#                            M_AXI_GP0 ───▶│  gp0 (1×1)          │─────────▶ M_AXI_GP0
#                                          │                     │           (external port)
#   ┌──────────┐                           └─────────────────────┘
#   │          │
#   │   PS7    │                           ┌─────────────────────┐
#   │  (Zynq   │ S_AXI_HP0 ◀──── M00_AXI ──│  axi_smartconnect_  │ S00_AXI
#   │  -7020)  │                           │  hp0 (1×1)          │◀──────── S_AXI_HP0
#   │          │                           │                     │           (external port)
#   │          │ FCLK_CLK0 ──┬───────────────────────────────────────────▶  fclk_clk0
#   │          │ FCLK_RESET0_N ─▶ rst_ps7_100M ──▶ peripheral_aresetn ───▶  fclk_aresetn
#   └──────────┘                                                          (external port)
#
# DS-P2.33. PS7 property dict (Zybo Z7-20) lives verbatim in the
# sibling zybo_z7_ps7_config.tcl — board-file-derived, no project-
# specific content; sourced below. Deepskopion-specific deltas
# (HP2 off, fabric IRQ off) follow.
###############################################################################

puts "INFO: Building deepskopion_bd (Zynq-7 PS subsystem)..."

# ── BD design ──────────────────────────────────────────────────────────────
# The hook owns BD creation directly — routertl's gen_project.tcl
# doesn't pre-create the design (post-RTL-P3.506 the design name comes
# from the hook itself, not a project.yml hardware.bd_name field).
create_bd_design deepskopion_bd
current_bd_design [get_bd_designs deepskopion_bd]

# ── Processing System 7 ────────────────────────────────────────────────────
set processing_system7_0 [create_bd_cell -type ip \
    -vlnv xilinx.com:ip:processing_system7:5.5 processing_system7_0]

# PS7 PCW configuration is owned by routertl (RTL-P2.618):
#   - Board preset (canonical, generic for any Z7-20 project) lives at
#     routertl/boards/zybo-z7-20/ps7_preset.tcl. Resolved from
#     hardware.platform: zybo-z7-20 in project.yml. Auto-applied by
#     gen_project.tcl AFTER this BD hook returns.
#   - Project-feature deltas (HP2 off, fabric IRQ off, future EMIO masters
#     for DS-P2.51) live in hardware.ps7_hooks in project.yml — small
#     focused TCL files at hw/bd/ds_*.tcl. Also auto-applied by
#     gen_project.tcl, in declared order, after the board preset.
#
# Nothing for this BD hook to do for PS7 PCW configuration — it just
# creates the cell. The hook chain handles the rest.

# ── Smart Connects ─────────────────────────────────────────────────────────
# PS GP0 master → external M_AXI_GP0 port (regbank slave is in user RTL)
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:smartconnect:1.0 axi_smartconnect_gp0
set_property -dict [list \
    CONFIG.NUM_SI {1} \
    CONFIG.NUM_MI {1} \
] [get_bd_cells axi_smartconnect_gp0]

# External S_AXI_HP0 port (rr-ddr-stream master is in user RTL) → PS HP0 slave
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:smartconnect:1.0 axi_smartconnect_hp0
set_property -dict [list \
    CONFIG.NUM_SI {1} \
    CONFIG.NUM_MI {1} \
] [get_bd_cells axi_smartconnect_hp0]

# ── PS clock topology overrides ────────────────────────────────────────────
# Two-clock topology: FCLK_CLK0 @ 100 MHz drives the AXI-Lite (M_AXI_GP0)
# + system fabric clock; FCLK_CLK1 @ 125 MHz drives the high-throughput
# AXI4 path (S_AXI_HP0 + smartconnect_hp0). HP0 carries the rr-ddr-stream
# DMA writes into PS DDR — running it faster than the AXI-Lite raises
# the burst-write ceiling. 125 MHz (not 150) because Zynq-7 PLLs don't
# reliably lock to exactly 150 MHz from the typical Zybo PS clock plan.
# The smartconnect handles the async crossing at its M00↔S_AXI_HP0
# boundary natively.
#
# Note: as of RTL-P3.557 (routertl ac3900e2 — create_bd_cell wrapper),
# the board preset + ps7_hooks auto-apply at PS-cell instantiation
# above. So PCW_USE_S_AXI_HP0 / PCW_EN_CLK0_PORT / PCW_USE_M_AXI_GP0 /
# PCW_EN_RST0_PORT are already on by the time we reach this set_property
# block. The board preset also handles HP1/HP3 (HP2 explicitly off via
# ds_hp2_off.tcl ps7_hook). We only need to express the project-specific
# DELTAS that the preset doesn't cover: fabric clock frequencies (FPGA0
# = AXI-Lite 100 MHz, FPGA1 = HP0 DMA 125 MHz) + enabling FCLK1/RESET1
# for the second clock domain.
set_property -dict [list \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100} \
    CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ {125} \
    CONFIG.PCW_EN_CLK1_PORT             {1} \
    CONFIG.PCW_EN_RST1_PORT             {1} \
    CONFIG.PCW_USE_M_AXI_GP1            {1} \
] $processing_system7_0

# ── Resets ─────────────────────────────────────────────────────────────────
# One proc_sys_reset per clock domain.
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_ps7_100M
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_ps7_125M

# ── Internal AXI Connections ───────────────────────────────────────────────
connect_bd_intf_net \
    [get_bd_intf_pins processing_system7_0/M_AXI_GP0] \
    [get_bd_intf_pins axi_smartconnect_gp0/S00_AXI]

connect_bd_intf_net \
    [get_bd_intf_pins axi_smartconnect_hp0/M00_AXI] \
    [get_bd_intf_pins processing_system7_0/S_AXI_HP0]

# ── External Interface Ports ───────────────────────────────────────────────
# PS dedicated pins (DDR + FIXED_IO) — connect to physical balls. Vivado
# requires these be explicitly made external; the BD wrapper exposes them
# to the user-authored deepskopion_top, which propagates them straight to
# the synth top's port surface (matches linux_zybo's stereoVision_top).
make_bd_intf_pins_external [get_bd_intf_pins processing_system7_0/DDR]
make_bd_intf_pins_external [get_bd_intf_pins processing_system7_0/FIXED_IO]
# Rename to drop the `_0` suffix Vivado appends so the component decl
# in deepskopion_top can use the natural DDR / FIXED_IO interface names.
set_property name DDR      [get_bd_intf_ports DDR_0]
set_property name FIXED_IO [get_bd_intf_ports FIXED_IO_0]

# AXI-Lite master out (drives regbank slave in user RTL).
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

# AXI4 slave in (accepts rr-ddr-stream master in user RTL).
set s_axi_hp0_port [create_bd_intf_port -mode Slave \
    -vlnv xilinx.com:interface:aximm_rtl:1.0 S_AXI_HP0]
set_property -dict [list \
    CONFIG.PROTOCOL    {AXI4} \
    CONFIG.ADDR_WIDTH  {32} \
    CONFIG.DATA_WIDTH  {128} \
    CONFIG.ID_WIDTH    {2} \
    CONFIG.HAS_BURST   {1} \
    CONFIG.HAS_LOCK    {1} \
    CONFIG.HAS_CACHE   {1} \
    CONFIG.HAS_QOS     {1} \
] $s_axi_hp0_port
connect_bd_intf_net $s_axi_hp0_port \
    [get_bd_intf_pins axi_smartconnect_hp0/S00_AXI]

# ── DS-P3.109: M_AXI_GP1 → 2× AXI4-Lite→APB bridge → CTU CAN FD register slaves ─
# A 2nd GP master drives the two CTU CAN FD cores (FD traffic source). GP1 →
# 1×2 smartconnect → 2× axi_apb_bridge; each bridge's APB master is exposed as an
# external port that deepskopion_top wires to a CTU can_top_apb s_apb_* slave.
# (IRQ_F2P for the cores is a follow-up build — connecting the APB to this real PS
# master already keeps the cores un-prunable, so this build gives true fit/timing.)
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:smartconnect:1.0 axi_smartconnect_gp1
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {2}] \
    [get_bd_cells axi_smartconnect_gp1]

create_bd_cell -type ip \
    -vlnv xilinx.com:ip:axi_apb_bridge:3.0 axi_apb_bridge_canfd0
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:axi_apb_bridge:3.0 axi_apb_bridge_canfd1
set_property CONFIG.C_APB_NUM_SLAVES {1} [get_bd_cells axi_apb_bridge_canfd0]
set_property CONFIG.C_APB_NUM_SLAVES {1} [get_bd_cells axi_apb_bridge_canfd1]

connect_bd_intf_net \
    [get_bd_intf_pins processing_system7_0/M_AXI_GP1] \
    [get_bd_intf_pins axi_smartconnect_gp1/S00_AXI]
connect_bd_intf_net \
    [get_bd_intf_pins axi_smartconnect_gp1/M00_AXI] \
    [get_bd_intf_pins axi_apb_bridge_canfd0/AXI4_LITE]
connect_bd_intf_net \
    [get_bd_intf_pins axi_smartconnect_gp1/M01_AXI] \
    [get_bd_intf_pins axi_apb_bridge_canfd1/AXI4_LITE]

# Expose each bridge's APB master as an external port (→ CTU s_apb_* in top RTL).
make_bd_intf_pins_external [get_bd_intf_pins axi_apb_bridge_canfd0/APB_M]
make_bd_intf_pins_external [get_bd_intf_pins axi_apb_bridge_canfd1/APB_M]
set_property name CANFD0_APB [get_bd_intf_ports APB_M_0]
set_property name CANFD1_APB [get_bd_intf_ports APB_M_1]

# Clock + reset out ports. fclk_clk0 is the AXI-Lite + system fabric
# clock (100 MHz); fclk_clk1 is the HP0 DMA-side clock (150 MHz). Each
# has its own active-low reset, synchronous to its own clock.
set fclk_clk0_port [create_bd_port -dir O -type clk fclk_clk0]
set_property -dict [list \
    CONFIG.FREQ_HZ           {100000000} \
    CONFIG.ASSOCIATED_BUSIF  {M_AXI_GP0} \
    CONFIG.ASSOCIATED_RESET  {fclk_aresetn} \
] $fclk_clk0_port
set fclk_aresetn_port [create_bd_port -dir O -type rst fclk_aresetn]
set_property CONFIG.POLARITY ACTIVE_LOW $fclk_aresetn_port

set fclk_clk1_port [create_bd_port -dir O -type clk fclk_clk1]
set_property -dict [list \
    CONFIG.FREQ_HZ           {125000000} \
    CONFIG.ASSOCIATED_BUSIF  {S_AXI_HP0} \
    CONFIG.ASSOCIATED_RESET  {fclk_clk1_aresetn} \
] $fclk_clk1_port
set fclk_clk1_aresetn_port [create_bd_port -dir O -type rst fclk_clk1_aresetn]
set_property CONFIG.POLARITY ACTIVE_LOW $fclk_clk1_aresetn_port

# ── Clock + Reset Distribution ─────────────────────────────────────────────
# Domain 0 — FCLK_CLK0 @ 100 MHz: AXI-Lite GP0 path + system fabric.
connect_bd_net \
    [get_bd_pins processing_system7_0/FCLK_CLK0] \
    [get_bd_pins rst_ps7_100M/slowest_sync_clk] \
    [get_bd_pins axi_smartconnect_gp0/aclk] \
    [get_bd_pins processing_system7_0/M_AXI_GP0_ACLK] \
    [get_bd_pins processing_system7_0/M_AXI_GP1_ACLK] \
    [get_bd_pins axi_smartconnect_gp1/aclk] \
    [get_bd_pins axi_apb_bridge_canfd0/s_axi_aclk] \
    [get_bd_pins axi_apb_bridge_canfd1/s_axi_aclk] \
    [get_bd_ports fclk_clk0]

connect_bd_net \
    [get_bd_pins processing_system7_0/FCLK_RESET0_N] \
    [get_bd_pins rst_ps7_100M/ext_reset_in]

connect_bd_net \
    [get_bd_pins rst_ps7_100M/peripheral_aresetn] \
    [get_bd_pins axi_smartconnect_gp0/aresetn] \
    [get_bd_pins axi_smartconnect_gp1/aresetn] \
    [get_bd_pins axi_apb_bridge_canfd0/s_axi_aresetn] \
    [get_bd_pins axi_apb_bridge_canfd1/s_axi_aresetn] \
    [get_bd_ports fclk_aresetn]

# Domain 1 — FCLK_CLK1 @ 150 MHz: HP0 DMA path (rr-ddr-stream master).
connect_bd_net \
    [get_bd_pins processing_system7_0/FCLK_CLK1] \
    [get_bd_pins rst_ps7_125M/slowest_sync_clk] \
    [get_bd_pins axi_smartconnect_hp0/aclk] \
    [get_bd_pins processing_system7_0/S_AXI_HP0_ACLK] \
    [get_bd_ports fclk_clk1]

connect_bd_net \
    [get_bd_pins processing_system7_0/FCLK_RESET1_N] \
    [get_bd_pins rst_ps7_125M/ext_reset_in]

connect_bd_net \
    [get_bd_pins rst_ps7_125M/peripheral_aresetn] \
    [get_bd_pins axi_smartconnect_hp0/aresetn] \
    [get_bd_ports fclk_clk1_aresetn]

# ── EMIO pins (DS-P2.51 Phase 1) ───────────────────────────────────────────
# The ds_emio_uart_i2c_spi.tcl hook (project.yml hardware.ps7_hooks)
# enables PS UART0 / I2C0 / SPI0 on EMIO. Expose their fabric-side
# pins to the BD wrapper here so deepskopion_top can wire them into
# the datalogger input matrix.
#
# We flatten every signal — including I2C and SPI tri-state triplets —
# rather than going through interface pins, because deepskopion_top
# models the open-drain busses internally and wants plain std_logic
# on each EMIO leg.

# UART0 — push-pull, 1 bit each. Linux drives /dev/ttyPS1.
foreach pin_name {UART0_TX UART0_RX} {
    make_bd_pins_external \
        -name $pin_name \
        [get_bd_pins processing_system7_0/$pin_name]
}

# I2C0 — open-drain bus. Triplets per wire (_I=in, _O=out, _T=tri-state-en).
#        T='0' means "drive the line", T='1' means "release / Hi-Z".
#        Linux drives /dev/i2c-0.
foreach pin_name {I2C0_SCL_I I2C0_SCL_O I2C0_SCL_T I2C0_SDA_I I2C0_SDA_O I2C0_SDA_T} {
    make_bd_pins_external \
        -name $pin_name \
        [get_bd_pins processing_system7_0/$pin_name]
}

# SPI0 — push-pull when master, but PS7 exposes triplets anyway for the
#        slave-mode case. We use master mode in Linux (/dev/spidev0).
#        SS line is single-slave (SS); the multi-slave SS1/SS2 outputs
#        aren't exposed (master only ever asserts one select in our model).
foreach pin_name {
    SPI0_SCLK_I SPI0_SCLK_O SPI0_SCLK_T
    SPI0_MOSI_I SPI0_MOSI_O SPI0_MOSI_T
    SPI0_MISO_I SPI0_MISO_O SPI0_MISO_T
    SPI0_SS_I   SPI0_SS_O   SPI0_SS_T
} {
    make_bd_pins_external \
        -name $pin_name \
        [get_bd_pins processing_system7_0/$pin_name]
}

# EMIO GPIO (DS-T2.16) — width-1 bank; bit 0 is the fabric-driven SPI
# chip-select that replaces the dead native SPI0_SS_O. Linux drives it via
# cs-gpios on the SPI node (PS GPIO line 54 = EMIO bit 0). Flattened O/I/T
# like the other EMIO legs; GPIO_O(0) is the CS we use, GPIO_I/GPIO_T are
# tied off in deepskopion_top.
foreach pin_name {GPIO_O GPIO_I GPIO_T} {
    make_bd_pins_external \
        -name $pin_name \
        [get_bd_pins processing_system7_0/$pin_name]
}

# CAN0 / CAN1 (DS-P3.102) — digital pre-transceiver CAN on EMIO. Enabled by the
# ds_emio_can.tcl ps7_hook (PCW_CANx_PERIPHERAL_ENABLE + _IO {EMIO} +
# EN_EMIO_CANx), already applied at the create_bd_cell above, so the pins exist
# here. PHY_TX = controller out, PHY_RX = controller in (single bit each, like
# UART0). deepskopion_top ANDs the two TX legs into one fabric CAN bus and feeds
# it back to both RX legs + the passive can_datalogger (id 20). Linux drives them as
# SocketCAN can0/can1 (xilinx_can; project.yml linux.dt_nodes).
foreach pin_name {CAN0_PHY_TX CAN0_PHY_RX CAN1_PHY_TX CAN1_PHY_RX} {
    make_bd_pins_external \
        -name $pin_name \
        [get_bd_pins processing_system7_0/$pin_name]
}

# IRQ_F2P (DS-P3.60 / DS-P3.109) — 3-bit PL→PS fabric interrupt bus.
# Enabled by the ds_irq_f2p_on.tcl ps7_hook (PCW_USE_FABRIC_INTERRUPT /
# PCW_IRQ_F2P_INTR), applied at the create_bd_cell above (RTL-P3.557).
#   [0] ring-watermark alarm  → GIC SPI #29
#   [1] CTU CAN FD core A irq  → GIC SPI #30 (can2)
#   [2] CTU CAN FD core B irq  → GIC SPI #31 (can3)
# On Zynq-7 the PS7 IRQ_F2P width is NOT a PCW knob (PCW_NUM_F2P_INTR_INPUTS is a
# ZynqMP/PSU property), and it does NOT auto-size to a directly-connected external
# port — validated empirically: a 3-bit external port left PS7 IRQ_F2P at [0:0]
# (bits [2:1] silently dropped) even after validate_bd_design. The PS7 widens its
# IRQ_F2P ONLY by parameter-propagation from an xlconcat dout. So drive it with a
# width-3 concat; the external IRQ_F2P stays a single 3-bit port (deepskopion_top
# unchanged), and the concat propagates 3 onto the PS7 pin → all of [2:0] reach
# the GIC. Bit map: [0] ring alarm (#29), [1] CTU A (#30), [2] CTU B (#31).
set irq_f2p_concat [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 irq_f2p_concat]
set_property -dict [list CONFIG.NUM_PORTS {1} CONFIG.IN0_WIDTH {3}] $irq_f2p_concat
connect_bd_net [get_bd_pins $irq_f2p_concat/dout] [get_bd_pins processing_system7_0/IRQ_F2P]
create_bd_port -dir I -from 2 -to 0 IRQ_F2P
connect_bd_net [get_bd_ports IRQ_F2P] [get_bd_pins $irq_f2p_concat/In0]

# ── Address Map ────────────────────────────────────────────────────────────
# PS GP0 → external M_AXI_GP0 region — regbank lives at 0x4000_0000, 4 KB.
# Vivado auto-creates a "Reg" segment on the external master port.
assign_bd_address -offset 0x40000000 -range 0x00001000 \
    -target_address_space [get_bd_addr_spaces processing_system7_0/Data] \
    [get_bd_addr_segs M_AXI_GP0/Reg]

# External master via S_AXI_HP0 → PS DDR. Auto-assign uses HP0_DDR_LOWOCM
# (covers the full PS DDR window the HP0 slave reaches).
assign_bd_address \
    -target_address_space [get_bd_addr_spaces S_AXI_HP0] \
    [get_bd_addr_segs processing_system7_0/S_AXI_HP0/HP0_DDR_LOWOCM]

# DS-P3.109: the 2 CTU CAN FD register windows (64 KB each, the CTU core's register
# map size). These addresses become the ctucanfd DT `reg`.
#
# Address space: the CTU cores hang off M_AXI_GP1, whose Zynq-7 aperture into the
# PS Data space is the 1 GB window based at 0x8000_0000 (GP0 owns 0x4000_0000–
# 0x7FFF_FFFF; the conventional 0x43C0_0000 PL region is a GP0 address and does NOT
# fit GP1 — that tripped BD 41-1075 "must fit an available aperture {0x8000_0000
# [1G]}"). So the FD cores live at 0x8000_0000 / 0x8001_0000.
#
# The APB_M masters were externalized (lines 207-210) and renamed CANFD0/1_APB, so
# the downstream slave segment the PS must reach lives on the EXTERNAL port:
# /CANFD0_APB/Reg and /CANFD1_APB/Reg (NOT axi_apb_bridge_*/AXI4_LITE/Reg, which
# does not exist once the master is external — that empties get_bd_addr_segs and
# trips BD 5-432 "a peripheral must be specified").
assign_bd_address -offset 0x80000000 -range 0x00010000 \
    -target_address_space [get_bd_addr_spaces processing_system7_0/Data] \
    [get_bd_addr_segs CANFD0_APB/Reg]
assign_bd_address -offset 0x80010000 -range 0x00010000 \
    -target_address_space [get_bd_addr_spaces processing_system7_0/Data] \
    [get_bd_addr_segs CANFD1_APB/Reg]

# ── Validate & Save ────────────────────────────────────────────────────────
regenerate_bd_layout
validate_bd_design
save_bd_design

puts "INFO: deepskopion_bd build complete."
