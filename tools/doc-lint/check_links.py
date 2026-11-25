#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Scan markdown files for broken relative links.

Walks docs/, hw/rtl/datalogger/, and the root README.md, extracts every
markdown link [text](path), resolves it relative to the source file,
and reports any targets that do not exist on disk.

Exit codes:
    0  – all links valid
    1  – one or more broken links found
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Directories to scan (relative to repo root).
SCAN_DIRS = ["docs", os.path.join("hw", "rtl", "datalogger")]

# Individual files to scan (relative to repo root).
SCAN_FILES = ["README.md"]

# Directories whose *targets* are ignored (resolved paths under these).
IGNORE_TARGET_DIRS = ["vendor"]

# Regex: standard markdown links  [text](target)
# Captures the target portion only.
LINK_RE = re.compile(r"\[(?:[^\[\]]|\[[^\]]*\])*\]\(([^)]+)\)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Return the repository root (parent of tools/)."""
    return Path(__file__).resolve().parent.parent.parent


def _should_skip_target(target: str) -> bool:
    """Return True if *target* should not be checked."""
    # HTTP(S) links
    if target.startswith(("http://", "https://", "mailto:")):
        return True
    # Anchor-only links
    if target.startswith("#"):
        return True
    return False


def _is_under_ignored_dir(resolved: Path, root: Path) -> bool:
    """Return True if *resolved* falls under an ignored directory."""
    try:
        rel = resolved.resolve().relative_to(root)
    except ValueError:
        return False
    for ignored in IGNORE_TARGET_DIRS:
        if str(rel).startswith(ignored + os.sep) or str(rel) == ignored:
            return True
    return False


def _strip_anchor(target: str) -> str:
    """Remove an optional #anchor suffix from *target*."""
    idx = target.find("#")
    if idx != -1:
        return target[:idx]
    return target


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def collect_md_files(root: Path) -> list[Path]:
    """Return a sorted list of .md files to scan."""
    files: list[Path] = []

    for scan_dir in SCAN_DIRS:
        dirpath = root / scan_dir
        if dirpath.is_dir():
            for md in sorted(dirpath.rglob("*.md")):
                files.append(md)

    for scan_file in SCAN_FILES:
        fpath = root / scan_file
        if fpath.is_file():
            files.append(fpath)

    return files


def check_file(md_path: Path, root: Path) -> list[str]:
    """Check all links in *md_path*. Return list of error strings."""
    errors: list[str] = []
    text = md_path.read_text(encoding="utf-8", errors="replace")

    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in LINK_RE.finditer(line):
            raw_target = match.group(1).strip()

            if _should_skip_target(raw_target):
                continue

            # Strip anchor before resolving on the filesystem.
            fs_target = _strip_anchor(raw_target)
            if not fs_target:
                # Was purely an anchor (e.g. "#heading") – already skipped.
                continue

            resolved = (md_path.parent / fs_target).resolve()

            if _is_under_ignored_dir(resolved, root):
                continue

            if not resolved.exists():
                rel_source = md_path.relative_to(root)
                errors.append(f"BROKEN: {rel_source}:{lineno} → {fs_target}")

    return errors


def main() -> int:
    root = _repo_root()
    md_files = collect_md_files(root)
    all_errors: list[str] = []

    for md in md_files:
        all_errors.extend(check_file(md, root))

    if all_errors:
        for err in all_errors:
            print(err)
        print(f"\n{len(all_errors)} broken link(s) found.")
        return 1

    print(f"✓ All links OK ({len(md_files)} files checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
