#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-P1.59 — the SHARED production armer for the DE-C0 live-silicon witness.

Arms DS-P2.100's fabric stimulus generator (``hw/rtl/stimulus/ds_stimulus_gen.vhd``)
for one protocol using regbank **WRITES ONLY**.  It never opens or reads the DDR
ring, so the production ``S99deepskopion-stream`` daemon (``python3 -m sw.stream``,
DS-P2.99/.102) keeps running and streaming the whole time: regbank WRITES do not
contend the daemon's ring READ pointer, whereas a second ring reader would
(Haz.59 — the two-reader race that hangs ``capture_session``).  This is what lets a
witness capture through ``live_capture_client`` against the LIVE daemon instead
of the DS-P2.101 stop-daemon-and-read-the-ring driver.

The per-protocol arm sequences are the DS-P1.58 production register contract
(``de_c0_stimulus_contract``):

  * ``i2c`` arms the on-chip responder BEFORE the master (``I2C_REGISTER_SEQUENCE``)
    so the single transaction is ACK-bound (``start,(0xA0,ACK),(0xAB,ACK),
    (0xCD,ACK),(0x3E,ACK),stop``) — the fix DS-P1.58 landed for the
    DS-P2.101 "unarmed responder NACKs every byte / one transaction per DATA
    write" gap;
  * ``edge`` drives the finite canonical vector (``EDGE_FINITE_CONTROL``) whose
    transitions land exactly on the 20 ns grid at either supported sample clock.

The SAME arming code path is used by the stop-daemon driver
(``de_c0_live_stimulus_driver.py``) and by the daemon itself on a CONTROL
``op="stim"`` (DS-P2.136), so none of them can drift. The sequences live in the
production module ``sw.stream.stimulus_armer`` (the DE25 rootfs strips this
testbench package); this module re-exports them and keeps the console entry
point.

On-board use (as root, WITHOUT stopping the daemon)::

    python3 -m sw.stream.testbench.de_c0_production_armer {uart,i2c,spi,edge}
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from typing import List

from sw.stream.stimulus_armer import (  # noqa: F401  (re-exported)
    ARMERS, PROTOCOLS, Regbank, arm, arm_edge, arm_i2c, arm_spi, arm_uart,
)

# DE25 lwh2f CSR window — the historical default. The PolarFire kit's FIC0 CSR
# is at 0x60000000; both come from the image's own bootargs (ds_mmio_base=).
DE25_MMIO_BASE = 0x20000000


def _read_cmdline() -> str:
    try:
        return Path("/proc/cmdline").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def resolve_mmio_base(cmdline: str | None = None) -> int:
    """CSR base: DS_MMIO_BASE env, else the cmdline's ds_mmio_base=, else DE25.

    Same source of truth as boot arming (arm_datalogger_defaults, fed by the
    init script from the cmdline), so a hand-run armer from a login shell maps
    the window the image was built for (DS-P2.126).
    """
    env = os.environ.get("DS_MMIO_BASE")
    if env:
        return int(env, 0)
    m = re.search(r"(?:^|\s)ds_mmio_base=(\S+)",
                  _read_cmdline() if cmdline is None else cmdline)
    return int(m.group(1), 0) if m else DE25_MMIO_BASE


def main(argv: List[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    protocol = argv[0] if argv else "uart"
    # Import here so the module is importable (for tests) without /dev/mem.
    sys.path.insert(0, "/usr/local/share/deepskopion")
    from sw.stream.regbank import MmioRegbank  # noqa: E402

    rb = MmioRegbank(base_addr=resolve_mmio_base(), size=0x1000)
    try:
        arm(rb, protocol)
    finally:
        rb.close()
    print(f"ARMED {protocol} (daemon left running; capture via live_capture_client)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
