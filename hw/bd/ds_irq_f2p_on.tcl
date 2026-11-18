# ds_irq_f2p_on.tcl — enable PL→PS fabric IRQ on processing_system7_0.
#
# DS-P3.60. Replaces the former ds_irq_f2p_off.tcl. The board preset
# leaves PCW_USE_FABRIC_INTERRUPT enabled with IRQ_F2P width 1
# (StereoVision default); the PoC previously DISABLED it because the
# DDR ring drain was polled. DS-P3.60 makes the drain interrupt-aware:
# the ring-fill watermark alarm (deepskopion_top.vhd p_ring_watermark,
# 75% set / 50% clear hysteresis) drives IRQ_F2P[0], routed to GIC SPI
# #29 (level-high) and consumed by the PS daemon over a generic-uio node.
#
# Set the flags explicitly so the BD's make_bd_pins_external of
# processing_system7_0/IRQ_F2P (deepskopion_bd.tcl) finds the pin
# regardless of preset drift.
#
# DS-P3.109: the two CTU CAN FD cores' irqs ride IRQ_F2P[1]/[2] alongside the
# ring alarm on [0]. The PS7 packs IRQ_F2P[0]→GIC SPI #29, [1]→#30, [2]→#31
# (the ctucanfd DT nodes use interrupts <0 30 4> / <0 31 4>). NB on Zynq-7 the
# IRQ_F2P width is NOT a PCW property — it auto-sizes to the net connected to
# the pin; deepskopion_bd.tcl drives it with a 3-bit external port.
#
# Listed under hardware.ps7_hooks in project.yml; runs AFTER the board
# preset's apply_preset {} populates the PS7 cell.

set_property -dict [list \
    CONFIG.PCW_USE_FABRIC_INTERRUPT {1} \
    CONFIG.PCW_IRQ_F2P_INTR         {1} \
] [get_bd_cells processing_system7_0]
