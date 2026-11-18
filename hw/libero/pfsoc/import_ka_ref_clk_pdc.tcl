# import_ka_ref_clk_pdc.tcl — gen_project hook (DS-P2.132). Imports the I/O PDC
# that pins ref_clk_50mhz to R18 so the fabric clock actually reaches the
# design, AND associates it with PLACEROUTE (an imported io_pdc that is not
# organized is ignored — Libero reports "No User PDC file(s) was specified").
# Sourced by tcl/microchip/gen_project.tcl after sources import; g_root_dir,
# g_project_dir and g_top_mod are in scope there (environment.tcl).
set _ka_pdc "${g_root_dir}/hw/pdc/pfsoc_ka_ref_clk.pdc"
if {![file exists $_ka_pdc]} {
    puts "ERROR: DS-P2.132 ref-clk PDC not found: $_ka_pdc"
    exit 1
}
puts "INFO: DS-P2.132 importing ref-clk PDC: $_ka_pdc"
import_files -io_pdc $_ka_pdc
# Associate the imported copy with PLACEROUTE (mirrors synth_check.tcl's SDC
# organize). import_files copies it under <project>/constraint/io/.
set _imported "${g_project_dir}/constraint/io/pfsoc_ka_ref_clk.pdc"
puts "INFO: DS-P2.132 organizing ref-clk PDC for PLACEROUTE: $_imported"
organize_tool_files -tool {PLACEROUTE} -file $_imported \
    -module "${g_top_mod}::work" -input_type {constraint}
