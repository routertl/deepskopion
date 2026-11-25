#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Stimulus-only driver: enable UART/I2C/SPI dataloggers + drive the EMIO buses in
a loop. The wave_stream daemon does the ring drain; this only fills it."""
import sys, time, random
sys.path.insert(0, "/usr/local/share/deepskopion")
from sw.stream.testbench.emio_inject import SelfTestBench

DEV = {"uart": 12, "i2c": 4, "spi": 8}
bench = SelfTestBench(spi_speed_hz=1_000_000)
rb = bench.regbank

# The UART datalogger must be told the wire format. The EMIO UART0 drives 8-bit
# bytes (8N1), but enable_datalogger's default extra=0 latches explicit *5N1*
# (cfg 0x6001) — the datalogger then samples the "stop bit" at data-bit-5, mid-byte,
# so ~54% of every 8-bit burst framing-errors (proven on silicon: 5N1 → 310/572
# STOP_ERROR; 8N1 → 0/470, bytes recovered 1:1, matching self-test REQ-5).
# extra=6 → cfg 0x600D → explicit 8N1. I2C/SPI carry no UART format, extra=0.
UART_8N1 = 6
rb.enable_datalogger(16, enable=False)                 # quiet JTAG
# DS-T2.31: reset-before-enable so each datalogger's calibration starts from a
# clean state (no stale traces). Dataloggers are armed HERE, before the traffic
# loop below — so they are never 'late to the party' (a datalogger that joins a
# live stream mid-frame calibrates on a partial frame).
for p in DEV:
    rb.reset_and_enable_datalogger(DEV[p], extra=(UART_8N1 if p == "uart" else 0))
rb.load_i2c_response(0x50, [0xD5, 0x09, 0xC0, 0xDE])
rb.load_spi_response([0x12, 0x34, 0x56, 0x78])

# UART auto-baud calibration burst
rb.set_emio_mux(uart=True); time.sleep(0.02)
rng = random.Random(0xC0FFEE)
bench.uart.write(bytes(rng.randint(0, 255) for _ in range(150))); time.sleep(0.3)

print("driving UART/I2C/SPI in a loop (Ctrl-C to stop)...")
while True:
    rb.set_emio_mux(uart=True); time.sleep(0.01)
    bench.uart.write(b"Hi DEEPSKOPION! ")
    rb.set_emio_mux(i2c=True); time.sleep(0.01)
    try:
        bench.i2c.write_then_read(0x50, bytes([0x00]), 4)
    except Exception:
        pass
    rb.set_emio_mux(spi=True); time.sleep(0.01)
    bench.spi.xfer([0x9F, 0xAA, 0x0F, 0xF0])
    time.sleep(0.5)
