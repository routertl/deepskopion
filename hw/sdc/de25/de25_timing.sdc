# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: CERN-OHL-W-2.0
# DS-P2.155: CLOCK2_50 (50 MHz) is now the REFERENCE for the fabric IOPLL
# (ip/de25_fabric_pll/de25_fabric_pll.ip), which produces the 100 MHz clk_sys
# that clocks the whole fabric AND the hps_subsys fabric bridges. clk_sys and
# clk_ddr are both that 100 MHz product (10 ns). derive_pll_clocks pulls in the
# altera_iopll-generated create_generated_clock on outclk_0 (the .ip ships its
# own SDC, gui_skip_sdc_generation=false), so the fabric datapath is analysed at
# the real 100 MHz — not the 50 MHz reference. Do not use set_clock_groups
# -asynchronous here: a later split DDR clock must restore the bounded two-way
# datapath-only constraints rather than hiding every crossing.
# Generated IOPLL timing may already put a clock on CLOCK2_50 before this file
# is read. Query the port's clock collection so we create the board clock only
# when it is genuinely absent; an unconditional duplicate is an ignored
# constraint (332049), not harmless documentation.
set _ds_ref_port [get_ports -nowarn {CLOCK2_50}]
set _ds_ref_clocks [get_clocks -nowarn -of_objects $_ds_ref_port]
if {[get_collection_size $_ds_ref_clocks] == 0} {
    create_clock -name CLOCK2_50 -period 20.000 $_ds_ref_port
}
derive_pll_clocks
derive_clock_uncertainty

# External capture pins enter the first synchronizer stage in the portable core.
# Only the async source-to-first-flop arc is false; all subsequent logic stays
# timed. Quartus get_registers is used instead of Vivado get_cells syntax.
set _ds_spi_meta [get_registers -nowarn {*|u_core|spi_edge_meta_r[*] *|u_core|spi_edge_*_sync_r[0]}]
if {[get_collection_size $_ds_spi_meta] > 0} {
    set_false_path -to $_ds_spi_meta
}
set _ds_i2c_meta [get_registers -nowarn {*|u_core|i2c_edge_meta_r[*] *|u_core|i2c_edge_*_sync_r[0]}]
if {[get_collection_size $_ds_i2c_meta] > 0} {
    set_false_path -to $_ds_i2c_meta
}
set _ds_uart_meta [get_registers -nowarn {*|u_core|uart_edge_meta_r}]
if {[get_collection_size $_ds_uart_meta] > 0} {
    set_false_path -to $_ds_uart_meta
}
set _ds_can_meta [get_registers -nowarn {*|u_core|can_edge_meta_r[*] *|u_core|can_edge_*_sync_r[0]}]
if {[get_collection_size $_ds_can_meta] > 0} {
    set_false_path -to $_ds_can_meta
}
set _ds_emio_probe_meta [get_registers -nowarn {*|u_core|emio_probe_sync_r[*][0]}]
if {[get_collection_size $_ds_emio_probe_meta] > 0} {
    set_false_path -to $_ds_emio_probe_meta
}

# DS-T1.19: REA JTAG clock (altera_reserved_tck) declaration and REA s1
# synchronizer net delay bounds are package-owned by routertl/rea's
# libs/routertl/rea/constraints/rr_rea_scoped.sdc.
# Only the project-specific HPS JTAG-to-Avalon master CDC false paths remain here.
#
# ORDER-INDEPENDENCE (DS-T1.19, proven on job 387f024a415d): rr_rea_scoped.sdc
# is a g_package_sdc and is EMITTED AFTER this project SDC — the build-log read
# order is master.sdc -> de25_timing.sdc -> rr_rea_scoped.sdc. So
# altera_reserved_tck does NOT yet exist when this file is read. The previous
# revision guarded the whole block on `[get_clocks altera_reserved_tck]` being
# non-empty; under the package-owned clock that guard is ALWAYS false, the block
# was silently skipped, and the 9 setup + 5 recovery clk_sys<->tck HPS
# sink_crosser crossings were left uncut (setup WNS -2.633, recovery -2.695).
# These false paths are therefore expressed WITHOUT any dependency on the
# altera_reserved_tck clock object, so the SDC read order cannot skip them:
#  - clk_sys (u_fabric_pll) IS created earlier (its IOPLL SDC is read first,
#    build-log line 148), so the clk_sys->tck direction keeps its directional
#    `-from $_ds_sys_clk`.
#  - the tck->clk_sys direction is cut by DESTINATION register set only (no
#    `-from` clock): cutting into these dedicated Intel synchronizer / reset-sync
#    keepers is the standard async-CDC treatment and needs no clock object.

