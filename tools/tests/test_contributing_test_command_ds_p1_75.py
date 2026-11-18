"""DS-P1.75 — the CONTRIBUTING test command must stage the locked packages first.

libs/open-logic/ and libs/enclustra/ are git-ignored and only exist after
`rr workspace sync`. Measured 2026-10-26 at a7f22ac3: pytest on a fresh
worktree without that step fails on missing sources, not on the change under
review. The documented command is the one contributors run, so it must sync
before pytest.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, re.DOTALL)


def test_contributing_syncs_the_workspace_before_the_python_suite() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    blocks = [b for b in _bash_blocks(text) if "pytest tools/tests/" in b]
    assert blocks, "CONTRIBUTING.md has no bash block running pytest tools/tests/"
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        pytest_at = next(i for i, line in enumerate(lines) if "pytest tools/tests/" in line)
        assert "rr workspace sync" in lines[:pytest_at], (
            "CONTRIBUTING.md runs pytest tools/tests/ without `rr workspace sync` "
            f"before it: {lines}")


def test_locked_package_dirs_are_not_tracked() -> None:
    """The premise of the sync step: these dirs come from the lock, not from git."""
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^/?libs/open-logic/?", ignored, re.M), ignored
