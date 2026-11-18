# pfsoc_ka_timing.sdc — timing constraints for pf_known_answer_top on the
# PolarFire SoC Discovery Kit (MPFS095T-1FCSG325E). DS-P2.132 (PF-T9).
#
# The 50 MHz on-board fabric oscillator (ref_clk_50mhz, pin R18, Bank 0, 1.8 V).
# 20 ns period. The design is a single free-running counter, so the only real
# constraint is the source clock; I/O delays are loose (LEDs are slow, and the
# REA control plane is a separate JTAG clock domain handled inside the core).
create_clock -name {ref_clk_50mhz} -period 20.000 [get_ports {ref_clk_50mhz}]

# DS-P2.327: led[3:0] drive human-visible status LEDs. Nothing samples them against a clock, so they carry no I/O
# timing requirement; without a declared intent SmartTime reports them as unconstrained outputs and the rr timing
# gate fails UNCONSTRAINED (measured: impl 8975fa9c2c77, "Max output delay constraint missing on ports: led[0..3]").
set_false_path -to [get_ports {led[*]}]
