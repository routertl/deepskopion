# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: CERN-OHL-W-2.0
#
# PF-H0 (DS-P2.124) — Libero SmartDesign builder for the PolarFire SoC MSS
# fabric subsystem (D1, PF-P0 §3.1). Referenced from
# targets/pfsoc_datalogger.yml tcl_hooks.gen_project; the Zybo/DE25 targets keep
# their own hooks (this hook is ONLY on the PFSoC target).
#
# It builds a SmartDesign named `pfsoc_mss` that presents EXACTLY the fabric
# interface declared by the `component pfsoc_mss` in
# hw/rtl/pfsoc/deepskopion_pfsoc_fabric.vhd — one writer owns both, the DE25
# hps_subsys discipline. Every SmartDesign top port this file creates is named
# after a VHDL component port and connected to an MSS component pin;
# tools/tests/test_pfsoc_mss_builder_contract.py asserts the created-port set
# equals the component's port set, so a drift or a re-invention fails the suite.
#
# FIC AXI pin names/widths (DS-P2.152, closing the DS-P2.133 deferred defect):
# every FIC pin below is the EXACT name+width of the harvested disco_mss.cxz's
# `disco_mss.v` (unzip the .cxz and read the module ports — that file is the
# only authority). The earlier `FIC_0_AXI4_INITIATOR_*` / `FIC_1_AXI4_TARGET_*`
# 32-bit guesses were invented names no MSS pin carries, and the very first
# sd_connect_pins against one aborted the whole gen_project ("Parameter 'to' is
# missing" — Libero drops the unresolvable pin from the list and then sees a
# 1-element connect). Real shape: FIC0 initiator = `FIC_0_AXI4_M_*` (38-bit
# addr, 64-bit data, 8-bit id, QOS carried); FIC1 target = `FIC_1_AXI4_S_*`
# (38-bit addr, 64-bit data, 4-bit id). The RTL top carries the same widths and
# thins FIC0's 64-bit data to the 32-bit CSR bridge lane-wise.
#
# The MSS itself is the FROZEN configuration in hardware.mss
# (hw/libero/pfsoc/mss/disco_mss.cxz), imported here rather than reconfigured, so
# the DDR4 (MSS BANK 6) / SD (BANK 4) / MMUART0 console on dedicated MSS BANK 2
# pads (MSSIO_B2_B, DS-P2.133 revise) / MAC-SGMII (BANK 5, MDIO fabric-side) /
# USB pinmux is a reproducible board fact, not a per-build guess.
#
# VERIFY-AT-BRING-UP facts this builder deliberately does NOT hard-code, because
# they only come back from the generated MSS memory-map handoff (PF-P0
# §3.2/§3.5), never assumed:
#   * <FIC_BASE> — the CPU-side fabric window base Libero assigns to FIC0.
#   * the MSS_INT_F2M[bit] -> PLIC source numbers for the three fabric interrupts.
# PF-L0 reads them from the handoff and writes them into
# sw/linux/pfsoc/system_memory_map.yml + the dt_nodes.
#
# NB (PF-P0 §3.1/§8.3): the frozen .cxz was harvested (DS-P2.133) on the bench
# with the PolarFire SoC MSS Configurator for THIS board's part
# (MPFS095T-1FCSG325E: DDR4 x16 @ 800 MHz, speed grade -1) and frozen at
# hw/libero/pfsoc/mss/disco_mss.cxz — see that directory's README for the exact
# batch command and the FCSG325 consumption log. The completeness gate
# (test_pfsoc_frozen_mss_is_vendored) now binds the artifact's DIE + PACKAGE to
# the target part, so a wrong-package re-harvest fails the suite.

# ---------------------------------------------------------------------------
# Arguments injected by rr's Microchip gen_project.tcl dispatcher.
# ---------------------------------------------------------------------------
# $project_dir / $mss_cxz are provided by the rr Libero backend when it sources
# a tcl_hooks.gen_project script (see sdk/engine/supported_vendors.yml). Fall
# back to the declared path so the file is also runnable stand-alone in Libero.
if {![info exists mss_cxz]} {
    set mss_cxz "hw/libero/pfsoc/mss/disco_mss.cxz"
}

