# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.238: a later queued stage of the same build leaves rom_info_pkg.vhd's bytes AND mtime as the first stage wrote them.

Quartus and Vivado judge a synthesis checkpoint stale against the mtime of its sources, so keeping the bytes is not
enough: a rewrite with identical bytes would still make the synthesis checkpoint look older than a source. The DS-P1.102
test holds the bytes; this one holds the mtime, and that a new HEAD still rewrites the file.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

import tools.generated_source_contract as contract

ROOT = Path(__file__).resolve().parents[2]
REL = "hw/rtl/pkg/rom_info_pkg.vhd"
_STAMP_ENV = ("STAGE_STAMP", "RR_STAGE_STAMP", "RR_JOB_ID", "BUILD_DIR", "RR_BUILD_DIR", "RR_TARGET")
OLD_NS = 1_700_000_000_000_000_000           # a fixed mtime well before any test run


def _base(tmp_path: Path) -> str:
    out = tmp_path / "gen"
    env = {k: v for k, v in os.environ.items() if k not in _STAMP_ENV}
    env["RR_BUILD_DIR"] = str(tmp_path / "gen_build")
    r = subprocess.run(["bash", "tools/regen_regbank.sh", "--rom-info-only", "--output-dir", str(out),
                        "--regbank-dir", "hw/rtl/regbank", "--library", "tich"],
                       cwd=ROOT, capture_output=True, text=True, env=env)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    return (out / "rom_info_pkg.vhd").read_text(encoding="utf-8")


def _set(text: str, field: str, line: str) -> str:
    rule = next(r for r in contract.load_manifest()["volatile_lines"][REL] if r["field"] == field)
    new, n = re.subn(rule["pattern"], line, text, count=1, flags=re.MULTILINE)
    assert n == 1, field
    return new


def _stage(text: str, date: str, created: str, git_hash: str | None = None) -> str:
    text = _set(text, "build_date",
                f'  constant C_BUILD_DATE      : std_logic_vector(C_ROM_DATA_WIDTH-1 downto 0) := x"{date}";')
    text = _set(text, "generated_at", f"-- File created @{created}")
    if git_hash:
        text = _set(text, "git_hash",
                    f'  constant C_GIT_HASH        : std_logic_vector(C_ROM_DATA_WIDTH-1 downto 0) := x"{git_hash}";')
    return text


@pytest.fixture
def build(tmp_path: Path, monkeypatch):
    for var in _STAMP_ENV:
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "root"
    (root / "build" / "zybo_datalogger").mkdir(parents=True)
    (root / "hw/rtl/pkg").mkdir(parents=True)
    monkeypatch.setattr(contract, "ROOT", root)
    monkeypatch.setenv("RR_BUILD_DIR", str(root / "build" / "zybo_datalogger"))
    return root


def _install(root: Path, text: str, job: str, monkeypatch) -> bool:
    monkeypatch.setenv("RR_JOB_ID", job)
    cand = root / f"cand_{job}.vhd"
    cand.write_text(text, encoding="utf-8")
    return contract.install_candidate(kind="rom-info", candidate=cand, output=root / REL)


def test_the_bitstream_stage_leaves_the_synth_stage_mtime(tmp_path, build, monkeypatch):
    base = _base(tmp_path)
    assert _install(build, _stage(base, "1A2B3C4D", "Thu Nov 19 05:35:45 2026"), "synth01", monkeypatch) is True
    out = build / REL
    os.utime(out, ns=(OLD_NS, OLD_NS))
    first = out.read_bytes()
    assert _install(build, _stage(base, "5E6F7081", "Thu Nov 19 06:04:44 2026"), "bitstr02", monkeypatch) is False
    assert out.read_bytes() == first
    assert out.stat().st_mtime_ns == OLD_NS


def test_a_new_head_between_stages_rewrites_the_file_and_its_mtime(tmp_path, build, monkeypatch):
    base = _base(tmp_path)
    _install(build, _stage(base, "1A2B3C4D", "Thu Nov 19 05:35:45 2026", "a6b59d44"), "synth01", monkeypatch)
    out = build / REL
    os.utime(out, ns=(OLD_NS, OLD_NS))
    assert _install(build, _stage(base, "5E6F7081", "Thu Nov 19 06:04:44 2026", "b23db8bf"),
                    "bitstr02", monkeypatch) is True
    assert b'x"b23db8bf"' in out.read_bytes()
    assert out.stat().st_mtime_ns != OLD_NS
