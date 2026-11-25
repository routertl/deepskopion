# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.205: a document that disagrees with its contract is refused at commit.

A hand edit once removed text from the generated section of
docs/DEPLOYMENT_CONTRACT.md while the declaration kept it. The contract check
failed from then on, and stage_daemon.sh runs that check before every rootfs.
These tests run the hook's own leg, cut out of hooks/pre-commit.local, against a
scratch copy of the tree.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from public_artifact_hygiene import _matches_drop_list  # noqa: E402  (the export's own drop list)
HOOK = ROOT / "hooks" / "pre-commit.local"
LEG_START = "# ── DS-P2.205 — deployment contract ──"
LEG_END = "# ── DS-P2.189"
TEST_REL = "sw/linux/tests/test_deployment_contract.py"
YML_REL = "sw/linux/deployment_contract.yml"
DOC_REL = "docs/DEPLOYMENT_CONTRACT.md"
LISTEN = "0.0.0.0:7476 (wave_events_v1"
TICKET = "DS-" + "P2.50"
MISMATCH = "generated autostart section no longer matches"


def _hook_leg() -> str:
    text = HOOK.read_text(encoding="utf-8")
    start = text.index(LEG_START)
    return text[start:text.index(LEG_END, start)]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "sw/linux", "targets", "docs/*.md",
         "docs/software/README.md", "README.md", ".gitignore",
         "bench"],  # DS-P1.103: the bench targets and bench/deployment_contract.yml
        check=True, capture_output=True, text=True,
    ).stdout
    work = tmp_path / "work"
    work.mkdir()
    for rel in filter(None, listing.split("\0")):
        src = ROOT / rel
        if not src.is_file() or src.stat().st_size > 1_000_000:
            continue
        dst = work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    return work


def _run_leg(tree: Path) -> subprocess.CompletedProcess[str]:
    script = f'set -u\nDS_GUARD_PY="{sys.executable}"\n{_hook_leg()}\necho LEG_PASSED\n'
    return subprocess.run(
        ["bash", "-c", script], cwd=tree, text=True, capture_output=True, check=False,
    )


def test_the_contract_as_committed_passes(tree):
    proc = _run_leg(tree)
    assert "LEG_PASSED" in proc.stdout, proc.stdout + proc.stderr


def test_the_regression_state_refuses_the_commit(tree):
    """The declaration keeps text that a hand edit took out of the document."""
    yml = tree / YML_REL
    text = yml.read_text(encoding="utf-8")
    needle = f"{LISTEN})"
    assert needle in text
    yml.write_text(text.replace(needle, f"{LISTEN}, {TICKET})"), encoding="utf-8")
    proc = _run_leg(tree)
    assert proc.returncode == 1
    assert "LEG_PASSED" not in proc.stdout
    assert "Commit aborted" in proc.stdout
    assert MISMATCH in proc.stdout


def test_a_hand_edit_of_the_generated_section_refuses_the_commit(tree):
    doc = tree / DOC_REL
    text = doc.read_text(encoding="utf-8")
    needle = f"{LISTEN})"
    assert needle in text
    doc.write_text(text.replace(needle, f"{LISTEN} edited)"), encoding="utf-8")
    proc = _run_leg(tree)
    assert proc.returncode == 1
    assert "LEG_PASSED" not in proc.stdout
    assert MISMATCH in proc.stdout


def test_a_gate_that_cannot_run_refuses_the_commit(tree):
    (tree / TEST_REL).unlink()
    proc = _run_leg(tree)
    assert proc.returncode == 1
    assert "LEG_PASSED" not in proc.stdout
    assert "GATE COULD NOT RUN" in proc.stdout


def _drop_verdict(path: str):
    sys.path.insert(0, str(ROOT / "tools"))
    from public_artifact_hygiene import _matches_drop_list
    return _matches_drop_list(path)


def test_the_public_contract_names_only_what_the_export_carries():
    import yaml
    for rel in (DOC_REL, YML_REL):
        text = (ROOT / rel).read_text(encoding="utf-8")
        paths = set(re.findall(r"\b(?:sw|docs|tools|targets|hw)/[\w./-]+\.\w+", text))
        assert paths, rel
        dropped = {path for path in paths if _drop_verdict(path) is not None}
        assert dropped == set(), rel
    decl = yaml.safe_load((ROOT / YML_REL).read_text(encoding="utf-8"))
    dropped = [name for name in decl["boot"]["targets"]
               if _drop_verdict(f"targets/{name}.yml") is not None]
    assert dropped == []


