#!/bin/bash
# Stage the OpenSSH `scp` client binary from the Buildroot target tree into
# rootfs_overlay/ so `rr linux rootfs` rsyncs it into the deployed initramfs.
#
# DS-P3.79: dropbear (the running sshd) does NOT ship scp when
# BR2_PACKAGE_DROPBEAR_SMALL=y (the Buildroot default, and unchanged here —
# dropbear stays small), so host->board file pushes during dev had to fall
# back to `ssh root@board 'cat > /path' < file`. BR2_PACKAGE_OPENSSH(+CLIENT)
# in the defconfig builds a real scp, but extract_python_overlay.sh is
# deliberately python-only, so — mirroring stage_dropbear_overlay.sh /
# stage_can_overlay.sh — this script closes that gap.
#
# Only `scp` is staged, not the rest of the OpenSSH client suite (ssh,
# sftp, ssh-keygen, ...): dropbear's dbclient already covers interactive
# ssh, and staging the whole suite would duplicate that for no dev-loop
# benefit. scp needs shared libs beyond what python_overlay already
# provides (libc.so.6 / ld-linux.so.3 / libcrypt.so.2): OpenSSL's
# libssl.so.3 + libcrypto.so.3, which dropbear-small never needed
# (BR2_PACKAGE_ZLIB/BR2_PACKAGE_LIBTOMCRYPT are only `select`ed
# `if !BR2_PACKAGE_DROPBEAR_SMALL`) — so those two libs are staged here too.
#
# Source:      sw/linux/buildroot/output/target/usr/bin/scp (+ usr/lib/lib{ssl,crypto}.so.3)
# Destination: sw/linux/rootfs_overlay/usr/bin/scp (+ usr/lib/lib{ssl,crypto}.so.3)

set -euo pipefail

cd "$(dirname "$0")"
HERE="$(pwd)"
TGT="$HERE/buildroot/output/target"

stage() {
    local src="$TGT/$1" dst="$HERE/rootfs_overlay/$2"
    if [ ! -e "$src" ]; then
        echo "❌ $src missing — run ./build_buildroot.sh first (BR2_PACKAGE_OPENSSH + _CLIENT)." >&2
        exit 1
    fi
    mkdir -p "$(dirname "$dst")"
    cp -a "$src" "$dst"
    echo "   ✅ $2 ($(du -h "$dst" | cut -f1))"
}

echo "Staging OpenSSH scp (+ libssl/libcrypto) into rootfs_overlay/ ..."
stage usr/bin/scp        usr/bin/scp
stage usr/lib/libssl.so.3    usr/lib/libssl.so.3
stage usr/lib/libcrypto.so.3 usr/lib/libcrypto.so.3

echo "   libc.so.6 / ld-linux.so.3 / libcrypt.so.2 already ride in python_overlay."
echo "   next: rr linux rootfs   (rebuild the initramfs) + redeploy BOOT.bin/uImage."
