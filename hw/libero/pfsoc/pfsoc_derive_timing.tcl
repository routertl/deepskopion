# PF-F0 (DS-P2.151) — derive + associate the component timing constraints so
# the CCC-generated fabric clock (clk_sys, 100 MHz) is actually TIMED.
#
# WHY. The PF_CCC (pfsoc_ccc_100) emits `create_generated_clock -multiply_by 2`
# for its OUT0 (the 100 MHz clk_sys) in its COMPONENT SDC, and the frozen MSS
# emits its own component SDC. In this HDL-root Libero flow (top_wrapper +
# SmartDesign built by pfsoc_mss_subsys.tcl, NOT a SmartDesign-root project)
# those component SDCs are NOT auto-associated with the top-level timing tools.
# MEASURED (DS-P2.151, Libero 2025.2): without this step the place-and-route
# constraint coverage is Setup 7 constrained / 78394 UNconstrained — clk_sys and
# essentially the entire fabric are untimed, so any "0 violations / timing met"
# is the vacuous "unconstrained-but-met" result PF-P0 §6 declares a FAIL. With
# it, coverage is 78398 constrained / 3 unconstrained (only the truly async I/O).
#
# HOW. `derive_constraints_sdc` reads every generated component's SDC and writes
# a top-level derived SDC carrying the FULL hierarchical create_generated_clock
# (e.g. `u_fabric/u_mss/u_ccc/pfsoc_ccc_100_0/pll_inst_0/OUT0`), and — the load-bearing
# part — auto-associates it to the Synthesis, Place-and-Route and Timing
# Verification tools. The derived file lands in the project constraint/ dir, so
# the rr synth_check.tcl SDC-association glob also picks it up.
#
# ORDER. This is a gen_project tcl_hook listed AFTER hw/libero/pfsoc/
# pfsoc_mss_subsys.tcl, which builds the SmartDesign and generates the CCC/MSS
# components — so their component SDCs exist when this runs. It is idempotent:
# the derived file is regenerated (and re-associated) each gen_project, and its
# own header warns "Any modifications to this file will be lost if derived
# constraints is re-run" — it is a generated artifact, never hand-edited.
#
# FAIL LOUD, DO NOT SWALLOW (DS-P2.151 land-refusal fix). An earlier revision
# `catch`-ed a derive failure and only `puts` a WARNING, so gen_project (and the
# whole fit) proceeded with clk_sys UNCONSTRAINED — the exact vacuous
# "unconstrained-but-met" state PF-P0 §6 declares a FAIL — and nothing on the
# always-on path caught it (the coverage gate lives only in the skip-gated
# DS_P2_151_E2E_IMPL leg). The derive step is load-bearing: if it fails there is
# no honest timing verdict to give, so a failure MUST abort gen_project. The
# `error` re-raise below is what makes a broken/absent derive visible instead of
# silently poisoning the timing signoff.
#
# Guarded by tools/tests/test_pfsoc_sdc_scope_ds_p2_151.py — both that this hook
# is listed after the MSS builder AND that it still calls derive_constraints_sdc
# and re-raises (does not swallow) a derive failure — and, end-to-end, by the
# DS_P2_151_E2E_IMPL leg (constraint coverage must show clk_sys constrained).
puts "PF-F0: deriving + associating component timing constraints (CCC clk_sys generated clock)"
if {[catch { derive_constraints_sdc } _pf_f0_derive_err]} {
    puts "PF-F0 ERROR: derive_constraints_sdc failed: $_pf_f0_derive_err"
    # Re-raise: a failed derive leaves clk_sys unconstrained, which would make
    # any "timing met" signoff vacuous (PF-P0 §6). Abort the fit rather than
    # proceed on an untimed fabric.
    error "PF-F0: derive_constraints_sdc failed — CCC clk_sys would be UNCONSTRAINED and the timing signoff vacuous (DS-P2.151): $_pf_f0_derive_err"
}
puts "PF-F0: derived constraints generated and associated to SYNTHESIZE / PLACEROUTE / VERIFYTIMING"
