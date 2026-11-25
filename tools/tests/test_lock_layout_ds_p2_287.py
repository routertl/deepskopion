# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.287: every target has a tracked targets/<t>.routertl.lock, except the targets rr cannot lock with
their own HDL covered.

The exemptions live in tools/lock_exemptions.yml with what rr does (it refuses, or it writes a lock that hashes
no source) and the ticket whose fix removes them. The last test runs the real `rr lock` for each exempted
target: when rr writes a lock that hashes the target's top source, the test fails, so an exemption cannot
outlive the fix it waits for.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files, target_file  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
SUFFIX = ".routertl.lock"


def _exemptions() -> list[dict]:
    doc = yaml.safe_load((ROOT / "tools/lock_exemptions.yml").read_text(encoding="utf-8")) or {}
    return list(doc.get("exemptions") or [])


def _tracked(pattern: str) -> set[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", pattern],
                         capture_output=True, text=True, check=True).stdout
    return {p for p in out.split("\0") if p}


def test_every_target_has_a_tracked_lock_or_an_exemption():
    targets = {p.stem for p in all_target_files(ROOT)}
    locked = {Path(p).name[: -len(SUFFIX)] for p in _tracked(f"targets/*{SUFFIX}")}
    exempt = {e["target"] for e in _exemptions()}
    assert not (locked & exempt), f"exempted targets that have a tracked lock: {sorted(locked & exempt)}"
    assert targets - locked == exempt, (
        f"targets with no tracked lock and no exemption: {sorted(targets - locked - exempt)}; "
        f"exemptions for unknown targets: {sorted(exempt - targets)}")
    assert locked <= targets, f"locks with no target file: {sorted(locked - targets)}"
    assert not _tracked("routertl.lock"), "a root routertl.lock is tracked (pre-5.5.0 layout)"


def test_each_exemption_names_rrs_refusal_and_a_routertl_ticket():
    entries = _exemptions()
    assert [e["target"] for e in entries] == ["pfsoc_mss"]
    for e in entries:
        assert e.get("refusal", "").startswith("Refusing to write an incomplete lockfile"), e
        assert "declares no HDL sources" in e.get("hollow", ""), e
        assert re.fullmatch(r"RTL-P\d\.\d+", e.get("ticket", "")), e


def _top(target: str) -> str:
    cfg = yaml.safe_load(target_file(target, ROOT).read_text(encoding="utf-8")) or {}
    return (cfg.get("project") or {}).get("top_module") or ""


def test_an_exempted_target_still_has_no_lock_covering_its_top(tmp_path):
    rr = shutil.which("rr")
    assert rr, "rr is not on PATH: the exemption cannot be re-judged"
    for e in _exemptions():
        lock = ROOT / "targets" / f"{e['target']}{SUFFIX}"
        assert not lock.exists(), f"{lock} exists before the probe"
        top = _top(e["target"])
        assert top, f"targets/{e['target']}.yml declares no top_module"
        env = dict(os.environ, RR_TARGET=str(target_file(e["target"], ROOT).relative_to(ROOT)), TMPDIR=str(tmp_path))
        try:
            proc = subprocess.run([rr, "lock"], cwd=ROOT, capture_output=True, text=True, errors="replace",
                                  env=env, stdin=subprocess.DEVNULL, timeout=300)
            written = yaml.safe_load(lock.read_text(encoding="utf-8")) if lock.exists() else None
        finally:
            lock.unlink(missing_ok=True)       # a probe must leave no lock behind
        out = proc.stdout + proc.stderr
        if written is None:
            # An rr without the RTL-P2.2357 fix refuses.
            assert e["refusal"].split(":")[0] in out, out[-1500:]
            continue
        hashed = [s.get("path", "") for s in written.get("sources") or [] if isinstance(s, dict)]
        assert not [p for p in hashed if Path(p).stem == top], (
            f"rr now locks {e['target']} with its top {top} hashed: drop its exemption and track the lock")
        assert e["hollow"] in out, out[-1500:]
