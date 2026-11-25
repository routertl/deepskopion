# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.342: DE25 desk tests stay green whatever ``rr target set`` chose in the checkout.

Since DS-P2.336 every tool resolves the active target as rr does: ``$RR_TARGET``, then the untracked
``.routertl/local/active-target`` that ``rr target set`` writes, then project.yml. A desk test that runs a DE25
tool against the checkout without pinning RR_TARGET therefore went red once a developer had run
``rr target set zybo_datalogger`` (the README's step 3).

This test runs a sample of those tests against a shadow of the checkout whose own
``.routertl/local/active-target`` names the Zybo target, through the ``DE25_TEST_ROOT`` override
test_de25_linux_image.py already honours, with RR_TARGET removed from the environment.

BASELINE: red before the fix (each sample test fails with "active target 'zybo_datalogger' is not a DE25
target"). MUTATION: removing a sample test's RR_TARGET pin turns this test red again.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_FILE = "sw/linux/de25/test_de25_linux_image.py"
SAMPLE = (
    "test_lock_cli_rejects_any_uboot_env_on_card",
    "test_lock_cli_rejects_artifact_drift",
    "test_normalize_provenance_removes_wall_clock_and_worktree_identity",
)


def _shadow_with_zybo_choice(tmp_path: Path) -> Path:
    """Every top-level entry of the checkout, linked, except ``.routertl``, plus a Zybo local choice."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    for entry in ROOT.iterdir():
        if entry.name != ".routertl":
            (shadow / entry.name).symlink_to(entry)
    local = shadow / ".routertl" / "local"
    local.mkdir(parents=True)
    (local / "active-target").write_text("targets/zybo_datalogger.yml\n", encoding="utf-8")
    return shadow


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k != "RR_TARGET" and not k.startswith(("PYTEST_", "COV_CORE_"))}
    return env


def test_the_shadow_resolves_the_zybo_choice(tmp_path):
    """Positive control: without it, a sample that never saw the Zybo choice would pass vacuously."""
    shadow = _shadow_with_zybo_choice(tmp_path)
    done = subprocess.run(
        [sys.executable, str(ROOT / "tools/active_target.py"), "--source", "--root", str(shadow)],
        capture_output=True, text=True, env=_clean_env(), check=False)
    stem = subprocess.run(
        [sys.executable, str(ROOT / "tools/active_target.py"), "--root", str(shadow)],
        capture_output=True, text=True, env=_clean_env(), check=False)
    assert done.returncode == 0 and stem.returncode == 0, done.stderr + stem.stderr
    assert done.stdout.strip() == ".routertl/local/active-target", done.stdout
    assert stem.stdout.strip() == "zybo_datalogger", stem.stdout


def test_de25_desk_tests_pass_with_a_zybo_local_choice(tmp_path):
    shadow = _shadow_with_zybo_choice(tmp_path)
    env = _clean_env()
    env["DE25_TEST_ROOT"] = str(shadow)
    ids = [f"{ROOT / SAMPLE_FILE}::{name}" for name in SAMPLE]
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         "-o", "addopts=", *ids],
        cwd=ROOT, capture_output=True, text=True, env=env, check=False, timeout=900)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out[-4000:]
    assert f"{len(SAMPLE)} passed" in out, out[-4000:]
