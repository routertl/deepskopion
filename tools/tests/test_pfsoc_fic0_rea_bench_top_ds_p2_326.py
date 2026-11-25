# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.326: the PolarFire FIC0 analyzer stays out of the product and wraps the product fabric unchanged.

deepskopion_pfsoc_top_fic0_rea (hw/rtl/pfsoc/) is a bench top: the product fabric plus the five JTAG TAP ports a
UJTAG analyzer needs. A measured impl with those ports on the product top put jtag_tdo on a user pin, so they must
never reach the product. These guards hold that split:

1. no HDL file but the bench tops instantiates the analyzer units, so the product's dependency walk cannot reach them;
2. the bench top's ports are exactly the product top's ports plus the five TAP ports, so the bench image has the
   product's pinout;
3. the bench top passes every fabric port straight through by name, so the fabric it captures is the product's.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PFSOC = ROOT / "hw" / "rtl" / "pfsoc"
PRODUCT_TOP = PFSOC / "deepskopion_pfsoc_top.vhd"
FABRIC = PFSOC / "deepskopion_pfsoc_fabric.vhd"
BENCH_TOP = PFSOC / "deepskopion_pfsoc_top_fic0_rea.vhd"
#: Tops that are allowed to instantiate the analyzer units (bench images only).
BENCH_TOPS = {BENCH_TOP.name, "pf_known_answer_top_rea.vhd"}
ANALYZER_UNITS = ("fic0_stall_trigger", "fic0_rea_sampler", "rr_rea_ujtag_local", "deepskopion_pfsoc_top_fic0_rea")
TAP_PORTS = {"jtag_tck": "in", "jtag_tdi": "in", "jtag_tms": "in", "jtag_trstb": "in", "jtag_tdo": "out"}


def _ports(path: Path, entity: str) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^entity\s+{entity}\s+is(.*?)^end\s+entity\s+{entity}\s*;", text, re.M | re.S)
    assert m, f"entity {entity} not found in {path.name}"
    body = "\n".join(ln.split("--", 1)[0] for ln in m.group(1).splitlines())
    return {n: d for n, d in re.findall(r"^\s*(\w+)\s*:\s*(inout|in|out)\b", body, re.M)}


def test_no_product_file_instantiates_the_analyzer_units():
    rx = re.compile(r"\b(?:entity\s+\w+\.|component\s+)(" + "|".join(ANALYZER_UNITS) + r")\b")
    hits = []
    for f in sorted((ROOT / "hw" / "rtl").rglob("*.vhd")):
        if f.name in BENCH_TOPS:
            continue
        for m in rx.finditer(f.read_text(encoding="utf-8", errors="replace")):
            hits.append(f"{f.relative_to(ROOT)}: {m.group(1)}")
    assert hits == [], f"analyzer units instantiated outside the bench tops: {hits}"


def test_bench_top_ports_are_the_product_ports_plus_the_tap():
    product = _ports(PRODUCT_TOP, "deepskopion_pfsoc_top")
    bench = _ports(BENCH_TOP, "deepskopion_pfsoc_top_fic0_rea")
    assert len(product) == 60, f"product top has {len(product)} ports (60 at DS-P2.326)"
    assert bench == {**product, **TAP_PORTS}, (
        f"bench top ports differ from product + TAP: extra {sorted(set(bench) - set(product) - set(TAP_PORTS))}, "
        f"missing {sorted(set(product) - set(bench))}")
    assert not set(TAP_PORTS) & set(product), "a TAP port reached the product top"


def test_bench_top_passes_every_fabric_port_through_by_name():
    fabric = _ports(FABRIC, "deepskopion_pfsoc_fabric")
    text = BENCH_TOP.read_text(encoding="utf-8")
    assert len(re.findall(r"\bentity\s+work\.deepskopion_pfsoc_fabric\b", text)) == 1
    own = {"fic0_rd_probe_o", "clk_sys_o", "rst_sys_o"}
    unmapped = sorted(p for p in fabric if p not in own and not re.search(rf"\b{p}\s*=>\s*{p}\b", text))
    assert not unmapped, f"fabric ports not passed straight through in the bench top: {unmapped}"
    for p in own:
        assert re.search(rf"\b{p}\s*=>\s*\w+", text), f"{p} not connected in the bench top"
