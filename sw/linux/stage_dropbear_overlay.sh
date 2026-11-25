#!/bin/bash
# rr unknown-llm (AI-generated)
# Stage the dropbear sshd binary from the Buildroot target tree into
# rootfs_overlay/ so `rr linux rootfs` rsyncs it into the deployed initramfs.
#
# dropbear IS built by build_buildroot.sh (BR2_PACKAGE_DROPBEAR=y in the
# defconfig), but extract_python_overlay.sh is deliberately python-only, so
# the binary never reaches the overlay that `rr linux rootfs` deploys — which
# is why the board boots with no sshd (port 22 closed). This script closes
# that gap, mirroring the python_overlay extraction convention.
#
# Its three NEEDED shared libs (libc.so.6, ld-linux.so.3, libcrypt.so.2) are
# ALREADY provided by python_overlay, so ONLY the binary is staged here.
#
# The staged binary is gitignored (an ARM ELF, regenerated from Buildroot) —
# the same policy python_overlay/ uses. The tracked init script keeps sshd
# closed until the owner supplies a key on first boot; no access key is staged
# by this repository (DS-C1).
#
# Source:      sw/linux/buildroot/output/target/usr/sbin/dropbear
# Destination: sw/linux/rootfs_overlay/usr/sbin/dropbear

set -euo pipefail

cd "$(dirname "$0")"
HERE="$(pwd)"

# DS_BUILDROOT_OUTPUT points a checkout without its own Buildroot build (a
# linked worktree) at another checkout's sw/linux/buildroot/output.
BR_OUT="${DS_BUILDROOT_OUTPUT:-$HERE/buildroot/output}"
SRC="$BR_OUT/target/usr/sbin/dropbear"
DST="$HERE/rootfs_overlay/usr/sbin/dropbear"

if [ ! -x "$SRC" ]; then
    # DS-P3.201: this script is part of linux.rootfs.stage_command, so a failure
    # here aborts `rr linux rootfs` instead of building an image with no sshd.
    echo "❌ stage_dropbear_overlay.sh: $SRC missing, so the rootfs would have no sshd."
    echo "   Run sw/linux/build_buildroot.sh first (BR2_PACKAGE_DROPBEAR=y), or set"
    echo "   DS_BUILDROOT_OUTPUT to a built sw/linux/buildroot/output; then rr linux rootfs."
    exit 1
fi

mkdir -p "$(dirname "$DST")"
cp -a "$SRC" "$DST"

echo "✅ staged dropbear -> rootfs_overlay/usr/sbin/dropbear ($(du -h "$DST" | cut -f1))"
echo "   NEEDED libs (libc.so.6 / ld-linux.so.3 / libcrypt.so.2) already ride in python_overlay."
echo "   next: rr linux rootfs   (rebuild the initramfs) + redeploy BOOT.bin."
