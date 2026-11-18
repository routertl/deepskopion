#------------------------------------------------------------------------------
# timing.xdc — clock-domain-crossing constraints for deepskopion_top
#------------------------------------------------------------------------------
# The PS7 emits two asynchronous fabric clocks:
#   clk_fpga_0 = 100 MHz (clk_sys — datalogger chain + AXI-Lite control)
#   clk_fpga_1 = 125 MHz (clk_ddr — S_AXI_HP0 DMA / rr-ddr-stream burst)
# The PS7 IP XDC itself notes: "The clocks are asynchronous, user should
# constrain them appropriately." The only fabric crossing between them is the
# rr-ddr-stream async FIFO in u_ddr_burst/i_ddr_fifo_intf — olo cc_bits /
# cc_reset gray-code-pointer + reset-handshake 2-FF synchronizers (the worst
# violated paths pre-constraint were i_cc_rd_wr and i_rst_cc/i_acka2b, each
# only ~1.7-1.9 ns of real datapath).
#
# We bound that crossing with set_max_delay -datapath_only — NOT
# set_clock_groups -asynchronous (see routertl rule CDC-001). -datapath_only
# removes the false setup/skew pessimism on the safe synchronizers while STILL
# timing the datapath, so an accidentally-missing synchronizer or stray
# combinational logic across the boundary would still be flagged. A blanket
# clock_groups -asynchronous would blind the tool to ALL fpga_0<->fpga_1 paths
# permanently.
#
# Value = 8 ns = the faster (125 MHz) period = min(10 ns, 8 ns): adjacent
# gray-code pointer values cannot overlap and CDC data settles within one
# destination cycle.
#
# FOLLOW-UP: this constraint really belongs scoped/OOC with the rr-ddr-stream
# IP so every consumer inherits correct CDC timing — tracked on the
# rr-ddr-stream backlog. This top-level block is the interim home.
#------------------------------------------------------------------------------

set_max_delay -datapath_only 8.000 -from [get_clocks clk_fpga_0] -to [get_clocks clk_fpga_1]
set_max_delay -datapath_only 8.000 -from [get_clocks clk_fpga_1] -to [get_clocks clk_fpga_0]

#------------------------------------------------------------------------------
# DS-P3.71 CDC: injected-SPI self-test source → edge dataloggers
#------------------------------------------------------------------------------
# When EMIO_MUX_SEL[3]=1 the four edge dataloggers observe the injected SPI set
# (EMIO SPI0 / PMOD — async to clk_sys). It is 2-FF synced (ASYNC_REG) through
# spi_edge_meta_r → spi_edge_sync_r before sampling. Declare the async capture
# into the first sync stage a false path so STA stops timing that single-cycle
# arc (it has no real source-clock relationship; it was closing only marginally).
set_false_path -to [get_cells -hier -filter {NAME =~ *spi_edge_meta_r* || NAME =~ *spi_edge_*_sync_r_reg[0]* || NAME =~ *spi_edge_*_sync_r[0]*}]

#------------------------------------------------------------------------------
# DS-P3.166 / Finding F5 / DS-T1.21: EMIO probe status readback (ja, jb, PS7 EMIO) → clk_sys
#------------------------------------------------------------------------------
# EMIO_PROBE samples raw external PMOD pins (ja[7:4], ja[1:0], jb[0]) and PS7
# EMIO peripheral outputs (UART0_TX, I2C0_*, SPI0_*) as a static status snapshot.
# These independent asynchronous lines are each synchronized by a scalar 2-FF
# shift register (emio_probe_sync_r(i)(0) -> (1), ASYNC_REG) before reaching the
# AXI-Lite regbank. Declare the async capture into the first sync stage a false
# path so STA stops treating it as an unconstrained path to clk_fpga_0.
set_false_path -to [get_cells -hier -filter {NAME =~ *emio_probe_sync_r_reg[*][0]*}]

#------------------------------------------------------------------------------
# DS-T1.17: REA JTAG clock (BSCANE2 TCK) → clk_sys
#------------------------------------------------------------------------------
# u_rea's USER1 BSCANE2 drives the REA's JTAG-side registers (TAP iface,
# regbank shift/update, DPRAM read port, status synchronizers) from its TCK
# pin. The routertl/rea package clocks them: its rr_rea_scoped.xdc (read_xdc
# -ref rr_rea_top, read before this file) creates the 30 MHz clock on
# rr_rea_top's tck_i. DS-P2.168 measured that on the first Zybo build on the
# package (queue job 25c16f6356a1, rr-rea 1.12.0): every REA register launches
# on u_rea/u_top/tck_i, and every cond/qual crossing leaves a registered
# u_regbank/*_o_reg into an ASYNC_REG s1. The interim create_clock rea_tck this
# file used to put on u_rea/u_bscane2/TCK is gone: a second clock on the same
# net clocked no REA register (the package clock overrides it below
# u_top/tck_i), so the crossing bounds written against it were dead
# (report_exceptions -ignored: "Non-existent path") and REA's TDO register
# reached the JTAG datalogger's synchronizer timed as a 1 ps crossing
# (WNS -1.715 ns).
#
# REA crosses between TCK and its sample clock (clk_sys = clk_fpga_0) through
# the capture BRAM (write port on clk_sys, read port on TCK), through
# rr_rea_sync_word / rr_rea_pulse_xfer 2-FF synchronizers (ASYNC_REG), and,
# via the tap_tdo mirror, into the JTAG datalogger's i_cond 2-FF. Same rule as
# the DDR crossing above (CDC-001): bound the datapath with
# set_max_delay -datapath_only, not set_clock_groups -asynchronous, so a
# crossing that bypasses a synchronizer is still timed. 10 ns = the faster
# period of the pair. The multi-bit sync_word coherence question is DS-T1.21.
# The clock is named by the pin the package puts it on, never by the name
# Vivado generates for it.
set_max_delay -datapath_only 10.000 -from [get_clocks -of_objects [get_pins u_rea/u_top/tck_i]] -to [get_clocks clk_fpga_0]
set_max_delay -datapath_only 10.000 -from [get_clocks clk_fpga_0] -to [get_clocks -of_objects [get_pins u_rea/u_top/tck_i]]

# DS-P3.56 routes TCK itself to the JTAG datalogger as DATA (capture_pin(24)),
# which oversamples it on clk_sys through i_cond's 2-FF synchronizer. That path
# starts at the BSCANE2 TCK pin, outside rr_rea_top, so no clock launches it
# (the build at 1447d2a, with a clock there, timed it with a 1 ps requirement,
# WNS -1.403 ns). Bound it by its startpoint pin with the same 10 ns
# datapath-only budget as the rest of the crossing.
set_max_delay -datapath_only 10.000 -from [get_pins u_rea/u_bscane2/TCK] -to [get_clocks clk_fpga_0]
