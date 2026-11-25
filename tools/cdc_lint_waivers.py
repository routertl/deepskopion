#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-P2.189 — RR-CDC-002 gate with deepskopion-side waivers that survive a re-vendor.

THE DEFECT. DS-T1.21 (31761198) waived the REA quasi-static MCP in
``rr_rea_sync_word`` (s1/s2) with inline ``rr-lint-disable-line`` pragmas in
the VENDORED ``libs/routertl/rea/rtl/rr_rea_cdc.vhd``. A vendored file is
reinstalled byte-for-byte from upstream (its bytes are pinned by ip.lock
``file_hashes``), and upstream rr-rea carries no such pragma, so the REA
re-vendor 30c53d04 (DS-P3.174) dropped them. Both DS-T1.21 whole-design CDC
tests went red on main on 2026-10-15 and nothing noticed for 4 days, because
the pre-commit gate did not run them.

THE FIX. Waivers for vendored sources live in ``cdc_lint_waivers.yml`` at the
project root, outside every vendored tree. Each one is anchored on
(path, entity, signal), not on a line number, so upstream churn that moves the
declaration does not break it. The waiver is refused, not silently ignored,
when:

  * it matches no RR-CDC-002 finding (stale: the signal was renamed or fixed);
  * its evidence file is missing or no longer contains the cited text;
  * it is malformed (missing id, rule, path, entity, signals or reason).

A finding in the same file but outside the named entity or signals is still
reported, so a waiver never turns into a blanket file exclusion.

DS-P2.193: rr-rea 1.12.3 ships the rr_rea_sync_word pragma upstream, so the REA
entry is gone from cdc_lint_waivers.yml; re-adding it is refused as stale. The
gate stays, for the next vendored crossing upstream does not yet waive.

DS-P2.188: the gate also runs RR-CDC-005 (routertl RTL-P2.1672), which infers
clock domains within a file and flags an unsynchronized multi-bit capture: no
synchroniser at all, a handshake that captures a register not launched with
the request (mutant B, 899d75b6), or a live port/instance output captured at a
handshake point. RR-CDC-002 is structural and misses all three. RR-CDC-005 is
waived by inline ``rr-lint-disable-line RR-CDC-005`` pragmas only (the vendored
rr-ddr-stream 0.3.4 base-address crossings, DDR-P2.29, carry them upstream);
cdc_lint_waivers.yml waivers are RR-CDC-002 only, anchored on a declaration.

CLI (fails closed, like every gate in this repo)::

    python tools/cdc_lint_waivers.py check [--root DIR] [--waivers FILE]

exit 0  no unwaived RR-CDC-002 / RR-CDC-005 finding and every waiver is valid and live
exit 1  an unwaived finding, or an invalid/stale waiver
exit 2  the gate could not run (no source files, unreadable waiver file)
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

RULE = "RR-CDC-002"
RULE_005 = "RR-CDC-005"
ROOT = Path(__file__).resolve().parents[1]
WAIVERS_FILE = "cdc_lint_waivers.yml"
# The DS-T1.21 design closure: project RTL plus every vendored routertl package.
SCAN_GLOBS = ("hw/rtl/**/*.vhd", "libs/routertl/**/*.vhd")

_SIGNAL_DECL_RE = re.compile(r"^\s*signal\s+([\w\s,]+?)\s*:", re.IGNORECASE)
_ARCH_RE = re.compile(r"^\s*architecture\s+\w+\s+of\s+(\w+)\s+is\b", re.IGNORECASE)
_UNIT_START_RE = re.compile(r"^\s*(architecture|entity|package|configuration)\b",
                            re.IGNORECASE)


