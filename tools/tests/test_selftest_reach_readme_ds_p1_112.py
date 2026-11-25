# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.112: the README states the measured board-alone self-test reach.

The README said the default targets generate their own bus traffic so a capture
runs with the board alone. Measured in simulation by
hw/sim/cocotb/tests/datalogger/infra/default_mix/test_selftest_coverage.py
(the real deepskopion_core, every logger of the default mix armed, header pins
idle, one board-alone source per scenario), the sources reach six of the nine
loggers on all three boards; I2C 5, UART 13 and CAN 20 need external traffic.
Edge 3 (MISO under the SPI edge route) is reached since DS-P2.343: arm_spi arms
the on-chip SPI responder and takes the edge route before its first byte
(DS-T2.100: the reach it showed before was the source-switch edge of the route,
never traffic).

This file holds the README's In-System Self-Test table to that test's own
expectation table (read from its source, not copied), and checks the test's
guard that a simulation never opens a host device.
"""
from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COVERAGE = ROOT / "hw/sim/cocotb/tests/datalogger/infra/default_mix/test_selftest_coverage.py"
README = ROOT / "README.md"
ROW_RE = re.compile(r"^\| (Edge|I2C|SPI|UART|CAN) (\d+) \| (.+) \|$")


def _literal(name: str):
    tree = ast.parse(COVERAGE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{COVERAGE.name} has no literal {name}")


def _reach(target: str) -> set[int]:
    expected, targets, not_traffic = _literal("EXPECTED"), _literal("TARGETS"), _literal("NOT_TRAFFIC")
    ids: set[int] = set()
    for scenario in targets[target]:
        ids |= set(expected[scenario]) - set(not_traffic.get(scenario, []))
    return ids


def _readme_rows() -> dict[int, str]:
    rows = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line)
        if m:
            rows[int(m.group(2))] = m.group(3)
    return rows


def test_the_readme_no_longer_claims_the_board_alone_covers_every_logger():
    text = README.read_text(encoding="utf-8")
    assert "the default targets generate their own bus traffic, so a capture runs with the board alone" not in text
    assert "six of the nine data loggers of the default mix" in text


def test_the_readme_table_lists_every_logger_of_the_mix():
    assert sorted(_readme_rows()) == sorted(_literal("ARM"))


@pytest.mark.parametrize("target", ["zybo", "de25", "pfsoc"])
def test_the_readme_reach_is_the_measured_reach_on_each_board(target):
    rows = _readme_rows()
    with_source = {i for i, src in rows.items() if not src.startswith("None")}
    assert with_source == _reach(target), (target, sorted(with_source), sorted(_reach(target)))


def test_the_external_only_loggers_are_named():
    rows = _readme_rows()
    assert {i for i, src in rows.items() if src.startswith("None: external traffic only")} == {5, 13, 20}


def test_the_reach_check_sees_a_changed_expectation(monkeypatch):
    """Mutant: an expectation row that gave I2C 5 a source must change the reach."""
    real = _literal

    def mutant(name):
        value = real(name)
        if name == "EXPECTED":
            value = dict(value, stim_i2c=[4, 5])
        return value

    monkeypatch.setitem(globals(), "_literal", mutant)
    assert 5 in _reach("zybo")


# ── the guard: a simulation never opens a host device ────────────────────────

@pytest.fixture(scope="module")
def coverage_module():
    spec = importlib.util.spec_from_file_location("_ds_selftest_coverage", COVERAGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_guard_refuses_a_dev_path(coverage_module):
    with coverage_module._no_host_devices():
        with pytest.raises(coverage_module.HostDeviceOpened, match="/dev/null"):
            open("/dev/null", "rb")


def test_the_guard_catches_the_real_mmuart_transmitter_before_it_opens(coverage_module):
    from sw.stream import stimulus_contract
    with coverage_module._no_host_devices():
        with pytest.raises(coverage_module.HostDeviceOpened, match="/dev/ttyS1"):
            stimulus_contract.transmit_mmuart1(b"\x5a")


def test_the_guard_lets_an_ordinary_file_through(coverage_module, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("ok", encoding="utf-8")
    with coverage_module._no_host_devices():
        assert open(f, encoding="utf-8").read() == "ok"


def test_the_polarfire_arm_is_recorded_without_a_device(coverage_module):
    coverage_module.MMUART1_SENT.clear()
    writes = coverage_module._armer_writes("arm_uart", mss=True)
    assert (0x24, 0x202) in writes
    assert coverage_module.MMUART1_SENT and coverage_module.MMUART1_SENT[-1]
