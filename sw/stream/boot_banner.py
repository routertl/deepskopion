# rr unknown-llm (AI-generated)
"""Boot-time SW + bitstream identity banner (DS-P3.85).

A stale daemon (e.g. a pre-DS-T3.8 decoders.py) once silently masked all UART
decode for a whole bench session before anyone noticed. The fix: at startup the
daemon logs, side by side, (1) its own SW version and (2) the bitstream identity
read from the regbank, plus a PASS/STALE verdict against an optional expected
manifest — so an operator/agent can tell at a glance whether they are running
the bits they think they are.

Expectations come from the environment (the deploy/init script bakes them):
    DS_EXPECTED_DAEMON_VERSION   — expected SW git-describe
    DS_EXPECTED_GIT_HASH         — expected bitstream GIT_HASH (hex, e.g. 0x7c3614aa)

This module is import-light and pure where it counts (format_boot_banner is a
string function) so it is unit-testable without a board.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Regbank identity register offsets (mirror regbank.Regs — kept local so this
# module has no hard dependency for the pure formatter).
REG_GIT_HASH = 0x0000
REG_SYSTEM_VERSION = 0x0004
REG_CONTRACT_ABI_VER = 0x0028


@dataclass(frozen=True)
class BitstreamIdentity:
    git_hash: Optional[int] = None
    system_version: Optional[int] = None
    abi_ver: Optional[int] = None


def read_bitstream_identity(regbank) -> BitstreamIdentity:
    """Read the identity slots from a regbank handle (read_u32). Any failure
    yields None for that field rather than raising — the banner is best-effort."""
    def _rd(off):
        try:
            return int(regbank.read_u32(off)) & 0xFFFFFFFF
        except Exception:
            return None
    return BitstreamIdentity(
        git_hash=_rd(REG_GIT_HASH),
        system_version=_rd(REG_SYSTEM_VERSION),
        abi_ver=_rd(REG_CONTRACT_ABI_VER),
    )


def _norm_hash(v) -> Optional[int]:
    """Normalise an expected-hash string/int to a uint32, or None."""
    if v is None:
        return None
    if isinstance(v, int):
        return v & 0xFFFFFFFF
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(s, 0) & 0xFFFFFFFF if s.lower().startswith("0x") else int(s, 16) & 0xFFFFFFFF
    except ValueError:
        return None


def format_boot_banner(daemon_ver: str,
                       ident: BitstreamIdentity,
                       *,
                       expected_daemon_ver: Optional[str] = None,
                       expected_git_hash=None) -> Tuple[List[str], str]:
    """Build the banner lines and an overall verdict ('PASS' / 'STALE' / 'UNKNOWN').

    Verdict is STALE if any provided expectation mismatches, PASS if at least one
    expectation is provided and all match, UNKNOWN if no expectations given."""
    def h(v):
        return "--------" if v is None else f"{v:08x}"

    lines = ["─" * 60,
             "deepskopion wave_stream daemon — boot identity (DS-P3.85)",
             f"  SW daemon version : {daemon_ver}",
             f"  bitstream GIT_HASH : 0x{h(ident.git_hash)}",
             f"  SYSTEM_VERSION     : 0x{h(ident.system_version)}",
             f"  CONTRACT_ABI_VER   : 0x{h(ident.abi_ver)}"]

    checks: List[bool] = []
    if expected_daemon_ver:
        ok = (daemon_ver == expected_daemon_ver)
        checks.append(ok)
        lines.append(f"  expected SW        : {expected_daemon_ver}  "
                     f"[{'OK' if ok else 'MISMATCH'}]")
    exp_hash = _norm_hash(expected_git_hash)
    if exp_hash is not None:
        ok = (ident.git_hash == exp_hash)
        checks.append(ok)
        lines.append(f"  expected GIT_HASH  : 0x{exp_hash:08x}  "
                     f"[{'OK' if ok else 'MISMATCH'}]")

    if not checks:
        verdict = "UNKNOWN"
    elif all(checks):
        verdict = "PASS"
    else:
        verdict = "STALE"

    if verdict == "STALE":
        lines.append("  ⚠ STALE DEPLOYMENT — running bits do NOT match the expected "
                     "manifest. Redeploy before trusting bench results.")
    elif verdict == "PASS":
        lines.append("  ✓ identity matches the expected manifest.")
    else:
        # DS-P2.76: this is the DEFAULT state on every board, and reading it as
        # benign is how a bench ends up trusting bits nobody verified. `rr hw
        # bit-sha` cannot read a Linux-running Zynq (it returns "could not read
        # USR_ACCESS"), and /proc/device-tree/model is empty here, so this
        # regbank read is the ONLY working identity check on this platform —
        # and unless an expectation is set it checks nothing. Say so plainly.
        lines.append("  ⚠ IDENTITY UNVERIFIED — no expected manifest, so the bits "
                     "above were READ but not CHECKED.")
        lines.append("    On this platform this is the only working bitstream "
                     "confirm (rr hw bit-sha cannot read a Linux-running Zynq).")
        lines.append("    Enable it: put DS_EXPECTED_GIT_HASH=0x<hash> in "
                     "/etc/deepskopion-expect")
    lines.append("─" * 60)
    return lines, verdict


def log_boot_banner(log_fn, daemon_ver: str, regbank=None) -> str:
    """Read identity (if a regbank is given), format the banner, emit it via
    ``log_fn`` (e.g. print or logger.warning), and return the verdict."""
    ident = read_bitstream_identity(regbank) if regbank is not None else BitstreamIdentity()
    lines, verdict = format_boot_banner(
        daemon_ver, ident,
        expected_daemon_ver=os.environ.get("DS_EXPECTED_DAEMON_VERSION"),
        expected_git_hash=os.environ.get("DS_EXPECTED_GIT_HASH"))
    for ln in lines:
        log_fn(ln)
    return verdict
