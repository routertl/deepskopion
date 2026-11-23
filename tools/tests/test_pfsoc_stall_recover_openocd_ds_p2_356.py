# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.356: pfsoc_stall_recover refuses an openocd that cannot drive the PolarFire SoC kit.

On 2026-11-18 (DS-P2.353 inventory) MPFS_OPENOCD was unset, PATH resolved Quartus's openocd (no Microchip scripts),
and the tool reported "REFUSED: JTAG read failed (openocd rc=1; board off? FlashPro5 absent?)", exit 4, on a powered
kit: the recovery tool fails exactly when a stall needs it, and names the wrong cause.

Each case runs the tool with a stub openocd that records every call, a stub ping that never answers (so the old tool
goes on to JTAG), and TEST-NET addresses. Cases 1, 3 and 4 are red on the previous tool, which exits 4 after calling
openocd.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "pfsoc_stall_recover.py"
KIT_IP = "192.0.2.10"
CFGS = ("interface/microchip/embedded_flashpro5.cfg", "target/microchip/mpfs.cfg")


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _install(tmp: Path, cfgs: tuple[str, ...]) -> Path:
    """An openocd install: <tmp>/ocd/bin/openocd (logs argv) and <tmp>/ocd/share/openocd/scripts/<cfgs>."""
    prefix = tmp / "ocd"
    (prefix / "bin").mkdir(parents=True)
    _exe(prefix / "bin" / "openocd", f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{tmp}/openocd.log"\nexit 1\n')
    for c in cfgs:
        f = prefix / "share" / "openocd" / "scripts" / c
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# stub\n")
    return prefix / "bin" / "openocd"


def _run(tmp: Path, *args: str) -> subprocess.CompletedProcess[str]:
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    _exe(bindir / "ping", "#!/bin/sh\nexit 1\n")
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    for k in ("MPFS_OPENOCD", "MPFS_OPENOCD_SCRIPTS", "MPFS_CONSOLE"):
        env.pop(k, None)
    argv = [sys.executable, str(TOOL), "--ip", KIT_IP, "--console", "/dev/null", "--boot-timeout", "0.5",
            "--ping-timeout", "0.5", *args]
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)


def _calls(tmp: Path) -> list[str]:
    log = tmp / "openocd.log"
    return log.read_text().splitlines() if log.exists() else []


def test_openocd_without_the_microchip_scripts_is_refused_before_any_call(tmp_path):
    ocd = _install(tmp_path, ())
    r = _run(tmp_path, "--openocd", str(ocd))
    assert r.returncode == 6, r.stdout + r.stderr
    assert "MPFS_OPENOCD" in r.stdout and "target/microchip/mpfs.cfg" in r.stdout, r.stdout
    assert "board off" not in r.stdout, r.stdout
    assert _calls(tmp_path) == [], "openocd must not be run when it cannot drive the kit"


def test_one_missing_config_is_enough_to_refuse(tmp_path):
    ocd = _install(tmp_path, (CFGS[0],))
    r = _run(tmp_path, "--openocd", str(ocd))
    assert r.returncode == 6 and "target/microchip/mpfs.cfg" in r.stdout, r.stdout
    assert "embedded_flashpro5.cfg" not in r.stdout.split("has no", 1)[1], r.stdout
    assert _calls(tmp_path) == []


def test_scripts_dir_given_explicitly_is_the_one_checked(tmp_path):
    ocd = _install(tmp_path, CFGS)
    empty = tmp_path / "empty_scripts"
    empty.mkdir()
    r = _run(tmp_path, "--openocd", str(ocd), "--scripts", str(empty))
    assert r.returncode == 6 and str(empty) in r.stdout, r.stdout
    assert _calls(tmp_path) == []


def test_dry_run_is_refused_the_same_way(tmp_path):
    ocd = _install(tmp_path, ())
    r = _run(tmp_path, "--openocd", str(ocd), "--dry-run")
    assert r.returncode == 6, r.stdout
    assert _calls(tmp_path) == []


def test_capable_openocd_proceeds_to_jtag_with_its_own_scripts_dir(tmp_path):
    """Positive control: both configs present, so the tool reaches JTAG, hands openocd the checked dir with -s,
    and the stub's failure is reported as the existing no-JTAG case (exit 4)."""
    ocd = _install(tmp_path, CFGS)
    r = _run(tmp_path, "--openocd", str(ocd))
    assert r.returncode == 4, r.stdout + r.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 1, calls
    assert f"-s {tmp_path / 'ocd' / 'share' / 'openocd' / 'scripts'}" in calls[0], calls