# ---------------------------------------------------------------------------
# 0. VHDL-2008 project mode (DS-P2.152). Libero analyzes VHDL as VHDL-93 by
# default and rr's microchip gen_project.tcl does not expose a language-
# standard knob (no rr_schema key exists for it — routertl gap, same pattern
# as the linux.* gaps documented in targets/pfsoc_datalogger.yml). The rr
# ddr_stream library and the datalogger sources are VHDL-2008 (`??`, matching
# operators), so without this the whole `rr` library fails analysis ("0
# definitions of operator" spam), ddr_stream_pkg never compiles, and synthesis
# dies at set_top_level with "'axi_h2c' is not declared / unable to find top
# level deepskopion_pfsoc_top" — measured on queue job a1eb52e59da2.
# ---------------------------------------------------------------------------
project_settings -vhdl_mode {VHDL_2008}


# rr_ddr_stream ships each rr_fifo_*_xpm_body entity TWICE (support/xilinx/
# real XPM body vs support/intel/ stub; the stub exists precisely for
# non-Xilinx flows — its header says so). Two consequences on Libero:
#   * the duplicate must be resolved to the stub (use_source_file; bare module
#     name — the `<module>::<lib>` form is rejected, probed live);
#   * Libero's auto synthesis fileset only carries HIERARCHY-REACHABLE modules,
#     and the stubs are referenced solely from the DORMANT (fifo_impl=olo)
#     GEN_XPM generates of rr_fifo_async_mixed/rr_fifo_sync — so Libero drops
#     them while Synplify's analyzer still requires the directly-instantiated
#     entity to be analyzed: CD590 "has not been analyzed", measured
#     synth4-20260919. Section 7 (at the END of this script, after
#     generate_component — the ordering matters, see its header) therefore
#     hands SYNTHESIZE the COMPLETE dependency-ordered fileset explicitly:
#     leaf libraries first (olo/en_tb/ctu_can_fd_rtl), then rr (uses olo),
#     then tich (uses rr+olo), then the work-library sources, each list in
#     resolver order. The Xilinx xpm bodies are excluded (`library xpm` does
#     not exist on Microchip).
build_design_hierarchy
foreach {stub module} {
    rr_fifo_async_mixed_xpm_body_stub.vhd rr_fifo_async_mixed_xpm_body
    rr_fifo_sync_xpm_body_stub.vhd        rr_fifo_sync_xpm_body
} {
    set stub_path [file join $g_project_dir hdl $stub]
    if {[file exists $stub_path]} {
        if {[catch { use_source_file -file $stub_path -module $module } use_err]} {
            puts "Warning: use_source_file $stub -> $module failed: $use_err"
        }
    }
}

# ---------------------------------------------------------------------------
# 1. Import the frozen MSS configuration and instantiate the MSS component.
# ---------------------------------------------------------------------------
# The .cxz carries the full MSS configuration (DDR4/SD/MMUART/MAC/USB + the two
# enabled FIC ports). import_mss_component materialises it as a design component
# named after the .cxz base name — `disco_mss`. (Libero SoC 2025.2's
# import_mss_component takes ONLY -file; it has no -name override, and the
# component name is fixed by the harvested .cxz. Verified DS-P2.133 by running
# the harvested disco_mss.cxz through this exact command on the bench Libero.)
import_mss_component \
    -file  $mss_cxz

# Idempotent re-run: rr re-invokes gen_project against a persistent Libero
# project, and create_smartdesign / create_and_configure_core HARD-fail on an
# existing component name (unlike import_mss_component, which overwrites with a
# warning). Drop any previous pass's components before re-creating them.
catch { delete_component -component_name {pfsoc_mss} }
catch { delete_component -component_name {pfsoc_ccc_100} }

create_smartdesign -sd_name {pfsoc_mss}

sd_instantiate_component \
    -sd_name {pfsoc_mss} \
    -component_name {disco_mss} \
    -instance_name {u_mss}

