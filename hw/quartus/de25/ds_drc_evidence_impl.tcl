#------------------------------------------------------------------------------
# ds_drc_evidence_impl.tcl — DS-T1.22 Quartus Design Assistant CDC blocking gate (post_implementation)
#------------------------------------------------------------------------------
# Runs on the fitted/routed design after TimeQuest signoff STA.
#
# Quartus Design Assistant emits the DRC report:
#   <project>.tq.drc.signoff.rpt (e.g. deepskopion.tq.drc.signoff.rpt)
# in $g_project_dir/ or current working directory.
#
# RouteRTL treats Design Assistant violations as non-fatal warnings by default.
# DS-T1.22 requires making Design Assistant CDC findings blocking:
#   1. Verifies that the signoff DRC report exists. Absence of a report is never a pass.
#   2. Copies the report to $g_reports_dir/deepskopion.tq.drc.signoff.rpt so it survives build cleanup.
#   3. Scans the report table for CDC-* rule violations.
#   4. If any High-severity (or unwaived) CDC violations exist, fails closed with an error.
#
# DS-T1.20 adds a second check on the same report: every TMC-20025 "Ignored or
# Overridden Constraints" row must be waived. The waivers live in
# de25_generated_sdc_waivers.dawf, one per justified SDC file:line. The
# summary's Violations column counts only UNWAIVED rows (measured: 22/0
# before the waivers, 0/22 after), so any value above 0 is an ignored
# constraint with no justification and fails the build. A report without a
# TMC-20025 summary row also fails, because the rule cannot then be checked.
#
# DS-P3.194 adds a third check. A waiver kept only because Design Assistant
# contradicts STA must not outlive that false report. Each row in
# ds_provisional_tmc_20025 must still be listed in the TMC-20025 detail table
# (waived or not). When a Quartus release (or a design change) stops reporting
# it, the build fails and names the report's Quartus version, so the waiver is
# removed from de25_generated_sdc_waivers.dawf in the same change.
#------------------------------------------------------------------------------

if {![info exists g_reports_dir]} {
    set g_reports_dir "reports"
}
file mkdir $g_reports_dir

set rpt_name "deepskopion.tq.drc.signoff.rpt"
if {[info exists g_project_name] && $g_project_name ne ""} {
    set rpt_name "${g_project_name}.tq.drc.signoff.rpt"
}

set candidate_paths [list]
if {[info exists g_project_dir]} {
    lappend candidate_paths [file join $g_project_dir $rpt_name]
    lappend candidate_paths [file join $g_project_dir "project" $rpt_name]
}
if {[info exists g_output_dir]} {
    lappend candidate_paths [file join $g_output_dir "project" $rpt_name]
    lappend candidate_paths [file join $g_output_dir "reports" $rpt_name]
    lappend candidate_paths [file join $g_output_dir $rpt_name]
}
lappend candidate_paths $rpt_name
lappend candidate_paths [file join "project" $rpt_name]
lappend candidate_paths [file join "reports" $rpt_name]

set drc_file ""
foreach p $candidate_paths {
    if {[file exists $p]} {
        set drc_file $p
        break
    }
}

if {$drc_file eq ""} {
    error "DS-T1.22: Design Assistant signoff report ($rpt_name) not found; cannot verify CDC constraints"
}

# Copy report to reports directory
set target_rpt [file join $g_reports_dir $rpt_name]
if {[file normalize $drc_file] ne [file normalize $target_rpt]} {
    catch { file copy -force $drc_file $target_rpt }
}

# Parse DRC report for CDC rule violations
set fp [open $drc_file r]
set cdc_violations [list]
set total_high_cdc 0

set tmc_20025_unwaived -1
set tmc_20025_rows [list]
set tmc_20025_reported [list]
set in_tmc_20025_detail 0
set quartus_version "unknown"

# DS-P3.194: {Location Reason} pairs whose waiver exists only because Design
# Assistant's report contradicts STA. Each must still be reported.
set ds_provisional_tmc_20025 [list \
    [list {altera_s10_user_rst_clkgate_fm.sdc:32} {Exception is invalid (covers no paths)}] \
]

