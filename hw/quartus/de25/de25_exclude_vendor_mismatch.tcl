# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: CERN-OHL-W-2.0
# DE-F0 (DS-P1.42): rr's dependency resolver does not act on either
# routertl/rr_ddr_stream's or local/rr_rea's `synthesis.sources_per_vendor`
# split (measured and pinned by DE-R0, docs/GOLDEN_PATH.md "DE-R0 portable
# core closure closure proof for altera" section) — no project.yml/target-yml
# key exists to exclude a source subtree from `paths.sources` auto-discovery
# (confirmed against `rr schema`). Two distinct fallout cases land in this
# target's synthesis source set as a result, and this hook is the DE-F0-owned
# fix for both, since neither has any other seam to attach to:
#
# 1. `local/rr_rea/rtl/rr_rea_jtag_xilinx7.vhd` is a whole file that only
#    exists for Xilinx (`library unisim; use unisim.vcomponents.all;`).
#    DE-F0 (re-running `rr lock` against a routertl.lock that had drifted
#    from the committed RTL) found the qsf's VHDL_FILE set is actually drawn
#    from routertl.lock's resolved, entity-scoped compile_order — NOT a raw
#    `paths.sources` glob of all of `libs/` (that gap is real but is a
#    sim-time auto-discovery issue per DE-R0, not a synthesis one). With a
#    correct lock, `deepskopion_de25_top`'s real REA leg
#    (`tich.rr_rea_intel`, wired to `rr_rea_jtag_intel.vhd`) is the only REA
#    JTAG backend the qsf ever contains — `rr_rea_jtag_xilinx7.vhd` is
#    already absent by construction, and this step is a no-op that stays
#    only as a defensive guard against future drift back to a stale lock.
#    There is no working remove_all_global_assignments/-section_id path for
#    a single VHDL_FILE entry in this Quartus Pro release (measured: 0
#    assignments removed against a real generated project) — so should the
#    file reappear, the file/version/library triples are parsed straight out
#    of the generated .qsf and the whole VHDL_FILE set is rebuilt, omitting
#    the excluded file, which IS confirmed to work.
#
# 2. `routertl/rr_ddr_stream`'s `rr_fifo_async_mixed.vhd` / `rr_fifo_sync.vhd`
#    each direct-instantiate an `entity work.rr_fifo_*_xpm_body` inside a
#    `GEN_XPM : if C_IMPL = "xpm" generate` branch that this target's
#    `fifo_impl_g="olo"` build never elaborates — but Quartus Pro's VHDL
#    binder resolves direct entity instantiation at analysis time regardless
#    of the generate condition ("Quartus Pro strict -library, Error 13785
#    class", predicted by DE-P0 §6's evidence matrix). The real XPM bodies
#    (file-scope `library xpm;`) are correctly excluded from this vendor's
#    source set already, so the entities are simply undefined. DE-R0 vendored
#    pure-IEEE stand-in entities upstream already ships for exactly this
#    purpose (`ip.yml`'s `synthesis.sources_per_vendor.altera`,
#    tools/sim/vendor_stubs/rr_ddr_stream_intel/README.md) but only wires
#    them into the nvc sim library, not synthesis. Add the two stubs this
#    target actually needs (mixed + sync; the design never uses the plain
#    async wrapper) to the same `rr` library the real bodies would occupy.

set _ds_project_root [file normalize [file join [file dirname [info script]] .. .. ..]]
set _ds_qsf [file join [pwd] "deepskopion.qsf"]

# FLUSH FIRST — this hook reads the qsf off DISK, and until it is exported the
# assignments gen_project.tcl has made live only in the open project. On a
# machine that has built this target before, the stale qsf from the previous
# build is sitting there with a full VHDL_FILE set, so the read appears to
# work and the drift is invisible; on a CLEAN CLONE the file Quartus has just
# created is empty, the regexp matches nothing, and the guard below aborts
# project generation with "parsed zero VHDL_FILE assignments". Measured
# off-bench 2026-08-30 (DE-G0 Record E): 0 parsed here, 208 in the same file
# once the project closed and flushed it. `export_assignments` is idempotent
# and gen_project.tcl exports again itself, so this costs a rewrite of a file
# that is about to be rewritten anyway.
export_assignments

if {![file isfile $_ds_qsf]} {
    error "DE-F0: expected generated qsf not found: $_ds_qsf"
}

set _ds_exclude [file join $_ds_project_root \
    "libs/routertl/rea/rtl/rr_rea_jtag_xilinx7.vhd"]

set _ds_fh [open $_ds_qsf r]
set _ds_lines [split [read $_ds_fh] "\n"]
close $_ds_fh

set _ds_files {}
foreach _ds_ln $_ds_lines {
    if {[regexp -- {set_global_assignment -name VHDL_FILE (?:\"([^\"]+)\"|(\S+))(.*)} \
             $_ds_ln -> _ds_p1 _ds_p2 _ds_rest]} {
        set _ds_path [expr {$_ds_p1 ne "" ? $_ds_p1 : $_ds_p2}]
        set _ds_ver ""
        set _ds_lib ""
        if {[regexp -- {-hdl_version\s+(\S+)} $_ds_rest -> _ds_v]} {
            set _ds_ver $_ds_v
        }
        if {[regexp -- {-library\s+(\S+)} $_ds_rest -> _ds_l]} {
            set _ds_lib $_ds_l
        }
        lappend _ds_files [list $_ds_path $_ds_ver $_ds_lib]
    }
}

if {![llength $_ds_files]} {
    error "DE-F0: parsed zero VHDL_FILE assignments out of $_ds_qsf — regexp drifted from the generated qsf's actual syntax"
}

set _ds_found_exclude 0
foreach _ds_f $_ds_files {
    if {[lindex $_ds_f 0] eq $_ds_exclude} { set _ds_found_exclude 1 }
}
if {!$_ds_found_exclude} {
    puts "DE-F0: $_ds_exclude is already absent from the generated qsf (routertl.lock's resolved compile_order never included it) — nothing to exclude"
} else {

remove_all_global_assignments -name VHDL_FILE

set _ds_kept 0
foreach _ds_f $_ds_files {
    set _ds_path [lindex $_ds_f 0]
    if {$_ds_path eq $_ds_exclude} { continue }
    set _ds_args [list -name VHDL_FILE $_ds_path]
    set _ds_ver [lindex $_ds_f 1]
    set _ds_lib [lindex $_ds_f 2]
    if {$_ds_ver ne ""} { lappend _ds_args -hdl_version $_ds_ver }
    if {$_ds_lib ne ""} { lappend _ds_args -library $_ds_lib }
    eval set_global_assignment $_ds_args
    incr _ds_kept
}
puts "DE-F0: dropped $_ds_exclude from the synthesis source set ($_ds_kept of [llength $_ds_files] VHDL_FILE assignments kept)"
}

set _ds_stub_dir [file join $_ds_project_root \
    "tools/sim/vendor_stubs/rr_ddr_stream_intel"]
foreach _ds_stub {rr_fifo_sync_xpm_body_stub.vhd rr_fifo_async_mixed_xpm_body_stub.vhd} {
    set _ds_stub_path [file join $_ds_stub_dir $_ds_stub]
    if {![file isfile $_ds_stub_path]} {
        error "DE-F0: expected DE-R0 vendor stub not found: $_ds_stub_path"
    }
    set_global_assignment -name VHDL_FILE $_ds_stub_path -hdl_version VHDL_2008 -library rr
    puts "DE-F0: added GEN_XPM binder stub $_ds_stub"
}
