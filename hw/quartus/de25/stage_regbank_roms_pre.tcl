# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: CERN-OHL-W-2.0
# Quartus evaluates VHDL file I/O (regbank_rom.vhd's `file_open` on
# G_HEX_FILE) from quartus_syn's own working directory, which for a
# rr-generated project is build/de25_datalogger/project — not the repo root the
# clone-relative hex paths in rom_info_pkg.vhd assume. Same defect class as
# the Vivado run (hw/bd/stage_regbank_roms_pre.tcl); Quartus's synth stage
# just has a different cwd, so re-stage identically here.

set _ds_project_root [file normalize [file join [file dirname [info script]] .. .. ..]]
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
