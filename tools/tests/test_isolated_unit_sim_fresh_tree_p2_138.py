# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.138: a tiny unit sim must not depend on unrelated root-project units.

`rr sim run test_config_control` on a fresh tree aborted in the ROOT-project
closure preflight. That preflight walks the whole active target: the
ungenerated REA design-identity package (generated/rr_rea_build_id_pkg.vhd is
gitignored and produced by a build hook) and the gitignored olo libs that only
`rr workspace sync` materialises. config_control's own closure is three tich
files over ieee and needs none of that.

The test declares its closure isolated (`include_project_packages=False`), the
supported rr signal that makes `rr sim run` skip the root closure preflight and
the package-generation gate. This guard runs the REAL CLI on a copy of the
tracked tree only (no generated/, no synced libs, no sim work libs), so the
unrelated artifacts are absent exactly as on a fresh clone, and asserts:

* the run exits 0 and reaches the behavioural assertions (3 PASS, 0 FAIL);
* the unrelated REA package was NOT generated as a side effect, so its absence
  or a failing REA generator cannot block this test.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REA_PKG = Path("generated") / "rr_rea_build_id_pkg.vhd"


def _copy_tracked_tree(dest: Path) -> None:
    """Copy git-tracked files only: what a fresh clone holds, nothing generated."""
    for tool, label in (("rr", "rr CLI"), ("nvc", "nvc simulator")):
        assert shutil.which(tool) is not None, (
            f"{label} not on PATH — fresh-tree config_control sim must fail, not skip"
        )
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True,
    ).stdout.decode("utf-8").split("\0")
    for rel in filter(None, listing):
        src = ROOT / rel
        if not src.exists() and not src.is_symlink():
            continue
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)


def test_config_control_sims_on_fresh_tree_without_rea_artifacts(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh"
    _copy_tracked_tree(fresh)
    assert not (fresh / REA_PKG).exists(), "fixture is not fresh: REA package tracked"

    env = dict(os.environ)
    # Never the opt-out the ticket forbids; never the bench queue.
    for key in ("RR_SKIP_CLOSURE_PREFLIGHT", "RR_TARGET", "RR_QUEUE_SIMS"):
        env.pop(key, None)
    env["RR_QUEUE_SIMS"] = "0"

    proc = subprocess.run(
        ["rr", "sim", "run", "test_config_control", "--target", "zybo_datalogger"],
        cwd=fresh, env=env, capture_output=True, text=True, timeout=900,
    )
    out = proc.stdout + proc.stderr
    tail = "\n".join(out.splitlines()[-40:])

    assert proc.returncode == 0, (
        f"config_control fresh-tree sim exited {proc.returncode}; unrelated "
        f"root-project units blocked an isolated unit test:\n{tail}"
    )
    passed = sum(int(n) for n in re.findall(r"^\*\* TESTS=\d+ PASS=(\d+)", out, re.M))
    failed = sum(int(n) for n in re.findall(r"^\*\* TESTS=\d+ PASS=\d+ FAIL=(\d+)", out, re.M))
    assert (passed, failed) == (3, 0), (
        f"config_control did not reach its 3 behavioural tests (PASS={passed}, "
        f"FAIL={failed}):\n{tail}"
    )
    assert not (fresh / REA_PKG).exists(), (
        "config_control sim generated the unrelated REA build-id package: it "
        "still depends on the REA generator succeeding"
    )
