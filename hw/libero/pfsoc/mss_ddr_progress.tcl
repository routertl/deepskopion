# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
#
# DS-P2.145 (PF-L0e) — DDR state-machine progress capture (OWNER DECISION step 4:
# "capture the mss_nwc_init path actually taken (halted-U54 technique on progress
# markers/HSS status structs) and stop honestly with the branch point named").
#
# The prior IOSCB iteration proved, from committed silicon evidence, that our HSS
# carries the CORRECT DDR/IO-calibration config (LIBERO_SETTING_DPC_BITS =
# 0x0004E563, byte-identical to the factory's runtime IOSCB_BANKCONT_DDR_DPC) yet
# at runtime IOSCB_BANKCONT_DDR_DPC / IOC_REG1 read 0x00000000 — the calibration
# is never applied. That points at mss_nwc_init_ddr()'s ddr_state_machine() never
# reaching DDR_SETUP_DONE (mss_nwc_init.c: `while((ddr_status & DDR_SETUP_DONE) !=
# DDR_SETUP_DONE)`), consistent with a DDR4 retrain loop (mss_ddr.c
# DDR_TRAINING_FAIL -> DDR_TRAINING_CHECK_FOR_OFFMODE -> (not off) -> re-init,
# retry_count++ with no fail-exit).
#
# This capture reads the DDR state-machine's runtime statics via a HALTED U54
# (the E51 free-runs the eNVM firmware and is un-haltable while spinning; the U54s
# halt cleanly and share the globally-mapped scratchpad the HSS decompresses into).
# Sampled repeatedly so a MOVING retry_count / cycling ddr_training_state is
# distinguishable from a single wedge.
#
# Symbol addresses are read from the built HSS ELF (L2-LIM/scratchpad-linked) — the
# driver passes them in; the defaults below match build/hss-l2scratch.elf this run
# (riscv64-linux-gnu-nm):
#   ddr_training_state.11 @ 0x0a0244a8  (DDR_TRAINING_SM enum index; mss_ddr.h:1083)
#   retry_count.4         @ 0x0a024488  (retrain counter)
#   ddr_diag              @ 0x0a02cd00  (ddr_diag_t: num_retrains etc.)
#
# Usage:
#   openocd -f <board-init>.cfg -f mss_ddr_progress.tcl
# The driver script (openocd_ddr_progress_run.cfg) supplies the interface/target.

proc rd {label addr} {
    if {[catch {set v [read_memory $addr 32 1]} e]} {
        echo [format "%-26s %s : READ_ERROR %s" $label $addr $e]
        return -1
    } else {
        echo [format "%-26s %s : 0x%08x" $label $addr [lindex $v 0]]
        return [lindex $v 0]
    }
}

# Addresses (override by setting these before sourcing, if the build relinks).
if {![info exists DDR_STATE_ADDR]}  { set DDR_STATE_ADDR  0x0a0244a8 }
if {![info exists RETRY_COUNT_ADDR]} { set RETRY_COUNT_ADDR 0x0a024488 }
if {![info exists DDR_DIAG_ADDR]}    { set DDR_DIAG_ADDR    0x0a02cd00 }

init
adapter speed 2000

# E51 un-haltable while running eNVM firmware; halt a U54 and read the shared
# scratchpad / MSS space through it.
targets mpfs.u54_1
catch {halt}

echo "=== DS-P2.145 DDR STATE-MACHINE PROGRESS CAPTURE (via halted u54_1) ==="
echo "# DDR_TRAINING_SM enum (mss_ddr.h): 0=INIT 1=FAIL 2=CHECK_FOR_OFFMODE"
echo "#   3=SET_MODE_VS_BITS 4=FLASH_REGS 5=CORRECT_RPC 6=SOFT_RESET"
echo "#   7=CALIBRATE_IO 8=CONFIG_PLL 9=SETUP_SEGS 10=VERIFY_PLL_LOCK"
echo "#   11=SETUP_DDRC 12=RESET 13=ROTATE_CLK 14=SET_TRAINING_PARAMETERS"
echo "#   15=IP_SM_BCLKSCLK_SW 16=MANUAL_ADDCMD_TRAINING_SW 17=IP_SM_START"
echo "#   18=IP_SM_START_CHECK 19=IP_SM_BCLKSCLK 20=IP_SM_ADDCMD 21=IP_SM_WRLVL"
echo "#   22=IP_SM_RDGATE 23=IP_SM_DQ_DQS 24=IP_SM_VERIFY 25=SET_FINAL_MODE"
echo "#   26=WRITE_CALIBRATION 27=WRITE_CALIBRATION_RETRY 28=SANITY_CHECKS ..."
echo "#   45=FINISHED  46+=FAIL_SM_* (see mss_ddr.h:1129-1142)"

# Sample repeatedly with a delay between reads so a live-cycling SM (retry_count
# climbing) is separable from a static wedge. read_memory re-fetches each time.
for {set i 0} {$i < 8} {incr i} {
    echo "--- sample $i ---"
    rd DDR_TRAINING_STATE $DDR_STATE_ADDR
    rd RETRY_COUNT        $RETRY_COUNT_ADDR
    rd DDR_DIAG_NUM_RETRAINS $DDR_DIAG_ADDR
    # A short host-side spin between samples (OpenOCD 'sleep' is ms).
    sleep 250
}

echo "--- current IOSCB DDR calibration status (should stay 0 while un-converged) ---"
rd IOSCB_BANKCONT_DDR_DPC  0x3e020004
rd IOSCB_IO_CALIB_DDR_STAT 0x3e040004
rd L2_WAYENABLE            0x02010008

echo "=== END DDR PROGRESS CAPTURE ==="
shutdown
