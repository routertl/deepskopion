#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""On-board datalogger health soak (runs ON the Zynq board) — DS-T2.24.

  python3 ds_datalogger_health.py <uart|i2c|spi|uart_i2c|all> [minutes]

A structured, self-verifying soak that drives the EMIO dataloggers and watches the
whole capture pipe (datalogger -> datalogger_gather -> zs_msg_framer -> ddr_stream_axi
-> DDR ring) for both RING health and ID health. Used to validate DS-T2.24 on
silicon: every mode (single, UART+I2C pair, all three) PASSed on bit 0x6F48C22D.
Complements run_self_test.py (one-shot contract checks) with a sustained loop.
On HW the EMIO mux routes one bus at a time, so multi-protocol modes keep all
selected dataloggers ENABLED and alternate the drive.

Enables ONLY the requested datalogger(s) (a fresh reboot leaves all disabled),
drives a SLOW infinite-ish loop for <minutes>, and every few rounds reports:
  * RING health  — commit-ptr advancing + wrapping, read-ptr (drain) tracking,
                   BURST/STREAM FIFO overflow counters, per-domain DROP_CNT.
  * ID health    — every framed zs_msg carries the EXPECTED (datalogger_id, domain)
                   for the protocol that produced it; counts domOK vs domBAD and
                   flags any UNEXPECTED datalogger_id (a disabled datalogger leaking).

The SW read-pointer drain (DS-P2.59) is exercised: after reading each batch we
advance RING_READ_PTR to the commit offset so the ring keeps wrapping cleanly.

