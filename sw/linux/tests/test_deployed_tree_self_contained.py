"""The staged board tree must be able to START the daemon (DS-X5, DS-P1.36).

WHAT ESCAPED. ``sw/linux/stage_daemon.sh`` staged ``sw/stream/`` and nothing
else. On 2026-08-12 ``sw/stream/wave_stream_guard.py`` (DS-P2.89) grew

    _CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "wave_stream_v1"
    sys.path.insert(0, str(_CONTRACT_DIR))
    import wave_stream_contract

— a path resolved inside the STAGED TREE, not the repo. Nothing staged
``contracts/``, so the first image built from a post-2026-08-12 tree shipped a
daemon that died on import:

    ModuleNotFoundError: No module named 'wave_stream_contract'

Measured on the Zybo Z7-20 during the DS-X5 silicon run: the board booted, the
PL carried the right bitstream, all nine PS-LOOP requirements passed — and
``/etc/init.d/S99deepskopion-stream`` could not start. Every existing guard was
green, because each one only compared ``sw/stream/`` against its own copy under
the overlay. "The files we chose to stage are in sync" cannot see a file we
never chose to stage.

WHAT THIS GUARD ASSERTS INSTEAD. Not a file list — the property that matters:
import the daemon's entry module with ONLY the staged tree on ``sys.path`` and
the staged tree as cwd, exactly as the board does (WorkingDirectory=
/usr/local/share/deepskopion, ``python3 -m sw.stream``). Any future module that
reaches outside the staged tree fails here instead of on silicon.

The staged tree is a gitignored build artefact (DS-P2.81), so this re-stages it
first — ``stage_daemon.sh`` is idempotent and is what ``rr linux rootfs`` runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# tests/ -> linux/ -> sw/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_STAGE_SCRIPT = _REPO_ROOT / "sw" / "linux" / "stage_daemon.sh"
_SHARE = (
    _REPO_ROOT
    / "sw" / "linux" / "rootfs_overlay"
    / "usr" / "local" / "share" / "deepskopion"
)

# What the board actually runs. `python3 -m sw.stream` pulls in server.py,
# which pulls in wave_stream_guard.py, which is where the escape happened.
# ORDER MATTERS: wave_stream_guard first. server.py imports msgpack at module
# level, and msgpack lives in the board's python overlay — on a dev box that
# import dies before ever reaching the guard, which would leave this test
# skipping past the exact defect it exists to catch.
_BOARD_ENTRY_MODULES = ("sw.stream.wave_stream_guard", "sw.stream.server")


def _stage() -> None:
    if not _STAGE_SCRIPT.is_file():
        pytest.skip(f"{_STAGE_SCRIPT} not present in this checkout")
    if shutil.which("rsync") is None:
        pytest.skip("rsync unavailable — stage_daemon.sh cannot run here")
    proc = subprocess.run(
        ["bash", str(_STAGE_SCRIPT)],
        cwd=_REPO_ROOT, capture_output=True, text=True, errors="replace",
    )
    assert proc.returncode == 0, (
        "stage_daemon.sh failed, so the board tree cannot be built at all:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


def test_staged_tree_imports_the_daemon_with_nothing_but_itself():
    """Import the board's entry modules from the staged tree ALONE.

    ``-E -s`` drops inherited PYTHONPATH and user site-packages but KEEPS the
    working directory on ``sys.path`` — which is the board's own arrangement
    (S99deepskopion-stream runs ``python3 -m sw.stream`` with cwd
    /usr/local/share/deepskopion). ``-I`` would be wrong here: isolated mode
    also removes cwd, so even a perfectly staged tree fails to import and the
    guard would report a defect that silicon does not have.
    """
    _stage()
    assert _SHARE.is_dir(), f"staging produced no {_SHARE}"

    src = "; ".join(f"import {m}" for m in _BOARD_ENTRY_MODULES)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    # DS-P2.262: -B and PYTHONDONTWRITEBYTECODE: this import runs in the REAL
    # staged overlay; without them it wrote host cpython-310 bytecode there,
    # which rr linux rootfs baked into a published image.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-B", "-E", "-s", "-c", src],
        cwd=_SHARE, env=env, capture_output=True, text=True, errors="replace",
    )
    # Only an unresolvable FIRST-PARTY import is this guard's business. The
    # board's third-party deps (msgpack, pyserial, smbus2, spidev) come from
    # the python overlay and are not expected on a dev box, so their absence
    # is an environment gap, not a staging defect.
    missing_first_party = [
        line for line in proc.stderr.splitlines()
        if "ModuleNotFoundError" in line
        and ("wave_stream_contract" in line or "'sw" in line)
    ]
    assert not missing_first_party, (
        "The STAGED board tree cannot import the daemon — the image would boot "
        "with a daemon that dies on startup, exactly as DS-X5 measured on the "
        "Z7-20. Stage whatever it reaches for in sw/linux/stage_daemon.sh (the "
        "ONE staging script, DS-P2.81):\n  " + "\n  ".join(missing_first_party)
    )
    if proc.returncode != 0:
        pytest.skip(
            "every first-party import resolved from the staged tree; the run "
            f"still needs an unavailable board dependency:\n{proc.stderr}"
        )


def test_wave_stream_contract_module_is_staged():
    """Point check for the specific escape, so the failure names itself.

    The property test above is the real guard; this one makes a regression
    report say 'wave_stream_contract is missing from the overlay' rather than
    'import failed'.
    """
    _stage()
    staged = _SHARE / "contracts" / "wave_stream_v1" / "wave_stream_contract.py"
    assert staged.is_file(), (
        f"{staged} is not staged, but sw/stream/wave_stream_guard.py imports it "
        "from inside the staged tree (parents[2]/contracts/wave_stream_v1). The "
        "deployed daemon would die with ModuleNotFoundError: No module named "
        "'wave_stream_contract'."
    )
