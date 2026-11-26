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

This file holds the self-test table (docs/getting_started/self_test.md, linked from the README's In-System
Self-Test section) to that test's own
expectation table (read from its source, not copied), and checks the test's
guard that a simulation never opens a host device.
"""
from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]
COVERAGE = ROOT / "hw/sim/cocotb/tests/datalogger/infra/default_mix/test_selftest_coverage.py"
README = ROOT / "README.md"
SELF_TEST = ROOT / "docs" / "getting_started" / "self_test.md"
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


def _readme_rows(doc: Path = SELF_TEST) -> dict[int, str]:
    rows = {}
    for line in doc.read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line)
        if m:
            rows[int(m.group(2))] = m.group(3)
    return rows


def test_the_readme_no_longer_claims_the_board_alone_covers_every_logger():
    text = README.read_text(encoding="utf-8")
    assert "the default targets generate their own bus traffic, so a capture runs with the board alone" not in text
    assert "six of the nine data loggers of the default mix" in text
    # The self-test guide the README links carries the full section; the old claim is banned there too.
    guide = SELF_TEST.read_text(encoding="utf-8")
    assert "the default targets generate their own bus traffic, so a capture runs with the board alone" not in guide
    # ... and in every guide the README's text moved to (T1 rv-e456f214c8).
    for doc in _guides():
        assert "the default targets generate their own bus traffic, so a capture runs with the board alone" not in \
            doc.read_text(encoding="utf-8"), doc
    assert "six of the nine data loggers of the default mix" in guide


def test_the_readme_table_lists_every_logger_of_the_mix():
    assert sorted(_readme_rows()) == sorted(_literal("ARM"))
    # The shortened README publishes no reach rows of its own: the guide's table is the one the oracle judges
    # (T1 rv-70ae358262).
    assert _readme_rows(README) == {}, "README.md publishes self-test reach rows; they belong in the self-test guide"
    assert _rendered_reach_rows(README.read_text(encoding="utf-8")) == []
    # The guide's table is the one a reader sees: every raw row renders as a table row (none hidden in a comment),
    # and the README carries no raw HTML a reach table could hide in (T1 rv-6661ed93e7).
    raw = [f"{m.group(1)} {m.group(2)}" for ln in SELF_TEST.read_text(encoding="utf-8").splitlines()
           if (m := ROW_RE.match(ln))]
    # One row per logger: a duplicate row would let two contradicting claims stand side by side (T1 rv-ad9722849e).
    rendered = _rendered_reach_rows(SELF_TEST.read_text(encoding="utf-8"))
    assert len(raw) == len(set(raw)) and len(rendered) == len(set(rendered)), (raw, rendered)
    assert sorted(rendered) == sorted(raw)
    # No other guide publishes reach rows: the self-test guide's table is the only one (T1 rv-ad9722849e).
    for guide in [p for p in _guides() if p != SELF_TEST]:
        assert _readme_rows(guide) == {} and _rendered_reach_rows(guide.read_text(encoding="utf-8")) == [], guide
    # No guide carries raw HTML a reach table could hide in (T1 rv-e456f214c8).
    for guide in _guides():
        assert _raw_html(guide.read_text(encoding="utf-8")) == [], guide
    # Logger numbers are unique across kinds before rows are keyed by number (T1 rv-e456f214c8).
    numbers = [int(lab.split()[1]) for lab in raw]
    assert len(numbers) == len(set(numbers)), raw
    # ... with both cells: the visible stimulus text of each row is the raw text the oracle judges (T1 rv-f41dcac302).
    assert _rendered_reach_cells(SELF_TEST.read_text(encoding="utf-8")) == {
        n: " ".join(d.split()) for n, d in _readme_rows().items()}
    assert _raw_html(README.read_text(encoding="utf-8")) == [], "README.md carries raw HTML"


def _rendered_reach_rows(markdown: str) -> list[str]:
    """Table rows whose first cell names a logger and its number ('CAN 20'), read from the rendered tables, so the
    row's spacing does not matter (T1 rv-59f510b8fd)."""
    tokens = MarkdownIt("commonmark").enable("table").parse(markdown)
    rows, i = [], 0
    while i < len(tokens):
        if tokens[i].type == "tr_open":
            cell = next(t for t in tokens[i:] if t.type == "inline")
            # The cell as it renders: parsed text (emphasis markers gone, entities decoded: T1 rv-3b05fb442d).
            label = "".join(c.content for c in cell.children or [] if c.type in ("text", "code_inline"))
            if re.fullmatch(r"\s*(Edge|I2C|SPI|UART|CAN)\s*\d+\s*", label):
                rows.append(" ".join(label.split()))
        i += 1
    return rows


