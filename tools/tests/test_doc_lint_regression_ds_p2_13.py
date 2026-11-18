# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.13 — Wire `make doc-lint` into the pytest regression suite.

Runs tools/doc-lint/check_links.py as a subprocess from the repository root,
surfacing stdout and stderr on failure to ensure broken documentation links
are caught by `pytest tools/tests/`.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
DOC_LINT_SCRIPT = ROOT / "tools" / "doc-lint" / "check_links.py"


def test_doc_lint_checker_clean_tree() -> None:
    """Run tools/doc-lint/check_links.py from repo root and assert exit code 0."""
    assert DOC_LINT_SCRIPT.is_file(), f"Doc lint script not found at {DOC_LINT_SCRIPT}"

    result = subprocess.run(
        [sys.executable, str(DOC_LINT_SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        errors="replace",
    )

    assert result.returncode == 0, (
        f"tools/doc-lint/check_links.py failed with exit code {result.returncode}:\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
