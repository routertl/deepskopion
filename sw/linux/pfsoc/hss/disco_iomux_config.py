#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""Point the HSS Discovery-Kit build at the AUTHORITATIVE MSS design XML so the
generated MSS-I/O config drives MMUART_4 -> MSSIO_B2 (the reference console routing).

WHY THIS EXISTS (DS-P2.145 / DS-P3.160 — REFERENCE CONSOLE ROUTING)
-------------------------------------------------------------------
The authoritative Libero MSS design for this kit (``hw/libero/pfsoc/mss/disco_mss.cxz``,
rebased in DS-P1.60 on the reference MSS design) routes the physical console via
``MMUART_4`` on ``MSSIO_B2``:
  * ``IOMUX0_CR.MMUART4_FABRIC = 0x0``  -> MMUART4 goes to the IOMUX/pads
  * ``IOMUX3_CR.PAD15/PAD16    = 0x5``  -> the MMUART4 pads are driven as MMUART

This overlay makes the build consume the authoritative XML: it copies the XML
straight out of the frozen ``disco_mss.cxz`` (byte-identical to the committed
``hw/libero/pfsoc/mss/disco_mss.cxz`` -> ``disco_mss_mss_cfg.xml``) into the
(gitignored) HSS clone, then rewrites ``def_config``'s
``CONFIG_SOC_FPGA_DESIGN_XML`` to point at it. Applied BEFORE the build's
configuration-generator + compile step so the regenerated ``hw_mssio_mux.h``
carries the correct reference pad mux. Idempotent.
"""
from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

# The frozen, authoritative Libero MSS design archive for this kit (committed).
# Its embedded XML is the SAME source that produced WAY_ENABLE=0xB (PF-L0d).
_CXZ_RELPATH = Path("hw") / "libero" / "pfsoc" / "mss" / "disco_mss.cxz"
_CXZ_INNER_XML = "disco_mss_mss_cfg.xml"

# Where the build config selects the design XML, relative to the HSS clone root,
# and the authoritative XML filename we stage there.
_AUTHORITATIVE_XML_RELPATH = (
    "boards/mpfs-disco-kit/soc_fpga_design/xml/disco_mss_mss_cfg.xml"
)


def _repo_root() -> Path:
    """deepskopion worktree root (this file is sw/linux/pfsoc/hss/<name>.py)."""
    return Path(__file__).resolve().parents[4]


def authoritative_xml_bytes(repo_root: Path | None = None) -> bytes:
    """Return the console-correct MSS design XML, read straight from the cxz."""
    root = repo_root or _repo_root()
    cxz = root / _CXZ_RELPATH
    if not cxz.is_file():
        raise SystemExit(f"authoritative MSS design archive missing: {cxz}")
    with zipfile.ZipFile(cxz) as z:
        return z.read(_CXZ_INNER_XML)


def _console_routing(xml_text: str) -> dict[str, str]:
    """Extract the console-path IOMUX fields from an mss_cfg.xml (for verification)."""
    out: dict[str, str] = {}
    m = re.search(r'name="IOMUX0_CR">(.*?)</register>', xml_text, re.S)
    if m:
        for f in re.finditer(r'name="(MMUART[0-4]_FABRIC)"[^>]*>(\w+)', m.group(1)):
            out[f.group(1)] = f.group(2)
    m = re.search(r'name="IOMUX3_CR">(.*?)</register>', xml_text, re.S)
    if m:
        for f in re.finditer(r'name="(PAD1[56])"[^>]*>(\w+)', m.group(1)):
            out[f.group(1)] = f.group(2)
    m = re.search(r'name="IOMUX4_CR">(.*?)</register>', xml_text, re.S)
    if m:
        for f in re.finditer(r'name="(PAD2[89])"[^>]*>(\w+)', m.group(1)):
            out[f.group(1)] = f.group(2)
    return out


def verify_console_routing(xml_text: str) -> None:
    """Fail loud if the XML does NOT route MMUART4 to the physical pads.

    Guards against a drifted/stale archive silently re-introducing the fabric
    routing that caused the silent console.
    """
    r = _console_routing(xml_text)
    problems = []
    if r.get("MMUART4_FABRIC") != "0x0":
        problems.append(f"IOMUX0_CR.MMUART4_FABRIC={r.get('MMUART4_FABRIC')} (need 0x0 = pads, not fabric)")
    if r.get("PAD15") != "0x5" or r.get("PAD16") != "0x5":
        problems.append(
            f"IOMUX3_CR.PAD15/16={r.get('PAD15')}/{r.get('PAD16')} (need 0x5/0x5 = MMUART, not tristate)"
        )
    if problems:
        raise SystemExit(
            "authoritative MSS XML does not route MMUART_4 -> MSSIO_B2: "
            + "; ".join(problems)
        )


def apply(hss_dir: Path, repo_root: Path | None = None) -> bool:
    """Stage the authoritative XML into the clone and repoint def_config at it.

    Returns True if anything changed. Must run BEFORE `rr linux hss` builds the
    clone (the configuration-generator reads CONFIG_SOC_FPGA_DESIGN_XML at build
    time to emit fpga_design_config/io/hw_mssio_mux.h).
    """
    hss_dir = hss_dir.resolve()
    board = hss_dir / "boards" / "mpfs-disco-kit"
    def_config = board / "def_config"
    if not def_config.is_file():
        raise SystemExit(f"HSS disco-kit def_config not found: {def_config} (clone HSS first).")

    xml_bytes = authoritative_xml_bytes(repo_root)
    xml_text = xml_bytes.decode("utf-8")
    verify_console_routing(xml_text)

    changed = False

    # 1. Stage the authoritative XML into the clone (byte-identical to the cxz).
    xml_dst = hss_dir / _AUTHORITATIVE_XML_RELPATH
    xml_dst.parent.mkdir(parents=True, exist_ok=True)
    if not xml_dst.is_file() or xml_dst.read_bytes() != xml_bytes:
        xml_dst.write_bytes(xml_bytes)
        changed = True

    # 2. Repoint CONFIG_SOC_FPGA_DESIGN_XML at the authoritative XML.
    want_line = f'CONFIG_SOC_FPGA_DESIGN_XML="{_AUTHORITATIVE_XML_RELPATH}"'
    for cfg in (def_config, hss_dir / ".config"):
        if not cfg.is_file():
            continue
        text = cfg.read_text(encoding="utf-8")
        if re.search(r"^CONFIG_SOC_FPGA_DESIGN_XML=", text, flags=re.MULTILINE):
            new = re.sub(
                r'^CONFIG_SOC_FPGA_DESIGN_XML=.*$', want_line, text, flags=re.MULTILINE
            )
        else:
            new = text.rstrip("\n") + "\n" + want_line + "\n"
        if new != text:
            cfg.write_text(new, encoding="utf-8")
            changed = True

    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hss-dir", required=True, type=Path)
    args = ap.parse_args(argv)
    changed = apply(args.hss_dir.resolve())
    print(
        "OK: MSS design XML retargeted to disco_mss (MMUART_4 -> MSSIO_B2)"
        + ("" if changed else " (already applied)")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
