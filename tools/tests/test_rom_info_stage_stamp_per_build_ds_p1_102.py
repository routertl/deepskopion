# rr unknown-llm (AI-generated)
"""DS-P1.102: the rom_info stage stamp belongs to the build, not to the queue job.

routertl >= 5.4.0 (RTL-P1.310) stops `rr bitstream run` on Quartus when a pre_build hook output's bytes differ
from the synthesized ones. regbank-regen rewrites hw/rtl/pkg/rom_info_pkg.vhd at every stage, and the
volatile fields (creation line, C_BUILD_DATE) were kept only against a stamp keyed on RR_JOB_ID. Every queued
stage is a new job, so the bitstream job of a build never found the synth job's stamp and wrote a new date:
DE-G0 run 18 (2026-11-13), synth 8332aee91ed6 then bitstream cbc441d8c842 exit 1.

Expected values are hard-coded: the build-date literals below are what the output must hold.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

import tools.generated_source_contract as contract

ROOT = Path(__file__).resolve().parents[2]
REL = "hw/rtl/pkg/rom_info_pkg.vhd"
_STAMP_ENV = ("STAGE_STAMP", "RR_STAGE_STAMP", "RR_JOB_ID", "BUILD_DIR", "RR_BUILD_DIR", "RR_TARGET")
DATE_A = "1A2B3C4D"
DATE_B = "5E6F7081"


def _rom_info(tmp_path: Path) -> str:
    out = tmp_path / "gen"
    r = subprocess.run(
        ["bash", "tools/regen_regbank.sh", "--rom-info-only", "--output-dir", str(out),
         "--regbank-dir", "hw/rtl/regbank", "--library", "tich"],
        cwd=ROOT, capture_output=True, text=True, env=_clean_env(tmp_path / "gen_build"))
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    return (out / "rom_info_pkg.vhd").read_text(encoding="utf-8")


def _clean_env(build_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STAMP_ENV}
    env["RR_BUILD_DIR"] = str(build_dir)
    return env


def _with(text: str, field: str, line: str) -> str:
    rule = next(r for r in contract.load_manifest()["volatile_lines"][REL] if r["field"] == field)
    new, n = re.subn(rule["pattern"], line, text, count=1, flags=re.MULTILINE)
    assert n == 1, field
    return new


def _dated(text: str, date: str, created: str) -> str:
    text = _with(text, "build_date",
                 f'  constant C_BUILD_DATE      : std_logic_vector(C_ROM_DATA_WIDTH-1 downto 0) := x"{date}";')
    return _with(text, "generated_at", f"-- File created @{created}")


def _hashed(text: str, git_hash: str) -> str:
    return _with(text, "git_hash",
                 f'  constant C_GIT_HASH        : std_logic_vector(C_ROM_DATA_WIDTH-1 downto 0) := x"{git_hash}";')


@pytest.fixture
def build(tmp_path: Path, monkeypatch):
    """An isolated clone root (so per-job records land in tmp) and a shared build dir."""
    for var in _STAMP_ENV:
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "root"
    (root / "build").mkdir(parents=True)
    (root / "hw/rtl/pkg").mkdir(parents=True)
    build_dir = root / "build" / "de25_datalogger"
    build_dir.mkdir()
    monkeypatch.setattr(contract, "ROOT", root)
    monkeypatch.setenv("RR_BUILD_DIR", str(build_dir))
    return root, build_dir


def _install(root: Path, text: str, job: str, monkeypatch) -> bool:
    monkeypatch.setenv("RR_JOB_ID", job)
    cand = root / f"cand_{job}.vhd"
    cand.write_text(text, encoding="utf-8")
    return contract.install_candidate(kind="rom-info", candidate=cand, output=root / REL)


def test_a_later_queued_stage_of_the_same_build_keeps_the_first_stage_bytes(tmp_path, build, monkeypatch):
    root, _ = build
    base = _rom_info(tmp_path)
    synth = _dated(base, DATE_A, "Fri Nov 13 12:22:05 2026")
    bitstream = _dated(base, DATE_B, "Fri Nov 13 12:25:58 2026")
    assert _install(root, synth, "8332aee91ed6", monkeypatch) is True
    first = (root / REL).read_bytes()
    assert _install(root, bitstream, "cbc441d8c842", monkeypatch) is False
    out = (root / REL).read_bytes()
    assert out == first
    assert f'x"{DATE_A}"'.encode() in out and f'x"{DATE_B}"'.encode() not in out


def test_each_job_leaves_a_record_of_the_rom_info_it_ran_with(tmp_path, build, monkeypatch):
    """DE-G0 point 13 reads <clone>/build/.rom_info_stage_stamp_<J>; both jobs keep one, and both hold the
    bytes the build synthesized."""
    root, _ = build
    base = _rom_info(tmp_path)
    _install(root, _dated(base, DATE_A, "Fri Nov 13 12:22:05 2026"), "8332aee91ed6", monkeypatch)
    _install(root, _dated(base, DATE_B, "Fri Nov 13 12:25:58 2026"), "cbc441d8c842", monkeypatch)
    for job in ("8332aee91ed6", "cbc441d8c842"):
        rec = (root / "build" / f".rom_info_stage_stamp_{job}").read_bytes()
        assert f'x"{DATE_A}"'.encode() in rec, job


def test_a_new_head_gets_its_own_date(tmp_path, build, monkeypatch):
    root, _ = build
    base = _rom_info(tmp_path)
    _install(root, _hashed(_dated(base, DATE_A, "Fri Nov 13 12:22:05 2026"), "56db13c3"), "job1", monkeypatch)
    assert _install(root, _hashed(_dated(base, DATE_B, "Fri Nov 13 12:25:58 2026"), "3ab6df90"),
                    "job2", monkeypatch) is True
    out = (root / REL).read_bytes()
    assert f'x"{DATE_B}"'.encode() in out and b'x"3ab6df90"' in out


def test_a_cleaned_build_dir_gets_a_new_date(tmp_path, build, monkeypatch):
    root, build_dir = build
    base = _rom_info(tmp_path)
    _install(root, _dated(base, DATE_A, "Fri Nov 13 12:22:05 2026"), "job1", monkeypatch)
    for p in build_dir.iterdir():
        p.unlink()
    assert _install(root, _dated(base, DATE_B, "Fri Nov 13 12:25:58 2026"), "job2", monkeypatch) is True
    assert f'x"{DATE_B}"'.encode() in (root / REL).read_bytes()


def test_the_hook_run_twice_under_two_job_ids_writes_identical_bytes(tmp_path):
    """The production surface: regen_regbank.sh at two different seconds, two RR_JOB_ID values, one build dir."""
    out = tmp_path / "pkg"
    build_dir = tmp_path / "build" / "de25_datalogger"
    build_dir.mkdir(parents=True)
    cmd = ["bash", "tools/regen_regbank.sh", "--rom-info-only", "--output-dir", str(out),
           "--regbank-dir", "hw/rtl/regbank", "--library", "tich"]
    jobs = ("aaaaaaaaaaaa", "bbbbbbbbbbbb")
    records = [ROOT / "build" / f".rom_info_stage_stamp_{job}" for job in jobs]
    runs = []
    try:
        for job in jobs:
            env = _clean_env(build_dir)
            env["RR_JOB_ID"] = job
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
            assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
            runs.append((out / "rom_info_pkg.vhd").read_bytes())
            time.sleep(1.2)      # C_BUILD_DATE has one-second resolution: the second run would stamp another value
        assert runs[0] == runs[1]
        for job, rec in zip(jobs, records):
            assert rec.read_bytes() == runs[0], job
    finally:
        for rec in records:      # the hook writes records into this checkout's build/; leave nothing behind
            rec.unlink(missing_ok=True)


def _hook_runs(tmp_path: Path, job_ids) -> list:
    """regen_regbank.sh --rom-info-only once per entry, a second apart, one build dir; None = RR_JOB_ID unset
    (the direct `rr synth run` / `rr bitstream run` path). Returns the bytes after each run."""
    out = tmp_path / "pkg"
    build_dir = tmp_path / "build" / "de25_datalogger"
    build_dir.mkdir(parents=True)
    cmd = ["bash", "tools/regen_regbank.sh", "--rom-info-only", "--output-dir", str(out),
           "--regbank-dir", "hw/rtl/regbank", "--library", "tich"]
    records = [ROOT / "build" / f".rom_info_stage_stamp_{j}" for j in job_ids if j]
    runs = []
    try:
        for job in job_ids:
            env = _clean_env(build_dir)
            if job:
                env["RR_JOB_ID"] = job
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
            assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
            runs.append((out / "rom_info_pkg.vhd").read_bytes())
            time.sleep(1.2)
    finally:
        for rec in records:
            rec.unlink(missing_ok=True)
    return runs


def test_the_direct_path_hook_run_twice_without_a_job_id_writes_identical_bytes(tmp_path):
    """The README path (no rr queue, RR_JOB_ID unset) at synth and at bitstream of one build."""
    first, second = _hook_runs(tmp_path, [None, None])
    assert first == second


def test_a_direct_run_after_a_queued_run_of_the_same_build_writes_identical_bytes(tmp_path):
    """A queued stage (RR_JOB_ID set) then a direct one (unset), same build dir: one stamp, one date."""
    first, second = _hook_runs(tmp_path, ["cccccccccccc", None])
    assert first == second