class GateCouldNotRun(Exception):
    """The gate could not inspect the design (exit 2, never a green)."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    column: int
    message: str
    rule: str = RULE

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.column}: {self.rule} {self.message}"


@dataclass(frozen=True)
class Waiver:
    id: str
    path: str
    entity: str
    signals: frozenset[str]


def scan_files(root: Path) -> list[Path]:
    files: set[Path] = set()
    for pattern in SCAN_GLOBS:
        files.update(root.glob(pattern))
    return sorted(files)


def _raw_findings(root: Path, path: Path) -> list[tuple[Finding, str]]:
    """RR-CDC-002 and RR-CDC-005 findings with their source line, inline
    pragmas applied per rule (a pragma naming one rule does not waive the other)."""
    from routertl_core.lint_checkers.rr_cdc_domain_vhdl import (
        check_rr_cdc_005_unsynchronized_multibit_crossing,
    )
    from routertl_core.lint_checkers.rr_cdc_vhdl import (
        check_rr_cdc_002_multibit_two_ff_sync,
    )
    from routertl_core.lint_runners import _line_suppresses

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    rel = path.relative_to(root).as_posix()
    out = []
    for rule, checker in ((RULE, check_rr_cdc_002_multibit_two_ff_sync),
                          (RULE_005, check_rr_cdc_005_unsynchronized_multibit_crossing)):
        for line_no, col, msg in checker(path, text):
            src = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
            if not _line_suppresses(rule, src):
                out.append((Finding(rel, line_no, col, msg, rule), src))
    return out


def _enclosing_entity(lines: list[str], line_no: int) -> str | None:
    """Entity whose architecture contains 1-based *line_no*, else None."""
    for i in range(min(line_no, len(lines)) - 1, -1, -1):
        m = _ARCH_RE.match(lines[i])
        if m:
            return m.group(1).lower()
        if _UNIT_START_RE.match(lines[i]):
            return None
    return None


def _declared_signals(src_line: str) -> set[str]:
    m = _SIGNAL_DECL_RE.match(src_line)
    if not m:
        return set()
    return {s.strip().lower() for s in m.group(1).split(",") if s.strip()}


def load_waivers(root: Path, waivers_file: Path) -> tuple[list[Waiver], list[str]]:
    """Valid waivers and the refusals for invalid ones."""
    if not waivers_file.is_file():
        return [], []
    try:
        raw = yaml.safe_load(waivers_file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GateCouldNotRun(f"cannot read {waivers_file}: {exc}") from exc
    entries = raw.get("waivers") or []
    if not isinstance(entries, list):
        return [], [f"{waivers_file.name}: waivers must be a list"]
    waivers: list[Waiver] = []
    errors: list[str] = []
    for idx, entry in enumerate(entries):
        tag = f"{waivers_file.name} waivers[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{tag}: must be a mapping")
            continue
        tag = f"{waivers_file.name} waiver {entry.get('id') or f'[{idx}]'}"
        missing = [k for k in ("id", "rule", "path", "entity", "signals", "reason")
                   if not entry.get(k)]
        if missing:
            errors.append(f"{tag}: missing {', '.join(missing)}")
            continue
        if entry["rule"] != RULE:
            errors.append(f"{tag}: rule {entry['rule']!r} is not {RULE}")
            continue
        signals = entry["signals"]
        if not (isinstance(signals, list) and all(isinstance(s, str) and s for s in signals)):
            errors.append(f"{tag}: signals must be a non-empty list of names")
            continue
        evidence = entry.get("evidence") or []
        if not evidence:
            errors.append(f"{tag}: names no evidence (requirement / constraint files)")
            continue
        bad_evidence = False
        for ev in evidence:
            ev_file = root / str((ev or {}).get("file", ""))
            needle = (ev or {}).get("contains")
            if not (ev_file.is_file() and needle):
                errors.append(f"{tag}: evidence file {ev.get('file')!r} missing")
                bad_evidence = True
            elif needle not in ev_file.read_text(encoding="utf-8", errors="replace"):
                errors.append(f"{tag}: evidence {ev['file']} no longer contains {needle!r}")
                bad_evidence = True
        if bad_evidence:
            continue
        waivers.append(Waiver(str(entry["id"]), str(entry["path"]),
                              str(entry["entity"]).lower(),
                              frozenset(s.lower() for s in signals)))
    return waivers, errors


def check(root: Path = ROOT, waivers_file: Path | None = None) -> tuple[list[Finding], list[str]]:
    """Unwaived RR-CDC-002 / RR-CDC-005 findings and waiver refusals for the
    design closure."""
    root = root.resolve()
    waivers_file = waivers_file or root / WAIVERS_FILE
    files = scan_files(root)
    if not files:
        raise GateCouldNotRun(f"no VHDL under {', '.join(SCAN_GLOBS)} in {root}")
    waivers, errors = load_waivers(root, waivers_file)
    used: set[str] = set()
    unwaived: list[Finding] = []
    for path in files:
        findings = _raw_findings(root, path)
        if not findings:
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for finding, src in findings:
            entity = _enclosing_entity(lines, finding.line)
            signals = _declared_signals(src)
            hit = next((w for w in waivers
                        if finding.rule == RULE and w.path == finding.path and w.entity == entity
                        and signals and signals <= w.signals), None)
            if hit is None:
                unwaived.append(finding)
            else:
                used.add(hit.id)
    for w in waivers:
        if w.id not in used:
            errors.append(f"{waivers_file.name} waiver {w.id}: stale — matches no "
                          f"{RULE} finding on {w.entity} {sorted(w.signals)} in "
                          f"{w.path}; remove it or fix its anchor")
    return unwaived, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="run the RR-CDC-002 / RR-CDC-005 gate")
    c.add_argument("--root", type=Path, default=ROOT)
    c.add_argument("--waivers", type=Path, default=None)
    args = ap.parse_args(argv)
    try:
        unwaived, errors = check(args.root, args.waivers)
    except GateCouldNotRun as exc:
        print(f"GATE COULD NOT RUN: {exc}")
        return 2
    for f in unwaived:
        print(f"UNWAIVED {f}")
    for e in errors:
        print(f"REFUSED {e}")
    if unwaived or errors:
        print(f"{RULE}/{RULE_005}: {len(unwaived)} unwaived finding(s), "
              f"{len(errors)} waiver refusal(s)")
        return 1
    print(f"{RULE}/{RULE_005}: design closure clean (waivers: {args.waivers or WAIVERS_FILE})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
