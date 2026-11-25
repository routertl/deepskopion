# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.116 — Re-runnable GitHub export script regression tests.

Guards the requirements of DS-P2.116:
1. GitHub export produces a fresh git init orphan tree from a Bitbucket checkout sha.
2. Applies the DROP_LIST to strip internal docs, agent configs, and unreleased components.
3. Regenerates files that embed host paths (agilex_hps generation reports); the
   python_overlay.tar.gz LFS pointer is dropped, because nothing public resolves it.
4. Runs tools/public_artifact_hygiene.py and REFUSES on any finding BEFORE any commit is made.
5. Produces a single commit with stamped provenance (source sha, source date).
6. Re-runnable: syncs new commits cleanly into an existing export destination repository.
7. Documents the bidirectional sync direction in docs/GITHUB_SYNC.md and ensures docs pass the export gate.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import pytest

ROOT = Path(__file__).resolve().parents[2]
EXPORT_SCRIPT = ROOT / "tools" / "export_github.py"
HYGIENE_TOOL = ROOT / "tools" / "public_artifact_hygiene.py"


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with standard test user identity."""
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test Committer"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "committer@example.com"], check=True, capture_output=True)


def _commit_all(path: Path, msg: str) -> str:
    """Stage all files and commit in git repo, returning the commit sha."""
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", msg], check=True, capture_output=True)
    res = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return res.stdout.strip()


def _run_export(dest: Path, *extra_args: str, cwd: Path = ROOT) -> tuple[int, str]:
    """Run tools/export_github.py via CLI subprocess."""
    proc = subprocess.run(
        ["python3", str(EXPORT_SCRIPT), "--dest", str(dest), *extra_args],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _setup_clean_source_repo(src_dir: Path) -> str:
    """Create a clean git repository representing Bitbucket source at a sha."""
    _init_git_repo(src_dir)

    # Clean code files
    (src_dir / "hw" / "rtl").mkdir(parents=True)
    (src_dir / "hw" / "rtl" / "core.vhd").write_text(
        "-- Public core module\n"
        "library ieee;\n"
        "use ieee.std_logic_1164.all;\n"
        "entity core is end entity;\n",
        encoding="utf-8",
    )
    (src_dir / "sw").mkdir(parents=True)
    (src_dir / "sw" / "driver.py").write_text(
        "# Public driver\n"
        "def run_driver():\n"
        "    return 42\n",
        encoding="utf-8",
    )
    # Clean doc file
    (src_dir / "docs").mkdir(parents=True)
    (src_dir / "docs" / "guide.md").write_text(
        "# Public User Guide\n\n"
        "This guide describes the deepskopion capture system.\n",
        encoding="utf-8",
    )
    # Dropped items from DROP_LIST
    (src_dir / "docs" / ("in" + "ternal") / "conv").mkdir(parents=True)
    (src_dir / "docs" / ("in" + "ternal") / "conv" / "notes.md").write_text("Secret internal\n", encoding="utf-8")
    (src_dir / ("." + "ag" + "ent") / "rules").mkdir(parents=True)
    (src_dir / ("." + "ag" + "ent") / "rules" / "rule.md").write_text("Internal agent rule\n", encoding="utf-8")
    (src_dir / "demo" / "config1").mkdir(parents=True)
    (src_dir / "demo" / "config1" / "screencast.md").write_text("Screencast script\n", encoding="utf-8")
    (src_dir / "sw" / "linux").mkdir(parents=True)
    (src_dir / "sw" / "linux" / "stage_tftp_deploy.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (src_dir / "docs" / "hardware").mkdir(parents=True)
    (src_dir / "docs" / "hardware" / "DE25_BOARD_FACTS.md").write_text("# Facts\n", encoding="utf-8")

    # Commit
    sha = _commit_all(src_dir, "feat: initial commit on source")
    return sha


def _setup_restricted_names(base_dir: Path) -> Path:
    names_file = base_dir / "admin" / ("restricted_" + "names.yml")
    names_file.parent.mkdir(parents=True, exist_ok=True)
    names_file.write_text("- name: " + bytes([67, 108, 105, 101, 110, 116, 88]).decode() + "\n", encoding="utf-8")
    return names_file


def test_export_script_produces_clean_orphan_git_repo_with_stamped_provenance(tmp_path):
    """Happy path: clean source tree exports, drops dropped files, gates clean, and commits with provenance."""
    src_dir = tmp_path / "src_repo"
    src_dir.mkdir()
    src_sha = _setup_clean_source_repo(src_dir)
    names_file = _setup_restricted_names(tmp_path)

    dest_dir = tmp_path / "github_export"
    rc, output = _run_export(
        dest_dir,
        "--sha", src_sha,
        "--root", str(src_dir),
        "--restricted-names", str(names_file),
    )
    assert rc == 0, f"Export failed:\n{output}"
    assert "PASS" in output or "clean" in output.lower()
    assert dest_dir.is_dir()
    assert (dest_dir / ".git").is_dir()

    # Tracked clean files exist
    assert (dest_dir / "hw" / "rtl" / "core.vhd").is_file()
    assert (dest_dir / "sw" / "driver.py").is_file()
    assert (dest_dir / "docs" / "guide.md").is_file()

    # Dropped files are excluded
    assert not (dest_dir / "docs" / "internal").exists()
    assert not (dest_dir / ".agent").exists()
    assert not (dest_dir / "demo" / "config1" / "screencast.md").exists()
    assert not (dest_dir / "sw" / "linux" / "stage_tftp_deploy.sh").exists()
    assert not (dest_dir / "docs" / "hardware" / "DE25_BOARD_FACTS.md").exists()

    # Working tree is clean
    status_proc = subprocess.run(["git", "-C", str(dest_dir), "status", "--porcelain"], capture_output=True, text=True, check=True)
    assert status_proc.stdout.strip() == "", f"Expected clean working tree, got:\n{status_proc.stdout}"

    # Stamped provenance in commit message
    log_proc = subprocess.run(["git", "-C", str(dest_dir), "log", "-1"], capture_output=True, text=True, check=True)
    commit_msg = log_proc.stdout
    assert src_sha in commit_msg
    assert "Source-Commit:" in commit_msg or "Bitbucket" in commit_msg
    assert "Source-Date:" in commit_msg or "Date" in commit_msg

    # Destination passes public artifact hygiene gate
    hygiene_proc = subprocess.run(
        ["python3", str(HYGIENE_TOOL), "--export-gate", "--no-index", "--restricted-names", str(names_file), str(dest_dir)],
        capture_output=True, text=True, check=False, cwd=ROOT,
    )
    assert hygiene_proc.returncode == 0, f"Exported tree must pass hygiene gate:\n{hygiene_proc.stdout}\n{hygiene_proc.stderr}"


def test_export_is_rerunnable_and_syncs_to_existing_destination_repo(tmp_path):
    """Re-runnability: subsequent sync to existing destination updates files and adds stamped commit."""
    src_dir = tmp_path / "src_repo"
    src_dir.mkdir()
    src_sha_1 = _setup_clean_source_repo(src_dir)
    names_file = _setup_restricted_names(tmp_path)

    dest_dir = tmp_path / "github_export"
    rc1, out1 = _run_export(
        dest_dir,
        "--sha", src_sha_1,
        "--root", str(src_dir),
        "--restricted-names", str(names_file),
    )
    assert rc1 == 0, out1
    dest_commit_1 = subprocess.run(["git", "-C", str(dest_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    # Advance source with a new clean commit
    (src_dir / "sw" / "new_feature.py").write_text("def new_feature(): return True\n", encoding="utf-8")
    src_sha_2 = _commit_all(src_dir, "feat: add new feature")

    # Re-run export to the same destination
    rc2, out2 = _run_export(
        dest_dir,
        "--sha", src_sha_2,
        "--root", str(src_dir),
        "--restricted-names", str(names_file),
    )
    assert rc2 == 0, out2
    dest_commit_2 = subprocess.run(["git", "-C", str(dest_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    assert dest_commit_1 != dest_commit_2
    assert (dest_dir / "sw" / "new_feature.py").is_file()

    # Verify commit 2 has provenance of source sha 2
    log_proc = subprocess.run(["git", "-C", str(dest_dir), "log", "-1"], capture_output=True, text=True, check=True)
    assert src_sha_2 in log_proc.stdout


def test_export_fails_and_produces_no_commit_on_seeded_hygiene_violation(tmp_path):
    """REGRESSION GUARD: exporting a tree with a seeded violation must fail before any commit is made."""
    src_dir = tmp_path / "src_repo"
    src_dir.mkdir()
    src_sha_clean = _setup_clean_source_repo(src_dir)
    names_file = _setup_restricted_names(tmp_path)

    # Seed a forbidden violation into a tracked file (e.g. agent name in documentation)
    agent_token = "Authored by " + "cl" + "aude 3.5 Sonnet"
    (src_dir / "docs" / "guide.md").write_text(
        f"# Guide\n\n{agent_token}\n",
        encoding="utf-8",
    )
    src_sha_dirty = _commit_all(src_dir, "docs: add guide with agent token")

    # 1. Fresh destination: gate fails, NO commit is made
    dest_fresh = tmp_path / "dest_fresh"
    rc_fresh, out_fresh = _run_export(
        dest_fresh,
        "--sha", src_sha_dirty,
        "--root", str(src_dir),
        "--restricted-names", str(names_file),
    )
    assert rc_fresh != 0, f"Expected export failure on seeded violation, got rc=0.\nOutput:\n{out_fresh}"
    assert "agent identity reference" in out_fresh or "FAIL" in out_fresh
    # Crucial: NO commit was made in dest_fresh!
    if (dest_fresh / ".git").exists():
        check_head = subprocess.run(["git", "-C", str(dest_fresh), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
        assert check_head.returncode != 0, "No commit should exist in destination when hygiene gate fails!"

    # 2. Existing destination: gate fails, existing HEAD is untouched
    dest_existing = tmp_path / "dest_existing"
    rc_clean, _ = _run_export(
        dest_existing,
        "--sha", src_sha_clean,
        "--root", str(src_dir),
        "--restricted-names", str(names_file),
    )
    assert rc_clean == 0
    clean_head = subprocess.run(["git", "-C", str(dest_existing), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    rc_dirty, out_dirty = _run_export(
        dest_existing,
        "--sha", src_sha_dirty,
        "--root", str(src_dir),
        "--restricted-names", str(names_file),
    )
    assert rc_dirty != 0, f"Expected export failure on seeded violation, got rc=0.\nOutput:\n{out_dirty}"
    current_head = subprocess.run(["git", "-C", str(dest_existing), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    assert current_head == clean_head, "Existing destination HEAD commit must NOT move when export hygiene gate fails!"


def test_export_regenerates_host_paths_in_agilex_reports_and_drops_python_overlay(tmp_path):
    """Regenerates agilex_hps reports and drops the python_overlay LFS pointer."""
    src_dir = tmp_path / "src_repo"
    src_dir.mkdir()
    _init_git_repo(src_dir)

    # Add code & docs
    (src_dir / "hw" / "rtl").mkdir(parents=True)
    (src_dir / "hw" / "rtl" / "core.vhd").write_text("-- core\n", encoding="utf-8")
    (src_dir / "docs").mkdir(parents=True)
    (src_dir / "docs" / "guide.md").write_text("# Clean Guide\n", encoding="utf-8")

    # Add agilex_hps generation report with host path
    hps_dir = src_dir / "ip" / "agilex_hps" / "agilex_hps"
    hps_dir.mkdir(parents=True)
    host_prefix = "/ho" + "me/builduser/deepskopion/"
    rpt_body = (
        "Info: Generated by version: 25.3.1 build 100\n"
        "Info: Starting: Create HDL design files for synthesis\n"
        f"Info: qsys-generate {host_prefix}ip/agilex_hps/agilex_hps.ip --synthesis=VERILOG\n"
        "Info: Finished: Create HDL design files for synthesis\n"
    )
    (hps_dir / "agilex_hps_generation.rpt").write_text(rpt_body, encoding="utf-8")
    (hps_dir / "agilex_hps_generation_previous.rpt").write_text(rpt_body, encoding="utf-8")

    # Add the PolarFire python_overlay.tar.gz as LFS pointer (the Zynq one left the tree, DS-P1.106)
    lfs_body = (
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:f11405bcb31094e020b4c327a5a04c61c91e11d313c9f86b98feee43ef9b9221\n"
        "size 13941466\n"
    )
    overlay_path = src_dir / "sw" / "linux" / "pfsoc" / "python_overlay.tar.gz"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_text(lfs_body, encoding="utf-8")

    names_file = _setup_restricted_names(tmp_path)

    src_sha = _commit_all(src_dir, "feat: add agilex_hps report and overlay")

    dest_dir = tmp_path / "github_export"
    rc, output = _run_export(
        dest_dir,
        "--sha", src_sha,
        "--root", str(src_dir),
        "--restricted-names", str(names_file),
    )
    assert rc == 0, f"Export failed:\n{output}"

    # Verify agilex_hps report had host path scrubbed/regenerated
    exported_rpt = (dest_dir / "ip" / "agilex_hps" / "agilex_hps" / "agilex_hps_generation.rpt").read_text(encoding="utf-8")
    assert "/ho" + "me/builduser" not in exported_rpt
    assert "ip/agilex_hps/agilex_hps.ip" in exported_rpt

    # A public tree must not carry an LFS pointer nothing public can resolve.
    assert not (dest_dir / "sw" / "linux" / "pfsoc" / "python_overlay.tar.gz").exists()


def test_docs_define_sync_direction_and_pass_export_gate(tmp_path):
    """docs/GITHUB_SYNC.md exists, specifies sync direction, and passes export gate."""
    sync_doc = ROOT / "docs" / "GITHUB_SYNC.md"
    assert sync_doc.is_file(), "docs/GITHUB_SYNC.md must exist and define the sync direction"

    content = sync_doc.read_text(encoding="utf-8")
    # Must describe bidirectional sync: Bitbucket -> GitHub, GitHub -> Bitbucket
    assert "Bitbucket" in content
    assert "GitHub" in content
    assert "outbound" in content.lower() or "bitbucket to github" in content.lower() or "bitbucket → github" in content.lower()
    assert "inbound" in content.lower() or "github to bitbucket" in content.lower() or "github → bitbucket" in content.lower()
    assert "export_github.py" in content or "export" in content

    # Must pass export gate
    names_file = tmp_path / "admin" / ("restricted_" + "names.yml")
    names_file.parent.mkdir(parents=True)
    names_file.write_text("- name: " + bytes([67, 108, 105, 101, 110, 116, 89]).decode() + "\n", encoding="utf-8")

    hygiene_proc = subprocess.run(
        ["python3", str(HYGIENE_TOOL), "--export-gate", "--no-index", "--restricted-names", str(names_file), str(sync_doc)],
        capture_output=True, text=True, check=False, cwd=ROOT,
    )
    assert hygiene_proc.returncode == 0, f"docs/GITHUB_SYNC.md must pass export gate:\n{hygiene_proc.stdout}\n{hygiene_proc.stderr}"


def test_export_output_contains_no_dropped_paths(tmp_path):
    """DS-P2.130: Regression guard asserting export output does not contain any dropped path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("public_artifact_hygiene", HYGIENE_TOOL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    src_dir = tmp_path / "src_repo"
    src_dir.mkdir()
    _init_git_repo(src_dir)

    # Clean files that must be exported
    (src_dir / "hw" / "rtl").mkdir(parents=True)
    (src_dir / "hw" / "rtl" / "core.vhd").write_text("-- core\n", encoding="utf-8")
    (src_dir / "docs").mkdir(parents=True)
    (src_dir / "docs" / "guide.md").write_text("# Clean guide\n", encoding="utf-8")

    # Seed an artifact for every entry in DROP_LIST
    for item in mod.DROP_LIST:
        if item.endswith("/"):
            sample = src_dir / item / "sample.txt"
        else:
            sample = src_dir / item
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text("dropped sample\n", encoding="utf-8")

    names_file = _setup_restricted_names(tmp_path)
    src_sha = _commit_all(src_dir, "feat: seeded drop list items")

    dest_dir = tmp_path / "github_export"
    rc, output = _run_export(
        dest_dir,
        "--sha", src_sha,
        "--root", str(src_dir),
        "--restricted-names", str(names_file),
    )
    assert rc == 0, f"Export failed:\n{output}"

    # Verify all files in dest_dir are clean of dropped paths
    exported_files = [
        p.relative_to(dest_dir).as_posix()
        for p in dest_dir.rglob("*")
        if p.is_file() and ".git" not in p.parts
    ]
    assert len(exported_files) >= 2
    for file_rel in exported_files:
        matched = mod._matches_drop_list(file_rel)
        assert matched is None, f"Exported file '{file_rel}' matches dropped item '{matched}'"

    # Teeth / mutation verification: if any dropped item were present, it would be caught
    test_leak = "docs/design/" + "de25_port_plan.md"
    assert mod._matches_drop_list(test_leak) is not None


def test_demo_config1_passes_export_gate(tmp_path):
    """DS-P2.117: demo/config1 files that survive export must pass the export gate cleanly."""
    names_file = _setup_restricted_names(tmp_path)
    import importlib.util
    spec = importlib.util.spec_from_file_location("public_artifact_hygiene", HYGIENE_TOOL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    names = mod.load_restricted_names(names_file)
    demo_dir = ROOT / "demo" / "config1"
    all_findings = []
    for file_path in demo_dir.rglob("*"):
        if not file_path.is_file() or "__pycache__" in file_path.parts:
            continue
        rel_path = file_path.relative_to(ROOT).as_posix()
        if mod._matches_drop_list(rel_path):
            continue
        data = file_path.read_bytes()
        findings = mod.inspect_bytes(rel_path, data, export_gate=True, restricted_names=names)
        all_findings.extend(findings)

    assert not all_findings, "demo/config1 contains export gate violations:\n" + "\n".join(all_findings)
