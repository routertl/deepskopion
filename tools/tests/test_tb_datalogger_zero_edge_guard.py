"""DS-P3.173: keep the zero-edge DDR bench independent of EDGE0.

The zero-edge fixture deliberately has no ``C_EDGE0_LINE`` declaration.  The
bench must therefore route scalar BFM pins through the generated board-pin
map, and must not elaborate the edge aggregator when the count is zero.  This
fast guard complements the full ``rr sim run`` evidence recorded for the
ticket: it fails on the old hard-wired bench before an expensive simulation is
started.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TB = ROOT / "hw" / "sim" / "tb_datalogger_to_ddr.vhd"
ZERO_EDGE_PKG = (
    ROOT / "tools" / "tests" / "datalogger_mix" / "fixtures" / "zero_edge"
    / "datalogger_config_pkg.vhd"
)


def test_zero_edge_bench_uses_generated_pin_map_and_gates_edge_aggregator() -> None:
    tb = TB.read_text(encoding="utf-8")
    pkg = ZERO_EDGE_PKG.read_text(encoding="utf-8")

    assert "C_EDGE0_LINE => EDGE0" not in tb, (
        "zero_edge: the bench still hard-wires the absent EDGE0 pin"
    )
    assert "constant C_EDGE_DATALOGGERS : integer := 0;" in pkg, (
        "the regression fixture must exercise the zero-edge configuration"
    )
    assert "signal board_pin : std_logic_vector(C_NUM_BOARD_PINS - 1 downto 0)" in tb, (
        "the bench must expose the generated board-pin routing seam"
    )
    assert "C_CAPTURE_BOARD_PIN(i)" in tb, (
        "capture_bus must be assembled from the generated logical-to-board map"
    )
    assert "gen_edge_agg : if C_EDGE_DATALOGGERS > 0 generate" in tb, (
        "the edge aggregator must not elaborate for a zero-edge mix"
    )
