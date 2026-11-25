# rr unknown-llm (AI-generated)
"""DS-X1 (DS-P1.27) — the frozen-lock refresh must not re-import the bench.

``linux.lock.frozen`` records a project-root-relative
``build.kernel_initramfs_source`` while the live lock copies the kernel
``.config``'s absolute form. Without a neutralizing refresh, the only
documented way to update the frozen lock (``cp out/linux.lock``) produces a
commit the DS-X1 pre-commit guard refuses — a provenance record with no legal
way to move it forward.

These tests pin the three behaviours that claim solves it: the refresh
neutralizes, ``--check`` is blind to the machine prefix but not to real
drift, and a $HOME path the root-strip cannot reach is refused rather than
silently written into a tracked file.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TOOL = _REPO_ROOT / "tools" / "freeze_linux_lock.py"

_spec = importlib.util.spec_from_file_location("freeze_linux_lock", _TOOL)
freeze_linux_lock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(freeze_linux_lock)


def _live_lock(root: pathlib.Path) -> pathlib.Path:
    """A minimal live lock whose initramfs source is absolute, as built."""
    path = root / "linux.lock"
    path.write_text(
        "version: 4\n"
        "generated_at: '2026-08-12T00:00:00+00:00'\n"
        "build:\n"
        f"  kernel_initramfs_source: {root}/sw/linux/out/rootfs\n"
        "  kernel_defconfig: xilinx_zynq_defconfig\n",
        encoding="utf-8",
    )
    return path


def test_refresh_strips_the_project_root_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(freeze_linux_lock, "_REPO_ROOT", tmp_path)
    live = _live_lock(tmp_path)
    frozen = tmp_path / "linux.lock.frozen"

    assert freeze_linux_lock.main(
        ["--lock", str(live), "--frozen", str(frozen)]) == 0

    body = frozen.read_text(encoding="utf-8")
    assert str(tmp_path) not in body, "the frozen lock still names the bench"
    assert (yaml.safe_load(body)["build"]["kernel_initramfs_source"]
            == "sw/linux/out/rootfs")


def test_check_ignores_the_machine_prefix_but_not_real_drift(
        tmp_path, monkeypatch):
    monkeypatch.setattr(freeze_linux_lock, "_REPO_ROOT", tmp_path)
    live = _live_lock(tmp_path)
    frozen = tmp_path / "linux.lock.frozen"
    freeze_linux_lock.main(["--lock", str(live), "--frozen", str(frozen)])

    # Same build, re-emitted: new timestamp, same absolute prefix → clean.
    live.write_text(
        live.read_text(encoding="utf-8").replace(
            "2026-08-12T00:00:00", "2026-08-13T09:30:00"),
        encoding="utf-8",
    )
    assert freeze_linux_lock.main(
        ["--lock", str(live), "--frozen", str(frozen), "--check"]) == 0

    # A genuine input change must still be caught.
    live.write_text(
        live.read_text(encoding="utf-8").replace(
            "xilinx_zynq_defconfig", "some_other_defconfig"),
        encoding="utf-8",
    )
    assert freeze_linux_lock.main(
        ["--lock", str(live), "--frozen", str(frozen), "--check"]) == 1


def test_refuses_a_home_path_outside_the_project_root(tmp_path, monkeypatch):
    monkeypatch.setattr(freeze_linux_lock, "_REPO_ROOT", tmp_path)
    live = tmp_path / "linux.lock"
    live.write_text(
        "version: 4\n"
        "build:\n"
        f"  kernel_initramfs_source: {pathlib.Path.home()}/elsewhere/rootfs\n",
        encoding="utf-8",
    )
    frozen = tmp_path / "linux.lock.frozen"

    assert freeze_linux_lock.main(
        ["--lock", str(live), "--frozen", str(frozen)]) == 2
    assert not frozen.exists(), "wrote a frozen lock naming the bench"


def test_missing_live_lock_is_not_reported_as_a_pass(tmp_path):
    assert freeze_linux_lock.main(
        ["--lock", str(tmp_path / "absent.lock"),
         "--frozen", str(tmp_path / "linux.lock.frozen"), "--check"]) == 2


def test_committed_frozen_lock_is_neutral():
    """The tracked artefact itself, not just the tool that writes it."""
    body = (_REPO_ROOT / "linux.lock.frozen").read_text(encoding="utf-8")
    assert freeze_linux_lock._residual_home(body) is None
    assert yaml.safe_load(body)["build"]["kernel_initramfs_source"] == (
        "sw/linux/out/rootfs sw/linux/out/initramfs_devices.list"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


@pytest.mark.parametrize("text,home,hit", [
    ("kernel_initramfs_source: sw/linux/out/rootfs", "/root", None),
    ("path: /root/deepskopion/sw/linux/out/uImage", "/root", "/root"),
    ("toolchain: /root", "/root", "/root"),
    ("x: /opt/ci/work/deepskopion/a.dtb", "/opt/ci", "/opt/ci"),
    ("x: /opt/ciX/a.dtb", "/opt/ci", None),
    ("x: /opt/ci.old/a.dtb", "/opt/ci", None),
])
def test_home_counts_only_as_a_whole_path(text, home, hit):
    """HOME=/root (a container run as root) must not match ``.../rootfs``."""
    assert freeze_linux_lock._residual_home(text, home) == hit
