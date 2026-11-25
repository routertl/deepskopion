# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P3.162 regression guard: init-script hints must resolve on init PATH.

MEASURED 2026-09-24 on the PolarFire kit (DS-P2.128, rr-built card):
S49dropbear prints:
    "dropbear: no owner key provisioned; SSH remains disabled"
    "          run deepskopion-authorize-key from the serial console"
and typing that at the root console gives:
    "/bin/sh: deepskopion-authorize-key: not found"
while /usr/local/bin/deepskopion-authorize-key works.

Kernel PID 1 starts with default PATH=/sbin:/bin:/usr/sbin:/usr/bin.
If /init does not export PATH, both the rcS scripts and the root console
shell (exec setsid cttyhack /bin/sh) inherit this default, which lacks
/usr/local/bin.

This guard verifies that every command named in an init-script operator
hint resolves on the init PATH within the rootfs, and that /usr/local/bin
is present on both the init environment and login-shell profile PATH.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import pytest

_LINUX_DIR = Path(__file__).resolve().parents[1]
_ROOT = _LINUX_DIR.parents[1]
_OVERLAY = _LINUX_DIR / "rootfs_overlay"
_INIT = _OVERLAY / "init"
_INIT_D = _OVERLAY / "etc" / "init.d"
_PROFILE = _OVERLAY / "etc" / "profile"

# Kernel default PATH for PID 1 when /init does not export one
_KERNEL_DEFAULT_PATH = ["/sbin", "/bin", "/usr/sbin", "/usr/bin"]


def get_init_path(init_script: Path = _INIT) -> list[str]:
    """Parse the PATH environment exported by /init, falling back to kernel default."""
    if not init_script.is_file():
        return list(_KERNEL_DEFAULT_PATH)
    text = init_script.read_text(encoding="utf-8")
    m = re.search(r'^\s*export\s+PATH=["\']?([^"\'\n]+)["\']?', text, re.MULTILINE)
    if m:
        return [p for p in m.group(1).split(":") if p]
    return list(_KERNEL_DEFAULT_PATH)


def extract_hint_commands(script_path: Path) -> list[str]:
    """Extract console operator command hints from an init script.

    Matches lines printing 'run <cmd> ...' intended for execution on the target
    serial console. Ignores host staging instructions (e.g. paths starting with
    'sw/').
    """
    text = script_path.read_text(encoding="utf-8")
    commands: list[str] = []
    # Match echo/printf lines telling the operator to run a command
    for line in text.splitlines():
        # Match 'echo "... run <cmd> ..."' or 'printf "... run <cmd> ..."'
        m = re.search(r'(?:echo|printf)\s+["\'].*?\brun\s+([a-zA-Z0-9_./-]+)', line)
        if not m:
            continue
        cmd = m.group(1)
        # Skip host-side repository commands (e.g. 'sw/linux/stage_...')
        if cmd.startswith("sw/"):
            continue
        commands.append(cmd)
    return commands


def resolve_command_on_path(cmd: str, path_dirs: list[str], rootfs: Path) -> Path | None:
    """Find cmd in path_dirs relative to rootfs.

    If cmd is an absolute path, checks rootfs / cmd directly.
    Otherwise checks each dir in path_dirs.
    """
    if cmd.startswith("/"):
        candidate = rootfs / cmd.lstrip("/")
        return candidate if candidate.is_file() else None

    for d in path_dirs:
        candidate = rootfs / d.lstrip("/") / cmd
        if candidate.is_file():
            return candidate
    return None


def test_hint_extractor_finds_dropbear_authorize_key_hint():
    """POWER CHECK: the extractor must find deepskopion-authorize-key in S49dropbear."""
    dropbear = _INIT_D / "S49dropbear"
    assert dropbear.is_file(), f"{dropbear} missing"
    hints = extract_hint_commands(dropbear)
    assert "deepskopion-authorize-key" in hints, (
        f"extract_hint_commands did not find 'deepskopion-authorize-key' in {dropbear}"
    )


def test_every_init_script_hint_command_resolves_on_init_path():
    """DS-P3.162: every command named in an init-script hint must resolve on init PATH."""
    init_path = get_init_path(_INIT)
    rootfs = _OVERLAY

    assert _INIT_D.is_dir(), f"{_INIT_D} missing"
    checked_any = False

    for script in sorted(_INIT_D.glob("S*")):
        if not script.is_file():
            continue
        hints = extract_hint_commands(script)
        rel = script.relative_to(_ROOT)
        for cmd in hints:
            checked_any = True
            resolved = resolve_command_on_path(cmd, init_path, rootfs)
            assert resolved is not None, (
                f"command {cmd!r} named in {rel} hint does not resolve on init PATH "
                f"({':'.join(init_path)})"
            )
            assert os.access(resolved, os.X_OK), (
                f"command {cmd!r} resolved to {resolved} but is not executable"
            )

    assert checked_any, "no init-script hint commands were checked"


def test_init_exports_usr_local_bin_on_path():
    """DS-P3.162: /init must explicitly export /usr/local/bin on PATH for the console shell."""
    init_path = get_init_path(_INIT)
    assert "/usr/local/bin" in init_path, (
        f"/init does not export /usr/local/bin on PATH (current: {':'.join(init_path)})"
    )


def test_etc_profile_exports_usr_local_bin_on_path():
    """DS-P3.162: /etc/profile must exist and export /usr/local/bin on PATH for login shells."""
    assert _PROFILE.is_file(), f"{_PROFILE} missing from rootfs overlay"
    text = _PROFILE.read_text(encoding="utf-8")
    m = re.search(r'^\s*export\s+PATH=["\']?([^"\'\n]+)["\']?', text, re.MULTILINE)
    assert m, f"{_PROFILE} does not contain 'export PATH=...'"
    profile_path = [p for p in m.group(1).split(":") if p]
    assert "/usr/local/bin" in profile_path, (
        f"/etc/profile does not include /usr/local/bin on PATH ({':'.join(profile_path)})"
    )