def test_public_ci_runs_the_contract_tests():
    assert "sw/linux/tests" in (ROOT / "tools" / "ci" / "run_desk_tests.sh").read_text(encoding="utf-8")


def _staged_scratch_tree(tmp_path, skip):
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "sw/linux", "sw/stream", "contracts",
         "targets", "docs/DEPLOYMENT_CONTRACT.md", "docs/software/telemetry_exporter.md",
         "README.md", ".gitignore"],
        check=True, capture_output=True, text=True,
    ).stdout
    work = tmp_path / "work"
    for rel in filter(None, listing.split("\0")):
        src = ROOT / rel
        if skip(rel) or not src.is_file():
            continue
        dst = work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
    return work


def _stage(work):
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    return subprocess.run(
        ["bash", str(work / "sw/linux/stage_daemon.sh")],
        cwd=work, text=True, capture_output=True, check=False,
    )


def test_staging_works_in_a_tree_without_a_board_sidecar(tmp_path):
    """stage_daemon.sh once aborted on a link into the directory the export drops
    (8bcb9830: the link's parent directory was absent). A tree with no board
    directory at all is still the case this guards. DS-P1.118: PolarFire is now
    declared in the public contract, so the scratch declaration drops it to match
    the tree, and the generated document is regenerated from that declaration."""
    work = _staged_scratch_tree(
        tmp_path, lambda rel: rel.startswith(("sw/linux/pfsoc/", "targets/pfsoc_")))
    yml = work / YML_REL
    text = yml.read_text(encoding="utf-8")
    assert "    - pfsoc_datalogger\n" in text
    yml.write_text(text.replace("    - pfsoc_datalogger\n", ""), encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    regen = subprocess.run([sys.executable, str(work / "sw/linux/deployment_contract.py"), "--write"],
                           cwd=work, capture_output=True, text=True)
    assert regen.returncode == 0, regen.stdout + regen.stderr
    assert "pfsoc_datalogger" not in (work / DOC_REL).read_text(encoding="utf-8")
    assert not (work / "sw/linux/pfsoc").exists()
    proc = _stage(work)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (work / "sw/linux/rootfs_overlay/usr/local/share/deepskopion/sw/stream").is_dir()


def test_staging_works_in_the_public_export_tree(tmp_path):
    """DS-P1.118: the scratch tree is what the export carries, by the export's own
    drop list. PolarFire ships, so its target and sw/linux/pfsoc/ stay in (only the
    LFS archive is dropped), and no board sidecar exists."""
    work = _staged_scratch_tree(tmp_path, _matches_drop_list)
    # DS-P3.314: the fixture is the export's tree: the PolarFire hook ships, its LFS archive does not.
    assert (work / "sw/linux/pfsoc/stage_hook.sh").is_file()
    assert not (work / "sw/linux/pfsoc/python_overlay.tar.gz").exists()
    proc = _stage(work)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (work / "sw/linux/rootfs_overlay/usr/local/share/deepskopion/sw/stream").is_dir()
    # Staging linked the overlay `rr linux rootfs` reads for PolarFire.
    link = work / "sw/linux/pfsoc/rootfs_overlay"
    assert link.is_symlink(), "staging did not create sw/linux/pfsoc/rootfs_overlay"
    assert link.readlink().as_posix() == "../rootfs_overlay"
    s99 = link / "etc/init.d/S99deepskopion-stream"
    assert s99.is_file(), "S99deepskopion-stream does not resolve through the PolarFire overlay link"
    assert s99.resolve() == (work / "sw/linux/rootfs_overlay/etc/init.d/S99deepskopion-stream").resolve()


def test_the_shared_staging_files_name_no_dropped_board():
    for rel in ("sw/linux/stage_daemon.sh", "sw/linux/rootfs_overlay/etc/init.d/S99deepskopion-stream"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"pfsoc|polarfire|riscv", text, re.I), rel
