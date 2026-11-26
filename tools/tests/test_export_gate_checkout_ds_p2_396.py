# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.396: the gate that judges a public export is the gate shipped with the exported source.

tools/export_github.py runs the hygiene gate from its own checkout. Run from an older checkout with a newer --sha, it
judged the newer source with the older gate. The export now refuses before writing anything unless its checkout is at
--sha with no tracked change. A separate source repository that ships the gate must ship the same gate bytes.

Each test runs a copy of both tools from a throwaway git checkout, so the copy's own checkout is the one judged.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ("export_github.py", "public_artifact_hygiene.py")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _names_file(tmp_path: Path) -> Path:
    path = tmp_path / "names.yml"
    path.write_text("- name: Zzqplaceholder\n", encoding="utf-8")
    return path


def _checkout(tmp_path: Path) -> tuple[Path, str, str]:
    """A checkout holding both tools with two commits: (checkout, older sha, newer sha); HEAD is the newer one."""
    co = tmp_path / "checkout"
    (co / "tools").mkdir(parents=True)
    for name in TOOLS:
        (co / "tools" / name).write_bytes((ROOT / "tools" / name).read_bytes())
    (co / "docs").mkdir()
    (co / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    _git(co.parent, "init", "-q", "-b", "main", str(co))
    _git(co, "add", "-A")
    _git(co, "commit", "-qm", "older")
    older = _git(co, "rev-parse", "HEAD")
    (co / "docs" / "guide.md").write_text("# Guide\n\nA second paragraph.\n", encoding="utf-8")
    _git(co, "commit", "-qam", "newer")
    return co, older, _git(co, "rev-parse", "HEAD")


def _export(co: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(co / "tools" / "export_github.py"),
                           "--restricted-names", str(_names_file(tmp_path)), *args],
                          cwd=co, capture_output=True, text=True, check=False)


def test_an_export_from_a_checkout_at_another_sha_is_refused_before_writing(tmp_path):
    co, older, newer = _checkout(tmp_path)
    _git(co, "checkout", "-q", older)
    dest = tmp_path / "dest"
    proc = _export(co, tmp_path, "--dest", str(dest), "--sha", newer, "--no-commit")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert older[:8] in out and newer[:8] in out, out
    assert "before any file was written" in out, out
    assert not dest.exists(), f"the refused export wrote {sorted(p.name for p in dest.iterdir())}"


def test_an_export_from_a_checkout_at_the_source_sha_runs(tmp_path):
    """Positive control on the same fixture: at --sha, the export writes and the gate passes."""
    co, _older, newer = _checkout(tmp_path)
    dest = tmp_path / "dest"
    proc = _export(co, tmp_path, "--dest", str(dest), "--sha", newer, "--no-commit")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "PUBLIC EXPORT GATE PASS" in out, out
    assert (dest / "docs" / "guide.md").read_text(encoding="utf-8") == "# Guide\n\nA second paragraph.\n"


def test_a_tracked_change_in_the_checkout_is_refused(tmp_path):
    co, _older, newer = _checkout(tmp_path)
    hygiene = co / "tools" / "public_artifact_hygiene.py"
    hygiene.write_bytes(hygiene.read_bytes() + b"\n# an unreviewed edit to the gate\n")
    dest = tmp_path / "dest"
    proc = _export(co, tmp_path, "--dest", str(dest), "--sha", newer, "--no-commit")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "tools/public_artifact_hygiene.py" in out and "tracked changes" in out, out
    assert not dest.exists()


def test_a_separate_source_with_a_different_gate_is_refused(tmp_path):
    co, _older, _newer = _checkout(tmp_path)
    src = tmp_path / "source"
    (src / "tools").mkdir(parents=True)
    for name in TOOLS:
        (src / "tools" / name).write_bytes((ROOT / "tools" / name).read_bytes())
    (src / "tools" / "public_artifact_hygiene.py").write_bytes(b"# another gate\n")
    _git(src.parent, "init", "-q", "-b", "main", str(src))
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "source")
    dest = tmp_path / "dest"
    proc = _export(co, tmp_path, "--dest", str(dest), "--root", str(src), "--no-commit")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "tools/public_artifact_hygiene.py at the source" in out, out
    assert not dest.exists()


def test_the_staged_gate_still_runs_from_a_checkout_at_another_sha(tmp_path):
    """--staged judges the index of the checkout it runs from; it names no source sha, so the rule does not apply."""
    co, older, _newer = _checkout(tmp_path)
    _git(co, "checkout", "-q", older)
    (co / "docs" / "guide.md").write_text("# Guide\n\nStaged text.\n", encoding="utf-8")
    _git(co, "add", "-A")
    proc = _export(co, tmp_path, "--staged")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "PUBLIC EXPORT GATE PASS" in out, out