# ---------------------------------------------------------------------------
# Helper: create a SmartDesign top port named after the VHDL component port and
# wire it to the given MSS instance pin. width==1 -> scalar port; width>1 ->
# bus port [width-1:0]. This is the same create-port/connect-pins pattern the
# clock section uses, applied uniformly so no port is invented.
# ---------------------------------------------------------------------------
proc pf_wire {port dir width mss_pin} {
    if {$width == 1} {
        sd_create_scalar_port -sd_name {pfsoc_mss} -port_name $port -port_direction $dir
    } else {
        sd_create_bus_port -sd_name {pfsoc_mss} -port_name $port -port_direction $dir \
            -port_range "\[[expr {$width - 1}]:0\]"
    }
    sd_connect_pins -sd_name {pfsoc_mss} -pin_names [list $port "u_mss:$mss_pin"]
}

# ---------------------------------------------------------------------------
# 2. Fabric clock: on-board 50 MHz oscillator -> CCC -> 100 MHz clk_sys.
# ---------------------------------------------------------------------------
# clk_sys is kept at 100 MHz (PF-P0 §2) so the core's ZS/CAN/telemetry
# arithmetic is bit-identical to the Zynq leg (also 100 MHz; the DE25 leg runs
# the core at 50 MHz, so parity is with Zynq). The CCC's generated 100 MHz is the
# `clk_fabric_o` promoted below; the CCC also emits the SDC create_generated_clock
# the pfsoc_timing.sdc header relies on (that file constrains only the input
# clock + the async capture arcs).
# Parameter names are the PF_CCC spirit names (per-PLL/per-output suffixes:
# PLL 0, output GL0_0) — verbatim from the Discovery-Kit reference design's
# PF_CCC_C0.tcl export for this exact part. The earlier bare GL0_IS_USED /
# REF_CLK_0_FREQ guesses are not defined parameters and abort
# create_and_configure_core ("Parameter not found in the spirit definition").
create_and_configure_core \
    -core_vlnv {Actel:SgCore:PF_CCC:2.2.220} \
    -component_name {pfsoc_ccc_100} \
    -params [list \
        "PLL_IN_FREQ_0:50" \
        "GL0_0_IS_USED:true" \
        "GL0_0_OUT_FREQ:100" \
        "GL0_0_FABCLK_USED:true" \
    ]
sd_instantiate_component -sd_name {pfsoc_mss} -component_name {pfsoc_ccc_100} -instance_name {u_ccc}

# ref_clk_50mhz -> CCC ref, CCC GL0 -> clk_sys export + MSS fabric clock sinks.
sd_create_scalar_port  -sd_name {pfsoc_mss} -port_name {ref_clk_50mhz_i} -port_direction {IN}
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {ref_clk_50mhz_i u_ccc:REF_CLK_0}
sd_create_scalar_port  -sd_name {pfsoc_mss} -port_name {clk_fabric_o} -port_direction {OUT}
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {clk_fabric_o u_ccc:OUT0_FABCLK_0}
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {u_ccc:OUT0_FABCLK_0 u_mss:FIC_0_ACLK}
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {u_ccc:OUT0_FABCLK_0 u_mss:FIC_1_ACLK}
# DS-P1.60: clock FIC_2/FIC_3 like the reference design (CLOCKS_AND_RESETS drives
# all four FIC clocks from its CCC). The MSS config enables FIC_2's embedded DLL;
# tied to GND it can never lock, and the reference gates bring-up on MSS_DLL_LOCKS.
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {u_ccc:OUT0_FABCLK_0 u_mss:FIC_2_ACLK}
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {u_ccc:OUT0_FABCLK_0 u_mss:FIC_3_PCLK}

# MSS fabric reset export — the MSS -> fabric reset MSS_RESET_N_M2F (active-LOW,
# LOW while the MSS holds the fabric in reset). The top inverts + synchronizes
# its deassertion (deepskopion_pfsoc_fabric.vhd clock/reset section).
sd_create_scalar_port  -sd_name {pfsoc_mss} -port_name {mss_reset_n_o} -port_direction {OUT}
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {mss_reset_n_o u_mss:MSS_RESET_N_M2F}

