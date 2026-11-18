#!/bin/bash
# PF-L0 (DS-P2.125) — build the riscv64 python_overlay for the PolarFire SoC kit.
# Runs DIRECTLY ON THE HOST: it clones Buildroot 2024.11.4 and `make`s it here,
# the same way sw/linux/build_buildroot.sh (armhf) does — there is no
# routertl-buildroot container in this flow. The riscv64 sibling of
# sw/linux/build_buildroot.sh + sw/linux/extract_python_overlay.sh (armhf),
# combined because the pfsoc overlay is a fresh, arch-distinct tree with no
# other consumer.
#
# Produces:
#   sw/linux/pfsoc/buildroot/output/target/   — staged rv64gc target tree
#   sw/linux/pfsoc/python_overlay/            — the extracted overlay handed to
#                                               `rr linux rootfs --python-overlay`
#
# WHY a separate build: the MPFS095T application cores are SiFive U54 (rv64gc);
# a riscv64 kernel cannot exec the armhf sw/linux/python_overlay, so the daemon's
# CPython + msgpack + _ssl/_zlib extensions must be recompiled for LP64D. Same
# userspace CONTRACT as the armhf overlay, different ABI.
#
# WHY dropbear + scp are extracted HERE and not by sw/linux/stage_dropbear_overlay.sh:
# that shared script stages the ARM binary out of the armhf Buildroot tree into
# the SHARED sw/linux/rootfs_overlay, which a riscv64 kernel cannot exec (S49
# dropbear would fail closed, port 22 dead). The PolarFire kit's dropbear/scp
# must be the rv64gc binaries built here, and they travel in THIS per-arch
# overlay (applied via `rr linux rootfs --python-overlay`), never the shared one.
# Guarded by tools/tests/test_pfsoc_overlay_arch_native.py.
#
# Typical first-run wall time: 1-3 hours (toolchain + glibc + ~50 packages).
set -euo pipefail

cd "$(dirname "$0")"
HERE="$(pwd)"

# The Buildroot release is a tag, never a branch: a branch tip moves with each
# point release and silently changes the compiler that builds this overlay and
# libds_edge_drain.so (sw/stream/native/README.md records the result per release).
BUILDROOT_REF="2024.11.4"

if [ ! -d buildroot ]; then
    echo "🥾 Cloning Buildroot $BUILDROOT_REF (shallow)..."
    git clone --depth 1 -b "$BUILDROOT_REF" \
        https://github.com/buildroot/buildroot.git \
        buildroot
fi
TREE_REF="$(git -C buildroot describe --tags --exact-match 2>/dev/null || echo unknown)"
echo "📌 Buildroot tree: $TREE_REF (pinned: $BUILDROOT_REF)"
if [ "$TREE_REF" != "$BUILDROOT_REF" ]; then
    echo "⚠ the existing buildroot/ tree is not the pinned $BUILDROOT_REF;" \
         "remove it to rebuild from the pinned release" >&2
fi

cd buildroot
echo "📋 Applying deepskopion riscv64 defconfig..."
cp "$HERE/buildroot_pfsoc_defconfig" configs/deepskopion_pfsoc_defconfig
make deepskopion_pfsoc_defconfig

echo "🔨 Building Buildroot (jobs=$(nproc)) — the long step..."
make -j"$(nproc)"

TARGET="$HERE/buildroot/output/target"
OVERLAY="$HERE/python_overlay"
[ -d "$TARGET" ] || { echo "❌ Buildroot target tree missing at $TARGET" >&2; exit 1; }

echo "🐍 Extracting python_overlay -> $OVERLAY ..."
rm -rf "$OVERLAY"
mkdir -p "$OVERLAY/usr/bin" "$OVERLAY/usr/sbin" "$OVERLAY/usr/lib" "$OVERLAY/lib"
cp -a "$TARGET/usr/bin/python3"*  "$OVERLAY/usr/bin/" 2>/dev/null
cp -a "$TARGET/usr/lib/python3"*  "$OVERLAY/usr/lib/" 2>/dev/null
cp -a "$TARGET/lib/"*.so*         "$OVERLAY/lib/"     2>/dev/null || true
cp -a "$TARGET/lib/ld-"*          "$OVERLAY/lib/"     2>/dev/null || true
cp -a "$TARGET/usr/lib/"*.so*     "$OVERLAY/usr/lib/" 2>/dev/null || true

# rv64gc dropbear + OpenSSH client scp — the arch-native SSH binaries the
# rootfs_overlay init scripts (S49dropbear) and the SW-refresh loop need. These
# must NOT come from the shared armhf stage_dropbear_overlay.sh (a riscv64 kernel
# cannot exec an ARM ELF). Staged here so they ride in THIS per-arch overlay.
if [ ! -x "$TARGET/usr/sbin/dropbear" ]; then
    echo "❌ $TARGET/usr/sbin/dropbear missing — defconfig must keep BR2_PACKAGE_DROPBEAR=y" >&2
    exit 1
fi
cp -a "$TARGET/usr/sbin/dropbear"  "$OVERLAY/usr/sbin/"
cp -a "$TARGET/usr/bin/dbclient"   "$OVERLAY/usr/bin/" 2>/dev/null || true
cp -a "$TARGET/usr/bin/scp"        "$OVERLAY/usr/bin/" 2>/dev/null || true
cp -a "$TARGET/usr/bin/ssh"        "$OVERLAY/usr/bin/" 2>/dev/null || true

# DS-P1.87: the daemon's native edge path, built from sw/stream/native with
# this Buildroot's riscv64 gcc, at the path edge_frames.load reads; its
# sha256s go in $OVERLAY/etc/deepskopion-native-edge.
echo "⚙️  Building libds_edge_drain.so from source..."
python3 "$HERE/../../../tools/native_edge_lib.py" install --profile pfsoc --overlay "$OVERLAY" \
    --cc "$HERE/buildroot/output/host/bin/riscv64-buildroot-linux-gnu-gcc"

echo ""
echo "=== overlay size ==="; du -sh "$OVERLAY"
echo "=== msgpack present? ==="
find "$OVERLAY/usr/lib" -name 'msgpack*' 2>/dev/null | head -5 || echo "  ⚠ msgpack missing!"
echo "=== python3 arch (host ldd will fail — different arch, expected) ==="
file "$OVERLAY/usr/bin/python3" 2>&1 | head -1 || true
echo "=== dropbear arch (must read RISC-V, NOT ARM) ==="
file "$OVERLAY/usr/sbin/dropbear" 2>&1 | head -1 || true
