"""DS-T2.84 — deepskopion_core routes gather words with the shared classifier.

The behaviour (a lifecycle frame bypasses the edge aggregator and lands in DDR
as its own 0x0608 frame) is proven in simulation by
test_datalogger_to_ddr::test_edge_to_ddr on tb_datalogger_to_ddr. That bench
re-instantiates the core's datapath rather than deepskopion_core itself, so it
only covers the core if both pick the aggregator path with the same function,
``zs_msg_pkg.is_edge_batch_word``. The real-core seam bench
(tb_stimulus_core_seam) cannot cover it here: it has no DDR sink, and it does
not elaborate without the CTU CAN FD sources.

This guard holds that link. Before DS-T2.84 the core classified on the domain
nibble alone (``dout_msg_type(11 downto 8) = C_DOMAIN_EDGE``), which batches the
lifecycle frame onto EDGE_EVENTS as an S=1/E=0 "line is low" word.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "hw" / "rtl" / "deepskopion_core.vhd"
TB = ROOT / "hw" / "sim" / "tb_datalogger_to_ddr.vhd"


def _code(path: Path) -> str:
    """VHDL text with comments removed."""
    return "\n".join(line.split("--", 1)[0] for line in path.read_text().splitlines())


def _classifier_rhs(code: str, signal: str) -> str:
    match = re.search(rf"\b{signal}\s*<=\s*([^;]+);", code)
    assert match, f"no concurrent assignment to {signal}"
    return " ".join(match.group(1).split())


def test_core_and_bench_route_with_the_shared_classifier():
    core = _classifier_rhs(_code(CORE), "gather_is_edge_w")
    bench = _classifier_rhs(_code(TB), "gather_is_edge")
    assert core == "is_edge_batch_word(dout_msg_type)", (
        "deepskopion_core does not route gather words with is_edge_batch_word: "
        f"gather_is_edge_w <= {core}; an edge lifecycle frame would be batched "
        "onto EDGE_EVENTS as an S=1 readback")
    assert bench == "is_edge_batch_word(g_msg_type)", (
        f"tb_datalogger_to_ddr no longer mirrors the core classifier: {bench}")
