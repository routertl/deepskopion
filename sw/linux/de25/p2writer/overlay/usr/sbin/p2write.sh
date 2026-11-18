#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
# DS-P2.264: write the DE25 SD rootfs partition (P2) from a TFTP-served image,
# from a RAM-root (initramfs) boot, so P2 is never mounted while it is written.
#
# This is a bench TOOL image's payload (target de25_p2writer), not a product
# image. It writes exactly one block device, P2, and nothing else: never P1,
# the U-Boot environment or QSPI.
#
# Parameters come from the kernel command line (never from a tracked file):
#   p2w_host=  bench host address (TFTP server and rollback listener)
#   p2w_img=   TFTP path of the image to write
#   p2w_sha=   sha256 of that image (64 hex)
#   p2w_bytes= its size in bytes
#   p2w_port=  TCP port of the bench rollback listener, or "none" (restore leg)
#
# Sequence, one console line per step with the prefix "P2W ", stopping at the
# first refusal ("P2W REFUSE <reason>", exit 1). Nothing is written before R2:
#   R1  P2 must not be mounted, and the image must fit in P2
#   rollback copy of the current P2 to the bench listener (sha256 + bytes printed)
#   R2  verify pass: the served image's sha256 and size equal p2w_sha/p2w_bytes
#   write pass, sync
#   R3  read-back: the first p2w_bytes of P2 hash to p2w_sha
#
# Test hooks (host tests only; unset on the board): P2W_DEV, P2W_MOUNTS,
# P2W_PARTITIONS, P2W_CMDLINE override the device and /proc paths.

DEV=${P2W_DEV:-/dev/mmcblk0p2}
MOUNTS=${P2W_MOUNTS:-/proc/mounts}
PARTS=${P2W_PARTITIONS:-/proc/partitions}
CMDLINE=${P2W_CMDLINE:-/proc/cmdline}
TMP=${TMPDIR:-/tmp}

say() { echo "P2W $*"; }
refuse() { echo "P2W REFUSE $*"; exit 1; }

arg() {
    tr ' ' '\n' < "$CMDLINE" | sed -n "s/^$1=//p" | tail -1
}

HOST=$(arg p2w_host); IMG=$(arg p2w_img); SHA=$(arg p2w_sha)
BYTES=$(arg p2w_bytes); PORT=$(arg p2w_port)
for v in HOST IMG SHA BYTES PORT; do
    eval "val=\$$v"
    [ -n "$val" ] || refuse "missing parameter $v on the kernel command line"
done
echo "$SHA" | grep -Eq '^[0-9a-f]{64}$' || refuse "p2w_sha is not a sha256"
echo "$BYTES" | grep -Eq '^[0-9]+$' || refuse "p2w_bytes is not a byte count"
say "params host=$HOST img=$IMG sha256=$SHA bytes=$BYTES port=$PORT dev=$DEV"

# R1: P2 unmounted, partition table shown, image fits.
say "partitions:"
sed 's/^/P2W   /' "$PARTS"
if awk -v d="$DEV" '$1 == d {f=1} END {exit !f}' "$MOUNTS"; then
    refuse "R1 $DEV is mounted"
fi
[ -b "$DEV" ] || [ -n "${P2W_DEV:-}" ] || refuse "R1 $DEV is not a block device"
SIZE=$(blockdev --getsize64 "$DEV" 2>/dev/null) || refuse "R1 cannot read the size of $DEV"
say "R1 $DEV size=$SIZE unmounted"
[ "$BYTES" -le "$SIZE" ] || refuse "R1 image $BYTES bytes exceeds $DEV size $SIZE"

# Rollback copy of the current P2 (skipped only for the restore leg).
if [ "$PORT" = "none" ]; then
    say "ROLLBACK skipped (p2w_port=none)"
else
    OLD_SHA=$(sha256sum < "$DEV" | cut -d' ' -f1) || refuse "cannot hash $DEV"
    nc "$HOST" "$PORT" < "$DEV" || refuse "rollback stream to $HOST:$PORT failed"
    say "ROLLBACK bytes=$SIZE sha256=$OLD_SHA"
fi

# R2: verify pass, before any write.
# Streamed, never stored: the image is larger than the board's RAM root allows.
GOT=$( { tftp -g -r "$IMG" -l - "$HOST" 2>"$TMP/p2w_tftp.err"; echo $? > "$TMP/p2w_tftp.rc"; } \
       | dd bs=65536 2>"$TMP/p2w_dd.err" | sha256sum | cut -d' ' -f1)
[ "$(cat "$TMP/p2w_tftp.rc")" = 0 ] || refuse "R2 tftp fetch of $IMG failed: $(tail -1 "$TMP/p2w_tftp.err")"
GOTB=$(sed -n 's/^\([0-9][0-9]*\) bytes.*/\1/p' "$TMP/p2w_dd.err" | tail -1)
say "R2 served sha256=$GOT bytes=$GOTB"
[ "$GOT" = "$SHA" ] || refuse "R2 sha256 $GOT != p2w_sha $SHA (nothing written)"
[ "$GOTB" = "$BYTES" ] || refuse "R2 bytes $GOTB != p2w_bytes $BYTES (nothing written)"

# Write pass.
tftp -g -r "$IMG" -l "$DEV" "$HOST" 2>"$TMP/p2w_tftp.err" || refuse "write pass tftp failed: $(tail -1 "$TMP/p2w_tftp.err")"
sync
say "WRITE done"

# R3: read-back.
BACK=$(head -c "$BYTES" "$DEV" | sha256sum | cut -d' ' -f1)
say "R3 readback sha256=$BACK"
[ "$BACK" = "$SHA" ] || refuse "R3 readback $BACK != p2w_sha $SHA"
say "DONE wrote $BYTES bytes to $DEV, readback equal"
exit 0
