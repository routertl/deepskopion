# U-Boot boot script — Zybo Z7-20 for deepskopion.
#
# SD-FIRST (DS-P2.68). The board boots ITS OWN SD (BOOT.bin + system.dtb + uImage
# flashed by `rr linux flash-sd`) by default — so each board always runs the
# image built for IT. TFTP is an explicit OPT-IN dev accelerator, not the default.
#
# Why the change: TFTP-first (the old default, RTL-P3.639) made the board boot
# whatever sat in the host's shared TFTP root. On a bench with BOTH a Zybo Classic
# (zybo, eye) and a Z7-20 (zybo-z7-20, canfd), a Classic-built system.dtb in
# that root got served to the Z7-20 — which then ran the Classic's 50 MHz ps-clk
# (UART garble: 76800 not 115200) and a dtb with NO ctucanfd nodes (no can0/can1).
# A whole "rebuild didn't take / CAN regressed" rabbit-hole (2026-06-20). SD-first
# kills that class of bug: the SD is board-specific and `flash-sd` keeps it fresh.
#
#   bootsrc=sd  (default) : boot the local SD image (board-correct, always fresh)
#   bootsrc=net           : OPT IN to the TFTP dev loop (set in u-boot env). Only
#                           safe when the server holds THIS board's artifacts —
#                           keep it synced (rr should sync on flash-sd; DS-P2.68).
#
# The bitstream always comes from BOOT.bin on SD (Zynq-7 BootROM can't net-boot
# the FSBL). System uses the bootargs embedded in the DT chosen node (RTL-P2.608,
# including owner-supplied network arguments). Compiled via mkimage into boot.scr.

if test "${bootsrc}" = "" ; then setenv bootsrc sd ; fi

# DS-T2.58: u-boot's default ${bootargs} env OVERWRITES /chosen/bootargs during
# bootm (fdt_chosen), silently dropping target-owned kernel arguments. Delete
# ${bootargs} so u-boot leaves the DT chosen node intact.
env delete bootargs

# DS-P2.84: fetch by an explicit ${ipaddr}/<project>/ path. The bench TFTP root
# is FLAT and shared by every board and every project, so the client — not the
# server — is what scopes the request:
#
#     <tftp-root>/<board-ip>/<project>/active -> <slot>
#     <tftp-root>/<board-ip>/<project>/<slot>/{uImage,system.dtb,...}
#
# A bare `tftpboot uImage` (what this used to do) resolves to the ONE shared
# root-level uImage, which is how a board ends up booting another board's kernel
# — measured 2026-08-10: a board at .7.4 asking for "uImage" was served a file
# 300 KB different from its own. The old mitigation was a server-side per-client
# root (dnsmasq tftp-unique-root), and it had been silently dead since
# 2026-07-21, leaving the bare form completely unguarded. An explicit path needs
# no such server feature, and lets one board host several projects at once.
# DS-P2.87 (NET-6): retry the fetch pair, sleeping between attempts. The
# Z7-20's link partner is a 10/100-only port whose autoneg takes >8 s
# (measured, NET-4) — longer than u-boot's PHY wait — so the FIRST tftpboot
# deterministically dies with "Waiting for PHY auto negotiation ... TIMEOUT"
# (observed live 2026-08-11, twice back-to-back). u-boot's phy_startup only
# WAITS for completion (genphy_update_link polls BMSR ANEGCOMPLETE; it does
# not restart autoneg), so the negotiation begun during attempt 1 finishes
# during the sleep and a later attempt finds the link up. Without the loop,
# bootsrc=net falls back to SD on every cold autoneg.
if test "${bootsrc}" = "net" ; then
    if test "${ipaddr}" = "" || test "${serverip}" = "" ; then
        echo "deepskopion: netboot has no ipaddr/serverip; requesting DHCP"
        dhcp
    fi
    if test "${ipaddr}" = "" || test "${serverip}" = "" ; then
        echo "deepskopion: netboot needs DHCP or user-supplied ipaddr/serverip"
        setenv bootsrc sd
    fi
fi
if test "${bootsrc}" = "net" ; then
    echo "deepskopion: TFTP dev-loop boot from ${serverip} (opt-in; unset bootsrc to pin SD)"
    echo "deepskopion: fetching ${ipaddr}/deepskopion/active/{system.dtb,uImage}"
    setenv netboot_ok 0
    for try in 1 2 3 ; do
        if test "${netboot_ok}" = "0" ; then
            if tftpboot 0x2000000 ${ipaddr}/deepskopion/active/system.dtb && tftpboot 0x2080000 ${ipaddr}/deepskopion/active/uImage ; then
                setenv netboot_ok 1
            else
                echo "deepskopion: TFTP attempt ${try}/3 failed (slow autoneg? NET-4) - waiting for link"
                sleep 8
            fi
        fi
    done
    if test "${netboot_ok}" = "1" ; then
        echo "deepskopion: booting TFTP image"
        bootm 0x2080000 - 0x2000000
    fi
    echo "deepskopion: TFTP unavailable - falling back to SD"
fi

fatload mmc 0:1 0x2000000 system.dtb
fatload mmc 0:1 0x2080000 uImage
bootm 0x2080000 - 0x2000000
