# Install the ROM staging script as Vivado's synth_1 pre-synthesis hook.
# This file is sourced by RouteRTL before it resets and launches synth_1;
# run properties survive reset_run, unlike files copied into the run directory.

set _ds_rom_stage_script [file normalize [file join \
    [file dirname [info script]] stage_regbank_roms_pre.tcl]]

if {![file exists $_ds_rom_stage_script]} {
    error "Regbank ROM staging hook not found: $_ds_rom_stage_script"
}

set_property STEPS.SYNTH_DESIGN.TCL.PRE $_ds_rom_stage_script [get_runs synth_1]
puts "Configured synth_1 ROM staging hook: $_ds_rom_stage_script"

# Update compile order to ensure all dynamically instantiated RTL units
# (such as uart_datalogger or qspi_datalogger in datalogger_gather)
# are included in synth_1 without AutoDisabled=1
update_compile_order -fileset sources_1
puts "Updated compile order for sources_1"