Stop with Ctrl-C (or it ends after <minutes>); a VERDICT is printed either way.
"""
import sys, time, random
sys.path.insert(0, "/usr/local/share/deepskopion")
from collections import Counter

from sw.stream.testbench.emio_inject import SelfTestBench
from sw.stream.testbench.ring_oracle import RingOracle
from sw.stream.ddr_mmap import RealDdrMmap
from sw.stream.regbank import Regs
from sw.stream.zs_msg import domain_of

DEV = {"uart": 12, "i2c": 4, "spi": 8}        # device / datalogger ids
DOM = {"uart": 0x1, "i2c": 0x2, "spi": 0x3}   # msg_type[11:8] domain codes
I2C_ADDR = 0x50
INJ_I2C  = [0xD5, 0x09, 0xC0, 0xDE]
INJ_SPI  = [0x12, 0x34, 0x56, 0x78]
SPI_PAT  = [0x55, 0xAA, 0x0F, 0xF0]
UART_MSG = b"Hello DEEPSKOPION!"
PAUSE    = 0.4                                  # slow loop

mode    = sys.argv[1] if len(sys.argv) > 1 else "uart"
minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
if mode in ("all", "uart_i2c_spi"):
    protos = ["uart", "i2c", "spi"]
elif mode == "uart_i2c":
    protos = ["uart", "i2c"]
else:
    protos = [mode]
assert all(p in DEV for p in protos), "mode must be uart|i2c|spi|uart_i2c|all"

bench = SelfTestBench(spi_speed_hz=1_000_000)
rb    = bench.regbank
loc   = rb.ring_locator()
RING  = loc["bytes"]
ring  = RingOracle(rb, RealDdrMmap(loc["base_low"], RING), ring_size_bytes=RING)
rng   = random.Random(0xC0FFEE)

GIT = rb.raw.read_u32(Regs.GIT_HASH)
print("[seq] bit GIT_HASH=0x%08X  mode=%s protos=%s  %.1f min  ring=%d KiB"
      % (GIT, mode, protos, minutes, RING // 1024))


def health():
    r = rb.raw.read_u32
    return dict(commit=r(Regs.RING_COMMIT_PTR), read=r(Regs.RING_READ_PTR),
                bovf=r(Regs.FIFO_OVERFLOW_CNT),
                sovf=r(Regs.FIFO_OVERFLOW_CNT_RSVD),
                dU=r(Regs.DROP_CNT_UART), dI=r(Regs.DROP_CNT_I2C),
                dS=r(Regs.DROP_CNT_SPI))


# ── enable ONLY the requested dataloggers + load injector responses ────────────
for p in protos:
    rb.enable_datalogger(DEV[p], enable=True)
if "i2c" in protos:
    rb.load_i2c_response(I2C_ADDR, INJ_I2C)
if "spi" in protos:
    rb.load_spi_response(INJ_SPI)

# ── one-time UART auto-baud calibration (150-byte random burst) ─────────────
if "uart" in protos:
    rb.set_emio_mux(uart=True); time.sleep(0.02)
    bench.uart.write(bytes(rng.randint(0, 0xFF) for _ in range(150)))
    time.sleep(0.30)
    ring.advance_read(ring.commit_offset())     # discard the cal burst

EXPECT = {DEV[p]: DOM[p] for p in protos}        # datalogger_id -> domain
ids, domok, dombad, alien = Counter(), 0, 0, Counter()
wraps = 0
last_commit = None
t0, t_end = time.time(), time.time() + minutes * 60
rnd = 0


def drive(p):
    if p == "uart":
        rb.set_emio_mux(uart=True); time.sleep(0.01)
        bench.uart.write(UART_MSG); bench.uart.write(b"\x00\xFF\x00\xFF")
    elif p == "i2c":
        rb.set_emio_mux(i2c=True); time.sleep(0.01)
        try:
            bench.i2c.write_then_read(I2C_ADDR, bytes([0x00]), 4)
        except Exception as e:
            print("  [i2c] drive err: %s" % e)
    elif p == "spi":
        rb.set_emio_mux(spi=True); time.sleep(0.01)
        bench.spi.xfer(SPI_PAT)


try:
    while time.time() < t_end:
        ring.snapshot()                 # mark commit; collect only THIS round's frames
        for p in protos:
            drive(p)
            time.sleep(PAUSE)
        # read new frames, classify ID-health, then drain (advance read ptr)
        try:
            frames = ring.collect_new_frames(min_count=1, timeout_s=1.0, max_frames=500)
        except Exception:
            frames = []
        for f in frames:
            sid, dom = f.datalogger_id, domain_of(f.msg_type)
            ids[sid] += 1
            if sid in EXPECT:
                domok += 1 if dom == EXPECT[sid] else 0
                dombad += 0 if dom == EXPECT[sid] else 1
            else:
                alien[sid] += 1
        ring.advance_read(ring.commit_offset())
        if rnd % 5 == 0:
            h = health()
            if last_commit is not None and h["commit"] < last_commit:
                wraps += 1
            last_commit = h["commit"]
            print("[%4ds] commit=0x%06x read=0x%06x wraps=%d bovf=%d sovf=%d "
                  "drop(U/I/S)=%d/%d/%d frames=%d ids=%s domBAD=%d alien=%s"
                  % (time.time() - t0, h["commit"], h["read"], wraps, h["bovf"],
                     h["sovf"], h["dU"], h["dI"], h["dS"], sum(ids.values()),
                     dict(ids), dombad, dict(alien)))
        rnd += 1
except KeyboardInterrupt:
    print("\n[seq] interrupted")

# ── verdict ─────────────────────────────────────────────────────────────────
h = health()
total = sum(ids.values())
exp_ids = set(EXPECT)
seen_ids = set(ids)
print("=" * 66)
print("[VERDICT] mode=%s  frames=%d  ids=%s  wraps=%d" % (mode, total, dict(ids), wraps))
print("  expected datalogger_ids=%s  seen=%s" % (sorted(exp_ids), sorted(seen_ids)))
print("  ID-health: domOK=%d domBAD=%d  alien_ids=%s" % (domok, dombad, dict(alien)))
print("  overflow burst=%d stream=%d  drops U/I/S=%d/%d/%d"
      % (h["bovf"], h["sovf"], h["dU"], h["dI"], h["dS"]))
ok = (total > 0 and seen_ids == exp_ids and not alien and dombad == 0
      and h["bovf"] == 0 and h["sovf"] == 0
      and h["dU"] == 0 and h["dI"] == 0 and h["dS"] == 0)
print("  RESULT: %s" % ("PASS" if ok else "FAIL"))
try:
    bench.close()
except Exception:
    pass
sys.exit(0 if ok else 1)
