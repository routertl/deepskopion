#!/usr/bin/env python3
"""Live UART/I2C/SPI capturing session — drive EMIO stimulus, decode the ring."""
import sys, time, random
sys.path.insert(0, "/usr/local/share/deepskopion")
from sw.stream.testbench.emio_inject import SelfTestBench
from sw.stream.testbench.ring_oracle import RingOracle
from sw.stream.ddr_mmap import RealDdrMmap
from sw.stream.regbank import Regs
from sw.stream import decoders, zs_msg

DEV = {"uart": 12, "i2c": 4, "spi": 8}
I2C_ADDR = 0x50
INJ_I2C = [0xD5, 0x09, 0xC0, 0xDE]
INJ_SPI = [0x12, 0x34, 0x56, 0x78]
SPI_PAT = [0x9F, 0xAA, 0x0F, 0xF0]
UART_MSG = b"Hi DEEPSKOPION!"

bench = SelfTestBench(spi_speed_hz=1_000_000)
rb = bench.regbank
loc = rb.ring_locator()
RING = loc["bytes"]
ring = RingOracle(rb, RealDdrMmap(loc["base_low"], RING), ring_size_bytes=RING)
print("bit GIT_HASH=0x%08X" % rb.raw.read_u32(Regs.GIT_HASH))

rb.enable_datalogger(16, enable=False)                 # quiet JTAG
for p in ("uart", "i2c", "spi"):
    rb.enable_datalogger(DEV[p], enable=True)
rb.load_i2c_response(I2C_ADDR, INJ_I2C)
rb.load_spi_response(INJ_SPI)

# one-time UART auto-baud calibration (150 random bytes), discard the burst
rb.set_emio_mux(uart=True); time.sleep(0.02)
rng = random.Random(0xC0FFEE)
bench.uart.write(bytes(rng.randint(0, 255) for _ in range(150))); time.sleep(0.3)
ring.advance_read(ring.commit_offset())


def session(label, drive):
    ring.snapshot()
    drive()
    time.sleep(0.35)
    try:
        frames = ring.collect_new_frames(min_count=1, timeout_s=1.5, max_frames=300)
    except Exception:
        frames = []
    print("\n=== %s — %d frame(s) decoded ===" % (label, len(frames)))
    for f in frames:
        d = decoders.decode_frame(f.header, f.payload, 100_000_000)
        dom = zs_msg.DOMAIN_NAMES.get(f.header.domain, hex(f.header.domain))
        txt = d.text if d else ("evt=0x%02x payload=%dB" %
                                (f.header.event_id, f.header.payload_len))
        print("  [%-4s id%2d] %s" % (dom, f.header.datalogger_id, txt))
    ring.advance_read(ring.commit_offset())


session("UART  (write 'Hi DEEPSKOPION!')",
        lambda: [rb.set_emio_mux(uart=True), time.sleep(0.01),
                 bench.uart.write(UART_MSG)])
session("I2C   (write_then_read @0x50, injector responds D5 09 C0 DE)",
        lambda: [rb.set_emio_mux(i2c=True), time.sleep(0.01),
                 bench.i2c.write_then_read(I2C_ADDR, bytes([0x00]), 4)])
session("SPI   (xfer 9F AA 0F F0, injector MISO 12 34 56 78)",
        lambda: [rb.set_emio_mux(spi=True), time.sleep(0.01),
                 bench.spi.xfer(SPI_PAT)])
print("\nsession done.")
