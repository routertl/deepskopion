# rr unknown-llm (AI-generated)
"""Ad-hoc ring diagnostic (resync investigation).

Reads the ring pointers + a window of raw ring bytes at the write head and tries
to parse zs_msg frames from it, so we can SEE whether the gather is writing valid
frames and whether the pointers are healthy — rather than guessing from the
daemon's resync annotations.

    python3 -m sw.stream.ring_probe            # one snapshot
    python3 -m sw.stream.ring_probe --watch 5  # 5 snapshots, 1s apart
"""
import argparse
import sys
import time

from .regbank import MmioRegbank, Regs
from .ddr_mmap import RealDdrMmap
from . import zs_msg

_BYTES_PER_MSG = 4


def _ptrs(rb):
    def r(x):
        try:
            return rb.read_u32(x)
        except Exception:
            return None
    return {
        "WRITE_PTR(msg)": r(Regs.RING_WRITE_PTR),
        "COMMIT_PTR": r(getattr(Regs, "RING_COMMIT_PTR", -1)) if hasattr(Regs, "RING_COMMIT_PTR") else None,
        "READ_PTR": r(getattr(Regs, "RING_READ_PTR", -1)) if hasattr(Regs, "RING_READ_PTR") else None,
        "RING_BYTES": r(Regs.RING_BYTES),
        "ALARM_CNT": r(getattr(Regs, "RING_ALARM_CNT", -1)) if hasattr(Regs, "RING_ALARM_CNT") else None,
        "BACKPRESSURE": r(getattr(Regs, "DDR_BACKPRESSURE_CNT", -1)) if hasattr(Regs, "DDR_BACKPRESSURE_CNT") else None,
        "PKT_EDGE": r(getattr(Regs, "PKT_CNT_EDGE", -1)) if hasattr(Regs, "PKT_CNT_EDGE") else None,
        "PKT_UART": r(getattr(Regs, "PKT_CNT_UART", -1)) if hasattr(Regs, "PKT_CNT_UART") else None,
    }


def _parse_window(span, label):
    cursor = 0
    parsed = 0
    resync = 0
    seen = []
    while cursor + zs_msg.HEADER_SIZE <= len(span) and parsed < 24:
        try:
            hdr = zs_msg.parse_header(span, cursor)
        except ValueError:
            cursor += _BYTES_PER_MSG
            resync += _BYTES_PER_MSG
            continue
        fb = hdr.total_size
        if cursor + fb > len(span):
            break
        pw = int.from_bytes(bytes(span[cursor + zs_msg.HEADER_SIZE:cursor + fb])[:4], "little") if fb > zs_msg.HEADER_SIZE else 0
        seen.append(f"dom={hdr.domain} capture={hdr.datalogger_id} evt={getattr(hdr,'event_id','?')} "
                    f"sz={fb} err={getattr(hdr,'is_error','?')} pw=0x{pw:08x}")
        cursor += fb
        parsed += 1
    print(f"    [{label}] parsed={parsed} frames, resync_slid={resync}B  head={bytes(span[:32]).hex()}")
    for s in seen[:6]:
        print(f"        {s}")
    return parsed, resync


def _snapshot(rb, ddr, ring_bytes, window=256):
    write_msg = rb.read_u32(Regs.RING_WRITE_PTR)
    write_ofs = (write_msg * _BYTES_PER_MSG) % ring_bytes
    commit_ofs = rb.read_u32(Regs.RING_COMMIT_PTR) % ring_bytes
    # Parse forward from BOTH candidate heads (align test): the correct head is
    # the one whose forward parse is clean (low resync).
    for label, ofs in (("WRITE_PTRx4", write_ofs), ("COMMIT_PTR", commit_ofs)):
        start = (ofs - window) % ring_bytes
        span = ddr.read_bytes(start, window)
        _parse_window(span, f"{label}@0x{start:x}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=1)
    ap.add_argument("--mmio-base", type=lambda v: int(v, 0), default=0x40000000)
    ap.add_argument("--mmio-size", type=lambda v: int(v, 0), default=0x10000)
    args = ap.parse_args()

    rb = MmioRegbank(base_addr=args.mmio_base, size=args.mmio_size)
    ring_base = (rb.read_u32(Regs.RING_BASE_HIGH) << 32) | rb.read_u32(Regs.RING_BASE_LOW)
    ring_bytes = rb.read_u32(Regs.RING_BYTES)
    print(f"ring_base=0x{ring_base:x} ring_bytes=0x{ring_bytes:x}")
    ddr = RealDdrMmap(base_addr=ring_base, size=ring_bytes)

    for i in range(max(1, args.watch)):
        print(f"--- snapshot {i} ---")
        p = _ptrs(rb)
        print("  ptrs:", {k: (f"0x{v:x}" if isinstance(v, int) else v) for k, v in p.items()})
        _snapshot(rb, ddr, ring_bytes)
        if i + 1 < args.watch:
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
