# ds_hp2_off.tcl — disable S_AXI_HP2 on processing_system7_0.
#
# Project-feature delta (RTL-P2.618 ps7_hook). Layered on top of the
# canonical Zybo Z7-20 board preset (routertl/boards/zybo-z7-20/ps7_preset.tcl)
# which enables all four HP slaves by default (StereoVision-derived
# baseline used HP2 for a second camera). Deepskopion only uses HP0
# (DDR-ring AXI4 slave for PL→PS writes); HP1/HP3 stay on for future
# expansion, HP2 explicitly off to keep the BD validate noise quiet.
#
# Listed under hardware.ps7_hooks in project.yml; runs AFTER the board
# preset's apply_preset {} populates the PS7 cell.

set_property -dict [list \
    CONFIG.PCW_USE_S_AXI_HP2 {0} \
] [get_bd_cells processing_system7_0]