# ---------------------------------------------------------------------------
# 3. FIC0 — MSS -> fabric AXI4 CSR initiator (38-bit addr / 64-bit data / 8 id).
# ---------------------------------------------------------------------------
# PolarFire FIC0 is a full AXI4 initiator (PF-P0 §3.2); every AXI channel pin is
# promoted as its VHDL-named fabric port and wired to the real FIC_0_AXI4_M_<AXI>
# MSS pin (names + widths verbatim from the harvested disco_mss.v — DS-P2.152).
# lwh2f_axilite_bridge in the top thins it to AXI-Lite for axil_demux3 (the top
# lane-selects the 64-bit WDATA/RDATA down to the 32-bit CSR). The <FIC_BASE>
# the MSS memory map assigns to FIC0 is read from the handoff.
# {vhdl_port direction width mss_pin_suffix}
foreach {port dir width suffix} {
    fic0_awid_o    OUT 8  AWID    fic0_awaddr_o  OUT 38 AWADDR  fic0_awlen_o   OUT 8  AWLEN
    fic0_awsize_o  OUT 3  AWSIZE  fic0_awburst_o OUT 2  AWBURST fic0_awlock_o  OUT 1  AWLOCK
    fic0_awcache_o OUT 4  AWCACHE fic0_awprot_o  OUT 3  AWPROT  fic0_awqos_o   OUT 4  AWQOS
    fic0_awvalid_o OUT 1  AWVALID fic0_awready_i IN  1  AWREADY fic0_wdata_o   OUT 64 WDATA
    fic0_wstrb_o   OUT 8  WSTRB   fic0_wlast_o   OUT 1  WLAST   fic0_wvalid_o  OUT 1  WVALID
    fic0_wready_i  IN  1  WREADY  fic0_bid_i     IN  8  BID     fic0_bresp_i   IN  2  BRESP
    fic0_bvalid_i  IN  1  BVALID  fic0_bready_o  OUT 1  BREADY  fic0_arid_o    OUT 8  ARID
    fic0_araddr_o  OUT 38 ARADDR  fic0_arlen_o   OUT 8  ARLEN   fic0_arsize_o  OUT 3  ARSIZE
    fic0_arburst_o OUT 2  ARBURST fic0_arlock_o  OUT 1  ARLOCK  fic0_arcache_o OUT 4  ARCACHE
    fic0_arprot_o  OUT 3  ARPROT  fic0_arqos_o   OUT 4  ARQOS   fic0_arvalid_o OUT 1  ARVALID
    fic0_arready_i IN  1  ARREADY fic0_rid_i     IN  8  RID     fic0_rdata_i   IN  64 RDATA
    fic0_rresp_i   IN  2  RRESP   fic0_rlast_i   IN  1  RLAST   fic0_rvalid_i  IN  1  RVALID
    fic0_rready_o  OUT 1  RREADY
} {
    pf_wire $port $dir $width "FIC_0_AXI4_M_$suffix"
}