def _guides() -> list[Path]:
    return sorted(p for p in (ROOT / "docs" / "getting_started").rglob("*")
                  if p.is_file() and p.suffix.lower() in (".md", ".markdown"))


def _rendered_reach_cells(markdown: str) -> dict[int, str]:
    """{logger number: the second cell's parsed text} for every rendered reach row."""
    tokens = MarkdownIt("commonmark").enable("table").parse(markdown)
    out = {}
    for i, tok in enumerate(tokens):
        if tok.type != "tr_open":
            continue
        cells = []
        for t in tokens[i + 1:]:
            if t.type == "tr_close":
                break
            if t.type == "inline":
                cells.append(" ".join("".join(c.content if c.type in ("text", "code_inline") else " "
                                              for c in t.children or [] if c.type in ("text", "code_inline", "softbreak"))
                                      .split()))
        m = re.fullmatch(r"(Edge|I2C|SPI|UART|CAN) (\d+)", cells[0]) if cells else None
        if m and len(cells) > 1:
            out[int(m.group(2))] = cells[1]
    return out


def _raw_html(markdown: str) -> list[str]:
    """Raw HTML other than comments, block or inline."""
    out = []
    for tok in MarkdownIt("commonmark").enable("table").parse(markdown):
        found = [tok] if tok.type == "html_block" else [c for c in tok.children or [] if c.type == "html_inline"]
        out += [x.content for x in found if not re.fullmatch(r"\s*(?:<!--(?:(?!-->)[\s\S])*-->\s*)+", x.content)]
    return out


def test_a_hidden_guide_table_or_an_html_readme_table_is_refused():
    """Negative controls (T1 rv-6661ed93e7)."""
    guide = SELF_TEST.read_text(encoding="utf-8")
    first = next(ln for ln in guide.splitlines() if ROW_RE.match(ln))
    hidden = guide.replace(first + "\n", f"<!--\n{first}\n-->\n", 1)
    raw = [f"{m.group(1)} {m.group(2)}" for ln in hidden.splitlines() if (m := ROW_RE.match(ln))]
    assert sorted(_rendered_reach_rows(hidden)) != sorted(raw)
    assert _raw_html("<table><tr><td>CAN 20</td><td>On-chip CAN stimulus</td></tr></table>\n")
    # A visible bold row claiming stimulus, with the true row in a comment (T1 rv-f41dcac302).
    row = next(ln for ln in guide.splitlines() if ln.startswith("| I2C 5 |"))
    # The decoy comment sits after the table, so the bold row really renders in place of the true one
    # (T1 rv-14f0886228).
    swapped = guide.replace(row + "\n", "| **I2C 5** | On-chip I2C stimulus |\n", 1) + f"\n<!--\n{row}\n-->\n"
    assert swapped != guide
    assert len(_rendered_reach_cells(swapped)) == len(_rendered_reach_cells(guide)) == 9
    raw_cells = {int(m.group(2)): " ".join(m.group(3).split()) for ln in swapped.splitlines() if (m := ROW_RE.match(ln))}
    assert _rendered_reach_cells(swapped) != raw_cells
    # A duplicate contradicting row above the true one (T1 rv-ad9722849e).
    dup = guide.replace(row + "\n", "| I2C 5 | On-chip I2C stimulus |\n" + row + "\n", 1)
    labels = [f"{m.group(1)} {m.group(2)}" for ln in dup.splitlines() if (m := ROW_RE.match(ln))]
    assert len(labels) != len(set(labels))
    # The same number under another kind (T1 rv-e456f214c8).
    other = guide.replace(row + "\n", "| CAN 5 | On-chip CAN stimulus |\n" + row + "\n", 1)
    numbers = [int(m.group(2)) for ln in other.splitlines() if (m := ROW_RE.match(ln))]
    assert len(numbers) != len(set(numbers))


def test_a_reach_row_in_the_readme_is_found_however_it_is_spaced():
    """Negative control (T1 rv-59f510b8fd): an unspaced row in a README table is a reach row."""
    text = README.read_text(encoding="utf-8")
    bad = text + "\n|Logger|Stimulus|\n|-|-|\n|CAN 20|On-chip CAN stimulus|\n"
    assert _rendered_reach_rows(bad) == ["CAN 20"]
    for cell in ("**CAN 20**", "CAN&nbsp;20"):          # formatted cells (T1 rv-3b05fb442d)
        assert _rendered_reach_rows(text + f"\n|Logger|Stimulus|\n|-|-|\n|{cell}|On-chip CAN stimulus|\n") == ["CAN 20"]


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
