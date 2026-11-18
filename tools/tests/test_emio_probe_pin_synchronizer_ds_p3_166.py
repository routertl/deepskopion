# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P3.166 — Raw ja[*], jb[*] and PS7 EMIO pins reach regs_in_reg status bits with no synchronizer.

Finding F5 (DS-T1.17 Zybo constraint evidence): EMIO_PROBE (slot 11, byte address 44)
sampled raw external PMOD pins (ja[7:4], ja[1:0], jb[0]) and PS7 EMIO peripheral
outputs (UART0_TX, I2C0_*, SPI0_*) directly through emio_probe_word into
telemetry_in, which u_regbank registered into regs_in_reg[11] on clk_fpga_0 without
a synchronizer or timing constraint.

The fix introduces one scalar 2-FF synchronizer per independent probe bit, with
ASYNC_REG="TRUE" in deepskopion_core.vhd before presenting the assembled snapshot
to the AXI-Lite regbank, and declares the asynchronous first stages as false paths
in timing.xdc (and de25_timing.sdc).
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]

# Receipt intended-assertion markers, assembled via concatenation so the contiguous
# substring exists nowhere in this test source file.
_MARKER_SYNC = "EMIO_PROBE status readback " + "is not synchronized with 2-FF async_reg"
_MARKER_XDC = "timing.xdc does not declare " + "EMIO probe async false path"
_MARKER_DE25 = "de25_timing.sdc does not declare " + "EMIO probe async false path"


def _code(path: Path) -> str:
    """Return file text stripped of comments and trailing whitespace."""
    return "\n".join(line.split("#", 1)[0].rstrip() for line in path.read_text(encoding="utf-8").splitlines())


def test_emio_probe_status_readback_is_synchronized() -> None:
    """DS-P3.166 / Finding F5: EMIO_PROBE must be 2-FF synchronized with ASYNC_REG
    before feeding the AXI-Lite register file (telemetry_in). Sampling raw
    emio_probe_word directly propagates metastability from PMOD pins (ja, jb)
    and PS7 EMIO outputs into regs_in_reg[11]."""
    core_path = ROOT / "hw/rtl/deepskopion_core.vhd"
    core = core_path.read_text(encoding="utf-8")

    # 1. Each of the 31 independent inputs must have its own two-flop chain.
    sync_type = re.search(
        r"type\s+emio_probe_sync_t\s+is\s+array\s*\(30\s+downto\s+0\)\s+of\s+std_logic_vector\s*\(1\s+downto\s+0\)",
        core,
    )
    assert sync_type, f"{_MARKER_SYNC}: per-bit 31 x 2-FF synchronizer type missing"
    sync_decl = re.search(r"signal\s+emio_probe_sync_r\s*:\s*emio_probe_sync_t", core)
    assert sync_decl, f"{_MARKER_SYNC}: emio_probe_sync_r per-bit array missing"

    # 2. The complete per-bit array must carry attribute async_reg = "TRUE".
    sync_async = re.search(r"attribute\s+async_reg\s+of\s+emio_probe_sync_r\s*:\s*signal\s+is\s+\"TRUE\"", core, re.I)
    assert sync_async, f"{_MARKER_SYNC}: attribute async_reg of emio_probe_sync_r is not TRUE"

    # 3. The clk_sys process must shift each raw bit through its dedicated chain.
    p_sync = re.search(
        r"emio_probe_sync_r\(i\)\s*<=\s*emio_probe_sync_r\(i\)\(0\)\s*&\s*emio_probe_word\(i\)",
        core,
    )
    assert p_sync, f"{_MARKER_SYNC}: per-bit 2-FF shift assignment missing"

    # 4. telemetry must sample only the assembled synchronized stages.
    tel_sync = re.search(
        r"telemetry_in\s*\(\s*C_INDEX_EMIO_PROBE\s*/\s*4\s*\)\.data\s*<=\s*rea_trig_sticky\s*&\s*emio_probe_sync_w\s*;",
        core,
    )
    assert tel_sync, f"{_MARKER_SYNC}: telemetry does not sample emio_probe_sync_w"

    tel_raw = re.search(
        r"telemetry_in\s*\(\s*C_INDEX_EMIO_PROBE\s*/\s*4\s*\)\.data\s*<=\s*rea_trig_sticky\s*&\s*emio_probe_word",
        core,
    )
    assert not tel_raw, f"{_MARKER_SYNC}: telemetry_in(C_INDEX_EMIO_PROBE / 4) still samples raw emio_probe_word"


def test_timing_xdc_declares_emio_probe_async_false_path() -> None:
    """DS-P3.166 / Finding F5: hw/xdc/timing.xdc must declare the async capture into
    each emio_probe_sync_r scalar first stage as a false path (or datapath-only bound)
    so report_timing_summary -report_unconstrained no longer lists it as an unconstrained
    path to clk_fpga_0."""
    xdc_path = ROOT / "hw/xdc/timing.xdc"
    xdc = _code(xdc_path)

    match = re.search(
        r"set_false_path\s+-to\s+\[get_cells\s+-hier(?:archical)?\s+-filter\s+\{\s*NAME\s*=~\s*\*emio_probe_sync_r_reg\[\*\]\[0\]\*\s*\}\]",
        xdc,
    )
    assert match, f"{_MARKER_XDC}: timing.xdc lacks the per-bit first-stage set_false_path"


def test_de25_timing_sdc_declares_emio_probe_async_false_path() -> None:
    """DS-P3.166: de25_timing.sdc must declare the per-bit EMIO first stages as
    false paths under Quartus get_registers syntax."""
    sdc_path = ROOT / "hw/sdc/de25/de25_timing.sdc"
    sdc = _code(sdc_path)

    reg_query = re.search(
        r"set\s+_ds_emio_probe_meta\s+\[get_registers\s+-nowarn\s+\{\*\|u_core\|emio_probe_sync_r\[\*\]\[0\]\}\]",
        sdc,
    )
    assert reg_query, f"{_MARKER_DE25}: de25_timing.sdc lacks the per-bit first-stage query"

    false_path = re.search(r"set_false_path\s+-to\s+\$_ds_emio_probe_meta", sdc)
    assert false_path, f"{_MARKER_DE25}: de25_timing.sdc lacks set_false_path -to $_ds_emio_probe_meta"
