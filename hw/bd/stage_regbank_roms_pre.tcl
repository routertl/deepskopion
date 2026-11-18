# Vivado evaluates VHDL file I/O from the synthesis run directory. Recreate
# the clone-relative paths emitted by rom_info_pkg immediately before
# synth_design, after reset_run has finished cleaning this directory.

set _ds_project_root [file normalize [file join [file dirname [info script]] ../..]]
set _ds_rom_files [list \
    hw/rtl/regbank/regbank_schema.hex \
    hw/rtl/regbank/config_schema.hex]

foreach _ds_relative_path $_ds_rom_files {
    set _ds_source [file join $_ds_project_root $_ds_relative_path]
    set _ds_destination [file join [pwd] $_ds_relative_path]
    if {![file isfile $_ds_source]} {
        error "Regbank ROM initializer does not exist: $_ds_source"
    }
    file mkdir [file dirname $_ds_destination]
    file copy -force $_ds_source $_ds_destination
    puts "Staged regbank ROM: $_ds_relative_path"
}
