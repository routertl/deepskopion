#!/bin/bash
# Stage the can-utils + iproute2 binaries from the Buildroot target tree into
# rootfs_overlay/ so `rr linux rootfs` rsyncs them into the deployed initramfs.
#
# They ARE built by build_buildroot.sh (BR2_PACKAGE_CAN_UTILS=y +
# BR2_PACKAGE_IPROUTE2=y in the defconfig), but extract_python_overlay.sh is
# deliberately python-only, so the binaries never reach the overlay that
# `rr linux rootfs` deploys. This script closes that gap, mirroring
# stage_dropbear_overlay.sh.
#
# Why iproute2 (not busybox `ip`): the FULL `ip` is needed for
# `ip link set canX type can bitrate ...` — busybox's `ip` applet cannot
# configure a CAN link. DS-P3.103 checkpoint B drives can0->can1 across the
# fabric wired-AND at 250 kbit and needs this.
#
# NB (RTL-P2.717): busybox's defconfig ships an `ip` applet, so `busybox
# --install` lays an `ip -> ../bin/busybox` symlink. The real iproute2 `ip`
# landing on that path used to make `rr linux rootfs` write THROUGH the symlink
# and clobber /bin/busybox (→ kernel panic on /init). Fixed in routertl
# (RTL-P2.717): the overlay copy now unlinks an existing dst first, plus a
# busybox-integrity backstop. This staging is safe on rr >= that fix.
#
# The staged binaries are gitignored (ARM ELFs, regenerated from Buildroot) —
# the same policy python_overlay/ and the dropbear staging use.

set -euo pipefail

cd "$(dirname "$0")"
HERE="$(pwd)"
TGT="$HERE/buildroot/output/target"

# src-in-buildroot  ->  dst-in-overlay
stage() {
    local src="$TGT/$1" dst="$HERE/rootfs_overlay/$2"
    if [ ! -x "$src" ]; then
        echo "❌ $src missing — run ./build_buildroot.sh first (CAN_UTILS + IPROUTE2)."
        exit 1
    fi
    mkdir -p "$(dirname "$dst")"
    cp -a "$src" "$dst"
    echo "   ✅ $2 ($(du -h "$dst" | cut -f1))"
}

echo "Staging can-utils + iproute2 into rootfs_overlay/ ..."
stage sbin/ip          usr/sbin/ip
stage usr/bin/cansend  usr/bin/cansend
stage usr/bin/candump  usr/bin/candump
stage usr/bin/cangen   usr/bin/cangen
stage usr/bin/candatalogger usr/bin/candatalogger

echo "   shared libs (libc / ld-linux) already ride in python_overlay."
echo "   next: rr linux rootfs  (rebuild initramfs) + redeploy BOOT.bin."
