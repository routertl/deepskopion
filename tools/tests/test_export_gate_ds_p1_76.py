# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.76: the public export gate is at zero findings and still bites.

The zero is only worth something beside the planted findings: each one proves
the gate reports that kind of finding through the path a real export takes.
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

ROW_RE = re.compile(r"^\| `([^`]+)` \| ([A-Z]+) \| (.+) \|$")
LFS_HEADER = b"version https://git-lfs.github.com/spec/v1"
# In two parts, so this file does not contain the name the planted test looks for.
FAKE_CLIENT = "Zzyzx" + "corp"


def _drop_rows() -> list[str]:
    text = (ROOT / "docs" / "REDISTRIBUTION.md").read_text(encoding="utf-8")
    rows = [ROW_RE.match(line) for line in text.splitlines()]
    return [m.group(1) for m in rows if m and m.group(2) == "DROP"]


def _names_file(base: Path) -> Path:
    path = base / "names.yml"
    path.write_text(f"- name: {FAKE_CLIENT}\n", encoding="utf-8")
    return path


def _index_tree(dest: Path) -> list[str]:
    """Export the index, so a pre-commit run judges what the commit will contain.

    write-tree runs on a private copy of the index (GIT_INDEX_FILE): on the
    real one it may rewrite the cache tree and so takes index.lock, which
    collides across xdist workers and with any other git command on the tree."""
    index = Path(subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "index"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    ).stdout.strip())
    private = dest.parent / "index.copy"
    shutil.copyfile(index, private)
    tree = subprocess.run(
        ["git", "write-tree"], cwd=ROOT, env={**os.environ, "GIT_INDEX_FILE": str(private)},
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    export_github._export_tree_from_sha(dest, tree, ROOT)
    return [p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()]


def test_every_drop_row_is_a_drop_pattern_and_back():
    assert sorted(_drop_rows()) == sorted(hygiene.DROP_PATTERNS)


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    dest = tmp_path_factory.mktemp("export") / "tree"
    dest.mkdir()
    return dest, _index_tree(dest)


@pytest.mark.parametrize("forbidden", [
    r"^context\.json$",
    r"^out/",
    r"^docs/(.*/)?evidence/",
    # DS-P1.81 (owner 2026-11-02): the PolarFire SoC design ships; these parts of
    # it do not.
    r"^hw/libero/pfsoc/mss/disco_mss\.cxz$",
    r"^hw/libero/pfsoc/mss/BRINGUP_HISTORY\.md$",
    r"^sw/linux/pfsoc/python_overlay\.tar\.gz$",
    r"^docs/hardware/pf-l0",
    r"^tools/pfsoc_stall_recover\.py$",
    r"^tools/tests/data/",
])
def test_export_excludes(exported, forbidden):
    _, files = exported
    assert [f for f in files if re.search(forbidden, f)] == []


def test_export_excludes_every_drop_row(exported):
    _, files = exported
    regexes = [hygiene._pattern_regex(p) for p in _drop_rows()]
    assert [f for f in files if any(r.match(f) for r in regexes)] == []


def test_export_carries_no_lfs_pointer(exported):
    dest, files = exported
    pointers = [f for f in files
                if (dest / f).read_bytes()[:len(LFS_HEADER)] == LFS_HEADER]
    assert pointers == []


def test_export_keeps_the_supported_board_documents(exported):
    _, files = exported
    for kept in ("README.md", "docs/REDISTRIBUTION.md", "docs/hardware/fpga_design.md",
                 "targets/zybo_datalogger.yml", "targets/de25_datalogger.yml",
                 # DS-P1.81: the PolarFire SoC Discovery Kit is a supported board.
                 "targets/pfsoc_datalogger.yml", "hw/rtl/pfsoc/deepskopion_pfsoc_top.vhd",
                 "hw/rtl/pfsoc/deepskopion_pfsoc_fabric.vhd",
                 "hw/libero/pfsoc/pfsoc_mss_subsys.tcl", "hw/libero/pfsoc/mss/disco_mss.cfg",
                 "hw/libero/pfsoc/mss/reference/LICENSE.md", "sw/linux/pfsoc/buildroot_pfsoc_defconfig"):
        assert kept in files


def test_gate_is_at_zero_findings_on_the_index(exported, tmp_path):
    dest, _ = exported
    try:
        hygiene.load_restricted_names(None)
        names = None
    except hygiene.InspectionError:
        names = _names_file(tmp_path)
    findings, errors = hygiene.scan(
        [dest], include_index=False, export_gate=True,
        root=ROOT, restricted_names_file=names,
    )
    assert errors == []
    assert findings == []


# DS-P2.360: an RFC 1918 address that is no bench address, built at run time so this file carries no address
# literal (the fixture used to hold a bench board address as two adjacent bytes literals).
_SYNTH_PRIVATE = ".".join(str(n) for n in (10, 255, 254, 253)).encode()


@pytest.mark.parametrize("rel,body,kind", [
    ("sw/a.py", b"model = 'cl" b"aude'\n", "agent identity reference"),
    ("sw/b.py", b"see ti" b"ch-super\n", "internal " "ti" "ch reference"),
    ("sw/c.py", b"the uart sn" b"iffer\n", "retired " "sn" "iffer reference"),
    ("sw/d.py", b"see docs/in" b"ternal/x.md\n", "internal docs reference"),
    ("sw/e.py", b"git_" b"repo/project\n", "internal bench workspace path"),
    ("docs/f.md", b"Fixed in DS-" b"P2.9.\n", "ticket narrative in docs"),
    ("sw/g.py", b"/ho" b"me/someone/x\n", "developer home"),
    ("sw/h.py", _SYNTH_PRIVATE + b"\n", "private-subnet assumption"),
    ("sw/i.py", "".join(("21", "ABC", "DEFGHIJ")).encode() + b"\n", "bench cable serial"),
    ("sw/j.py", FAKE_CLIENT.encode() + b"\n", "restricted client name"),
    ("sw/k.py", b"-----BEGIN " b"PRIVATE KEY-----\n", "private key"),
])
def test_gate_reports_a_planted_finding(tmp_path, rel, body, kind):
    tree = tmp_path / "tree"
    target = tree / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(body)
    findings, errors = hygiene.scan(
        [tree], include_index=False, export_gate=True, root=ROOT,
        restricted_names_file=_names_file(tmp_path),
    )
    assert errors == []
    assert any(kind in f for f in findings), findings


@pytest.mark.parametrize("rel", [
    "out/pfsoc_mss.job",
    "context.json",
    "docs/hardware/pf-l0e/x.md",
    "docs/some/new/evidence/capture.bin",
    "tools/tests/data/witness/session.json",
])
def test_gate_reports_a_dropped_file_that_reaches_an_export(tmp_path, rel):
    tree = tmp_path / "tree"
    target = tree / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(b"clean\n")
    findings, _ = hygiene.scan(
        [tree], include_index=False, export_gate=True, root=ROOT,
        restricted_names_file=_names_file(tmp_path),
    )
    assert any("dropped artifact in export" in f for f in findings), findings


def test_an_export_under_a_directory_named_out_is_not_condemned(tmp_path):
    tree = tmp_path / "out" / "tree"
    target = tree / "sw" / "a.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"clean\n")
    findings, errors = hygiene.scan(
        [tree], include_index=False, export_gate=True, root=ROOT,
        restricted_names_file=_names_file(tmp_path),
    )
    assert (findings, errors) == ([], [])


def _init_export_fixture_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)


