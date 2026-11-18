"""On-silicon CAN decode validator — runs the real CanReassembler over the DDR ring.

Reads ONE contiguous ring span and feeds CAN-domain events (device_id 20, the
can_datalogger) to :class:`CanReassembler`, printing the structured frame per
complete ``frame_begin..frame_end``. Same decode path the daemon (DS-P3.104)
uses, so it validates datalogger + ring + reassembler end-to-end on real hardware.

Used to clear DS-P3.103 checkpoint C (byte-exact decode of STD / extended /
remote / heavily-stuffed frames). Prefer ``--since`` with a prior COMMIT value
(a frame boundary) over a fixed window to avoid leading misalignment artifacts.

    # enable the datalogger first:  devmem 0x40000020 32 0xA033
    C0=$(devmem 0x40000164 32)              # RING_COMMIT_PTR before sending
    cansend can0 123#ABCD
    python3 -m sw.stream.can_decode --since $C0
    # or a trailing window:
    python3 -m sw.stream.can_decode --window 2048
"""
import argparse
import sys

from .regbank import MmioRegbank, Regs
from .ddr_mmap import RealDdrMmap
from . import zs_msg
from .can_reassembler import CanReassembler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=lambda v: int(v, 0), default=None,
                    help="parse [start_ofs .. COMMIT]; start_ofs = a prior RING_COMMIT_PTR (frame boundary)")
    ap.add_argument("--window", type=lambda v: int(v, 0), default=2048,
                    help="parse this many bytes ending at COMMIT (ignored if --since given)")
    ap.add_argument("--mmio-base", type=lambda v: int(v, 0), default=0x40000000)
    ap.add_argument("--mmio-size", type=lambda v: int(v, 0), default=0x10000)
    args = ap.parse_args()

    rb = MmioRegbank(base_addr=args.mmio_base, size=args.mmio_size)
    ring_base = (rb.read_u32(Regs.RING_BASE_HIGH) << 32) | rb.read_u32(Regs.RING_BASE_LOW)
    ring_bytes = rb.read_u32(Regs.RING_BYTES)
    ddr = RealDdrMmap(base_addr=ring_base, size=ring_bytes)

    commit = rb.read_u32(Regs.RING_COMMIT_PTR) % ring_bytes
    if args.since is not None:
        start = args.since % ring_bytes
        length = (commit - start) % ring_bytes
    else:
        length = args.window
        start = (commit - length) % ring_bytes
    span = ddr.read_bytes(start, length)

    reasm = CanReassembler()
    cursor = 0
    n = 0
    while cursor + zs_msg.HEADER_SIZE <= len(span):
        try:
            hdr = zs_msg.parse_header(span, cursor)
        except ValueError:
            cursor += 4
            continue
        fb = hdr.total_size
        if cursor + fb > len(span):
            break
        payload = bytes(span[cursor + zs_msg.HEADER_SIZE:cursor + fb])
        if hdr.domain == zs_msg.DOMAIN_CAN:
            fr = reasm.feed(hdr, payload)
            if fr is not None:
                f = fr.fields
                n += 1
                print("FRAME id=0x%X ide=%d rtr=%d dlc=%d data=[%s] crc=0x%X ack=%d errors=%s" % (
                    f["id"], f["ide"], f["rtr"], f["dlc"],
                    " ".join("%02X" % x for x in f["data"]),
                    (f["crc"] or 0), f["ack"], f["errors"]))
        cursor += fb
    print("DECODED_FRAMES=%d" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
