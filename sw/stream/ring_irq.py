"""DS-P3.60 — ring-fill watermark interrupt (UIO) helper + validation tool.

The PL raises IRQ_F2P[0] when the DDR ring crosses the 75% HIGH watermark
(deepskopion_top u_ring_watermark, hysteresis 75%↑ / 50%↓). The PS7 maps that
to GIC SPI #29; a `compatible = "generic-uio"` DT node + the
`uio_pdrv_genirq.of_id=generic-uio` bootarg expose it as /dev/uioN.

This module turns that into a blocking Python event so the drain loop can sleep
until the ring needs attention instead of polling RING_COMMIT_PTR in a busy
loop — freeing the CPU for other work (and making "did another task starve the
drain?" answerable; see ring_cpuprofile.py).

UIO interrupt protocol (uio_pdrv_genirq):
  * write(uint32(1))  → unmask/enable the IRQ in the kernel.
  * read(4)           → blocks until the IRQ fires; returns the cumulative
                        interrupt count (uint32). The driver masks the line in
                        its top-half, so userspace owns the ack: handle the
                        event, then write(1) again to re-arm.
  * For a LEVEL irq still asserted on re-arm (ring still > LOW), it re-fires
    immediately — correct: keep draining. Hysteresis guarantees it goes quiet
    once the consumer drains below the 50% LOW watermark.
"""

from __future__ import annotations

import os
import select
import struct
import sys
from pathlib import Path
from typing import Optional

_UIO_SYSFS = Path("/sys/class/uio")


def find_uio_by_name(name: str = "ring_watermark") -> Optional[str]:
    """Return /dev/uioN whose sysfs `name` matches, or None if not present.

    The name comes from the DT node's `interrupt-names` (uio_pdrv_genirq uses
    the node name / interrupt-names as the UIO device name)."""
    if not _UIO_SYSFS.is_dir():
        return None
    for entry in sorted(_UIO_SYSFS.iterdir()):
        try:
            nm = (entry / "name").read_text(errors="replace").strip()
        except OSError:
            continue
        if nm == name:
            return f"/dev/{entry.name}"
    return None


class RingIrq:
    """Blocking wrapper around the ring-watermark UIO interrupt."""

    def __init__(self, dev: Optional[str] = None, name: str = "ring_watermark"):
        self.path = dev or find_uio_by_name(name)
        if self.path is None:
            raise FileNotFoundError(
                f"no UIO device named {name!r} — check the dt_node + "
                "uio_pdrv_genirq.of_id=generic-uio bootarg + that the bitstream "
                "drives IRQ_F2P[0]")
        # Unbuffered binary R/W: write enables the IRQ, read blocks on it.
        self._fd = os.open(self.path, os.O_RDWR)

    def arm(self) -> None:
        """Enable/unmask the interrupt (write uint32 1)."""
        os.write(self._fd, struct.pack("<I", 1))

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        """Block until the IRQ fires (or timeout). Returns the cumulative IRQ
        count, or None on timeout. Caller must arm() first (and re-arm after)."""
        r, _, _ = select.select([self._fd], [], [], timeout)
        if not r:
            return None
        data = os.read(self._fd, 4)
        return struct.unpack("<I", data)[0] if len(data) == 4 else None

    def close(self) -> None:
        if getattr(self, "_fd", None) is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "RingIrq":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _main(argv: list[str]) -> int:
    """Validation tool: block on the ring-watermark IRQ and report each firing
    alongside the RING_ALARM_CNT regbank slot (count + live alarm bit)."""
    import argparse

    ap = argparse.ArgumentParser(description="watch the DS-P3.60 ring-watermark IRQ")
    ap.add_argument("--dev", help="explicit /dev/uioN (else auto-find by name)")
    ap.add_argument("--count", type=int, default=0, help="exit after N firings (0 = forever)")
    ap.add_argument("--timeout", type=float, default=30.0, help="per-wait timeout (s)")
    args = ap.parse_args(argv)

    from .regbank import MmioRegbank, Regs

    try:
        irq = RingIrq(dev=args.dev)
    except FileNotFoundError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2
    print(f"watching {irq.path} (ring-watermark IRQ_F2P[0] = GIC SPI #29)")

    fired = 0
    with MmioRegbank() as rb, irq:
        while True:
            irq.arm()
            n = irq.wait(timeout=args.timeout)
            alarm = rb.read_u32(Regs.RING_ALARM_CNT)
            live = (alarm >> 31) & 1
            excursions = alarm & 0x7FFFFFFF
            if n is None:
                print(f"  [timeout {args.timeout}s] no IRQ; "
                      f"RING_ALARM_CNT excursions={excursions} live={live}")
                continue
            fired += 1
            print(f"  IRQ #{n}: ring crossed 75% — RING_ALARM_CNT "
                  f"excursions={excursions} live_alarm={live}")
            if args.count and fired >= args.count:
                return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
