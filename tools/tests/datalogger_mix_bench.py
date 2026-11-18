# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.65 — shared runner for the datalogger-mix `rr sim sweep` benches.

Used by test_datalogger_mix_contract.py (the ticket matrix) and
test_datalogger_mix_mechanisms.py (one isolated bench per mechanism). Every
case runs `rr sim sweep <profile>` over tools/tests/datalogger_mix/
sim_profiles.yml with the cocotb module test_datalogger_mix_sweep.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIX_DIR = ROOT / "tools" / "tests" / "datalogger_mix"
SWEEP_MODULE = (ROOT / "hw" / "sim" / "cocotb" / "tests" / "datalogger" / "infra"
                / "datalogger_gather" / "test_datalogger_mix_sweep.py")

# Lines that say WHY a bench failed: nvc analysis / elaboration diagnostics,
# and the cocotb assertion that fired.
_HDL_ERROR = re.compile(r"\*\* (?:Error|Fatal)\b.*")
_COCOTB_ASSERT = re.compile(r"AssertionError: .*|Profile '[^']*': .*")


def workspace_synced():
    """Generator for a module fixture: stage locked packages, restore lock files.

    pytest-xdist runs this once per worker module, all in one worktree. The
    check and the `rr workspace sync` run under one exclusive file lock, so a
    second worker waits for the first sync to finish instead of syncing the
    same libs/ at the same time (a half-staged libs/ also passed the check).
    Only the worker that ran the sync restores the lock files it captured."""
    import fcntl

    rr = shutil.which("rr")
    work_lock = ROOT / "hw" / "sim" / "work.lock"
    lock_file = ROOT / "ip.lock"
    guard = ROOT / ".routertl_cache" / "ds_test_workspace_sync.lock"
    guard.parent.mkdir(parents=True, exist_ok=True)
    synced = False
    orig_work_lock = orig_lock = None

    with open(guard, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        # Only sync if locked libraries have not been staged yet
        needs_sync = not (ROOT / "libs" / "open-logic" / "olo_base_fifo_sync").is_dir()
        if rr and needs_sync:
            orig_work_lock = work_lock.read_bytes() if work_lock.exists() else None
            orig_lock = lock_file.read_bytes() if lock_file.exists() else None
            synced = True
            sync = subprocess.run(
                [rr, "workspace", "sync"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=600,
            )
            assert sync.returncode == 0, (
                f"rr workspace sync failed — cannot stage the sim's locked packages:\n"
                f"STDOUT:\n{sync.stdout}\n"
                f"STDERR:\n{sync.stderr}"
            )

    try:
        yield
    finally:
        if synced:
            if orig_work_lock is not None:
                work_lock.write_bytes(orig_work_lock)
            elif work_lock.exists():
                work_lock.unlink()
            if orig_lock is not None:
                lock_file.write_bytes(orig_lock)


def _unique(lines: list[str]) -> list[str]:
    return list(dict.fromkeys(line.strip() for line in lines))


def bench_failure(profile: str) -> str | None:
    """`rr sim sweep` one profile; None when it passes, else why it failed.

    The reason names the layer that broke: the nvc analysis / elaboration
    diagnostics, or the cocotb assertion that fired.
    """
    rr = shutil.which("rr")
    if not rr:
        return "rr CLI not found on PATH"

    cmd = [
        rr, "sim", "sweep",
        profile,
        "--profiles", str(MIX_DIR / "sim_profiles.yml"),
        "--tests", "test_datalogger_mix_sweep",
    ]
    env = {
        **os.environ,
        "RR_QUEUE_SIMS": "0",
        "RR_QUEUE_BUILDS": "0",
        "RR_SKIP_CLOSURE_PREFLIGHT": "1",
        "RR_SKIP_TOPLEVEL_CHECK": "1",
        "RR_SKIP_REGBANK_CHECK": "1",
    }
    # Every sweep regenerates and reads the same in-tree sources (the regbank
    # schema among them), so two sweeps in one worktree race; pytest-xdist
    # workers take turns under one exclusive file lock.
    import fcntl

    guard = ROOT / ".routertl_cache" / "ds_test_mix_bench.lock"
    guard.parent.mkdir(parents=True, exist_ok=True)
    with open(guard, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=300,
            env=env,
        )
    if proc.returncode == 0:
        return None
    out = f"{proc.stdout}\n{proc.stderr}"
    hdl_errors = _unique(_HDL_ERROR.findall(out))
    if hdl_errors:
        return (f"DS-P1.65 bench '{profile}' does not analyse/elaborate:\n"
                + "\n".join(hdl_errors[:20]))
    sim_failures = _unique(_COCOTB_ASSERT.findall(out))
    if sim_failures:
        return (f"DS-P1.65 bench '{profile}' simulation assertion failed:\n"
                + "\n".join(sim_failures[:20]))
    return (f"rr sim sweep failed for profile {profile} (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout[-6000:]}\n"
            f"STDERR:\n{proc.stderr[-6000:]}")
