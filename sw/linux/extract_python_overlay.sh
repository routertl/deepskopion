#!/bin/bash
# DS-P2.49 — extract python_overlay tree from Buildroot output/target/.
#
# rr linux rootfs's --python-overlay convention (RTL-P2.603) wants a
# tree that mirrors rootfs layout, containing ONLY the Python artefacts
# (interpreter + stdlib + site-packages) plus the shared libs they
# need. dropbear and other system binaries go into rootfs_overlay/
# instead.
#
# Source:  sw/linux/buildroot/output/target/
# Destination: sw/linux/python_overlay/

set -euo pipefail

cd "$(dirname "$0")"
HERE="$(pwd)"

TARGET="$HERE/buildroot/output/target"
OVERLAY="$HERE/python_overlay"

if [ ! -d "$TARGET" ]; then
    echo "❌ Buildroot target tree missing at $TARGET — run build_buildroot.sh first."
    exit 1
fi

# Wipe + re-create — keep this idempotent.
rm -rf "$OVERLAY"
mkdir -p "$OVERLAY"

echo "🐍 Copying Python interpreter + stdlib + site-packages..."
mkdir -p "$OVERLAY/usr/bin" "$OVERLAY/usr/lib"
cp -a "$TARGET/usr/bin/python3"*           "$OVERLAY/usr/bin/" 2>/dev/null
cp -a "$TARGET/usr/lib/python3"*           "$OVERLAY/usr/lib/" 2>/dev/null

echo "📚 Copying shared libs python3 links against..."
mkdir -p "$OVERLAY/lib"
# /lib has libc + ld-linux + the C runtime. Buildroot puts most libs
# here for ARM glibc targets.
cp -a "$TARGET/lib/"*.so*                  "$OVERLAY/lib/" 2>/dev/null || true
cp -a "$TARGET/lib/ld-"*                   "$OVERLAY/lib/" 2>/dev/null || true
# /usr/lib has libssl, libcrypto, libffi, libz (often), libbz2, libsqlite3...
cp -a "$TARGET/usr/lib/"*.so*              "$OVERLAY/usr/lib/" 2>/dev/null || true

# DS-P1.87: the daemon's native edge path, built from sw/stream/native with
# this Buildroot's own (soft-float) gcc, at the path edge_frames.load reads;
# its sha256s go in $OVERLAY/etc/deepskopion-native-edge.
echo "⚙️  Building libds_edge_drain.so from source..."
python3 "$HERE/../../tools/native_edge_lib.py" install --profile zybo --overlay "$OVERLAY" \
    --cc "$HERE/buildroot/output/host/bin/arm-buildroot-linux-gnueabi-gcc"

# DS-P2.262: drop the build-only files that carry the Buildroot build paths and
# rewrite those paths out of _sysconfigdata, with the target-version host python3.
echo "🧹 Removing build-host data from the overlay..."
HOST_PY="$HERE/buildroot/output/host/bin/python3"
if [ ! -x "$HOST_PY" ]; then
    echo "❌ $HOST_PY is missing: the target-version host python3 rewrites _sysconfigdata." >&2
    echo "   Buildroot builds it with the image; run sw/linux/build_buildroot.sh first." >&2
    exit 1
fi
python3 "$HERE/clean_python_overlay.py" --fix "$OVERLAY" --python "$HOST_PY"

echo ""
echo "=== python_overlay tree ==="
find "$OVERLAY" -maxdepth 3 -type d | head -20
echo ""
echo "=== size ==="
du -sh "$OVERLAY"
echo ""
echo "=== msgpack present? ==="
find "$OVERLAY/usr/lib" -name 'msgpack*' 2>/dev/null | head -5 || echo "  ⚠ msgpack missing!"
echo ""
echo "=== python3 sanity (host-side ldd will fail — different arch, expected) ==="
file "$OVERLAY/usr/bin/python3" 2>&1 | head -1 || true
