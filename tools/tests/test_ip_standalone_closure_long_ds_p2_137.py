"""DS-P2.137 — the long ``rr sim run --ip`` checks: UART smoke and negative controls.

Each of these runs longer than the test-power runner's 120 s per pytest run,
so they are not receipt parts. See ip_standalone_closure.py for the split.

* The UART smoke test exactly as the ticket audits it: no ``-g``, the manifest
  supplies the closure and G_UART_DEV_ID=23. Its calibration traffic is
  150 bytes at 115200 baud (about 13.5 ms of simulation).
* Negative controls copy the config_control closure into a throwaway project,
  break it in one place each (dependency removed, package file deleted,
  package dropped from the package manifest), and require rr to fail before
  writing any result. An unbroken copy must pass, which proves each failure
  comes from the mutation and not from the copy.

No skip paths: a gate that cannot run the simulator fails loudly.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ip_standalone_closure import (
    CONFIG_CONTROL_IP,
    ROOT,
    UART_IP,
    green_failure,
    rr,
    run,
    standalone_sim,
    sync_workspace,
    tail,
)

# The config_control closure, copied verbatim into the negative-control project.
CONFIG_CONTROL_CLOSURE = (
    "hw/rtl/pkg/datalogger_config_pkg.vhd",
    "hw/rtl/pkg/datalogger_pkg.vhd",
    "hw/rtl/pkg_ip/datalogger/ip.yml",
    f"{CONFIG_CONTROL_IP}/ip.yml",
    f"{CONFIG_CONTROL_IP}/config_control.vhd",
    f"{CONFIG_CONTROL_IP}/requirements.yml",
    "hw/sim/cocotb/tests/datalogger/infra/config_control/test_config_control.py",
)


def test_uart_datalogger_smoke_exact_ip_flow_runs_green() -> None:
    sync_workspace()
    proc, verdict = standalone_sim(UART_IP, "test_uart_datalogger_smoke", timeout_s=900)
    failure = green_failure("uart_datalogger smoke (exact --ip flow)", proc, verdict, expected=1)
    assert not failure, failure


# ── Negative controls: a broken closure must fail before any PASS ──────────


def _copy_config_control_closure(dest: Path) -> Path:
    for rel in CONFIG_CONTROL_CLOSURE:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    (dest / "project.yml").write_text(
        "project: {name: ds_p2_137_closure_copy, top_module: config_control}\n",
        encoding="utf-8")
    return dest


def _drop_dependencies(project: Path) -> None:
    manifest = project / CONFIG_CONTROL_IP / "ip.yml"
    text = manifest.read_text(encoding="utf-8")
    stripped = re.sub(r"\ndependencies:\n(?:  .*\n)+", "\n", text)
    assert stripped != text, "config_control ip.yml has no dependencies block to drop"
    manifest.write_text(stripped, encoding="utf-8")


def _delete_config_pkg_file(project: Path) -> None:
    (project / "hw/rtl/pkg/datalogger_config_pkg.vhd").unlink()


def _undeclare_config_pkg(project: Path) -> None:
    manifest = project / "hw/rtl/pkg_ip/datalogger/ip.yml"
    text = manifest.read_text(encoding="utf-8")
    stripped = re.sub(r"\n *- [^\n]*datalogger_config_pkg\.vhd(?=\n)", "", text)
    assert stripped != text, "package manifest does not list datalogger_config_pkg.vhd"
    manifest.write_text(stripped, encoding="utf-8")


def _copy_sim(project: Path) -> tuple[subprocess.CompletedProcess, Path]:
    proc = run(
        [rr(), "sim", "run", "test_config_control", "--ip", CONFIG_CONTROL_IP,
         "--timeout", "120"],
        project, timeout=900)
    return proc, project / "sim" / "results" / "test_config_control" / "latest.json"


def test_unbroken_closure_copy_passes(tmp_path: Path) -> None:
    proc, latest = _copy_sim(_copy_config_control_closure(tmp_path))
    verdict = json.loads(latest.read_text()) if latest.is_file() else {}
    failure = green_failure("config_control (fresh copy)", proc, verdict, expected=3)
    assert not failure, failure


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (_drop_dependencies, "library TICH not found"),
        (_delete_config_pkg_file, "Missing from disk"),
        (_undeclare_config_pkg, "DATALOGGER_CONFIG_PKG not found in library TICH"),
    ],
    ids=["dependency-removed", "package-file-deleted", "package-undeclared"],
)
def test_broken_closure_fails_before_any_pass(tmp_path: Path, mutate, expected_error: str) -> None:
    project = _copy_config_control_closure(tmp_path)
    mutate(project)
    proc, latest = _copy_sim(project)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, "broken closure exited 0:\n" + tail(proc)
    assert not latest.exists(), (
        f"broken closure wrote a result file {latest}: {latest.read_text()[:800]}")
    assert expected_error in output, (
        f"broken closure failed for another reason (wanted {expected_error!r}):\n"
        + tail(proc))
