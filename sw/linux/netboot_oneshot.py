#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-P2.87 NET-6: drive the Z7-20 through a one-shot bootsrc=net TFTP boot.

Opens the UART for capture, reboots the board from its console shell,
interrupts u-boot autoboot, runs `setenv bootsrc net; boot` (one-shot: NOT
saveenv — this board has no persistent u-boot env area anyway, so the
SD-first default survives any reset), and logs the whole boot. Exit 0 only
if the TFTP branch ran, both fetches completed, and Linux came back up.

Run it ONLY while holding the board lease (`rr lease acquire zybo-z7-20`).

Usage: sw/linux/netboot_oneshot.py --uart <persistent-device> [uart-log-path]
"""
import argparse
import subprocess
import sys
import time

import serial

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--uart", required=True, help="deployment-owned UART device")
parser.add_argument("log", nargs="?", default="/tmp/netboot_uart.log")
args = parser.parse_args()

UART = args.uart
LOG = args.log

ser = serial.Serial(UART, 115200, timeout=0.2)
log = open(LOG, "wb")
buf = b""


def pump(dur=0.2):
    global buf
    data = ser.read(65536)
    if data:
        log.write(data)
        log.flush()
        buf += data
    time.sleep(dur)


def wait_for(markers, timeout, send=None, send_every=None):
    """Wait until any marker appears in fresh output. Optionally keep sending
    `send` every `send_every` seconds (autoboot interrupt spam)."""
    global buf
    buf = b""
    t0 = time.time()
    last_send = 0.0
    while time.time() - t0 < timeout:
        if send is not None and time.time() - last_send > (send_every or 0.1):
            ser.write(send)
            last_send = time.time()
        pump(0.05)
        for m in markers:
            if m in buf:
                return m
    return None


# 1. Reboot from the UART console shell (ssh `reboot` proved a no-op on this
#    busybox image — measured 2026-08-11: uptime kept counting).
print("== rebooting board via UART console ==")
ser.write(b"\n")
if wait_for([b"# "], timeout=10) is None:
    print("FATAL: no console shell on UART")
    sys.exit(1)
# Plain `reboot` is a silent no-op on this initramfs (init doesn't act on
# it — measured 2026-08-11, uptime kept counting). -f does the syscall
# directly; rootfs is tmpfs so a sync is all the caution there is to take.
ser.write(b"sync; reboot -f\n")

# 2. Catch u-boot: interrupt autoboot by spamming a harmless char.
m = wait_for([b"Hit any key to stop autoboot", b"U-Boot 20"], timeout=90)
if m is None:
    print("FATAL: no u-boot banner after reboot")
    sys.exit(1)
print(f"== u-boot seen ({m!r}); interrupting autoboot ==")
m = wait_for([b"Zynq> "], timeout=15, send=b"x", send_every=0.1)
if m is None:
    print("FATAL: could not reach u-boot prompt")
    sys.exit(1)

# 3. One-shot netboot.
ser.write(b"\x15")  # ctrl-U: clear the spammed chars off the command line
time.sleep(0.3)
ser.write(b"setenv bootsrc net\n")
time.sleep(0.5)
ser.write(b"boot\n")
print("== bootsrc=net; boot issued ==")

# 4. Watch the TFTP branch. boot.scr echoes its intent before fetching.
m = wait_for([b"TFTP dev-loop boot"], timeout=60)
if m is None:
    print("FATAL: boot.scr never took the TFTP branch (bootsrc=net ignored?)")
    sys.exit(1)
print("== TFTP branch taken ==")

# 5. Both fetches + bootm. "Bytes transferred" appears once per tftpboot.
t0 = time.time()
fetches = 0
global_buf = b""
while time.time() - t0 < 180 and fetches < 2:
    got = wait_for([b"Bytes transferred", b"falling back to SD",
                    b"TFTP error", b"link down", b"Retry count exceeded"],
                   timeout=120)
    if got == b"Bytes transferred":
        fetches += 1
        print(f"== fetch {fetches}/2 complete ==")
    elif got is None:
        print("FATAL: timed out waiting for TFTP transfer")
        sys.exit(1)
    else:
        print(f"FATAL: TFTP path failed: {got!r} (autoneg/link issue? see log)")
        sys.exit(2)

m = wait_for([b"booting TFTP image", b"Starting kernel"], timeout=30)
if m is None:
    print("FATAL: fetches done but no TFTP bootm")
    sys.exit(1)
print("== kernel handoff from TFTP image ==")

# 6. Linux up: wait for a login prompt / rc completion.
m = wait_for([b"login:", b"/ #", b"Welcome to", b"deepskopion"], timeout=180)
if m is None:
    print("FATAL: kernel started but no userspace prompt within 180s")
    sys.exit(1)
print(f"== Linux userspace up ({m!r}) ==")
print("NETBOOT OK")
