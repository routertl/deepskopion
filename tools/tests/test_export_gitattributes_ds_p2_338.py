# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.338: the exported .gitattributes names only files the export ships,
and carries no internal ticket ID.

The clean-machine rehearsal of the export found a git-lfs rule for the PolarFire
overlay tarball (a file the export drops) and a comment citing a ticket. The
export now removes a rule that only matches dropped files, and the export gate
refuses a ticket ID in any .gitattributes.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import export_github  # noqa: E402
import public_artifact_hygiene as hygiene  # noqa: E402

LFS_RULE = "sw/linux/pfsoc/python_overlay.tar.gz"


def _rules(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _index_tree(work: Path) -> str:
    """The index as a tree, written from a private copy so index.lock is never taken."""
    index = Path(subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "index"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    ).stdout.strip())
    private = work / "index.copy"
    shutil.copyfile(index, private)
    return subprocess.run(
        ["git", "write-tree"], cwd=ROOT, env={**os.environ, "GIT_INDEX_FILE": str(private)},
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    work = tmp_path_factory.mktemp("ds_p2_338")
    tree = _index_tree(work)
    dest = work / "tree"
    dest.mkdir()
    export_github._export_tree_from_sha(dest, tree, ROOT)
    tracked = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", tree], cwd=ROOT,
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    source_attrs = subprocess.run(
        ["git", "cat-file", "blob", f"{tree}:.gitattributes"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    ).stdout
    files = [p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()]
    return dest, files, tracked, source_attrs


def test_the_source_tree_routes_the_dropped_tarball_through_lfs(exported):
    """Positive control: without it the next test could pass on a source that never had the rule."""
    _, files, tracked, source_attrs = exported
    assert any(line.startswith(LFS_RULE) for line in _rules(source_attrs))
    assert LFS_RULE in tracked
    assert LFS_RULE not in files
    assert hygiene._matches_drop_list(LFS_RULE)


def test_the_exported_gitattributes_has_no_rule_for_the_dropped_tarball(exported):
    dest, _, _, _ = exported
    text = (dest / ".gitattributes").read_text(encoding="utf-8")
    assert LFS_RULE not in text
    assert "filter=lfs" not in text


def test_every_exported_rule_matches_a_shipped_file_or_no_tracked_file(exported):
    dest, files, tracked, _ = exported
    for rule in _rules((dest / ".gitattributes").read_text(encoding="utf-8")):
        rx = export_github._gitattributes_regex(rule.split()[0])
        ships = [p for p in files if rx.match(p)]
        names_tracked = [p for p in tracked if rx.match(p)]
        assert ships or not names_tracked, f"{rule!r} only names dropped files: {names_tracked[:3]}"


def test_the_exported_gitattributes_cites_no_ticket(exported):
    dest, _, _, _ = exported
    data = (dest / ".gitattributes").read_bytes()
    assert [m.group(0) for m in hygiene._TICKET_ID_RE.finditer(data)] == []


@pytest.mark.parametrize("name", [".gitattributes", "hw/quartus/de25/.gitattributes"])
def test_the_export_gate_refuses_a_ticket_id_in_gitattributes(name):
    planted = b"# keeps the overlay out of the history (DS-P1.106)\n*.tar.gz filter=lfs\n"
    findings = hygiene.inspect_bytes(name, planted, export_gate=True)
    assert any("ticket narrative in .gitattributes" in f and "DS-P1.106" in f for f in findings), findings


def test_the_gate_passes_the_same_gitattributes_without_the_id():
    clean = b"# keeps the overlay out of the history\n*.tar.gz filter=lfs\n"
    assert hygiene.inspect_bytes(".gitattributes", clean, export_gate=True) == []


def test_a_ticket_id_outside_the_export_gate_is_not_a_finding():
    planted = b"# DS-P1.106\n*.tar.gz filter=lfs\n"
    assert hygiene.inspect_bytes(".gitattributes", planted, export_gate=False) == []


@pytest.mark.parametrize("pattern, path, hit", [
    ("out/*.job", "out/a.job", True),
    ("out/*.job", "out/x/a.job", False),
    ("out/*.job", "x/out/a.job", False),
    ("*.job", "hw/libero/a.job", True),
    ("docs/evidence/**/*.patch", "docs/evidence/a.patch", True),
    ("docs/evidence/**/*.patch", "docs/evidence/x/y/a.patch", True),
    ("docs/evidence/**/*.patch", "docs/a.patch", False),
    ("runs/hw/**/bit_sha.txt", "runs/hw/r1/bit_sha.txt", True),
    ("/top.txt", "top.txt", True),
    ("/top.txt", "a/top.txt", False),
    ("a?c", "x/abc", True),
    ("libs/**", "libs/a/b.vhd", True),
])
def test_gitattributes_patterns_follow_gitignore_matching(pattern, path, hit):
    assert bool(export_github._gitattributes_regex(pattern).match(path)) is hit


def test_a_removed_rule_takes_its_comment_and_nothing_else(tmp_path):
    (tmp_path / ".gitattributes").write_text(
        "# about the tarball\n"
        "# second line\n"
        "pkg/blob.tar.gz filter=lfs diff=lfs merge=lfs -text\n"
        "\n"
        "# about the vhdl\n"
        "*.vhd text eol=lf\n"
        "\n"
        "# generated, never tracked\n"
        "out/*.job binary\n",
        encoding="utf-8",
    )
    removed = export_github.publish_gitattributes(
        tmp_path, exported=["hw/a.vhd"], dropped=["pkg/blob.tar.gz"],
    )
    assert removed == ["pkg/blob.tar.gz filter=lfs diff=lfs merge=lfs -text"]
    assert (tmp_path / ".gitattributes").read_text(encoding="utf-8") == (
        "# about the vhdl\n"
        "*.vhd text eol=lf\n"
        "\n"
        "# generated, never tracked\n"
        "out/*.job binary\n"
    )


def test_a_rule_matching_a_shipped_file_is_kept_even_if_it_also_matches_a_dropped_one(tmp_path):
    original = "*.vhd text eol=lf\n"
    (tmp_path / ".gitattributes").write_text(original, encoding="utf-8")
    removed = export_github.publish_gitattributes(
        tmp_path, exported=["hw/a.vhd"], dropped=["private/b.vhd"],
    )
    assert removed == []
    assert (tmp_path / ".gitattributes").read_text(encoding="utf-8") == original


def test_the_comment_rewrite_kept_every_source_rule():
    """The source file's comments lost their ticket IDs; its rules must be the ones it had."""
    rules = _rules((ROOT / ".gitattributes").read_text(encoding="utf-8"))
    assert rules == [
        "sw/linux/pfsoc/python_overlay.tar.gz filter=lfs diff=lfs merge=lfs -text",
        "hw/libero/pfsoc/mss/disco_mss.cxz binary",
        "hw/libero/pfsoc/mss/*.job binary",
        "out/*.job binary",
        "docs/evidence/pf-l0/**/*.txt -whitespace",
        "docs/evidence/**/*.patch -whitespace",
        "runs/hw/**/bit_sha.txt -whitespace",
    ]
    assert re.search(r"(?:DS|RTL|LT|PM|TIC|DDR)-[PTCXDE][0-9]", (ROOT / ".gitattributes").read_text()) is None
