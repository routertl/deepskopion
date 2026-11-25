# rr unknown-llm (AI-generated)
"""DS-P1.57 boot-time regression guard: S99deepskopion-stream must refuse to
start (loudly) when python3 cannot execute at all.

MEASURED on silicon (boot21): the DE25 overlay staged a dynamically linked
python3 with no ELF interpreter anywhere in the rootfs. Nothing in the OLD
init script noticed — `arm_datalogger_defaults` failure was only echoed, and the
daemon launch is backgrounded (`... &`; `echo $! > PID_FILE`), so `echo $!`
reports the subshell's pid regardless of whether the python3 INSIDE it ever
ran. The daemon had, on the evidence, NEVER run on DE25, and boot looked
clean. This is the "assert the EFFECT" gate the ticket demands: an actual
`python3 -c "print(1)"` run, checked BEFORE anything else, that fails the
boot log loud and refuses to background a dead process.

Runs the real init script (not a reimplementation) against a scratch tree via
DS_ROOT_PREFIX, the same hermetic-test override sw/linux/tests/
test_first_boot_access.py already uses for S49dropbear/deepskopion-authorize-
key. `python3` itself is swapped for a fake shim placed first on PATH so this
test needs neither a real board nor a real broken/working Python build.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


_LINUX_DIR = Path(__file__).resolve().parents[1]
_ROOT = _LINUX_DIR.parents[1]
_INIT_SCRIPT = _LINUX_DIR / "rootfs_overlay" / "etc" / "init.d" / "S99deepskopion-stream"


def _fake_python3(bin_dir: Path, *, exit_code: int) -> None:
    """A python3 that behaves like the measured silicon defect (exit_code=1,
    every invocation fails the same way a missing ELF interpreter would) or
    like a working runtime (exit_code=0)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "python3"
    script.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    script.chmod(0o755)


def _run(root: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    (root / "usr/local/share/deepskopion").mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "DS_ROOT_PREFIX": str(root),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    return subprocess.run(
        [str(_INIT_SCRIPT), "start"], env=env,
        capture_output=True, text=True, check=False,
    )


def test_broken_python3_refuses_to_start_and_says_so(tmp_path):
    """BASELINE for the DS-P1.57 boot guard, hermetic (no network, no real
    board). A python3 that cannot exec at all must make `start` fail loud
    and must not report a pid — the exact silent-success shape the ticket
    measured."""
    root = tmp_path / "rootfs"
    bin_dir = tmp_path / "fakebin"
    _fake_python3(bin_dir, exit_code=1)
    proc = _run(root, bin_dir)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "python3 self-test FAILED" in proc.stderr, proc.stderr
    assert "DS-P1.57" in proc.stderr
    assert not (root / "var/run/deepskopion-stream.pid").exists(), (
        "a broken python3 must never produce a pid file — that pid file "
        "is exactly what let boot17-20 look clean while the daemon had "
        "never run"
    )


def test_working_python3_passes_the_self_test_and_starts(tmp_path):
    """MUTATION check the other direction: once python3 genuinely works (the
    DS-P1.57 fix — a staged ELF interpreter + libc), the SAME gate goes
    quiet and the daemon actually launches, proving the guard is not simply
    "always refuse"."""
    root = tmp_path / "rootfs"
    bin_dir = tmp_path / "fakebin"
    _fake_python3(bin_dir, exit_code=0)
    proc = _run(root, bin_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "self-test FAILED" not in proc.stderr
    assert (root / "var/run/deepskopion-stream.pid").is_file()


def test_pythonhome_is_exported_before_the_cwd_change_ds_p2_102(tmp_path):
    """DS-P2.102 MEASURED on silicon, on top of the DS-P1.57 fix above: even
    with a genuinely working, dynamically-linked python3 (shared libs
    resolved), `python3 -c "print(1)"` still failed — but ONLY after this
    script's own `cd "${DAEMON_DIR}"`, not from the board's boot-time cwd.
    python-build-standalone's runtime prefix detection turned out to be
    cwd-sensitive on this toolchain build: it could not find its own stdlib
    once cwd was /usr/local/share/deepskopion, dying before importing
    `encodings`. `PYTHONHOME=/` (i.e. `${ROOT_PREFIX}/` under this script's
    own hermetic-test override) was confirmed on the board to make the
    interpreter stop autodetecting its prefix and just work.

    A python3 stub that reports the EFFECT (whether PYTHONHOME reached its
    environment) rather than re-deriving CPython's own path logic — this
    test cannot depend on a real python-build-standalone binary being
    available in the test environment, so it checks the one thing this
    script controls: that PYTHONHOME is exported, with the value this
    script's own ROOT_PREFIX convention implies, before `start()` ever
    execs python3.

    BASELINE (proves this is not vacuous): this test fails against the
    pre-fix script (verified by hand: reverting the `export PYTHONHOME=...`
    line makes the marker file empty, and this test's own assertion catch
    that — see the ticket's landing notes for the reverted-script run).

    MUTATION: restoring the export line (the shipped fix) makes the marker
    show the exact expected value, and the guard goes quiet.
    """
    root = tmp_path / "rootfs"
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "pythonhome-seen"
    script = bin_dir / "python3"
    script.write_text(
        f"#!/bin/sh\nprintf '%s' \"$PYTHONHOME\" > {marker}\nexit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    proc = _run(root, bin_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marker.is_file(), "python3 was never invoked — self-test did not run"
    assert marker.read_text() == f"{root}/", (
        f"PYTHONHOME was {marker.read_text()!r}, expected {root}/ — "
        "the DS-P2.102 cwd-sensitive prefix-detection defect regressed"
    )


def _pythonhome_seen(tmp_path, stdlib_rel):
    root = tmp_path / "rootfs"
    if stdlib_rel:
        (root / stdlib_rel / "encodings").mkdir(parents=True)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "pythonhome-seen"
    script = bin_dir / "python3"
    script.write_text(
        f"#!/bin/sh\nprintf '%s' \"$PYTHONHOME\" > {marker}\nexit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    proc = _run(root, bin_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return root, marker.read_text()


def test_pythonhome_follows_a_usr_lib_stdlib_ds_p2_157(tmp_path):
    """DS-P2.157 MEASURED on the PolarFire kit (2026-09-23): its overlay is a
    distro python3.12 whose stdlib lives at /usr/lib/python3.12. The fixed
    PYTHONHOME=/ pointed getpath at /lib/python3.12, so the self-test died
    with "No module named 'encodings'" and the daemon refused to start —
    while `python3` ran fine from a shell. PYTHONHOME must follow where the
    stdlib actually is."""
    root, seen = _pythonhome_seen(tmp_path, "usr/lib/python3.12")
    assert seen == f"{root}/usr/", f"PYTHONHOME was {seen!r}, expected {root}/usr/"


def test_pythonhome_keeps_root_for_a_lib_stdlib_ds_p2_102(tmp_path):
    """The DE25 python-build-standalone layout (stdlib at /lib/python3.13)
    must keep PYTHONHOME=/ — the DS-P2.102 fix must not regress."""
    root, seen = _pythonhome_seen(tmp_path, "lib/python3.13")
    assert seen == f"{root}/", f"PYTHONHOME was {seen!r}, expected {root}/"
