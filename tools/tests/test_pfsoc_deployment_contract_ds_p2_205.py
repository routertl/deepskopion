# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.205: the PolarFire SoC part of the deployment contract.

DS-P1.118 (owner 2026-11-23, PolarFire ships in the public release): the target
and its riscv64 facts moved from the sidecar sw/linux/pfsoc/deployment_contract.yml
into the public declaration sw/linux/deployment_contract.yml. The sidecar
mechanism stays for a board kept out of the public export; the tmp_path tests
below hold it.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]
_ROOT = ROOT
SCRIPT = ROOT / "sw" / "linux" / "deployment_contract.py"
SIDECAR = ROOT / "sw" / "linux" / "pfsoc" / "deployment_contract.yml"


def _contract():
    spec = importlib.util.spec_from_file_location("deployment_contract_p2_205", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def decl():
    return _contract().load_declaration()


def test_pfsoc_datalogger_is_a_declared_linux_target(decl):
    """PF-L0 (DS-P2.125): the third board (PolarFire SoC kit) builds a Linux
    rootfs, so its boot behaviour must be declared here — this is the exact
    violation `check_targets` raised LIVE while targets/pfsoc_datalogger.yml
    shipped a `rootfs:` block before it was listed in boot.targets.

    The second assertion pins the guard to the real precondition: it only
    demands the declaration while the target actually builds a rootfs, so it is
    not a tautology that would still pass if the pfsoc target lost its rootfs.
    """
    assert "pfsoc_datalogger" in decl["boot"]["targets"], (
        "pfsoc_datalogger builds a Linux rootfs but is not a declared Linux "
        "target — the board whose boot behaviour nobody described"
    )
    target = yaml.safe_load((_ROOT / "targets" / "pfsoc_datalogger.yml").read_text(encoding="utf-8"))
    # DS-P1.118 review: the key itself, not the substring "rootfs:", which linux.sdcard.rootfs also matches.
    assert "rootfs" in ((target or {}).get("linux") or {}), (
        "guard precondition gone: pfsoc_datalogger no longer declares a rootfs, "
        "so revisit whether it still belongs in boot.targets"
    )


def test_the_public_declaration_lists_it_and_no_sidecar_does():
    """DS-P1.118: declared once, in the public contract; a leftover PolarFire
    sidecar would declare it a second time."""
    contract = _contract()
    assert contract.load_declaration()["boot"]["targets"].count("pfsoc_datalogger") == 1
    assert not SIDECAR.exists(), f"{SIDECAR.relative_to(ROOT)} still exists"
    # Every sidecar the checker discovers (bench/ and sw/linux/<board>/), not only the old path.
    again = [s.relative_to(ROOT).as_posix() for s in contract._sidecars(ROOT)
             if "pfsoc_datalogger" in ((yaml.safe_load(s.read_text(encoding="utf-8")) or {}).get("targets") or [])]
    assert again == [], f"pfsoc_datalogger is declared a second time in {again}"
    # The generated header a reader sees names it exactly once too.
    doc = (ROOT / "docs" / "DEPLOYMENT_CONTRACT.md").read_text(encoding="utf-8")
    _assert_header_names_the_declared_targets(contract, doc)


_HEADER = re.compile(r"^\*\*Init system: `([^`]+)`\. There is exactly one, on every "
                     r"target that boots Linux \(([^()]*)\)\.\*\*$")


def _assert_header_names_the_declared_targets(contract, doc: str) -> None:
    """DS-P3.314: the header a reader sees inside the generated section, by its
    exact form, and its comma-separated names compared whole, not a substring of
    the first line that mentions it. The document is parsed as CommonMark (the
    parser the export gate uses), so a header inside a comment or a code block is
    not a header, and the markers count only as standalone HTML blocks."""
    assert doc.count(contract.BEGIN_MARK) == 1 and doc.count(contract.END_MARK) == 1
    tokens = MarkdownIt("commonmark").parse(doc)
    blocks = [(i, tok.content.strip()) for i, tok in enumerate(tokens)
              if tok.type == "html_block" and tok.level == 0]
    begins = [i for i, text in blocks if text == contract.BEGIN_MARK]
    ends = [i for i, text in blocks if text == contract.END_MARK]
    assert len(begins) == 1 and len(ends) == 1, "the generated markers are not top-level HTML blocks"
    begin, end = begins[0], ends[0]
    assert begin < end, "the generated section's END marker comes before its BEGIN marker"
    # The generator writes the header as the section's opening paragraph.
    opening = tokens[begin + 1:begin + 3]
    assert [(tok.type, tok.level) for tok in opening] == [("paragraph_open", 0), ("inline", 1)], (
        "the generated section does not open with a paragraph")
    m = _HEADER.match(opening[1].content)
    assert m, f"the section's opening paragraph is not the generated header: {opening[1].content!r}"
    headers = [h.group(2) for i in range(begin + 1, end)
               if tokens[i].type == "inline" and tokens[i - 1].type == "paragraph_open"
               and tokens[i - 1].level == 0 and (h := _HEADER.match(tokens[i].content))]
    assert headers == [m.group(2)], f"expected one generated header, found {headers}"
    decl = contract.load_declaration()
    assert m.group(1) == decl["boot"]["init_system"], m.group(1)
    names = m.group(2).split(", ")
    assert names.count("pfsoc_datalogger") == 1, names
    assert names == decl["boot"]["targets"], f"header names {names} differ from the declared targets"


def _committed_doc() -> str:
    return (ROOT / "docs" / "DEPLOYMENT_CONTRACT.md").read_text(encoding="utf-8")


def _header_line(doc: str) -> str:
    return next(ln for ln in doc.splitlines() if _HEADER.match(ln))


def _refused(bad: str) -> None:
    with pytest.raises(AssertionError):
        _assert_header_names_the_declared_targets(_contract(), bad)


def test_the_header_check_passes_the_committed_document():
    _assert_header_names_the_declared_targets(_contract(), _committed_doc())


def test_the_header_check_refuses_a_misspelled_target():
    bad = _committed_doc().replace("pfsoc_datalogger, ", "pfsoc_datalogger_typo, ", 1)
    assert "pfsoc_datalogger_typo" in bad
    _refused(bad)


def test_the_header_check_reads_only_the_generated_section():
    """The exact header, visible, above the section, and none inside it: a search
    of the whole document would accept this."""
    contract, doc = _contract(), _committed_doc()
    header = _header_line(doc)
    bad = doc.replace(header + "\n", "", 1)
    bad = bad.replace(contract.BEGIN_MARK, header + "\n\n" + contract.BEGIN_MARK, 1)
    assert [ln for ln in bad.splitlines() if _HEADER.match(ln)] == [header]
    _refused(bad)


def test_the_header_check_ignores_a_comment_above_the_section():
    """A multi-line comment above the section holds the exact header, on its own
    line, and the section has none: a whole-document search that does not drop
    comments would accept this."""
    contract, doc = _contract(), _committed_doc()
    header = _header_line(doc)
    bad = doc.replace(header + "\n", "", 1)
    bad = bad.replace(contract.BEGIN_MARK, "<!--\n" + header + "\n-->\n\n" + contract.BEGIN_MARK, 1)
    assert header in bad.splitlines()
    _refused(bad)


def test_the_header_check_ignores_a_header_hidden_in_a_comment():
    """Inside the section, the correct header wrapped in a multi-line comment is
    not a header a reader sees."""
    header = _header_line(_committed_doc())
    bad = _committed_doc().replace(header, "<!--\n" + header + "\n-->", 1)
    assert header in bad.splitlines()
    _refused(bad)


def test_the_header_check_refuses_markers_out_of_order():
    contract, doc = _contract(), _committed_doc()
    bad = doc.replace(contract.END_MARK, "", 1)
    bad = bad.replace(contract.BEGIN_MARK, contract.END_MARK + "\n" + contract.BEGIN_MARK, 1)
    assert bad.count(contract.BEGIN_MARK) == 1 and bad.count(contract.END_MARK) == 1
    _refused(bad)


def test_the_header_check_ignores_a_comment_closed_by_the_end_marker():
    """A comment opened just above the header is closed only by the END marker's
    own delimiter, so the whole header is comment text."""
    header = _header_line(_committed_doc())
    bad = _committed_doc().replace(header, "<!--\n" + header, 1)
    assert header in bad.splitlines()
    _refused(bad)


def test_the_header_check_ignores_a_section_inside_a_code_fence():
    contract, doc = _contract(), _committed_doc()
    bad = doc.replace(contract.BEGIN_MARK, "```\n" + contract.BEGIN_MARK, 1)
    bad = bad.replace(contract.END_MARK, contract.END_MARK + "\n```", 1)
    assert _header_line(doc) in bad.splitlines()
    _refused(bad)


def test_the_header_check_refuses_a_quoted_marker():
    contract = _contract()
    bad = _committed_doc().replace(contract.BEGIN_MARK, "> " + contract.BEGIN_MARK, 1)
    _refused(bad)


def test_the_header_check_reads_the_opening_header_not_a_later_copy():
    """A malformed opening header without the target, and the correct header
    inserted later in the section."""
    contract, doc = _contract(), _committed_doc()
    header = _header_line(doc)
    broken = header.replace("`busybox`", "busybox", 1).replace("pfsoc_datalogger, ", "", 1)
    assert broken != header
    bad = doc.replace(header, broken, 1).replace(contract.END_MARK, header + "\n\n" + contract.END_MARK, 1)
    _refused(bad)


def test_the_header_check_refuses_a_target_name_in_the_init_system():
    bad = _committed_doc().replace("`busybox`", "`busybox pfsoc_datalogger`", 1)
    assert "`busybox pfsoc_datalogger`" in bad
    _refused(bad)


def test_the_header_check_compares_the_whole_target_list():
    """The generated header keeps pfsoc_datalogger but misspells another target,
    and the correct header sits in a comment above it: only the full-list
    comparison can refuse this."""
    contract, doc = _contract(), _committed_doc()
    header = _header_line(doc)
    assert "de25_datalogger, " in header
    bad = doc.replace(header, header.replace("de25_datalogger, ", "de25_datalogger_typo, ", 1), 1)
    bad = bad.replace(contract.BEGIN_MARK, "<!--\n" + header + "\n-->\n\n" + contract.BEGIN_MARK, 1)
    with pytest.raises(AssertionError, match="differ from the declared targets"):
        _assert_header_names_the_declared_targets(contract, bad)


def test_the_check_needs_the_sidecar(tmp_path):
    """Without the sidecar the same target is an undeclared board."""
    contract = _contract()
    targets = tmp_path / "targets"
    targets.mkdir()
    (targets / "pfsoc_datalogger.yml").write_text("linux:\n  rootfs: {}\n", encoding="utf-8")
    decl = {"boot": {"targets": ["pfsoc_datalogger"]}}
    assert contract.check_targets(decl, tmp_path) == []

    decl = {"boot": {"targets": ["other"]}}
    (targets / "other.yml").write_text("linux:\n  rootfs: {}\n", encoding="utf-8")
    fails = contract.check_targets(decl, tmp_path)
    assert [f.what for f in fails] == ["targets/pfsoc_datalogger.yml"]

    side = tmp_path / "sw" / "linux" / "pfsoc"
    side.mkdir(parents=True)
    (side / "deployment_contract.yml").write_text("targets: [pfsoc_datalogger]\n", encoding="utf-8")
    assert contract.check_targets(decl, tmp_path) == []


def test_a_sidecar_target_with_no_rootfs_fails(tmp_path):
    contract = _contract()
    targets = tmp_path / "targets"
    targets.mkdir()
    (targets / "other.yml").write_text("linux:\n  rootfs: {}\n", encoding="utf-8")
    side = tmp_path / "sw" / "linux" / "pfsoc"
    side.mkdir(parents=True)
    (side / "deployment_contract.yml").write_text("targets: [gone]\n", encoding="utf-8")
    fails = contract.check_targets({"boot": {"targets": ["other"]}}, tmp_path)
    assert [f.what for f in fails] == ["boot.targets: gone"]


def test_a_sidecar_with_no_target_list_fails(tmp_path):
    contract = _contract()
    targets = tmp_path / "targets"
    targets.mkdir()
    (targets / "other.yml").write_text("linux:\n  rootfs: {}\n", encoding="utf-8")
    side = tmp_path / "sw" / "linux" / "pfsoc"
    side.mkdir(parents=True)
    (side / "deployment_contract.yml").write_text("autostart: []\n", encoding="utf-8")
    fails = contract.check_targets({"boot": {"targets": ["other"]}}, tmp_path)
    assert [f.what for f in fails] == ["sw/linux/pfsoc/deployment_contract.yml"]
