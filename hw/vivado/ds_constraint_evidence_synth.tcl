#------------------------------------------------------------------------------
# ds_constraint_evidence_synth.tcl — DS-T1.17 constraint-load evidence (post_synthesis)
#------------------------------------------------------------------------------
# The DS-P1.72 launch timing audit could not say whether hw/xdc/timing.xdc,
# the scoped rr_ddr_stream XDC and the PS7/board/IP XDC set were even loaded on
# Zybo: no build had reached Vivado, and the RouteRTL Xilinx flow does not keep
# a constraint compile order. Implementation opens the synthesis checkpoint
# without a project, so the compile order has to be captured here, while the
# synthesis project is still open.
#
# Writes into the build's reports dir:
#   ds_constraint_compile_order.rpt  report_compile_order -constraints
#   ds_constraint_files.tsv          one row per XDC: enabled, processing
#                                    order, used-in, scoped ref / cells
#
# A report that cannot be written stops the build. A missing report is not a
# clean one.
#------------------------------------------------------------------------------

if {![info exists g_reports_dir]} {
	error "DS-T1.17: g_reports_dir is not set; cannot record constraint evidence"
}
file mkdir $g_reports_dir

set _ds_order_rpt "$g_reports_dir/ds_constraint_compile_order.rpt"
if {[catch {report_compile_order -constraints -file $_ds_order_rpt} _ds_err]} {
	error "DS-T1.17: report_compile_order -constraints failed: $_ds_err"
}

set _ds_xdc [get_files -quiet -all -filter {FILE_TYPE == XDC}]
if {[llength $_ds_xdc] == 0} {
	error "DS-T1.17: the synthesis project lists no XDC files at all"
}
set _ds_fh [open "$g_reports_dir/ds_constraint_files.tsv" "w"]
puts $_ds_fh "file\tis_enabled\tprocessing_order\tused_in\tscoped_to_ref\tscoped_to_cells"
foreach _ds_f $_ds_xdc {
	set _ds_row [list $_ds_f]
	foreach _ds_p {IS_ENABLED PROCESSING_ORDER USED_IN SCOPED_TO_REF SCOPED_TO_CELLS} {
		lappend _ds_row [get_property -quiet $_ds_p $_ds_f]
	}
	puts $_ds_fh [join $_ds_row "\t"]
}
close $_ds_fh
puts "DS_EVIDENCE constraint_compile_order $_ds_order_rpt ([llength $_ds_xdc] XDC file(s))"
