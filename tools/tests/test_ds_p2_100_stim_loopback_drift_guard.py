"""DS-P2.100 landing-review Finding 1 (fourth-pass revision): drift guard for
the DE25-top stim-gen loopback that no simulation compile set can elaborate.

Why this exists: `deepskopion_de25_top.vhd` instantiates the Agilex 5 HPS, so
it can never be elaborated by nvc/cocotb (the owner's structural guidance is
explicit: do NOT try). `hw/sim/tb_stimulus_core_seam.vhd` (and the older
`tb_stimulus_gen_loop.vhd`) therefore HAND-MIRROR the same loopback block
(stim_*_o -> emio_*_i) that the top wires at
`deepskopion_de25_top.vhd:731-756` so the real `tich.deepskopion_core` can be
compiled standalone. A hand-mirrored copy can silently drift from the
production file it mirrors — in particular, someone could revert the
production top's loopback to the DS-P2.95 tied-off constants
(`emio_uart_tx_i => '1'`, `emio_gpio_t_i => '1'`, ...) and EVERY cocotb test
would stay green, because none of them ever compile
`deepskopion_de25_top.vhd` (reproduced directly during the third landing
review: reverting the production top's tie-offs left
test_stimulus_core_seam_uart / test_stimulus_spi GREEN).

This is a pure, no-simulation, line-anchored/regex structural test: it
extracts the loopback port-map assignments from the PRODUCTION top and from
`tb_stimulus_core_seam.vhd`'s mirrored `u_core` instantiation and asserts
they agree, key for key. It cannot prove `deepskopion_de25_top` elaborates
or behaves correctly (nothing here can, short of real hardware or an HPS
model) — it only proves the TB's mirror has not silently diverged from what
production actually wires, so a revert of the tie-offs is caught even though
the top itself never compiles in sim.

Mutation-proof (performed by hand during this revision, then reverted):
    sed -i "s/emio_uart_tx_i => stim_uart_tx_w/emio_uart_tx_i => '1'/;
            s/emio_gpio_t_i => '0'/emio_gpio_t_i => '1'/"
        hw/rtl/de25/deepskopion_de25_top.vhd
    pytest tools/tests/test_ds_p2_100_stim_loopback_drift_guard.py -> FAILED
    git checkout -- hw/rtl/de25/deepskopion_de25_top.vhd

Run:  pytest tools/tests/test_ds_p2_100_stim_loopback_drift_guard.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOP = ROOT / "hw" / "rtl" / "de25" / "deepskopion_de25_top.vhd"
TB_SEAM = ROOT / "hw" / "sim" / "tb_stimulus_core_seam.vhd"

# The anchors bracket the loopback assignments inside each file's
# `deepskopion_core`/`tich.deepskopion_core` port-map — unique substrings
# present in both files (proven by construction: the TB's header states it
# mirrors this exact span verbatim from the top).
_START_ANCHOR = "emio_uart_tx_i"
_END_ANCHOR = "emio_can_rx_o"

# The port names actually load-bearing for the DS-P2.95 tied-off-constants
# regression: every `emio_*_i` leg the stim-gen loopback drives, plus the
# `emio_*_o`/`emio_gpio_i_o` legs left `open` (an `open` reverting to a
# stray signal name would be just as real a drift as a tied constant).
_TRACKED_KEYS = (
    "emio_uart_tx_i",
    "emio_uart_rx_o",
    "emio_i2c_scl_o_i",
    "emio_i2c_scl_t_i",
    "emio_i2c_sda_o_i",
    "emio_i2c_sda_t_i",
    "emio_i2c_scl_i_o",
    "emio_i2c_sda_i_o",
    "emio_spi_sclk_o_i",
    "emio_spi_sclk_t_i",
    "emio_spi_mosi_o_i",
    "emio_spi_mosi_t_i",
    "emio_spi_miso_o_i",
    "emio_spi_miso_t_i",
    "emio_spi_ss_o_i",
    "emio_spi_ss_t_i",
    "emio_gpio_o_i",
    "emio_gpio_t_i",
    "emio_gpio_i_o",
    "emio_can0_tx_i",
    "emio_can1_tx_i",
    "emio_can_rx_o",
)


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _extract_loopback_assignments(path: Path) -> dict[str, str]:
    text = _strip_comments(path.read_text(encoding="utf-8"))
    start = text.index(_START_ANCHOR)
    end = text.index(_END_ANCHOR, start)
    end = text.index(",", end) + 1  # include the final `emio_can_rx_o => open,`
    block = text[start:end]

    assignments: dict[str, str] = {}
    for m in re.finditer(r"(\w+)\s*=>\s*([^,]+?)\s*,", block):
        key, value = m.group(1), m.group(2).strip()
        if key in _TRACKED_KEYS:
            assignments[key] = value
    return assignments


def test_production_top_loopback_matches_core_seam_tb_mirror():
    top_assignments = _extract_loopback_assignments(TOP)
    tb_assignments = _extract_loopback_assignments(TB_SEAM)

    missing_in_top = set(_TRACKED_KEYS) - set(top_assignments)
    assert not missing_in_top, (
        f"deepskopion_de25_top.vhd's stim-gen loopback is missing expected "
        f"port(s) {sorted(missing_in_top)} — did the loopback block move or "
        f"get restructured? Update _START_ANCHOR/_END_ANCHOR/_TRACKED_KEYS."
    )
    missing_in_tb = set(_TRACKED_KEYS) - set(tb_assignments)
    assert not missing_in_tb, (
        f"tb_stimulus_core_seam.vhd's mirrored u_core port map is missing "
        f"expected port(s) {sorted(missing_in_tb)} — the TB mirror has "
        f"drifted structurally from the production top."
    )

    drifted = {
        key: (top_assignments[key], tb_assignments[key])
        for key in _TRACKED_KEYS
        if top_assignments[key] != tb_assignments[key]
    }
    assert not drifted, (
        "hw/rtl/de25/deepskopion_de25_top.vhd's stim-gen EMIO loopback has "
        "drifted from hw/sim/tb_stimulus_core_seam.vhd's mirrored copy — "
        "the sim-only mirror can no longer stand in for a reverted/edited "
        "production tie-off. Mismatched port(s) (production -> TB): "
        + ", ".join(
            f"{k}: {prod!r} != {tb!r}" for k, (prod, tb) in sorted(drifted.items())
        )
    )
