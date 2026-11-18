# import_header_pullups_pdc.tcl — gen_project hook (DS-P2.309).
# DS-P2.309 interim (RTL-P2.2402): remove when rr ships the per-target pull key (DS-P3.269).
# Imports the I/O PDC that puts a weak pull-up on every Raspberry Pi header
# input, so an undriven input idles HIGH. Import only: rr's own
# rr_mc_associate_constraints (timing_flow.tcl) associates every
# constraint/io/*.pdc with PLACEROUTE before synthesis and place-and-route, in one
# organize_tool_files call, so this hook must not organize (a second organize
# call replaces the PLACEROUTE file list).
# Sourced by tcl/microchip/gen_project.tcl after sources import; g_root_dir is
# in scope there (environment.tcl).
set _ds_pull_pdc "${g_root_dir}/hw/pdc/pfsoc_header_pullups.pdc"
if {![file exists $_ds_pull_pdc]} {
    puts "ERROR: DS-P2.309 header pull-up PDC not found: $_ds_pull_pdc"
    exit 1
}
puts "INFO: DS-P2.309 importing header pull-up PDC: $_ds_pull_pdc"
import_files -io_pdc $_ds_pull_pdc