while {[gets $fp line] >= 0} {
    if {$quartus_version eq "unknown" && [regexp {^Quartus Prime Version (.+)$} $line -> qv]} {
        set quartus_version [string trim $qv]
        continue
    }
    # DS-T1.20: summary row. Violations already excludes waived rows.
    if {[regexp {^\s*;\s*TMC-20025 - [^;]*;\s*[^;]+?\s*;\s*(\d+)\s*;\s*(\d+)\s*;} $line -> tmc_v tmc_w]} {
        set tmc_20025_unwaived [scan $tmc_v %d]
        continue
    }
    # DS-T1.20: detail table, so the error names each unwaived constraint.
    if {[regexp {^;\s*TMC-20025 - Ignored or Overridden Constraints\s*;\s*$} $line]} {
        set in_tmc_20025_detail 1
        continue
    }
    if {$in_tmc_20025_detail} {
        if {[string match "+*" $line] || [string match "; Ignored Constraint *" $line]} {
            continue
        }
        if {![string match ";*" $line]} {
            set in_tmc_20025_detail 0
        } else {
            set cells [split [string trim [string trim $line] ";"] ";"]
            if {[llength $cells] >= 3} {
                lappend tmc_20025_reported [list [string trim [lindex $cells 1]] [string trim [lindex $cells 2]]]
            }
            if {[llength $cells] >= 4 && [string trim [lindex $cells 3]] eq ""} {
                lappend tmc_20025_rows "[string trim [lindex $cells 1]]: [string trim [lindex $cells 0]] ([string trim [lindex $cells 2]])"
            }
            continue
        }
    }
    # Match table row: ; Rule ; Severity ; Violations ; Waived ; Tags ;
    if {[regexp {^\s*;\s*(CDC-[^;]+?)\s*;\s*([^;]+?)\s*;\s*(\d+)\s*;\s*(\d+)\s*;} $line -> rule severity violations waived]} {
        set rule [string trim $rule]
        set severity [string trim $severity]
        set v [scan $violations %d]
        set w [scan $waived %d]
        set unwaived [expr {$v - $w}]
        if {$unwaived > 0} {
            lappend cdc_violations "$rule ($severity: $unwaived unwaived violation(s))"
            if {[string equal -nocase $severity "high"] || [string equal -nocase $severity "critical"]} {
                incr total_high_cdc $unwaived
            }
        }
    }
}
close $fp

set gate_errors [list]

if {$total_high_cdc > 0} {
    puts "ERROR: DS-T1.22: Quartus Design Assistant reports $total_high_cdc blocking High-severity CDC violation(s):"
    foreach item $cdc_violations {
        puts "  * $item"
    }
    lappend gate_errors "DS-T1.22: Blocking CDC violations found in Design Assistant signoff report ($drc_file)"
}

if {$tmc_20025_unwaived < 0} {
    puts "ERROR: DS-T1.20: the signoff report has no TMC-20025 summary row; ignored constraints cannot be checked."
    lappend gate_errors "DS-T1.20: TMC-20025 rule not reported in Design Assistant signoff report ($drc_file)"
} elseif {$tmc_20025_unwaived > 0} {
    puts "ERROR: DS-T1.20: $tmc_20025_unwaived TMC-20025 ignored or overridden constraint(s) have no waiver in de25_generated_sdc_waivers.dawf:"
    foreach item $tmc_20025_rows {
        puts "  * $item"
    }
    lappend gate_errors "DS-T1.20: Unwaived TMC-20025 ignored constraints found in Design Assistant signoff report ($drc_file)"
}

if {$tmc_20025_unwaived >= 0} {
    foreach pw $ds_provisional_tmc_20025 {
        if {[lsearch -exact $tmc_20025_reported $pw] < 0} {
            puts "ERROR: DS-P3.194: Design Assistant (Quartus $quartus_version) no longer reports TMC-20025 for [lindex $pw 0] ([lindex $pw 1])."
            puts "  Its waiver in de25_generated_sdc_waivers.dawf is kept only for that report: remove the waiver and this ds_provisional_tmc_20025 entry together."
            lappend gate_errors "DS-P3.194: provisional TMC-20025 waiver for [lindex $pw 0] has outlived its report ($drc_file)"
        }
    }
}

if {[llength $gate_errors] > 0} {
    error [join $gate_errors "; "]
}

puts "DS_EVIDENCE $rpt_name $target_rpt"
puts "INFO: DS-T1.22: Quartus Design Assistant signoff CDC check passed (0 blocking High-severity CDC violations)."
puts "INFO: DS-T1.20: every TMC-20025 ignored or overridden constraint is waived with a justification."
