#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-P1.13 demo config #1 traffic driver — SUSTAINED per-protocol EMIO injection.

Board-side stimulus for the daemon-client live-capture demo (captured
UART/I2C/SPI + 4 EDGE channels). Only WRITES the regbank/peripherals — the
running daemon owns the DDR ring read, so this does NOT contend with it (the
standalone ring-reading capture_session.py DOES → Haz.59 hang).

Hard-won choreography (see the deepskopion-board-console skill):
  * UART enable needs extra=6 (8N1); the default (5N1) storms uart_stop_error
    on 8N1 EMIO data (H.94) — a config bug, not a DUT defect.
  * ONE sustained pass per protocol; a mux cycle back to UART drops the
    auto-baud lock. Rotating the mux mid-frame cuts frames → false STOP errors.
  * EDGE via edge_from_spi (bit[3]): the injected SPI signal set feeds the 4
    EDGE dataloggers (#1<-SCLK #2<-MOSI #3<-MISO #4<-CS), DS-P3.71.

Run on the board:  python3 -m sw.stream.testbench.demo_config1_drive [mixed|uart-only]
"""
import random
import sys
import time

sys.path.insert(0, "/usr/local/share/deepskopion")
from sw.stream.testbench.emio_inject import SelfTestBench

b = SelfTestBench(spi_speed_hz=1_000_000)
rb = b.regbank
rb.enable_datalogger(4, enable=True)              # I2C #4
rb.enable_datalogger(12, enable=True, extra=6)    # UART #12 — 8N1 (H.94: enable default is 5N1)
rb.enable_datalogger(8, enable=True)              # SPI #8
for _eid in (1, 2, 3, 6):                      # 4 EDGE dataloggers
    rb.enable_datalogger(_eid, enable=True)
rb.load_i2c_response(0x50, [0xD5, 0x09, 0xC0, 0xDE])
rb.load_spi_response([0x12, 0x34, 0x56, 0x78])

# One-time UART auto-baud calibration (disambiguating bytes, then let it settle).
rb.set_emio_mux(uart=True)
time.sleep(0.05)
_rng = random.Random(0xC0FFEE)
b.uart.write(bytes(_rng.randint(0, 255) for _ in range(150)))
time.sleep(0.6)


def phase_uart(secs, gap=0.35):
    rb.set_emio_mux(uart=True)
    time.sleep(0.05)
    n = 0
    t0 = time.time()
    while time.time() - t0 < secs:
        b.uart.write(b"DS-P1.13 UART frame %02d\n" % (n & 0xFF))
        n += 1
        time.sleep(gap)          # > one frame at the locked baud, so it completes
    return n


def phase_i2c(secs, gap=0.20):
    rb.set_emio_mux(i2c=True)
    time.sleep(0.05)
    n = 0
    t0 = time.time()
    while time.time() - t0 < secs:
        b.i2c.write_then_read(0x50, bytes([n & 0xFF]), 4)
        n += 1
        time.sleep(gap)
    return n


def phase_spi(secs, gap=0.20):
    rb.set_emio_mux(spi=True)
    time.sleep(0.05)
    n = 0
    t0 = time.time()
    while time.time() - t0 < secs:
        b.spi.xfer([0x9F, (n & 0xFF), 0x0F, 0xF0])
        n += 1
        time.sleep(gap)
    return n


def phase_edge(secs, gap=0.20):
    # Route the injected SPI signal set into the 4 EDGE dataloggers (edge_from_spi,
    # bit[3]): EDGE#1<-SCLK #2<-MOSI #3<-MISO #4<-CS — the DS-P3.71 self-test source.
    rb.set_emio_mux(spi=True, edge_from_spi=True)
    time.sleep(0.05)
    n = 0
    t0 = time.time()
    while time.time() - t0 < secs:
        b.spi.xfer([0x9F, (n & 0xFF), 0x0F, 0xF0])
        n += 1
        time.sleep(gap)
    return n


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "mixed"
    if mode == "uart-only":        # confirm UART errors aren't a choreography artifact
        print("UART-ONLY sustained 8s", flush=True)
        print("uart=%d" % phase_uart(8), flush=True)
        return
    # ONE sustained pass per protocol (no return to UART mid-capture).
    u = phase_uart(6)
    i = phase_i2c(6)
    s = phase_spi(6)
    e = phase_edge(6)
    print("uart=%d i2c=%d spi=%d edge=%d" % (u, i, s, e), flush=True)


if __name__ == "__main__":
    main()
