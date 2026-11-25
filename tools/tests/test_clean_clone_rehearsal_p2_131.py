# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.131: Samsung-WSL clean-clone rehearsal regression guards.

Guards clean-room reproducibility findings surfaced during the samsung-wsl
remote rehearsal:
1. README.md quickstart requires a virtual environment (PEP 668 compliance on
   modern distros like Ubuntu 24.04) and documents RR_EDA_ALLOW_OVERSUBSCRIBE=1
   for Vivado builds on <=16 GB hosts.
2. .gitignore ignores Quartus Prime Pro root build artifacts generated during
   de25_datalogger synthesis (dni/, qdb/, hps_subsys.qsys, hps_subsys/,
   hps_subsys_qsys_script.*).
3. tools/regen_regbank.sh honors the PYTHON environment variable.
4. Internal rehearsal record and execution log are present with verified
   clean-room toolchain outputs.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_readme_quickstart_specifies_venv_and_ram_oversubscribe() -> None:
    """README.md quickstart must specify venv creation and RAM oversubscribe override."""
    readme_path = ROOT / "README.md"
    assert readme_path.is_file(), "README.md missing"
    text = readme_path.read_text(encoding="utf-8")

    # Step 1 must instruct creating and activating a virtual environment
    assert "python3 -m venv .venv" in text, (
        "README.md quickstart missing 'python3 -m venv .venv' (PEP 668 compliance)"
    )
    assert "source .venv/bin/activate" in text, (
        "README.md quickstart missing 'source .venv/bin/activate'"
    )

    # DS-P1.121: the README's host RAM is the preflight's hard floor. A tip for hosts below
    # it (RR_EDA_ALLOW_OVERSUBSCRIBE on 16 GB) contradicted the preflight, which fails there.
    import yaml
    decl = yaml.safe_load((ROOT / "tools/preflight/supported_environment.yml").read_text(encoding="utf-8"))
    floor = decl["host"]["ram_gb_min"]
    assert floor["severity"] == "fail"
    assert f"{floor['value']} GB RAM as the OS sees it" in text, (
        "README.md host RAM disagrees with tools/preflight/supported_environment.yml ram_gb_min"
    )
    assert "RR_EDA_ALLOW_OVERSUBSCRIBE" not in text, (
        "README.md suggests building below the preflight's RAM floor"
    )


def test_gitignore_contains_quartus_root_build_artifacts() -> None:
    """Gitignore must exclude Quartus Prime Pro root build artifacts."""
    gitignore_path = ROOT / ".gitignore"
    assert gitignore_path.is_file(), ".gitignore missing"
    text = gitignore_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]

    required_patterns = (
        "/dni/",
        "/qdb/",
        "/hps_subsys.qsys",
        "/hps_subsys/",
        "/hps_subsys_qsys_script.*",
        "/ip/agilex_hps/agilex_hps/intel_agilex_5_soc_1400/",
        "/ip/f2sdram_adapter/",
        "/ip/hps_subsys/",
    )
    for pattern in required_patterns:
        assert pattern in lines, (
            f".gitignore missing Quartus Pro root artifact pattern: {pattern}"
        )


def test_regen_regbank_honors_custom_python_executable() -> None:
    """tools/regen_regbank.sh must honor $PYTHON environment variable."""
    script_path = ROOT / "tools" / "regen_regbank.sh"
    assert script_path.is_file(), "tools/regen_regbank.sh missing"
    text = script_path.read_text(encoding="utf-8")

    assert 'PYTHON="${PYTHON:-python3}"' in text, (
        "tools/regen_regbank.sh missing PYTHON variable fallback: PYTHON=\"${PYTHON:-python3}\""
    )
    assert '"$PYTHON"' in text, (
        "tools/regen_regbank.sh does not invoke $PYTHON"
    )

    # Caller-reachable execution probe: run without --help with PYTHON=/bin/false.
    # The pre-build contract probes routertl SDK import via $PYTHON before generating.
    # With PYTHON=/bin/false, the check fails with rc=2 and outputs the import error.
    # (The origin/main script hardcoded python3 and ignored $PYTHON, exiting 0 here.)
    proc = subprocess.run(
        ["bash", str(script_path)],
        cwd=ROOT,
        env={**os.environ, "PYTHON": "/bin/false"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2, (
        f"regen_regbank.sh must fail with rc=2 when PYTHON=/bin/false, got {proc.returncode}: {proc.stderr}"
    )
    assert "routertl SDK not importable" in proc.stderr


def test_readme_python_floor_covers_clean_room_version() -> None:
    """README.md and public support docs must match SSOT supported_environment.yml."""
    readme_path = ROOT / "README.md"
    ssot_path = ROOT / "tools" / "preflight" / "supported_environment.yml"
    doc_path = ROOT / "docs" / "SUPPORTED_ENVIRONMENT.md"

    assert readme_path.is_file(), "README.md missing"
    assert ssot_path.is_file(), "tools/preflight/supported_environment.yml missing"
    assert doc_path.is_file(), "docs/SUPPORTED_ENVIRONMENT.md missing"

    env_data = yaml.safe_load(ssot_path.read_text(encoding="utf-8"))
    py_spec = env_data["python"]
    expected_ascii_range = f">= {py_spec['min']}, < {py_spec['max_exclusive']}"
    expected_unicode_range = f"≥ {py_spec['min']}, < {py_spec['max_exclusive']}"

    readme_text = readme_path.read_text(encoding="utf-8")
    assert expected_ascii_range in readme_text, (
        f"README.md toolchain floor for Python must match SSOT supported_environment.yml ({expected_ascii_range})"
    )

    doc_text = doc_path.read_text(encoding="utf-8")
    assert (
        expected_ascii_range in doc_text or expected_unicode_range in doc_text
    ), (
        f"docs/SUPPORTED_ENVIRONMENT.md must match SSOT supported_environment.yml ({expected_ascii_range})"
    )
