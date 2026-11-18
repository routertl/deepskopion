#!/usr/bin/env python3
"""On-board JTAG-datalogger validation (DS-P3.56 P3 HW bring-up).

Reads the DDR ring and decodes any DOMAIN_JTAG frames with the deployed
decoder + chain-aware reassembler. Proves the REA BSCANE2 tap -> jtag_datalogger
-> gather -> ring path on silicon. Usage: jtag_capture_check.py [snapshot_secs]
  no arg  -> dump whatever is currently in the ring [0..commit) and decode
  N (sec) -> snapshot, wait N s for NEW frames (drive JTAG meanwhile), decode
"""
import sys, time
sys.path.insert(0, "/usr/local/share/deepskopion")
from sw.stream import zs_msg, decoders, jtag_devices
from sw.stream.jtag_reassembler import JtagReassembler
from sw.stream.regbank import MmioRegbank
from sw.stream.ddr_mmap import RealDdrMmap
from sw.stream.testbench.emio_inject import RegbankExt
from sw.stream.testbench.ring_oracle import RingOracle

rb = RegbankExt(MmioRegbank(base_addr=0x40000000))
loc = rb.ring_locator()
base, size = loc["base_low"], loc["bytes"]
ddr = RealDdrMmap(base, size)
commit = rb.raw.read_u32(0x0164) % size
wptr = rb.raw.read_u32(0x0150)
print(f"ring base={base:#x} size={size:#x} commit={commit}B write_ptr={wptr}w "
      f"GIT_HASH={rb.raw.read_u32(0x0):#010x}")

frames = []
if len(sys.argv) > 1:
    secs = float(sys.argv[1])
    oracle = RingOracle(rb, ddr, ring_size_bytes=size)
    oracle.snapshot()
    print(f"snapshot taken; waiting {secs}s for new frames (drive JTAG now)...")
    try:
        frames = oracle.collect_new_frames(min_count=1, timeout_s=secs, max_frames=500)
    except TimeoutError as e:
        print(f"  (timeout) {e}")
else:
    blob = ddr.read_bytes(0, commit if commit else 4096)
    RingOracle._parse_blob(blob, 0, frames)

print(f"parsed {len(frames)} frame(s)")
ra = JtagReassembler(chain=jtag_devices.ZYNQ7_CHAIN)   # DS-P3.58 multi-device
njtag = 0
kinds = {}
summaries = []           # high-level reassembled register scans
for f in frames:
    h = f.header
    if h.domain != zs_msg.DOMAIN_JTAG:
        continue
    njtag += 1
    d = decoders.decode_jtag(h, f.payload, 100_000_000)
    kinds[d.kind] = kinds.get(d.kind, 0) + 1
    s = ra.feed(h, f.payload)
    if s is not None:
        summaries.append((s.kind, s.text))
print(f"=== DOMAIN_JTAG frames: {njtag}   reassembler desyncs recovered: {ra.desync_count} ===")
print("per-frame kind histogram:")
for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]):
    print(f"  {k:18s} {v}")
print(f"reassembled register scans: {len(summaries)}")
user1 = [t for k, t in summaries if "USER1" in t]
bscan = [t for k, t in summaries if k == "jtag_bscan"]
idcode = [t for k, t in summaries if k == "jtag_idcode"]
print(f"  scans naming PL=USER1: {len(user1)}   BSCAN: {len(bscan)}   IDCODE: {len(idcode)}")
print("  --- sample PL=USER1 IR decodes ---")
for t in user1[:4]:
    print(f"  >>> {t}")
print("  --- sample USER1 BSCAN DR reads (the REA payload) ---")
for t in bscan[:4]:
    print(f"  >>> {t}")
print("  --- sample IDCODE reads ---")
for t in idcode[:4]:
    print(f"  >>> {t}")