# DS-P1.71/DS-T1.19: Intel HPS JTAG-to-Avalon master
# (hps_subsys f2s_term_master, jtag_phy_embedded_in_jtag_master) CDC structures:
# the sink_crosser handshake (in_data_buffer captured by out_data_buffer only
# after the toggle passes in_to_out_synchronizer) and the
# altera_reset_synchronizer / clock_sense_reset_n chains (async-assert reset
# synchronizers, recovery). Intel's own CDC structures built for unrelated
# clocks, so only the crossings that land in that instance are cut, by instance
# name; REA's own crossings stay bounded in rr_rea_scoped.sdc and every
# same-clock path stays timed.
set _ds_jp {*|jtag_phy_embedded_in_jtag_master|normal.jtag_dc_streaming|}

# clk_sys -> tck: handshake capture, its toggle synchronizer, and the tck-side
# reset synchronizer (async assert -> recovery). clk_sys exists at read time.
set _ds_sys_clk [get_clocks -nowarn {u_fabric_pll|de25_fabric_pll_outclk0}]
set _ds_to_tck [get_registers -nowarn [list \
    "${_ds_jp}sink_crosser|out_data_buffer*" \
    "${_ds_jp}sink_crosser|in_to_out_synchronizer|din_s1" \
    "${_ds_jp}synchronizer|dreg*" \
    "${_ds_jp}sink_crosser|alt_rst_req_sync_out_rst|*"]]
if {[get_collection_size $_ds_to_tck] > 0 && [get_collection_size $_ds_sys_clk] > 0} {
    set_false_path -from $_ds_sys_clk -to $_ds_to_tck
} else {
    # Fail loud, never silently skip (this is the exact regression this revision
    # fixes): if the HPS instance or clk_sys is absent the crossings would be
    # left timed.
    post_message -type critical_warning "de25_timing.sdc (DS-T1.19): clk_sys->tck HPS JTAG CDC false path NOT applied (sink_crosser regs=[get_collection_size $_ds_to_tck], clk_sys=[get_collection_size $_ds_sys_clk]) — the REA JTAG (TCK) setup paths will be timed and may fail."
}

# tck -> clk_sys: the return toggle synchronizer and the clk_sys-side reset
# synchronizers. altera_reserved_tck is created by a package SDC read AFTER this
# file, so this cut names NO clock — it cuts by destination register set, the
# standard async-CDC synchronizer/reset-sync treatment, order-independent.
set _ds_to_sys [get_registers -nowarn [list \
    "${_ds_jp}sink_crosser|out_to_in_synchronizer|din_s1" \
    "${_ds_jp}sink_crosser|alt_rst_req_sync_in_rst|*" \
    "${_ds_jp}jtag_streaming|clock_sense_reset_n_synchronizer|dreg*"]]
if {[get_collection_size $_ds_to_sys] > 0} {
    set_false_path -to $_ds_to_sys
} else {
    post_message -type critical_warning "de25_timing.sdc (DS-T1.19): tck->clk_sys HPS JTAG CDC false path NOT applied (return-sync regs=0) — the REA JTAG (TCK) recovery paths will be timed and may fail."
}
