#------------------------------------------------------------------------------
# ds_constraint_evidence_impl.tcl — DS-T1.17 binding and coverage evidence (post_implementation)
#------------------------------------------------------------------------------
# Runs on the routed design. RouteRTL already writes cdc.rpt (report_cdc) and
# check_timing_noclock.rpt. The DS-P1.72 audit also needs reports the flow
# does not keep:
#
#   ds_exceptions_ignored.rpt   report_exceptions -ignored: every timing
#                               exception Vivado dropped or overrode, so the
#                               count of ignored exceptions is read, not assumed
#   ds_exceptions_ignored_objects.rpt
#                               report_exceptions -ignored_objects: exceptions
#                               whose -from/-to/-through objects bound nothing
#   ds_check_timing.rpt         check_timing -verbose with the default checks:
#                               unconstrained internal endpoints, missing
#                               input/output delays, unclocked registers
#   ds_timing_unconstrained.rpt report_timing_summary -report_unconstrained:
#                               the unconstrained paths themselves
#
# A report that cannot be written stops the build. A missing report is not a
# clean one.
#------------------------------------------------------------------------------

if {![info exists g_reports_dir]} {
	error "DS-T1.17: g_reports_dir is not set; cannot record timing evidence"
}
file mkdir $g_reports_dir

foreach {_ds_name _ds_cmd} {
	ds_exceptions_ignored.rpt   {report_exceptions -ignored}
	ds_exceptions_ignored_objects.rpt {report_exceptions -ignored_objects}
	ds_check_timing.rpt         {check_timing -verbose}
	ds_timing_unconstrained.rpt {report_timing_summary -report_unconstrained -max_paths 20}
} {
	set _ds_out "$g_reports_dir/$_ds_name"
	if {[catch {eval $_ds_cmd -file [list $_ds_out]} _ds_err]} {
		error "DS-T1.17: '$_ds_cmd' failed: $_ds_err"
	}
	puts "DS_EVIDENCE $_ds_name $_ds_out"
}