# ---------------------------------------------------------------------------
# 4. FIC1 — fabric -> MSS DDR AXI4 target (64-bit native).
# ---------------------------------------------------------------------------
# The fabric ring DMA (ddr_stream_fic inside the core) is the master; the MSS is
# the AXI4 target. 64-bit data / 64-beat cap (PF-P0 §3.4). Ring lands at
# 0xA0000000 in MSS DDR4 (PF-P0 §3.3, carved below Linux's mem=512M). Pin names
# + widths verbatim from the harvested disco_mss.v (FIC_1_AXI4_S_*, 38-bit addr,
# 4-bit id — DS-P2.152).
foreach {port dir width suffix} {
    fic1_awid_i    IN  4  AWID    fic1_awaddr_i  IN  38 AWADDR  fic1_awlen_i   IN  8  AWLEN
    fic1_awsize_i  IN  3  AWSIZE  fic1_awburst_i IN  2  AWBURST fic1_awlock_i  IN  1  AWLOCK
    fic1_awcache_i IN  4  AWCACHE fic1_awprot_i  IN  3  AWPROT  fic1_awqos_i   IN  4  AWQOS
    fic1_awvalid_i IN  1  AWVALID fic1_awready_o OUT 1  AWREADY fic1_wdata_i   IN  64 WDATA
    fic1_wstrb_i   IN  8  WSTRB   fic1_wlast_i   IN  1  WLAST   fic1_wvalid_i  IN  1  WVALID
    fic1_wready_o  OUT 1  WREADY  fic1_bid_o     OUT 4  BID     fic1_bresp_o   OUT 2  BRESP
    fic1_bvalid_o  OUT 1  BVALID  fic1_bready_i  IN  1  BREADY  fic1_arid_i    IN  4  ARID
    fic1_araddr_i  IN  38 ARADDR  fic1_arlen_i   IN  8  ARLEN   fic1_arsize_i  IN  3  ARSIZE
    fic1_arburst_i IN  2  ARBURST fic1_arlock_i  IN  1  ARLOCK  fic1_arcache_i IN  4  ARCACHE
    fic1_arprot_i  IN  3  ARPROT  fic1_arqos_i   IN  4  ARQOS   fic1_arvalid_i IN  1  ARVALID
    fic1_arready_o OUT 1  ARREADY fic1_rid_o     OUT 4  RID     fic1_rdata_o   OUT 64 RDATA
    fic1_rresp_o   OUT 2  RRESP   fic1_rlast_o   OUT 1  RLAST   fic1_rvalid_o  OUT 1  RVALID
    fic1_rready_i  IN  1  RREADY
} {
    pf_wire $port $dir $width "FIC_1_AXI4_S_$suffix"
}

# ---------------------------------------------------------------------------
# 4a. DS-P2.126 — export the fabric-routed MSS peripherals (the EMIO analogue).
# The frozen .cxz already sets MMUART_1 / I2C_0 / SPI_0 / CAN_0 to FABRIC
# (MMUART_0 stays MSSIO_B2_B, the console pad). These used to be tied to
# constants, so Linux could never drive a logger. Directions are the
# SmartDesign top's: OUT leaves the MSS (M2F), IN is driven back (F2M).
# SPI_0_CLK_F2M / SPI_0_SS_F2M stay tied below — they are the slave-mode
# inputs, and SPI_0 is configured MASTER.
# ---------------------------------------------------------------------------
pf_wire mmuart1_txd_o OUT 1 MMUART_1_TXD_M2F
pf_wire mmuart1_rxd_i IN  1 MMUART_1_RXD_F2M
pf_wire i2c0_scl_oe_o OUT 1 I2C_0_SCL_OE_M2F
pf_wire i2c0_sda_oe_o OUT 1 I2C_0_SDA_OE_M2F
pf_wire i2c0_scl_i    IN  1 I2C_0_SCL_F2M
pf_wire i2c0_sda_i    IN  1 I2C_0_SDA_F2M
pf_wire spi0_clk_o    OUT 1 SPI_0_CLK_M2F
pf_wire spi0_clk_oe_o OUT 1 SPI_0_CLK_OE_M2F
pf_wire spi0_do_o     OUT 1 SPI_0_DO_M2F
pf_wire spi0_do_oe_o  OUT 1 SPI_0_DO_OE_M2F
pf_wire spi0_ss_o     OUT 1 SPI_0_SS1_M2F
pf_wire spi0_ss_oe_o  OUT 1 SPI_0_SS1_OE_M2F
pf_wire spi0_di_i     IN  1 SPI_0_DI_F2M
pf_wire can0_tx_o     OUT 1 CAN_0_TXBUS_M2F
pf_wire can0_rx_i     IN  1 CAN_0_RXBUS_F2M

# DS-P2.157: MAC_0 management (MDC/MDIO) is FABRIC in the .cxz (MAC_0_MANAGEMENT
# FABRIC, as in the reference). Tying MDI high left the VSC8221 PHY unreadable:
# on silicon macb reported "Could not attach PHY (-19)". Exported here; the top
# drives the mdc/mdio board pins (D15/D16) exactly as the reference's MSS_WRAPPER
# BIBUF does (D=MDO, E=MDO_OE, Y=MDI).
pf_wire mac0_mdc_o    OUT 1 MAC_0_MDC_M2F
pf_wire mac0_mdo_o    OUT 1 MAC_0_MDO_M2F
pf_wire mac0_mdo_oe_o OUT 1 MAC_0_MDO_OE_M2F
pf_wire mac0_mdi_i    IN  1 MAC_0_MDI_F2M

