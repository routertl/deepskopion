# rr unknown-llm (AI-generated)
"""DS-C1 first boot creates access state; the repository does not ship it."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[3]
OVERLAY = ROOT / "sw" / "linux" / "rootfs_overlay"
DROPBEAR_INIT = OVERLAY / "etc" / "init.d" / "S49dropbear"
AUTHORIZE = OVERLAY / "usr" / "local" / "bin" / "deepskopion-authorize-key"


def _root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "rootfs"
    dropbear = root / "usr" / "sbin" / "dropbear"
    invoked = root / "dropbear.args"
    dropbear.parent.mkdir(parents=True)
    dropbear.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" > {invoked}\n", encoding="utf-8")
    dropbear.chmod(0o755)
    (root / "root").mkdir()
    return root, invoked


def _env(root: Path) -> dict[str, str]:
    return {**os.environ, "DS_ROOT_PREFIX": str(root)}


def _fixture_key() -> str:
    return ("ssh-" + "ed25519 "
            "AAAAC3NzaC1lZDI1NTE5AAAAIFixtureOnlyNotARealKey1234 owner@example\n")


def test_first_boot_creates_empty_state_and_keeps_ssh_closed(tmp_path):
    root, invoked = _root(tmp_path)
    proc = subprocess.run(
        [str(DROPBEAR_INIT), "start"], env=_env(root),
        capture_output=True, text=True, check=False,
    )
    keys = root / "root" / ".ssh" / "authorized_keys"
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert keys.is_file() and keys.read_bytes() == b""
    assert not invoked.exists(), "dropbear started before an owner key existed"
    assert "SSH remains disabled" in proc.stdout


def test_first_boot_accepts_a_user_supplied_key_and_disables_passwords(tmp_path):
    root, invoked = _root(tmp_path)
    supplied = root / "etc" / "deepskopion" / "authorized_keys"
    supplied.parent.mkdir(parents=True)
    supplied.write_text(_fixture_key(), encoding="utf-8")

    proc = subprocess.run(
        [str(DROPBEAR_INIT), "start"], env=_env(root),
        capture_output=True, text=True, check=False,
    )
    installed = root / "root" / ".ssh" / "authorized_keys"
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert installed.read_text(encoding="utf-8") == _fixture_key()
    assert invoked.is_file()
    args = invoked.read_text(encoding="utf-8")
    assert "-R" in args and "-s" in args and "-p 22" in args


def test_console_provisioner_rejects_non_key_content(tmp_path):
    root, _ = _root(tmp_path)
    bad = tmp_path / "not-a-key"
    bad.write_text("password=hunter-two\n", encoding="utf-8")
    proc = subprocess.run(
        [str(AUTHORIZE), str(bad)], env=_env(root),
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 1
    assert not (root / "root" / ".ssh" / "authorized_keys").exists()


def test_console_provisioner_installs_an_owner_key(tmp_path):
    root, _ = _root(tmp_path)
    source = tmp_path / "owner.pub"
    source.write_text(_fixture_key(), encoding="utf-8")
    proc = subprocess.run(
        [str(AUTHORIZE), str(source)], env=_env(root),
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    installed = root / "root" / ".ssh" / "authorized_keys"
    assert installed.read_text(encoding="utf-8") == _fixture_key()
    assert installed.stat().st_mode & 0o777 == 0o600
