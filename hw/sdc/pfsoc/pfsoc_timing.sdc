# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: CERN-OHL-W-2.0
# PF-H0 (DS-P2.124): CDC / capture-source timing for deepskopion_pfsoc_top,
# re-derived for the Libero SmartTime SDC dialect from the Xilinx source of
# truth (hw/xdc/timing.xdc) and the DE25 Quartus port (hw/sdc/de25_timing.sdc).
#
# Dialect note (PF-P0 §1.3): the porting work here is the SDC *dialect*, not the
# file kind — Microchip's constraint extension is also `.sdc`, but the object
# accessors are Synopsys/SmartTime (`get_ports`/`get_cells`/`get_clocks`), NOT
# Quartus's `get_registers`/`get_collection_size` conditional idiom used in
# de25_timing.sdc. So the false-path arcs below are expressed as plain
# `set_false_path -to [get_cells ...]`, exactly as the Xilinx xdc expresses them.
#
# ---------------------------------------------------------------------------
# Primary fabric clock.
# ---------------------------------------------------------------------------
# The on-board 50 MHz fabric oscillator (ref_clk_50mhz, pin R18, HSIO Bank 0 —
# PF-P0 §2/§3.9) enters the fabric CCC/PLL, whose generated clk_sys (nominally
# 100 MHz, kept at the Zynq rate — Zynq's PS fabric clock is 100 MHz — so the
# ZS/CAN/telemetry arithmetic matches the Zynq leg) is owned by the CCC-generated
# SDC the Libero SmartDesign emits. (NB the DE25 leg runs its core at 50 MHz:
# clk_sys there IS CLOCK2_50, no PLL — so parity is with the Zynq leg, not DE25.)
# This file constrains only the board input and the async capture arcs; the
# generated clock and its uncertainty come from the CCC component SDC.
create_clock -name {ref_clk_50mhz} -period 20.000 [get_ports {ref_clk_50mhz}]

# ---------------------------------------------------------------------------
# No fpga_0 <-> fpga_1 asynchronous DDR-FIFO crossing in phase 1.
# ---------------------------------------------------------------------------
# On Zynq the PS7 emits two ASYNCHRONOUS fabric clocks (100 MHz clk_sys /
# 125 MHz clk_ddr) and timing.xdc bounds the rr-ddr-stream async-FIFO crossing
# with `set_max_delay -datapath_only 8.0` in BOTH directions (never
# set_clock_groups -asynchronous — routertl CDC-001). PF-P0 §2 pins PolarFire
# phase 1 to a SINGLE clock domain: clk_ddr = clk_sys. With one domain there is
# no clk_sys<->clk_ddr crossing to constrain, so the two set_max_delay arcs are
# deliberately ABSENT rather than copied inert — a copied-but-meaningless
# cross-clock exception is the "unconstrained-but-reported-met" trap PF-F0 must
# fail on. If PF-F0 splits the FIC user clock off clk_sys (a tuning option, not
# a phase-1 dependency) the two set_max_delay arcs return here, sourced from the
# then-real generated clock names.

# ---------------------------------------------------------------------------
# Async capture-source metastability arcs (from the implemented gather).
# ---------------------------------------------------------------------------
# External capture pins are asynchronous to clk_sys and enter each active
# datalogger gather pin's glitch_filter first synchronizer stage (i_cond/meta_r).
# The source-to-first-flop arc has no launch-clock relationship; sync_r and all
# downstream logic remain timed. The synthesis report for c5af91ad proved the
# older core-level capture endpoint names were optimized away and matched
# nothing; only the implemented gather hierarchy is selected here.
#
# PF-F0 (DS-P2.151) SYNTAX FIX. The original arcs were written in the Vivado
# accessor form `get_cells -hierarchical -filter {name =~ "..."}`. Libero
# SmartTime's `get_cells` has NO `-filter` parameter (SDC0061 "Unknown parameter
# -filter", Libero 2025.2) — it takes a positional wildcard NAME pattern, where
# `*` spans the hierarchy separator. PF-H0 never caught this because it only ran
# SYNTHESIZE, where a bad SDC line is a non-fatal warning; PF-F0 is the first to
# ASSOCIATE this SDC with the timing tools (VERIFYTIMING / timing-driven
# PLACEROUTE), where the parse error aborts the run. Re-expressed as the Libero
# positional pattern so the arcs actually load AND apply (an SDC that fails to
# parse leaves these CDC arcs unconstrained — the metastability hazard the arcs
# exist to bound). Guarded by tools/tests/test_pfsoc_sdc_scope_ds_p2_151.py.
#
# CAN arc DELIBERATELY OMITTED here (PF-F0). pfsoc_datalogger is CAN-free by
# default (target header: no CTU CAN nodes; the ctu_can_fd_rtl library is a
# lightweight stub), so `can_edge_meta_r` does NOT exist in this netlist. Libero
# SmartTime `set_false_path -to <empty-collection>` is a HARD error (SDC0023
# "the -to value ... is incorrect") — unlike Vivado/Quartus it will not accept
# an empty target — and, unlike the DE25 Quartus SDC's `get_collection_size`
# guard, SmartTime's restricted SDC Tcl exposes no working
# empty-collection test (`sizeof_collection` is undefined; `llength`/string
# compare choke on the bracketed `[..]` cell names / opaque collection). So the
# arc for an absent protocol is simply not emitted. The CAN opt-in path
# (DS-P2.121), which brings the real CTU cores — and thus `can_edge_meta_r` —
# into the build, is the writer that re-adds this arc when CAN is present.
set_false_path -to [get_cells {*u_core*u_datalogger_gather*i_cond*meta_r*}]
