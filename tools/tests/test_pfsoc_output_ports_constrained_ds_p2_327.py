# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.327: every top-level output of a PolarFire target declares its I/O timing intent.

WHY
---
Libero SmartTime reports an output port with neither an output delay nor a false path as UNCONSTRAINED, and the rr
post-implementation timing gate then fails the build with no setup or hold violation. Measured on pfsoc_ka_rea
(impl 8975fa9c2c77): "Max output delay constraint missing on ports: led[0] led[1] led[2] led[3]".

WHAT THIS GUARDS
----------------
For every PolarFire target (products and bench, `extends:` resolved as rr does: dicts deep-merged, lists replaced),
each `out`/`inout` port of its top must be covered by `set_output_delay` or `set_false_path -to [get_ports ...]` in
one of the target's `sources.sdc` files, or be listed in EXEMPT with the reason SmartTime has no output check on it.
"""
from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402

# Ports with no fabric-clocked output path, so SmartTime has no output check to constrain. Measured on the product:
# impl 97ff939d5a6d coverage "Output Setup 0 constrained 0 unconstrained" for every one of these.
_MSS_DEDICATED = (
    "A ACT_N BA BG0 CAS_N CK0 CK0_N CKE0 CS0_N DM ODT0 RAS_N RESET_N WE_N DQ DQS DQS_N "  # MSS DDR4 hard PHY
    "SD_CLK SD_CMD SD_DATA0 SD_DATA1 SD_DATA2 SD_DATA3 SD_VOLT_CMD_DIR SD_VOLT_DIR_0 SD_VOLT_DIR_1_3 "
    "SD_VOLT_EN SD_VOLT_SEL MMUART_4_TXD SPI_1_DO SPI_1_CLK SPI_1_SS0 SGMII_TX0_N SGMII_TX0_P"
).split()
EXEMPT: dict[str, str] = {p: "MSS hard-IP dedicated pin (no fabric timing arc)" for p in _MSS_DEDICATED}
EXEMPT.update({
    "jtag_tdo": "dedicated JTAG pad, Reserved in the pin report (no fabric timing arc)",
    "mdc": "MSS MAC0 MDC routed straight to the pad (no fabric register)",
    "mdio": "MSS MAC0 MDO/MDO_OE routed straight to the pad (deepskopion_pfsoc_fabric.vhd: mdio <= mac0_mdo when ...)",
    "phy_rst_n": "constant '1' (deepskopion_pfsoc_fabric.vhd)",
    "phy_txdis_sresetn": "constant '1' (deepskopion_pfsoc_fabric.vhd)",
})


def _merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base.get(k), v) if k in base else v
        return out
    return over


def _resolved(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "extends" not in raw:
        return raw
    parent = (path.parent / str(raw["extends"])).resolve()
    if parent.suffix != ".yml":
        parent = parent.with_suffix(".yml")
    return _merge(_resolved(parent), {k: v for k, v in raw.items() if k != "extends"})


def _pfsoc_targets() -> list[tuple[str, dict]]:
    out = []
    for p in all_target_files(ROOT):
        c = _resolved(p)
        if str((c.get("hardware") or {}).get("tool", "")).lower() == "libero":
            out.append((p.stem, c))
    return out


def _top_outputs(top: str) -> list[str]:
    for f in list((ROOT / "hw").rglob("*.vhd")) + list((ROOT / "hw").rglob("*.v")):
        text = f.read_text(encoding="utf-8", errors="replace")
        if f.suffix == ".vhd":
            m = re.search(rf"^entity\s+{top}\s+is(.*?)^end\s+(?:entity\s+)?{top}\s*;", text, re.M | re.S | re.I)
            if m:
                body = "\n".join(ln.split("--", 1)[0] for ln in m.group(1).splitlines())
                names = []
                for grp, _ in re.findall(r"^\s*([\w\s,]+?)\s*:\s*(out|inout)\b", body, re.M | re.I):
                    names += [n.strip() for n in grp.split(",") if n.strip()]
                return names
        else:
            m = re.search(rf"^\s*module\s+{top}\s*(?:#\s*\(.*?\))?\s*\((.*?)\);", text, re.M | re.S)
            if m:
                body = re.sub(r"//.*", "", m.group(1))
                return re.findall(r"\b(?:output|inout)\s+(?:wire|reg|logic)?\s*(?:\[[^\]]*\])?\s*(\w+)", body)
    raise AssertionError(f"top {top} not found under hw/")


def _sdc_port_patterns(sdc_files: list[str]) -> list[str]:
    pats = []
    for rel in sdc_files:
        text = "\n".join(ln.split("#", 1)[0] for ln in (ROOT / rel).read_text(encoding="utf-8").splitlines())
        for cmd in re.findall(r"^\s*(set_output_delay\b.*|set_false_path\b.*-to\s+\[get_ports.*)$", text, re.M):
            for grp in re.findall(r"get_ports\s+\{?([^\]\}]*)\}?\]", cmd):
                pats += grp.split()
    return pats


def _covered(port: str, pats: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(port, p) or fnmatch.fnmatchcase(f"{port}[0]", p) for p in pats)


def test_the_scan_sees_the_polarfire_targets():
    names = {n for n, _ in _pfsoc_targets()}
    assert {"pfsoc_datalogger"} <= names, names


def test_every_polarfire_output_declares_its_io_timing_intent():
    missing = {}
    for name, c in _pfsoc_targets():
        pats = _sdc_port_patterns((c.get("sources") or {}).get("sdc") or [])
        bad = [p for p in _top_outputs(c["project"]["top_module"]) if p not in EXEMPT and not _covered(p, pats)]
        if bad:
            missing[name] = bad
    assert missing == {}, (
        f"output ports with no set_output_delay / set_false_path -to [get_ports ...] in the target's sdc: {missing}. "
        "SmartTime reports them unconstrained and the rr timing gate fails (DS-P2.327). Declare the intent, or add "
        "the port to EXEMPT with the measured reason SmartTime has no output check on it.")