def _commit_export_fixture_repo(repo: Path, msg: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", msg], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_export_refuses_when_exported_md_names_dropped_path(tmp_path):
    """DS-P3.190: export_github.py refuses when an exported .md names a repo
    path matching DROP_LIST or DROP_PATTERNS, naming the doc line and the rule."""
    src_repo = tmp_path / "src_repo"
    src_repo.mkdir()
    _init_export_fixture_repo(src_repo)
    names = _names_file(tmp_path)

    # Seed dropped files in the source repo so _export_tree_from_sha drops them
    for dropped_rel in (
        "tools/pfsoc_stall_recover.py",
        "debug/pfsoc_warm_reset.yml",
        "docs/GOLDEN_PATH.md",
        "demo/config1/screencast.md",
    ):
        p = src_repo / dropped_rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# dropped\n", encoding="utf-8")

    readme_lines = [f"Line {i}\n" for i in range(1, 17)]
    readme_lines.append(
        "Cold boot recovery uses `tools/pfsoc_stall_recover.py` on the kit.\n"
    )
    readme_lines.append(
        "Probe configuration lives in `debug/pfsoc_warm_reset.yml`.\n"
    )
    readme_lines.append(
        "Validation path is documented in `docs/GOLDEN_PATH.md`.\n"
    )
    (src_repo / "README.md").write_text("".join(readme_lines), encoding="utf-8")
    (src_repo / "demo" / "config1" / "README.md").write_text(
        "# Demo\n\nSee [`screencast.md`](screencast.md) for the walkthrough.\n",
        encoding="utf-8",
    )
    dirty_sha = _commit_export_fixture_repo(src_repo, "docs: seed dropped path refs")

    dest_dir = tmp_path / "export_dest"
    rc, output = export_github.export_github(
        dest_dir,
        source_sha=dirty_sha,
        root=src_repo,
        restricted_names_file=names,
    )
    assert rc != 0, f"Expected export refusal for dropped path in .md, got rc=0:\n{output}"
    assert "PUBLIC EXPORT GATE FAIL" in output
    assert (
        "README.md:17: dropped path in docs (tools/pfsoc_stall_recover.py): "
        "tools/pfsoc_stall_recover.py"
    ) in output
    assert (
        "README.md:18: dropped path in docs (debug/pfsoc_*.yml): "
        "debug/pfsoc_warm_reset.yml"
    ) in output
    assert (
        "README.md:19: dropped path in docs (docs/GOLDEN_PATH.md): "
        "docs/GOLDEN_PATH.md"
    ) in output
    assert (
        "demo/config1/README.md:3: dropped path in docs (demo/config1/screencast.md): "
        "screencast.md"
    ) in output
    assert not (dest_dir / ".git").exists()

    # Once the dropped references are resolved, the export passes cleanly.
    clean_lines = [f"Line {i}\n" for i in range(1, 17)]
    clean_lines.append("Cold boot recovery uses a warm reset of the MSS.\n")
    (src_repo / "README.md").write_text("".join(clean_lines), encoding="utf-8")
    (src_repo / "demo" / "config1" / "README.md").write_text(
        "# Demo\n\nWalkthrough package.\n",
        encoding="utf-8",
    )
    clean_sha = _commit_export_fixture_repo(src_repo, "docs: resolve dropped path refs")
    rc_clean, output_clean = export_github.export_github(
        dest_dir,
        source_sha=clean_sha,
        root=src_repo,
        restricted_names_file=names,
    )
    assert rc_clean == 0, output_clean
    assert "PUBLIC EXPORT GATE PASS" in output_clean


def test_export_refuses_when_exported_md_names_missing_tree_path(tmp_path):
    """DS-P3.190: export_github.py refuses when an exported .md names a repo
    path or link target that does not exist in the exported tree."""
    src_repo = tmp_path / "src_repo"
    src_repo.mkdir()
    _init_export_fixture_repo(src_repo)
    names = _names_file(tmp_path)

    (src_repo / "README.md").write_text(
        "# Project\n"
        "Intro line.\n"
        "Another line.\n"
        "Run `tools/pfsoc_warm_reset.py` to reset.\n"
        "See [missing guide](docs/nonexistent.md) for details.\n",
        encoding="utf-8",
    )
    dirty_sha = _commit_export_fixture_repo(src_repo, "docs: seed missing tree paths")

    dest_dir = tmp_path / "export_dest"
    rc, output = export_github.export_github(
        dest_dir,
        source_sha=dirty_sha,
        root=src_repo,
        restricted_names_file=names,
    )
    assert rc != 0, f"Expected export refusal for missing tree path in .md, got rc=0:\n{output}"
    assert "PUBLIC EXPORT GATE FAIL" in output
    assert (
        "README.md:4: missing path in docs (not in exported tree): "
        "tools/pfsoc_warm_reset.py"
    ) in output
    assert (
        "README.md:5: missing path in docs (not in exported tree): "
        "docs/nonexistent.md"
    ) in output
    assert not (dest_dir / ".git").exists()

    # Once the referenced paths exist in the exported tree, the export passes.
    (src_repo / "tools").mkdir(parents=True, exist_ok=True)
    (src_repo / "tools" / "pfsoc_warm_reset.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (src_repo / "docs").mkdir(parents=True, exist_ok=True)
    (src_repo / "docs" / "nonexistent.md").write_text("# Guide\n", encoding="utf-8")
    clean_sha = _commit_export_fixture_repo(src_repo, "feat: add referenced files")
    rc_clean, output_clean = export_github.export_github(
        dest_dir,
        source_sha=clean_sha,
        root=src_repo,
        restricted_names_file=names,
    )
    assert rc_clean == 0, output_clean
    assert "PUBLIC EXPORT GATE PASS" in output_clean


# A gitignored build output: a build writes it, so no export carries it.
_GEN_OUT = "hw/rtl/pkg/gen_info_pkg.vhd"
_RUNTIME_DECL = (
    "schema_version: 1\n"
    "runtime_families:\n"
    "  gen-info:\n"
    "    hook: regen\n"
    "    outputs:\n"
    f"      - {_GEN_OUT}\n"
)


def _export_with_declaration(tmp_path: Path, readme: str, decl: str | None) -> tuple[int, str]:
    """Export a fixture repo whose README is ``readme`` and whose
    tools/generated_sources.yml is ``decl`` (absent when None)."""
    src_repo = tmp_path / "src_repo"
    src_repo.mkdir()
    _init_export_fixture_repo(src_repo)
    (src_repo / "hw" / "rtl" / "pkg").mkdir(parents=True)
    (src_repo / "hw" / "rtl" / "pkg" / "kept_pkg.vhd").write_text("-- kept\n", encoding="utf-8")
    if decl is not None:
        (src_repo / "tools").mkdir()
        (src_repo / "tools" / "generated_sources.yml").write_text(decl, encoding="utf-8")
    (src_repo / "README.md").write_text(readme, encoding="utf-8")
    sha = _commit_export_fixture_repo(src_repo, "docs: name a generated output")
    return export_github.export_github(
        tmp_path / "export_dest", source_sha=sha, root=src_repo,
        restricted_names_file=_names_file(tmp_path),
    )


_NAMES_GEN_OUT = (
    "# Project\n"
    f"Generated to: `{_GEN_OUT}` at build time.\n"
    f"The pre_build hook writes {_GEN_OUT} before lint.\n"
)


def test_export_refuses_an_undeclared_generated_output_named_in_docs(tmp_path):
    """DS-P3.190: without a runtime_families declaration a doc naming a path
    the export lacks is refused, in backticks and in bare text."""
    rc, output = _export_with_declaration(tmp_path, _NAMES_GEN_OUT, None)
    assert rc != 0, f"undeclared generated output not refused:\n{output}"
    for line in (2, 3):
        assert (
            f"README.md:{line}: missing path in docs (not in exported tree): {_GEN_OUT}"
        ) in output, output


def test_export_refuses_a_generated_output_declared_only_as_tracked(tmp_path):
    """DS-P3.190: only runtime_families (gitignored outputs) are declared
    absent from the tree; a tracked_families output must be in the export."""
    decl = (
        "schema_version: 1\n"
        "tracked_families:\n"
        "  gen-info:\n"
        "    producer: tools/regen.sh\n"
        "    outputs:\n"
        f"      - path: {_GEN_OUT}\n"
        "        candidate: pkg/gen_info_pkg.vhd\n"
        "runtime_families:\n"
        "  other:\n"
        "    hook: regen\n"
        "    outputs:\n"
        "      - hw/rtl/pkg/other_pkg.vhd\n"
    )
    rc, output = _export_with_declaration(tmp_path, _NAMES_GEN_OUT, decl)
    assert rc != 0, f"tracked-only generated output excused:\n{output}"
    assert (
        f"README.md:2: missing path in docs (not in exported tree): {_GEN_OUT}"
    ) in output, output


def test_export_accepts_a_declared_generated_output_named_in_docs(tmp_path):
    """DS-P3.190: a doc may say where the build writes a runtime output that
    the exported tools/generated_sources.yml declares (hw/rtl/pkg/rom_info_pkg.vhd
    in this repository)."""
    rc, output = _export_with_declaration(tmp_path, _NAMES_GEN_OUT, _RUNTIME_DECL)
    assert "path in docs" not in output, f"declared generated output refused:\n{output}"
    assert rc == 0, output
    assert "PUBLIC EXPORT GATE PASS" in output


def test_export_refuses_a_link_to_a_declared_generated_output(tmp_path):
    """DS-P3.190: a declared runtime output is not in the public tree, so a
    link to it is broken on GitHub and still refused."""
    readme = f"# Project\n\nSee [the stamp]({_GEN_OUT}).\n"
    rc, output = _export_with_declaration(tmp_path, readme, _RUNTIME_DECL)
    assert rc != 0, f"link to a declared generated output not refused:\n{output}"
    assert (
        f"README.md:3: missing path in docs (not in exported tree): {_GEN_OUT}"
    ) in output, output


def test_export_refuses_a_declared_generated_output_that_a_drop_rule_removes(tmp_path):
    """DS-P3.190: the declaration excuses absence only, never a DROP rule."""
    dropped = "tools/pfsoc_stall_recover.py"
    decl = _RUNTIME_DECL + f"      - {dropped}\n"
    readme = f"# Project\nRun `{dropped}` after a hang.\n"
    rc, output = _export_with_declaration(tmp_path, readme, decl)
    assert rc != 0, f"dropped declared generated output not refused:\n{output}"
    rule = hygiene._matches_drop_list(dropped)
    assert rule, dropped
    assert f"README.md:2: dropped path in docs ({rule}): {dropped}" in output, output


@pytest.mark.parametrize("decl,message", [
    ("runtime_families: [a]\n", "runtime_families is not a mapping"),
    ("runtime_families:\n  x:\n    hook: h\n", "runtime_families.x.outputs is not a non-empty list"),
    ("runtime_families:\n  x:\n    outputs: [/abs/out.vhd]\n", "is not a relative repository path"),
    ("runtime_families: {x: [\n", "unreadable generated-output declaration"),
])
def test_export_refuses_a_malformed_generated_output_declaration(tmp_path, decl, message):
    """DS-P3.190: a declaration the gate cannot read is an error, never a
    silent exemption or a silent loss of one."""
    rc, output = _export_with_declaration(tmp_path, _NAMES_GEN_OUT, decl)
    assert rc != 0, f"malformed declaration not reported:\n{output}"
    assert message in output, f"malformed declaration not reported:\n{output}"


@pytest.mark.parametrize("link", [
    "[Existing guide](docs/guide_(v1).md)",
    "[Existing guide](<docs/guide_(v1).md>)",
    '[Existing guide](docs/guide_(v1).md "The v1 guide")',
    "[Existing guide](docs/guide_(v1).md#top)",
])
def test_export_accepts_link_to_tracked_file_with_balanced_parens(tmp_path, link):
    """DS-P3.190: a Markdown link destination may hold balanced parentheses
    (CommonMark), be <...>-wrapped, and carry a title; none of these may be
    truncated into a false missing path."""
    src_repo = tmp_path / "src_repo"
    src_repo.mkdir()
    _init_export_fixture_repo(src_repo)
    names = _names_file(tmp_path)
    (src_repo / "docs").mkdir()
    (src_repo / "docs" / "guide_(v1).md").write_text("# Guide\n", encoding="utf-8")
    (src_repo / "README.md").write_text(f"# Project\n\n{link}\n", encoding="utf-8")
    sha = _commit_export_fixture_repo(src_repo, "docs: paren link")
    rc, output = export_github.export_github(
        tmp_path / "export_dest", source_sha=sha, root=src_repo,
        restricted_names_file=names,
    )
    assert rc == 0, output
    assert "missing path" not in output


def test_export_refuses_missing_target_with_balanced_parens(tmp_path):
    """The paren-aware parser still reports a missing file, naming the whole
    destination and not a truncated one."""
    src_repo = tmp_path / "src_repo"
    src_repo.mkdir()
    _init_export_fixture_repo(src_repo)
    names = _names_file(tmp_path)
    (src_repo / "README.md").write_text(
        "# Project\n\n[Gone](docs/gone_(v2).md)\n", encoding="utf-8"
    )
    sha = _commit_export_fixture_repo(src_repo, "docs: missing paren link")
    rc, output = export_github.export_github(
        tmp_path / "export_dest", source_sha=sha, root=src_repo,
        restricted_names_file=names,
    )
    assert rc != 0, output
    assert (
        "README.md:3: missing path in docs (not in exported tree): "
        "docs/gone_(v2).md"
    ) in output


def test_export_accepts_a_bare_path_that_ends_a_sentence(tmp_path):
    """DS-P3.190: the period that ends a sentence is not part of a bare path
    in prose; "Use tools/export_github.py." names a file the export carries."""
    src_repo = tmp_path / "src_repo"
    src_repo.mkdir()
    _init_export_fixture_repo(src_repo)
    (src_repo / "tools").mkdir()
    (src_repo / "tools" / "export_github.py").write_text("x\n", encoding="utf-8")
    (src_repo / "README.md").write_text(
        "# Project\n\nUse tools/export_github.py.\n", encoding="utf-8"
    )
    sha = _commit_export_fixture_repo(src_repo, "docs: sentence-ending path")
    rc, output = export_github.export_github(
        tmp_path / "export_dest", source_sha=sha, root=src_repo,
        restricted_names_file=_names_file(tmp_path),
    )
    assert "path in docs" not in output and rc == 0, (
        f"sentence-ending period read as part of a bare path:\n{output}"
    )


def test_export_refuses_a_dropped_file_named_parent_relative_in_prose(tmp_path):
    """DS-P3.190: a bare ../ path in prose is resolved against the document's
    directory; docs/recovery.md naming ../tools/pfsoc_stall_recover.py names
    tools/pfsoc_stall_recover.py, a file the exporter drops."""
    src_repo = tmp_path / "src_repo"
    src_repo.mkdir()
    _init_export_fixture_repo(src_repo)
    (src_repo / "tools").mkdir()
    (src_repo / "tools" / "pfsoc_stall_recover.py").write_text("x\n", encoding="utf-8")
    (src_repo / "docs").mkdir()
    (src_repo / "docs" / "recovery.md").write_text(
        "# Recovery\n\nRun ../tools/pfsoc_stall_recover.py after a stall.\n",
        encoding="utf-8",
    )
    sha = _commit_export_fixture_repo(src_repo, "docs: parent-relative dropped path")
    rc, output = export_github.export_github(
        tmp_path / "export_dest", source_sha=sha, root=src_repo,
        restricted_names_file=_names_file(tmp_path),
    )
    assert rc != 0 and (
        "docs/recovery.md:3: dropped path in docs (tools/pfsoc_stall_recover.py): "
        "../tools/pfsoc_stall_recover.py"
    ) in output, f"parent-relative dropped path in prose not refused:\n{output}"


def _export_parent_relative_doc(tmp_path: Path, doc: str) -> tuple[int, str]:
    """Export a fixture repo carrying tools/present.py and docs/recovery.md
    with body ``doc``."""
    src_repo = tmp_path / "src_repo"
    src_repo.mkdir()
    _init_export_fixture_repo(src_repo)
    (src_repo / "tools").mkdir()
    (src_repo / "tools" / "present.py").write_text("x\n", encoding="utf-8")
    (src_repo / "docs").mkdir()
    (src_repo / "docs" / "recovery.md").write_text(doc, encoding="utf-8")
    sha = _commit_export_fixture_repo(src_repo, "docs: parent-relative path")
    return export_github.export_github(
        tmp_path / "export_dest", source_sha=sha, root=src_repo,
        restricted_names_file=_names_file(tmp_path),
    )


def test_export_refuses_a_missing_file_named_parent_relative_in_prose(tmp_path):
    """DS-P3.190: docs/recovery.md naming ../tools/nonexistent.py names
    tools/nonexistent.py, which the exported tree does not carry."""
    rc, output = _export_parent_relative_doc(
        tmp_path, "# Recovery\n\nRun ../tools/nonexistent.py after a stall.\n",
    )
    assert rc != 0 and (
        "docs/recovery.md:3: missing path in docs (not in exported tree): "
        "../tools/nonexistent.py"
    ) in output, f"parent-relative missing path in prose not refused:\n{output}"


def test_export_accepts_a_present_file_named_parent_relative_in_prose(tmp_path):
    """DS-P3.190: docs/recovery.md naming ../tools/present.py names
    tools/present.py, which the exported tree carries."""
    rc, output = _export_parent_relative_doc(
        tmp_path, "# Recovery\n\nRun ../tools/present.py after a stall.\n",
    )
    assert "path in docs" not in output and rc == 0, (
        f"parent-relative present path in prose refused:\n{output}"
    )



# --- DS-P3.190: one table of Markdown reference forms ---------------------
#
# Every row is a README body exported from a fixture repo, with the verdict the
# gate must reach.  "present": the target ships, the export passes.  "missing":
# the export refuses, naming the README line and the target as written.
# "dropped": the export refuses, naming the line and the DROP rule.
# "ignored": not a repository path (external link, same-page anchor, a token
# that is not a filename, or link syntax inside a fenced code block); the
# export passes.  The README starts "# Project\n\n", so a one-line body is
# line 3.

_REF_FIXTURE_FILES = (
    "docs/guide.md",
    "docs/guide_(v1).md",
    "docs/my guide.md",
    "docs/img/logo.png",
    "docs/sub/notes.md",
    "tools/present.py",
    "project.yml",
    # Dropped by the exporter (DROP rule debug/pfsoc_*.yml).
    "debug/pfsoc_warm_reset.yml",
)
_DROP_RULE = "debug/pfsoc_*.yml"

_REFERENCE_FORMS = [
    # inline links [t](p)
    ("inline-present", "[G](docs/guide.md)", "present", None, 3),
    ("inline-dot-slash-present", "[G](./docs/guide.md)", "present", None, 3),
    ("inline-missing", "[G](docs/missing.md)", "missing", "docs/missing.md", 3),
    ("inline-dir-present", "[Docs](docs/sub/)", "present", None, 3),
    ("inline-nested-text-missing", "[see [the] guide](docs/gone.md)", "missing", "docs/gone.md", 3),
    ("inline-escapes-tree-missing", "[Up](../outside.md)", "missing", "../outside.md", 3),
    ("inline-dropped", "[Probe](debug/pfsoc_warm_reset.yml)", "dropped", "debug/pfsoc_warm_reset.yml", 3),
    # balanced parentheses
    ("parens-present", "[G](docs/guide_(v1).md)", "present", None, 3),
    ("parens-missing", "[G](docs/gone_(v2).md)", "missing", "docs/gone_(v2).md", 3),
    # <p>-wrapped
    ("angle-present", "[G](<docs/guide_(v1).md>)", "present", None, 3),
    ("angle-space-present", "[G](<docs/my guide.md>)", "present", None, 3),
    ("angle-missing", "[G](<docs/gone.md>)", "missing", "docs/gone.md", 3),
    ("percent-encoded-present", "[G](docs/my%20guide.md)", "present", None, 3),
    # optional title
    ("title-double-present", '[G](docs/guide.md "The guide")', "present", None, 3),
    ("title-single-missing", "[G](docs/gone.md 'Gone')", "missing", "docs/gone.md", 3),
    ("title-paren-present", "[G](docs/guide.md (Guide))", "present", None, 3),
    # #fragments and ?queries
    ("fragment-present", "[G](docs/guide.md#install)", "present", None, 3),
    ("fragment-missing", "[G](docs/gone.md#install)", "missing", "docs/gone.md", 3),
    ("query-present", "[G](docs/guide.md?plain=1)", "present", None, 3),
    ("query-missing", "[G](docs/gone.md?plain=1)", "missing", "docs/gone.md", 3),
    ("fragment-only-ignored", "[Top](#project)", "ignored", None, 3),
    # reference-style [t][ref] plus [ref]: p
    ("ref-present", "Read the [guide][g].\n\n[g]: docs/guide.md", "present", None, 5),
    ("ref-missing", "Read the [guide][g].\n\n[g]: docs/gone.md", "missing", "docs/gone.md", 5),
    ("ref-collapsed-missing", "Read the [g][].\n\n[g]: docs/gone.md", "missing", "docs/gone.md", 5),
    ("ref-indented-angle-title-missing",
     'Read the [guide][g].\n\n   [g]: <docs/gone_(v2).md> "Gone"', "missing", "docs/gone_(v2).md", 5),
    ("ref-external-ignored", "Read the [guide][g].\n\n[g]: https://example.com/docs/gone.md", "ignored", None, 5),
    ("ref-dropped", "Read the [probe][p].\n\n[p]: debug/pfsoc_warm_reset.yml", "dropped",
     "debug/pfsoc_warm_reset.yml", 5),
    # autolinks <https://...>
    ("autolink-https-ignored", "<https://example.com/docs/gone.md>", "ignored", None, 3),
    ("autolink-mailto-ignored", "<mailto:dev@example.com>", "ignored", None, 3),
    # protocol-relative //host/...
    ("protocol-relative-ignored", "[CDN](//cdn.example.com/guide.md)", "ignored", None, 3),
    ("protocol-relative-image-ignored", "![logo](//cdn.example.com/logo.png)", "ignored", None, 3),
    # http(s):, mailto:, other schemes
    ("https-ignored", "[S](https://example.com/docs/gone.md)", "ignored", None, 3),
    ("http-upper-ignored", "[S](HTTP://example.com/tools/gone.py)", "ignored", None, 3),
    ("mailto-ignored", "[Mail](mailto:dev@example.com)", "ignored", None, 3),
    ("ftp-ignored", "[F](ftp://example.com/pub/gone.md)", "ignored", None, 3),
    ("tel-ignored", "[Call](tel:+41000000000)", "ignored", None, 3),
    ("bare-url-ignored", "Mirror at https://example.com/tools/gone.py today.", "ignored", None, 3),
    # absolute /p is the repository root (as GitHub renders a README)
    ("root-absolute-present", "[G](/docs/guide.md)", "present", None, 3),
    ("root-absolute-missing", "[G](/docs/gone.md)", "missing", "/docs/gone.md", 3),
    ("root-absolute-image-present", "![L](/docs/img/logo.png)", "present", None, 3),
    # backticked relative filenames: a token counts as a path when it has a
    # path separator or a known source/document extension
    # (a bare token counts when it names a document; one with a separator
    # when it starts at ./, ../ or a known top-level directory)
    ("backtick-bare-missing", "Read `missing.md` next.", "missing", "missing.md", 3),
    ("backtick-bare-rst-missing", "Read `missing.rst` next.", "missing", "missing.rst", 3),
    ("backtick-bare-root-present", "Read `README.md` first.", "present", None, 3),
    ("backtick-bare-elsewhere-present", "Read `guide.md` next.", "present", None, 3),
    ("backtick-bare-source-ignored", "Edit `hw.local.yml` and `regs_uio.h`.", "ignored", None, 3),
    ("backtick-sep-present", "Run `tools/present.py`.", "present", None, 3),
    ("backtick-sep-missing", "Run `tools/gone.sh`.", "missing", "tools/gone.sh", 3),
    ("backtick-dot-relative-present", "Read `./docs/sub/notes.md`.", "present", None, 3),
    ("backtick-dot-relative-missing", "Read `./docs/gone.md`.", "missing", "./docs/gone.md", 3),
    ("backtick-sep-unlisted-dir-ignored", "Fetched into `libs/open-logic/`.", "ignored", None, 3),
    ("backtick-sep-prose-ignored", "Both `read/write` and `etc/systemd/`.", "ignored", None, 3),
    ("backtick-dropped", "Edit `debug/pfsoc_warm_reset.yml`.", "dropped", "debug/pfsoc_warm_reset.yml", 3),
    ("backtick-command-ignored", "Run `make all` then `rr sim run`.", "ignored", None, 3),
    ("backtick-version-ignored", "Needs `v1.2` or `3.11`.", "ignored", None, 3),
    ("backtick-unknown-ext-ignored", "Boot `system.dtb` and `uImage`.", "ignored", None, 3),
    ("backtick-call-ignored", "Call `os.path.join()` here.", "ignored", None, 3),
    ("backtick-host-path-ignored", "Open `/dev/ttyUSB0` and `~/.bashrc`.", "ignored", None, 3),
    ("backtick-glob-ignored", "Every `*.md` and `docs/**/*.md`.", "ignored", None, 3),
    ("backtick-url-ignored", "Fetch `https://example.com/docs/gone.md`.", "ignored", None, 3),
    # images ![a](p)
    ("image-present", "![L](docs/img/logo.png)", "present", None, 3),
    ("image-missing", "![L](docs/img/gone.png)", "missing", "docs/img/gone.png", 3),
    ("image-title-missing", '![L](docs/img/gone.png "Logo")', "missing", "docs/img/gone.png", 3),
    # raw HTML references
    ("html-img-present", '<img src="docs/img/logo.png" alt="logo">', "present", None, 3),
    ("html-img-missing", '<img alt="logo" src="docs/img/gone.png">', "missing", "docs/img/gone.png", 3),
    ("html-a-single-quote-missing", "<a href='docs/gone.md'>guide</a>", "missing", "docs/gone.md", 3),
    ("html-a-external-ignored", '<a href="https://example.com/tools/gone.py">x</a>', "ignored", None, 3),
    # footnotes are prose; link syntax in a code span is literal
    ("footnote-ignored", "A claim.[^1]\n\n[^1]: Measured on the kit.", "ignored", None, 5),
    ("codespan-link-ignored", "Write `[G](docs/gone.md)` in Markdown.", "ignored", None, 3),
    # an indented block is read as text (documented limitation)
    ("indented-code-read-as-text-missing", "Text.\n\n    [G](docs/gone.md)", "missing", "docs/gone.md", 5),
    # links inside fenced code blocks are literal text, not links
    ("fence-unclosed-ignored", "```\n[G](docs/gone.md)", "ignored", None, 4),
    ("fence-backtick-link-ignored", "```\n[G](docs/gone.md)\n```", "ignored", None, 4),
    ("fence-tilde-image-ignored", "~~~md\n![L](docs/img/gone.png)\n~~~", "ignored", None, 4),
    ("fence-ref-def-ignored", "```\n[g]: docs/gone.md\n```", "ignored", None, 4),
    ("fence-backtick-token-ignored", "````\nRead `missing.md`\n````", "ignored", None, 4),
    # ...but a bare repository path in a fenced command is still a reference
    ("fence-bare-dropped", "```sh\npython3 debug/pfsoc_warm_reset.yml\n```", "dropped",
     "debug/pfsoc_warm_reset.yml", 4),
    ("fence-closed-then-link-missing", "```\ncode\n```\n[G](docs/gone.md)", "missing", "docs/gone.md", 6),
    # backslash escapes: CommonMark removes them from a destination, so
    # docs/guide_\(v1\).md names docs/guide_(v1).md
    ("escaped-parens-present", r"[Guide](docs/guide_\(v1\).md)", "present", None, 3),
    ("escaped-parens-image-present", r"![L](docs/img/logo\.png)", "present", None, 3),
    ("escaped-parens-missing", r"[G](docs/gone_\(v2\).md)", "missing", "docs/gone_(v2).md", 3),
    ("escaped-parens-ref-present",
     "Read the [guide][g].\n\n[g]: docs/guide_\\(v1\\).md", "present", None, 5),
    ("escaped-parens-ref-missing",
     "Read the [guide][g].\n\n[g]: docs/gone_\\(v2\\).md", "missing", "docs/gone_(v2).md", 5),
    ("escaped-backslash-text-present", r"Edit tools/present\.py first.", "present", None, 3),
    # entity and numeric character references in a destination are decoded
    ("entity-dec-parens-present", "[G](docs/guide_&#40;v1&#41;.md)", "present", None, 3),
    ("entity-hex-parens-present", "[G](docs/guide_&#x28;v1&#x29;.md)", "present", None, 3),
    ("entity-named-space-present", "[G](<docs/my&#32;guide.md>)", "present", None, 3),
    ("entity-dec-parens-missing", "[G](docs/gone_&#40;v2&#41;.md)", "missing", "docs/gone_(v2).md", 3),
    # nested brackets in link text, at any depth, and an image inside a link
    ("nested-brackets-deep-present", "[see [the [v1] guide] here](docs/guide.md)", "present", None, 3),
    ("nested-brackets-deep-missing", "[see [the [v1] guide] here](docs/gone.md)", "missing",
     "docs/gone.md", 3),
    ("nested-brackets-escaped-missing", r"[see \[v1\] guide](docs/gone.md)", "missing",
     "docs/gone.md", 3),
    ("nested-image-in-link-present", "[![L](docs/img/logo.png)](docs/guide.md)", "present", None, 3),
    ("nested-image-in-link-missing", "[![L](docs/img/gone.png)](docs/guide.md)", "missing",
     "docs/img/gone.png", 3),
    ("nested-link-target-missing", "[![L](docs/img/logo.png)](docs/gone.md)", "missing",
     "docs/gone.md", 3),
]


@pytest.mark.parametrize(
    "body,verdict,shown,line",
    [row[1:] for row in _REFERENCE_FORMS],
    ids=[row[0] for row in _REFERENCE_FORMS],
)
def test_export_judges_markdown_reference_forms(tmp_path, body, verdict, shown, line):
    """DS-P3.190: the export gate classifies each Markdown reference form."""
    src_repo = tmp_path / "src_repo"
    src_repo.mkdir()
    _init_export_fixture_repo(src_repo)
    for rel in _REF_FIXTURE_FILES:
        f = src_repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n", encoding="utf-8")
    (src_repo / "README.md").write_text(f"# Project\n\n{body}\n", encoding="utf-8")
    sha = _commit_export_fixture_repo(src_repo, "docs: reference form")
    rc, output = export_github.export_github(
        tmp_path / "export_dest", source_sha=sha, root=src_repo,
        restricted_names_file=_names_file(tmp_path),
    )
    if verdict in ("present", "ignored"):
        assert "path in docs" not in output, f"{verdict} reference refused:\n{output}"
        assert rc == 0, output
    elif verdict == "missing":
        assert (
            f"README.md:{line}: missing path in docs (not in exported tree): {shown}"
        ) in output, f"missing reference not refused:\n{output}"
        assert rc != 0, output
    else:
        assert verdict == "dropped"
        assert (
            f"README.md:{line}: dropped path in docs ({_DROP_RULE}): {shown}"
        ) in output, f"dropped reference not refused:\n{output}"
        assert rc != 0, output



def test_markdown_reference_parser_is_commonmark_and_declared():
    """DS-P3.190: links are extracted by markdown-it-py (CommonMark), which
    the project declares as a requirement with a tested range, and the module docstring
    says so."""
    doc = hygiene.__doc__ or ""
    assert "markdown-it-py" in doc and "CommonMark" in doc, doc
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"(?m)^markdown-it-py>=\d+\.\d+\.\d+,<\d+$", reqs), reqs



def test_markdown_scan_without_the_parser_is_an_error(monkeypatch):
    """DS-P3.190: without markdown-it-py a public .md cannot be judged, which
    is an inspection error and never a clean result."""
    monkeypatch.setattr(hygiene, "_DOC_MD_PARSER", None)
    monkeypatch.setitem(sys.modules, "markdown_it", None)
    with pytest.raises(hygiene.InspectionError, match="markdown-it-py"):
        hygiene.inspect_bytes(
            "README.md", b"[G](docs/gone.md)\n", export_gate=True,
            restricted_names=[], rel_path="README.md", exported_paths=set(),
        )


_PROJECT_RELEASE_URL = re.compile(r"github\.com/routertl/", re.IGNORECASE)


def _quick_start_step(number: int) -> str:
    """The README quick-start block from `# <number>.` up to the next numbered step."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index(f"# {number}. ")
    return text[start:text.index(f"# {number + 1}. ", start)]


def test_the_quick_start_builds_the_zynq_python_overlay_from_source():
    """DS-P1.106: step 4 builds the overlay (step 6 before DS-P1.121 moved install and clone into a
    common section). The retired fetch script exited 1 on every public clone, which carries no tarball
    (docs/REDISTRIBUTION.md item 1)."""
    step = _quick_start_step(4)
    assert "sw/linux/build_buildroot.sh" in step
    assert "sw/linux/extract_python_overlay.sh" in step
    assert "fetch_python_overlay" not in step
    assert step.index("build_buildroot.sh") < step.index("extract_python_overlay.sh")
    assert step.index("extract_python_overlay.sh") < step.index("rr linux build")
    for script in ("sw/linux/build_buildroot.sh", "sw/linux/extract_python_overlay.sh"):
        assert (ROOT / script).is_file(), script
        assert os.access(ROOT / script, os.X_OK), script


# Files allowed to name the retired script: the record of its removal, this test, and the PolarFire
# test that names it in prose only. Files the export drops (measurement records) are not checked.
_RETIRED_FETCH_MENTIONS = ("docs/REDISTRIBUTION.md", "tools/tests/test_export_gate_ds_p1_76.py",
                           "tools/tests/test_pfsoc_overlay_distribution_ds_p2_141.py")


def test_no_tracked_file_uses_the_retired_zynq_overlay_tarball_or_its_fetch_script():
    """DS-P1.106: the Zynq overlay tarball and sw/linux/fetch_python_overlay.sh left the tree."""
    assert not (ROOT / "sw/linux/fetch_python_overlay.sh").exists()
    assert not (ROOT / "sw/linux/python_overlay.tar.gz").exists()
    proc = subprocess.run(["git", "grep", "-l", "sw/linux/fetch_python_overlay.sh"], cwd=ROOT,
                          capture_output=True, text=True, errors="replace", check=False)
    assert proc.returncode in (0, 1), proc.stderr
    callers = [f for f in proc.stdout.split()
               if f not in _RETIRED_FETCH_MENTIONS and hygiene._matches_drop_list(f) is None]
    assert callers == []
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "sw/linux/python_overlay.tar.gz" not in attributes
    assert "sw/linux/pfsoc/python_overlay.tar.gz filter=lfs" in attributes
    assert "sw/linux/pfsoc/python_overlay.tar.gz" in hygiene.DROP_PATTERNS
    contract = (ROOT / "sw/linux/deployment_contract.yml").read_text(encoding="utf-8")
    assert "sw/linux/extract_python_overlay.sh" in contract
    assert "fetch_python_overlay" not in (ROOT / "docs/DEPLOYMENT_CONTRACT.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("script", ["sw/linux/pfsoc/fetch_python_overlay.sh"])
def test_fetch_scripts_name_no_project_release_url(script):
    text = (ROOT / script).read_text(encoding="utf-8")
    assert not _PROJECT_RELEASE_URL.search(text), f"a release URL is back in {script}"


def test_export_gate_fails_when_shipped_target_declares_pin_missing_on_top(tmp_path):
    """DS-P3.192: the export gate must pass when a shipped target's hardware.pins
    are all declared on its top_module, and fail when a shipped target declares
    a pin that its top_module lacks.
    """
    import yaml

    rfile = _names_file(tmp_path)
    target_src = (ROOT / "targets" / "pfsoc_datalogger.yml").read_text(encoding="utf-8")
    top_src = (ROOT / "hw" / "rtl" / "pfsoc" / "deepskopion_pfsoc_top.vhd").read_text(
        encoding="utf-8"
    )

    clean_export = tmp_path / "clean_export"
    (clean_export / "targets").mkdir(parents=True)
    (clean_export / "hw" / "rtl" / "pfsoc").mkdir(parents=True)
    (clean_export / "targets" / "pfsoc_datalogger.yml").write_text(
        target_src, encoding="utf-8"
    )
    (clean_export / "hw" / "rtl" / "pfsoc" / "deepskopion_pfsoc_top.vhd").write_text(
        top_src, encoding="utf-8"
    )

    clean_findings, clean_errors = hygiene.scan(
        [clean_export],
        include_index=False,
        export_gate=True,
        root=clean_export,
        restricted_names_file=rfile,
    )
    assert clean_errors == []
    assert clean_findings == []

    drifted_export = tmp_path / "drifted_export"
    (drifted_export / "targets").mkdir(parents=True)
    (drifted_export / "hw" / "rtl" / "pfsoc").mkdir(parents=True)
    drifted_cfg = yaml.safe_load(target_src)
    drifted_cfg["hardware"]["pins"] = list(drifted_cfg["hardware"]["pins"]) + ["led[0]"]
    (drifted_export / "targets" / "pfsoc_datalogger.yml").write_text(
        yaml.safe_dump(drifted_cfg, sort_keys=False), encoding="utf-8"
    )
    (drifted_export / "hw" / "rtl" / "pfsoc" / "deepskopion_pfsoc_top.vhd").write_text(
        top_src, encoding="utf-8"
    )

    findings, errors = hygiene.scan(
        [drifted_export],
        include_index=False,
        export_gate=True,
        root=drifted_export,
        restricted_names_file=rfile,
    )
    assert errors == []
    assert any("not declared on top" in f and "led[0]" in f for f in findings), (
        f"export gate missed target pin drift: {findings}"
    )
