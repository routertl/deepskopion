#!/bin/bash
# rr unknown-llm (AI-generated)
# DS-P2.49 — Buildroot python_overlay build script (runs inside routertl-buildroot container).
#
# Produces:
#   sw/linux/buildroot/output/images/rootfs.tar.gz   — tarball of /
#   sw/linux/buildroot/output/target/                — staged target tree
#
# The Python overlay we hand to `rr linux rootfs --python-overlay`
# is extracted from the target/ tree (next step, on the host side).

set -euo pipefail

cd "$(dirname "$0")"
HERE="$(pwd)"

# DS-P2.350 / DS-P2.351: Buildroot's own dependency check stops the 1-3 h build
# only after the clone and defconfig, with a message that names one problem at a
# time. This check runs first and names every missing host program at once.
# The set is the one support/dependencies/dependencies.sh of Buildroot 2024.11.4
# requires on the host (bison, flex, dtc, pkg-config and OpenSSL are built by
# Buildroot itself).
case "$PATH" in
    *[[:space:]]*)
        echo "ERROR: PATH contains whitespace (WSL appends Windows entries such as" >&2
        echo "       '/mnt/c/Program Files/...'), and Buildroot refuses to build with it." >&2
        echo "       Remove those entries for this shell, as in the README section" >&2
        echo "       'Host requirements of step 4, on Ubuntu and Debian':" >&2
        echo "         export PATH=\"\$(printf '%s' \"\$PATH\" | tr ':' '\\n' | grep -v '[[:space:]]' | paste -sd:)\"" >&2
        exit 2
        ;;
esac
MISSING=""
for prog in bash bc cmp cpio file find g++ gcc git make patch perl rsync sed tar unzip wget which xargs; do
    command -v "$prog" >/dev/null 2>&1 || MISSING="$MISSING $prog"
done
if [ -n "$MISSING" ]; then
    echo "ERROR: host programs missing for the Buildroot build:$MISSING" >&2
    echo "       On Ubuntu and Debian, the README section 'Host requirements of step 4'" >&2
    echo "       lists the apt-get line that installs them." >&2
    exit 2
fi

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

echo "📋 Applying deepskopion defconfig..."
cp "$HERE/buildroot_deepskopion_defconfig" configs/deepskopion_defconfig
make deepskopion_defconfig

echo "🔨 Building Buildroot (jobs=$(nproc))..."
echo "   This is the long step — toolchain + ~50 packages + glibc."
echo "   Typical first-run wall time: 1-3 hours."

# Use the host's nproc for parallelism. Buildroot itself doesn't honour
# -j across the whole graph, but each package's make is parallelised
# via BR2_JLEVEL=0 (= nproc).
make -j"$(nproc)"

echo ""
echo "✅ Buildroot build complete"
echo ""
echo "=== output/images ==="
ls -la output/images/
echo ""
echo "=== python3 in target ==="
ls -la output/target/usr/bin/python3 2>&1 || echo "  ⚠ python3 missing!"
ls output/target/usr/lib/python3* 2>&1 | head -3 || echo "  ⚠ python3 stdlib missing!"
echo ""
echo "=== msgpack in target ==="
find output/target/usr/lib/python3* -name 'msgpack*' 2>&1 | head -5 || echo "  ⚠ msgpack missing!"
echo ""
echo "=== dropbear in target ==="
ls output/target/usr/sbin/dropbear 2>&1 || echo "  ⚠ dropbear missing!"
