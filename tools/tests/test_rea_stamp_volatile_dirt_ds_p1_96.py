# rr unknown-llm (AI-generated)
"""DS-P1.96: build-metadata dirt does not make the REA design stamp dirty.

THE DEFECT (DS-P2.164 run 4, 2026-11-07, clean clone at fa20aa5f, rr 5.3.0).
DE-G0 builds in two queued rr invocations, synth then bitstream, and the
``rr_rea_build_id`` pre_build hook runs in both:

* synth: the tree is clean, so the hook stamps 0x72B6066D into the netlist;
  the same invocation's regbank-regen then rewrites the TRACKED
  ``hw/rtl/pkg/rom_info_pkg.vhd`` (file date, git hash, system version, build
  date: exactly its declared ``volatile_lines`` in tools/generated_sources.yml);
* bitstream: the tree is now dirty, so the hook stamps 0xDA3B1CB9 (dirty bit
  set) and overwrites the sidecar, but the bitstream stage does not
  re-synthesize.

The board read 72b6066d, `rr hw bit-sha` expected da3b1cb9: MISMATCH on a board
running the right build.

THE FIX: tools/hooks/run_sdk_generator.py treats a tree whose ONLY change is
the declared volatile lines of a declared file as clean, and stamps rr's
``compute_design_identity(dirty=False)``. Any other change (a non-volatile line
of the same file, another file, an untracked file) still goes to rr's own
dirty stamp. Making the clone stay clean after a build is DS-P2.231.

POWER:
* BASELINE: on the pre-fix wrapper the volatile-only case and the two-stage
  case are RED (the stamp has the dirty bit and differs between stages).
* MUTATION: comparing paths only (not the masked content) turns the
  non-volatile-line case RED; accepting any modified file turns the
  other-file case RED.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "tools" / "hooks" / "run_sdk_generator.py"
ROM_INFO = "hw/rtl/pkg/rom_info_pkg.vhd"
PROJECT_NAME = "deepskopion"

bit_sha = pytest.importorskip("sdk.engine.bit_sha")
rea_build_id = pytest.importorskip("sdk.scripts.rr_rea_build_id")


def _git_env() -> dict:
    """The caller's env minus any GIT_* that would point git at another repo
    (the pre-commit gate exports GIT_INDEX_FILE and friends)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, env=_git_env(), check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A committed, clean project: project.yml + target pointer, the real
    rom_info_pkg.vhd and one RTL file.

    DS-P3.245: since DS-P2.231 rom_info_pkg.vhd is a gitignored build output, so a fresh clone
    has none. The fixture generates its own, the way the pre_sim hook does
    (regen_regbank.sh --rom-info-only), into this test's temporary directory; it never reads
    the checkout's build output."""
    gen = tmp_path / "rom_info"
    g = subprocess.run(
        ["bash", "tools/regen_regbank.sh", "--rom-info-only", "--output-dir", str(gen),
         "--regbank-dir", "hw/rtl/regbank", "--library", "tich"],
        cwd=ROOT, capture_output=True, text=True, errors="replace")
    assert g.returncode == 0, (g.stdout + g.stderr)[-2000:]
    r = tmp_path / "proj"
    (r / "targets").mkdir(parents=True)
    (r / "hw" / "rtl" / "pkg").mkdir(parents=True)
    (r / "project.yml").write_text(
        f"name: {PROJECT_NAME}\ntarget: targets/t.yml\n", encoding="utf-8")
    (r / "targets" / "t.yml").write_text(
        f"project:\n  name: {PROJECT_NAME}\nhardware:\n  vendor: altera\n",
        encoding="utf-8")
    shutil.copyfile(gen / "rom_info_pkg.vhd", r / ROM_INFO)
    (r / "hw" / "rtl" / "foo.vhd").write_text("entity foo is end;\n",
                                              encoding="utf-8")
    # As in the real tree: the hook's own outputs are gitignored.
    (r / ".gitignore").write_text("build/\ngenerated/\n", encoding="utf-8")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def _stamp(repo: Path) -> int:
    """Run the hook the way the de25_datalogger pre_build entry does."""
    sidecar = repo / "build" / "bitstream" / f"{PROJECT_NAME}.rea_build_id"
    out = repo / "generated" / "rr_rea_build_id_pkg.vhd"
    r = subprocess.run(
        [sys.executable, str(WRAPPER), "rea-build-id",
         "--design-identity", PROJECT_NAME,
         "--sidecar", str(sidecar), "--out", str(out)],
        cwd=repo, env=_git_env(), capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    value = int(sidecar.read_text(encoding="utf-8").strip(), 16)
    assert f'x"{value:08X}"' in out.read_text(encoding="utf-8"), (
        "the package and the sidecar disagree")
    return value


def _clean_identity(repo: Path) -> int:
    head = _git(repo, "rev-parse", "HEAD").strip()
    key = bit_sha.usr_access_target_key(repo, PROJECT_NAME)
    return rea_build_id.compute_design_identity(head, key, dirty=False)


def _rewrite_volatile_lines(repo: Path, stamp: str) -> None:
    """What regbank-regen does to the tracked file on every build."""
    p = repo / ROM_INFO
    out = []
    for line in p.read_text(encoding="utf-8").splitlines(keepends=True):
        s = line.lstrip()
        if line.startswith("-- File created @"):
            line = f"-- File created @{stamp}\n"
        elif s.startswith("constant C_GIT_HASH"):
            line = line.split(":=")[0] + ':= x"fa20aa5f";\n'
        elif s.startswith("constant C_SYSTEM_VERSION"):
            line = line.split(":=")[0] + ':= x"00012801";\n'
        elif s.startswith("constant C_BUILD_DATE"):
            line = line.split(":=")[0] + f':= x"{zlib.crc32(stamp.encode()):08x}";\n'
        out.append(line)
    p.write_text("".join(out), encoding="utf-8")
    assert _git(repo, "status", "--porcelain").strip() == f"M {ROM_INFO}"


def test_a_clean_tree_stamps_the_clean_identity(repo):
    assert _stamp(repo) == _clean_identity(repo)


def test_volatile_only_dirt_stamps_the_clean_identity(repo):
    _rewrite_volatile_lines(repo, "Sat Nov  7 17:41:33 2026")
    value = _stamp(repo)
    assert value & 0x80000000 == 0, f"0x{value:08X} carries the dirty bit"
    assert value == _clean_identity(repo)


def test_both_stages_of_a_two_stage_build_stamp_the_same_value(repo):
    """Run 4's sequence: stamp on the clean tree (synth), regbank-regen
    rewrites the volatile lines, stamp again (bitstream)."""
    synth = _stamp(repo)
    _rewrite_volatile_lines(repo, "Sat Nov  7 17:36:08 2026")
    bitstream = _stamp(repo)
    assert bitstream == synth, f"synth 0x{synth:08X} != bitstream 0x{bitstream:08X}"


def test_a_non_volatile_change_to_rom_info_still_stamps_dirty(repo):
    _rewrite_volatile_lines(repo, "Sat Nov  7 17:41:33 2026")
    p = repo / ROM_INFO
    p.write_text(p.read_text(encoding="utf-8") + "-- a real edit\n",
                 encoding="utf-8")
    value = _stamp(repo)
    assert value & 0x80000000, f"0x{value:08X} lost the dirty bit"
    assert value != _clean_identity(repo)


def test_a_change_to_another_file_still_stamps_dirty(repo):
    _rewrite_volatile_lines(repo, "Sat Nov  7 17:41:33 2026")
    foo = repo / "hw" / "rtl" / "foo.vhd"
    foo.write_text(foo.read_text(encoding="utf-8") + "-- edit\n",
                   encoding="utf-8")
    assert _stamp(repo) & 0x80000000


def test_an_untracked_file_still_stamps_dirty(repo):
    _rewrite_volatile_lines(repo, "Sat Nov  7 17:41:33 2026")
    (repo / "hw" / "rtl" / "new.vhd").write_text("entity n is end;\n",
                                                 encoding="utf-8")
    assert _stamp(repo) & 0x80000000
