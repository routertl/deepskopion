# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P3.177: the gate runs both DE25 Linux suites under an unprivileged PATH.

Eight tests across sw/linux/de25/test_de25_linux_image.py and
sw/linux/de25/test_de25_toolchain_stage.py went stale unseen (DS-T2.90) because
neither suite ran in the gate, and sbin on PATH hid missing sbin_tool()
resolution for sfdisk, mke2fs and debugfs.

These guards cut the DS-P3.177 leg out of hooks/pre-commit.local and execute it
with real pytest in a scratch repository:
- both files run and pass when green;
- a deliberately broken test in sw/linux/de25/test_de25_linux_image.py reds the
  gate;
- a deliberately broken test in sw/linux/de25/test_de25_toolchain_stage.py reds
  the gate;
- every sbin entry is stripped from PATH before pytest runs, so a bare sfdisk
  invocation fails with FileNotFoundError while sbin_tool("sfdisk") resolves;
- tools/ci/de25_toolchain_stage_leg.py also strips sbin from PATH when running
  the toolchain-stage suite in CI.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "pre-commit.local"
CI_LEG = ROOT / "tools" / "ci" / "de25_toolchain_stage_leg.py"
IMAGE_SUITE_REL = "sw/linux/de25/test_de25_linux_image.py"
STAGE_SUITE_REL = "sw/linux/de25/test_de25_toolchain_stage.py"


