# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.339: no exported dotfile or dot-directory file carries an internal ticket ID.

DS-P2.338 held the exported .gitattributes to the docs' no-ticket-ID rule. The
same export still shipped 20 ticket IDs in .gitignore comments, 2 in
.github/workflows/ci.yml and 1 in .test-power.json. The export gate now judges
every exported path with a dot component, and those files carry no ID; the
.gitignore rules are pinned, so only comments changed.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import export_github  # noqa: E402
import public_artifact_hygiene as hygiene  # noqa: E402

# The rule lines of .gitignore (non-blank, non-comment), unchanged by this ticket.
GITIGNORE_RULES = 147   # DS-P2.360: + libs/routertl/rea/, libs/routertl/rr_ddr_stream/; DS-P2.219: + generated_mix/canfd_demo/, generated_mix/de25_canfd/
GITIGNORE_RULES_SHA256 = "48c35d926407d00397382c637724508381799d046b3c705098bc2bbe73809965"
PLANTED = b"# kept out of the history (DS-P1.106)\n"


def _index_tree(work: Path) -> str:
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
def dot_paths(tmp_path_factory):
    work = tmp_path_factory.mktemp("ds_p2_339")
    dest = work / "tree"
    dest.mkdir()
    export_github._export_tree_from_sha(dest, _index_tree(work), ROOT)
    out = {}
    for p in dest.rglob("*"):
        rel = p.relative_to(dest).as_posix()
        if p.is_file() and any(part.startswith(".") for part in rel.split("/")):
            out[rel] = p.read_bytes()
    return out


def test_the_export_carries_the_dotfiles_this_ticket_cleaned(dot_paths):
    """Vacuity control: the scan below sees the files that held IDs."""
    assert {".gitignore", ".gitattributes", ".github/workflows/ci.yml", ".test-power.json"} <= set(dot_paths)


def test_no_exported_dot_path_cites_a_ticket(dot_paths):
    found = {rel: [m.group(0).decode() for m in hygiene._TICKET_ID_RE.finditer(data)]
             for rel, data in dot_paths.items()}
    assert {rel: ids for rel, ids in found.items() if ids} == {}


def test_the_gitignore_rules_are_unchanged():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    rules = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    assert len(rules) == GITIGNORE_RULES
    assert hashlib.sha256("".join(r + "\n" for r in rules).encode()).hexdigest() == GITIGNORE_RULES_SHA256


@pytest.mark.parametrize("name, dot", [
    (".gitignore", ".gitignore"),
    ("hw/quartus/de25/.gitignore", ".gitignore"),
    (".gitattributes", ".gitattributes"),
    (".github/workflows/ci.yml", ".github"),
    (".test-power.json", ".test-power.json"),
    ("contracts/.hidden/notes.txt", ".hidden"),
])
def test_the_export_gate_refuses_a_ticket_id_in_each_dotfile_type(name, dot):
    findings = hygiene.inspect_bytes(name, PLANTED, export_gate=True)
    assert any(f"ticket narrative in {dot}" in f and "DS-P1.106" in f for f in findings), findings


@pytest.mark.parametrize("name", ["./.gitignore", "index:.gitignore"])
def test_a_prefixed_name_keeps_its_leading_dot(name):
    """A "./" or "index:" prefix is removed as a prefix, never as characters."""
    assert hygiene._export_rel_name(name, None) == ".gitignore"


def test_the_same_text_in_an_ordinary_source_file_is_not_a_finding():
    """Ticket IDs stay allowed in code and tests; the rule is about dot-paths and docs."""
    assert hygiene.inspect_bytes("tools/x.py", PLANTED, export_gate=True) == []


def test_a_dot_in_the_directory_holding_the_export_is_not_a_dot_path(tmp_path):
    """Through the real gate (the CLI the export runs): the export sits under a
    dot-directory, an ordinary file with an ID passes, a dotfile with one fails."""
    dest = tmp_path / ".cache" / "tree"
    (dest / "tools").mkdir(parents=True)
    (dest / "tools" / "x.py").write_bytes(PLANTED)
    rc, out = export_github.run_hygiene_gate(dest)
    assert rc == 0, out
    (dest / ".gitignore").write_bytes(PLANTED)
    rc, out = export_github.run_hygiene_gate(dest)
    assert rc != 0 and "ticket narrative in .gitignore" in out and "tools/x.py" not in out, out