# ---------------------------------------------------------------------------
# 4b. Tie the unused MSS/CCC INPUT pins (SmartDesign DRC: an undriven input is
# an ERROR that fails generate_component; floating outputs and unconnected bus
# interfaces are only warnings). Each is tied at its electrically idle level so
# firmware that later enables the peripheral sees an inactive line, not an
# asserted one: UART/CAN RX, I2C, SPI SS, QSPI HOLD/WP and MDIO idle HIGH;
# SPI DI/CLK, GPIO and the unused FIC2/FIC3 clocks are LOW. MSS_RESET_N_F2M is
# the active-LOW fabric->MSS reset request — deasserted (VCC). The CCC's
# active-LOW PLL_POWERDOWN_N is deasserted (VCC) so the PLL always runs.
# ---------------------------------------------------------------------------
foreach {pin lvl} {
    u_ccc:PLL_POWERDOWN_N_0 VCC
    u_mss:SPI_0_SS_F2M      VCC
    u_mss:SPI_0_CLK_F2M     GND
    u_mss:GPIO_2_F2M_31     GND
    u_mss:GPIO_2_F2M_30     GND
    u_mss:GPIO_2_F2M_0        GND
    u_mss:GPIO_2_F2M_1        GND
    u_mss:GPIO_2_F2M_2        GND
    u_mss:GPIO_2_F2M_3        GND
    u_mss:GPIO_2_F2M_4        GND
    u_mss:GPIO_2_F2M_5        GND
    u_mss:GPIO_2_F2M_6        GND
    u_mss:GPIO_2_F2M_7        GND
    u_mss:GPIO_2_F2M_8        GND
    u_mss:GPIO_2_F2M_9        GND
    u_mss:GPIO_2_F2M_10       GND
    u_mss:GPIO_2_F2M_11       GND
    u_mss:GPIO_2_F2M_12       GND
    u_mss:GPIO_2_F2M_13       GND
    u_mss:GPIO_2_F2M_14       GND
    u_mss:GPIO_2_F2M_15       GND
    u_mss:GPIO_2_F2M_16       GND
    u_mss:GPIO_2_F2M_24       GND
    u_mss:GPIO_2_F2M_25       GND
    u_mss:GPIO_2_F2M_26       GND
    u_mss:GPIO_2_F2M_27       GND
    u_mss:GPIO_2_F2M_28       GND
    u_mss:MMUART_0_RXD_F2M    VCC
    u_mss:MSS_RESET_N_F2M   VCC
} {
    sd_connect_pins_to_constant -sd_name {pfsoc_mss} -pin_names $pin -value $lvl
}

# ---------------------------------------------------------------------------
# 5. F2M fabric interrupts -> PLIC.
# ---------------------------------------------------------------------------
# PolarFire F2M is the 64-bit MSS_INT_F2M group. The top drives {ring watermark,
# CTU A, CTU B} on its low three lines (bits [2:0]) and ties the rest inactive;
# the MSS_INT_F2M[bit] -> PLIC source numbers are read from the handoff, not set
# here (PF-P0 §3.5).
pf_wire mss_f2m_i IN 64 MSS_INT_F2M

# ---------------------------------------------------------------------------
# 6. Finalise.
# ---------------------------------------------------------------------------
# save_smartdesign (the reference design's idiom) — `sd_save_core` is not a
# Libero SoC 2025.2 command and aborts the script.
save_smartdesign   -sd_name {pfsoc_mss}
generate_component -component_name {pfsoc_mss}