def _extract_ds_p3_177_leg() -> str:
    hook_text = HOOK.read_text(encoding="utf-8")
    match = re.search(
        r"^# ── DS-P3\.177 .*?(?=\n# ── |\Z)",
        hook_text,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, (
        "DS-P3.177 gate leg missing from hooks/pre-commit.local"
    )
    return match.group(0)


def _load_ci_leg():
    spec = importlib.util.spec_from_file_location("de25_toolchain_stage_leg", CI_LEG)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_suite(repo: Path, rel: str, body: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _run_hook_leg(
    repo: Path,
    *,
    extra_path: str = "",
) -> subprocess.CompletedProcess[str]:
    leg = _extract_ds_p3_177_leg()
    script = f'set -u\nDS_GUARD_PY="{sys.executable}"\n{leg}\necho LEG_PASSED\n'
    env = dict(os.environ)
    if extra_path:
        env["PATH"] = f"{extra_path}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", "-c", script],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_precommit_gate_runs_both_de25_suites_and_reds_on_broken_test(
    tmp_path: Path,
) -> None:
    """DS-P3.177: both sw/linux/de25 suites run in the gate, and a deliberately
    broken test in either file reds the gate."""
    leg = _extract_ds_p3_177_leg()
    assert IMAGE_SUITE_REL in leg and STAGE_SUITE_REL in leg, (
        "DS-P3.177 gate must run both test_de25_linux_image.py and "
        "test_de25_toolchain_stage.py"
    )

    repo = tmp_path / "repo"
    _write_suite(
        repo,
        IMAGE_SUITE_REL,
        "def test_image_contract_green():\n    assert 1 + 1 == 2\n",
    )
    _write_suite(
        repo,
        STAGE_SUITE_REL,
        "def test_toolchain_stage_green():\n    assert 2 + 2 == 4\n",
    )

    green = _run_hook_leg(repo)
    assert green.returncode == 0 and "LEG_PASSED" in green.stdout, (
        f"DS-P3.177 gate failed on green suites: rc={green.returncode}\n"
        f"{green.stdout}\n{green.stderr}"
    )
    assert "2 passed" in green.stdout, (
        f"DS-P3.177 gate did not execute both DE25 test files:\n{green.stdout}"
    )

    # Deliberately break sw/linux/de25/test_de25_linux_image.py -> gate reds.
    _write_suite(
        repo,
        IMAGE_SUITE_REL,
        "def test_image_contract_broken():\n"
        "    assert False, 'deliberate break in test_de25_linux_image.py'\n",
    )
    broken_image = _run_hook_leg(repo)
    assert (
        broken_image.returncode == 1
        and "LEG_PASSED" not in broken_image.stdout
        and "deliberate break in test_de25_linux_image.py" in broken_image.stdout
        and "DE25 image or toolchain-stage suite failed (DS-P3.177)"
        in broken_image.stdout
    ), (
        "DS-P3.177 gate did not red on broken test_de25_linux_image.py: "
        f"rc={broken_image.returncode}\n{broken_image.stdout}"
    )

    # Restore image suite; deliberately break test_de25_toolchain_stage.py -> gate reds.
    _write_suite(
        repo,
        IMAGE_SUITE_REL,
        "def test_image_contract_green():\n    assert 1 + 1 == 2\n",
    )
    _write_suite(
        repo,
        STAGE_SUITE_REL,
        "def test_toolchain_stage_broken():\n"
        "    assert False, 'deliberate break in test_de25_toolchain_stage.py'\n",
    )
    broken_stage = _run_hook_leg(repo)
    assert (
        broken_stage.returncode == 1
        and "LEG_PASSED" not in broken_stage.stdout
        and "deliberate break in test_de25_toolchain_stage.py" in broken_stage.stdout
        and "DE25 image or toolchain-stage suite failed (DS-P3.177)"
        in broken_stage.stdout
    ), (
        "DS-P3.177 gate did not red on broken test_de25_toolchain_stage.py: "
        f"rc={broken_stage.returncode}\n{broken_stage.stdout}"
    )

    # Missing file / collection error (exit != 0 and != 1) fails closed too.
    (repo / STAGE_SUITE_REL).unlink()
    missing = _run_hook_leg(repo)
    assert (
        missing.returncode == 1
        and "LEG_PASSED" not in missing.stdout
        and "GATE COULD NOT RUN: DE25 image and toolchain-stage suites"
        in missing.stdout
    ), (
        "DS-P3.177 gate did not fail closed on collection error: "
        f"rc={missing.returncode}\n{missing.stdout}"
    )


def test_precommit_gate_strips_sbin_from_path_and_catches_bare_sfdisk_regression(
    tmp_path: Path,
) -> None:
    """DS-P3.177 / DS-T2.90 Cause 1: the gate strips sbin directories from PATH
    so a bare sfdisk call (without sbin_tool) fails under the gate."""
    fake_sbin = tmp_path / "custom" / "sbin"
    fake_sbin.mkdir(parents=True)
    fake_tool = fake_sbin / "ds_p3_177_sbin_probe"
    fake_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_tool.chmod(0o755)
    injected_path = os.pathsep.join(
        ["/usr/local/sbin", "/usr/sbin", "/sbin", str(fake_sbin)]
    )

    repo = tmp_path / "repo"
    _write_suite(
        repo,
        IMAGE_SUITE_REL,
        "import os\n"
        "from pathlib import Path\n"
        "import shutil\n"
        "def test_unprivileged_path_enforced():\n"
        "    entries = [p for p in os.environ.get('PATH', '').split(os.pathsep) if p]\n"
        "    sbin_entries = [p for p in entries if 'sbin' in Path(p).parts]\n"
        "    assert sbin_entries == [], f'sbin leaked onto PATH: {sbin_entries}'\n"
        "    assert shutil.which('ds_p3_177_sbin_probe') is None\n",
    )
    _write_suite(
        repo,
        STAGE_SUITE_REL,
        "def test_stage_ok():\n    assert True\n",
    )

    clean = _run_hook_leg(repo, extra_path=injected_path)
    assert clean.returncode == 0 and "LEG_PASSED" in clean.stdout, (
        "DS-P3.177 gate did not strip sbin from PATH: "
        f"rc={clean.returncode}\n{clean.stdout}\n{clean.stderr}"
    )

    # Reproducing the DS-T2.90 Cause 1 defect (invoking a tool in sbin by bare
    # name without sbin_tool) must fail under the gate even when the caller's
    # ambient PATH contains sbin.
    _write_suite(
        repo,
        IMAGE_SUITE_REL,
        "import subprocess\n"
        "def test_bare_sbin_tool_regression():\n"
        "    subprocess.run(['ds_p3_177_sbin_probe'], check=True)\n",
    )
    regressed = _run_hook_leg(repo, extra_path=injected_path)
    assert (
        regressed.returncode == 1
        and "LEG_PASSED" not in regressed.stdout
        and "FileNotFoundError" in regressed.stdout
    ), (
        "DS-P3.177 gate allowed a bare sbin tool invocation to pass: "
        f"rc={regressed.returncode}\n{regressed.stdout}"
    )


def test_ci_toolchain_stage_leg_runs_suite_under_unprivileged_path(
    tmp_path: Path, monkeypatch
) -> None:
    """DS-P3.177: tools/ci/de25_toolchain_stage_leg.py strips sbin from PATH
    when executing sw/linux/de25/test_de25_toolchain_stage.py."""
    leg = _load_ci_leg()
    assert hasattr(leg, "unprivileged_path") and hasattr(leg, "unprivileged_env"), (
        "de25_toolchain_stage_leg must define unprivileged_path and unprivileged_env"
    )
    raw = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    stripped = leg.unprivileged_path(raw)
    assert stripped == "/usr/local/bin:/usr/bin:/bin", (
        f"de25_toolchain_stage_leg must strip sbin from PATH: got {stripped!r}"
    )

    captured: list[tuple[list[str], dict[str, str] | None]] = []

    class _Done:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured.append(([str(c) for c in cmd], kwargs.get("env")))
        return _Done()

    monkeypatch.setenv("PATH", raw)
    monkeypatch.setattr(leg.subprocess, "run", fake_run)
    rc = leg.run_suite(tmp_path / "junit.xml")
    assert rc == 0 and len(captured) == 1
    _cmd, env = captured[0]
    assert env is not None and env.get("PATH") == "/usr/local/bin:/usr/bin:/bin", (
        f"de25_toolchain_stage_leg must strip sbin from PATH in run_suite: "
        f"got env PATH={None if env is None else env.get('PATH')!r}"
    )


def test_desk_guard_leg_includes_ds_p3_177_gate_test() -> None:
    """hooks/pre-commit.local's fast DS-P2.197 desk-guard leg must run this file."""
    hook_text = HOOK.read_text(encoding="utf-8")
    match = re.search(
        r"^# ── DS-P2\.197 — the public CI workflow.*?^echo \"✓ Public CI",
        hook_text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "hooks/pre-commit.local has no DS-P2.197 public CI leg"
    suite_rel = "tools/tests/test_de25_gate_ds_p3_177.py"
    assert suite_rel in match.group(0), (
        f"hooks/pre-commit.local DS-P2.197 leg omits {suite_rel}"
    )
