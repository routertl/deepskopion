# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
#
# DS-P2.145 (PF-L0e) IOSCB LIVE-POKE experiment (OWNER DECISION step 2).
#
# While OUR silent HSS runs from eNVM, JTAG-write (via a halted U54, which shares
# the globally-mapped MSS address space) the FACTORY-measured values for the
# registers that DIFFER in the golden-round-2 diff — the IO-calibration / clock /
# reset layer — and read each back to prove whether the write TOOK. A poke that
# sticks + a console byte = layer confirmed; a poke that reads back unchanged =
# the register is E51/system-controller-owned and not U54-writable (also a
# result: it says the fix must be in the HSS boot path, not a live poke).
#
# Read-then-write-then-read each target so the transcript is self-proving.
#
# Invoke via openocd_poke_run.cfg (adapter + mpfs target model sourced first),
# NOT directly — this file assumes the tap+targets already exist.

init
adapter speed 2000
targets mpfs.u54_1
catch {halt}

proc rd {label addr} {
    if {[catch {set v [read_memory $addr 32 1]} e]} {
        echo [format "%-26s %s : READ_ERROR %s" $label $addr $e]
        return 0xffffffff
    }
    set val [lindex $v 0]
    echo [format "%-26s %s : 0x%08x" $label $addr $val]
    return $val
}

proc poke {label addr val} {
    echo [format "POKE %-20s %s <- 0x%08x" $label $addr $val]
    catch {write_memory $addr 32 [list $val]} e
    if {$e ne ""} { echo "  write err: $e" }
    # read back
    if {[catch {set v [read_memory $addr 32 1]} e2]} {
        echo [format "  readback %s : READ_ERROR %s" $addr $e2]
    } else {
        set got [lindex $v 0]
        if {$got == $val} {
            echo [format "  readback %s : 0x%08x  <-- TOOK" $addr $got]
        } else {
            echo [format "  readback %s : 0x%08x  <-- DID NOT STICK (wanted 0x%08x)" $addr $got $val]
        }
    }
}

echo "=== DS-P2.145 IOSCB LIVE-POKE (factory values into our running silent HSS) ==="

echo "--- BEFORE: current (ours-silent) values ---"
rd SUBBLK_CLOCK_CR   0x20002084
rd SOFT_RESET_CR     0x20002088
rd BANKCONT_DDR_DPC  0x3e020004
rd IOC_REG1          0x3e040004
rd IOC_REG2          0x3e040008
rd IOC_REG3          0x3e04000c
rd IOC_REG5          0x3e040014
rd IOC_REG6          0x3e04001c
rd MMUART0_LSR       0x20000014

echo "--- POKE the factory-measured values ---"
# Factory subblock-clock / soft-reset steady state (U54-readable SYSREG space)
poke SUBBLK_CLOCK_CR   0x20002084 0x0f800223
poke SOFT_RESET_CR     0x20002088 0x307dfddc
# Factory IO-calibration / DDR-bankcont content (E51/SCB-owned — may not stick)
poke BANKCONT_DDR_DPC  0x3e020004 0x0004e563
poke IOC_REG1          0x3e040004 0x0000e000
poke IOC_REG2          0x3e040008 0x00000014
poke IOC_REG3          0x3e04000c 0x13c50f14
poke IOC_REG5          0x3e040014 0x26000000
poke IOC_REG6          0x3e04001c 0x00000006

echo "--- Nudge the UART: poke a test byte into MMUART0 THR ---"
poke MMUART0_THR       0x20000000 0x55
rd   MMUART0_LSR       0x20000014

echo "--- resume the E51 area (u54 stays halted; we only observe the console) ---"
catch {resume}

echo "=== END POKE ==="
shutdown