# ---------------------------------------------------------------------------
# 7. Pin the SYNTHESIZE fileset — AFTER generate_component, deliberately.
# The component/work/** globs below match files that generate_component just
# wrote; running this block earlier (the refused delivery's ordering) finds
# NOTHING on a clean build dir, so the pinned fileset silently omits the
# generated pfsoc_mss sources, Synplify blackboxes pfsoc_mss, and Libero's
# post-synthesis compile dies with "Module or UDP: (pfsoc_mss) is not
# defined" (read_verilog) — reproduced on a fresh `rm -rf build` run,
# 20260920. The prior placement only ever worked against a stale project
# whose components survived from an earlier pass.
# ---------------------------------------------------------------------------
if {[info exists g_libraries] && [info exists syn_files]} {
    set pf_ordered {}
    set pf_seen [dict create]
    # The pinned fileset REPLACES Libero's auto list, so the SmartDesign-
    # generated component sources must be carried explicitly too — without
    # them the post-synthesis netlist link dies with "Module or UDP:
    # (pfsoc_mss) is not defined" (synth7-20260919). Order: leaves first
    # (MSS blackbox syn_comps -> disco_mss wrapper -> CCC -> pfsoc_mss).
    foreach comp_pat {
        component/work/disco_mss/*_syn_comps.v
        component/work/disco_mss/disco_mss.v
        component/work/pfsoc_ccc_100/*/*.v
        component/work/pfsoc_ccc_100/pfsoc_ccc_100.v
        component/work/pfsoc_mss/pfsoc_mss.vhd
    } {
        foreach pf [lsort [glob -nocomplain [file join $g_project_dir $comp_pat]]] {
            if {![dict exists $pf_seen $pf]} {
                dict set pf_seen $pf 1
                lappend pf_ordered $pf
            }
        }
    }
    # DS-P2.128: rr_rea (the vendored upstream REA behind the MMIO door) is a
    # leaf library; without it here its files never reach SYNTHESIZE and
    # Synplify reports CD592 "rr_rea.rr_rea_axi4lite has not been analyzed".
    foreach lib {olo en_tb ctu_can_fd_rtl rr rr_rea tich} {
        if {![dict exists $g_libraries $lib]} { continue }
        foreach f [dict get $g_libraries $lib] {
            if {[string match "*support/xilinx/*" $f]} { continue }
            # Xilinx-primitive shim: `use unisim.vcomponents.BSCANE2` cannot
            # resolve here ("'bscane2' is not declared", synth6-20260919) and
            # nothing in the PFSoC closure instantiates rr_rea_xilinx7 — the
            # altera flow skips Xilinx-primitive files the same way (RTL-P3.789).
            if {[string match "*rr_rea_jtag_xilinx7.vhd" $f]} { continue }
            if {[string tolower [file extension $f]] ni {.vhd .vhdl .v .sv}} { continue }
            set pf [file join $g_project_dir hdl [file tail $f]]
            if {[file exists $pf] && ![dict exists $pf_seen $pf]} {
                dict set pf_seen $pf 1
                lappend pf_ordered $pf
            }
        }
    }
    # Everything else in the synthesis closure belongs to `work` (the library
    # loop above already placed the library-owned files).
    set pf_libfiles [dict create]
    dict for {lib lf} $g_libraries {
        foreach f $lf { dict set pf_libfiles [file tail $f] 1 }
    }
    foreach f $syn_files {
        if {[string tolower [file extension $f]] ni {.vhd .vhdl .v .sv}} { continue }
        if {[dict exists $pf_libfiles [file tail $f]]} { continue }
        set pf [file join $g_project_dir hdl [file tail $f]]
        if {[file exists $pf] && ![dict exists $pf_seen $pf]} {
            dict set pf_seen $pf 1
            lappend pf_ordered $pf
        }
    }
    set pf_cmd [list organize_tool_files -tool {SYNTHESIZE}]
    foreach pf $pf_ordered { lappend pf_cmd -file $pf }
    lappend pf_cmd -module {deepskopion_pfsoc_top::work} -input_type {source}
    if {[catch { eval $pf_cmd } org_err]} {
        puts "Warning: organize_tool_files (SYNTHESIZE fileset) failed: $org_err"
    } else {
        puts "INFO: organize_tool_files pinned [llength $pf_ordered] synthesis sources"
    }
}
