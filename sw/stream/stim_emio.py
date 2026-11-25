# rr unknown-llm (AI-generated)
"""DS-P2.253: does arming the stimulus generator take the PS EMIO legs off the bus?

On the Zybo the fabric top (hw/rtl/deepskopion_top.vhd) sets G_STIM_DRIVES_EMIO
(since 8ebcce01, DS-P1.90). With it set, while a protocol's STIM_*_CONTROL
bit 0 is 1 the stimulus generator drives that protocol's EMIO legs in place of
the PS controller (hw/rtl/deepskopion_core.vhd, the stim_*_on muxes), so
PS-driven traffic on that protocol does not reach the bus. Zybo session 3
(2026-11-10) measured it: after a stim i2c arm, 200 i2cget reads failed with
EAGAIN; with STIM_I2C_CONTROL cleared (session 4) all 200 reached the logger.

No regbank field reports the generic. This module infers it from the platform:
every Zynq-7000 image uses deepskopion_top. The inference is named as such in
the text it returns. DE25 and PolarFire tops loop the generator back without
displacing a PS controller, so they report nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

#: The device-tree root compatible list (NUL-separated strings).
DT_COMPATIBLE = Path("/proc/device-tree/compatible")
ZYNQ7000_COMPATIBLE = b"xlnx,zynq-7000"
#: The protocols whose EMIO legs the generator takes (the stim_*_on muxes).
EMIO_LEG_PROTOCOLS = ("uart", "i2c", "spi")


def is_zynq7000(compatible_path: Union[Path, str, None] = None) -> bool:
    """True when the running kernel's DT root is compatible with xlnx,zynq-7000.

    *compatible_path* defaults to DT_COMPATIBLE, read at call time."""
    try:
        compat = Path(compatible_path or DT_COMPATIBLE).read_bytes()
    except OSError:
        return False
    return ZYNQ7000_COMPATIBLE in compat.split(b"\0")


def stim_takes_ps_emio(protocol: str,
                       compatible_path: Union[Path, str, None] = None) -> Optional[str]:
    """Why arming the generator for *protocol* takes the PS EMIO legs, or None.

    None when the protocol has no EMIO leg mux or the platform is not a
    Zynq-7000 image. The text names the inference, the generic and the call
    that hands the legs back."""
    if protocol not in EMIO_LEG_PROTOCOLS or not is_zynq7000(compatible_path):
        return None
    reg = f"STIM_{protocol.upper()}_CONTROL"
    return (f"inferred from the DT compatible {ZYNQ7000_COMPATIBLE.decode()}: the Zynq-7000 "
            f"top sets G_STIM_DRIVES_EMIO (since 8ebcce01), so while {reg} bit 0 is set the "
            f"stimulus generator drives the PS EMIO {protocol} legs and PS-driven {protocol} "
            f"traffic does not reach the bus; release_stimulus_generator() hands them back "
            f"(DS-P2.253)")
