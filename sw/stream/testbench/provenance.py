#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""PS-side provenance + ABI-contract helpers for the self-test harness.

Covers three field-feedback tickets that all bear on "can an archived
verdict be trusted in hindsight, and is the harness even testing a matched
DUT?":

* RTL-P3.636 — a verdict JSON embedded the *bitstream* identity (git_hash /
  build_date from the regbank) but nothing about the *testbench* vintage
  that produced it, so an archived FAIL could not be classified as DUT
  defect vs test-skew. `gather_provenance()` records both halves.

* RTL-P3.637 — a deployed board carried no record of what was flashed
  (read it back over JTAG), and RTC-less boards stamped every verdict file
  `1970-…` so they collided / would not order. `read_deploy_stamp()` reads
  the deploy manifest `rr linux sdcard` now drops at /etc, and
  `verdict_filename()` returns a collision-free, monotonically-ordered name
  when the wall clock is untrustworthy.

* RTL-P2.654 — the RTL↔PS-software pairing was bound only by an opaque SHA,
  so an old testbench ran against a bitstream that *required* the new driver
  sequence and reported pure version-skew as a DUT FAIL. `CONTRACT_ABI_VER`
  is the shared semantic contract version mirrored by a regbank slot;
  `abi_verdict()` lets the harness self-check it at startup and report
  ERROR (harness-broken) rather than FAIL on a mismatch.

Everything here is pure / dependency-light so it unit-tests off-target.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from typing import Optional

# ── RTL-P2.654: shared RTL↔PS-software ABI contract version ──────────────
# Semantic pairing of the bitstream's RTL contract (e.g. an edge-triggered
# injector load) and the PS driver sequence that exercises it (e.g. an
# atomic enable+reset write). Bump the *minor* when the PS driver and the
# RTL must change together but stay wire-compatible for reads; bump *major*
# on a breaking pairing change. This constant is the PS-side source of
# truth — its mirror is the regbank CONTRACT_ABI_VER slot, baked into the
# bitstream. The two MUST be bumped in the same commit; the startup check
# (abi_verdict) is what guarantees a build can't silently drift from them.
#
# Encoding matches the other identity slots: 0xMMmmpp00 (major.minor.patch).
CONTRACT_ABI_VER = 0x00_02_00_00  # v0.2.0 — DS-T9 EMIO-seam REA probe widened
                                  # 31->32 bits (added bit 31 = SPI SS read-in;
                                  # capped at 32 by rr_rea trigger width,
                                  # RTL-P2.658). Regbank EMIO_PROBE slot stays
                                  # read-compatible; emio_seam.yml must match.

# Deploy manifest written by `rr linux sdcard` (RTL-P3.637). The tool stages
# it onto the boot FAT partition (deploy.json); a rootfs build may also copy
# it to /etc. Checked in order — first hit wins.
DEPLOY_STAMP_CANDIDATES = (
    Path("/etc/deepskopion-build.json"),
    Path("/boot/deploy.json"),
    Path("/run/media/mmcblk0p1/deploy.json"),
    Path("/mnt/deploy.json"),
)

# A wall clock reading before this year means the board has no RTC (or it
# was never set): treat timestamps as untrustworthy for ordering/naming.
_RTC_PLAUSIBLE_YEAR = 2021


def clock_is_trustworthy(now: Optional[datetime.datetime] = None) -> bool:
    """True when the system clock looks like a real wall-clock (RTC set).

    RTC-less Zynq boards boot at the Unix epoch, so a year in the 1970s is
    the tell that any timestamp we stamp is fiction for ordering purposes.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.year >= _RTC_PLAUSIBLE_YEAR


def _next_nortc_seq(out_dir: Path) -> int:
    """Highest existing ``nortc-NNNN-*.json`` sequence in *out_dir*, + 1.

    Persisting the counter in the directory (rather than monotonic time,
    which resets every boot) is what gives RTC-less verdicts a stable,
    collision-free creation order across reboots.
    """
    seq = 0
    if out_dir.is_dir():
        for p in out_dir.glob("nortc-*.json"):
            part = p.name.split("-", 2)
            if len(part) >= 2 and part[1].isdigit():
                seq = max(seq, int(part[1]) + 1)
    return seq


def verdict_filename(
    out_dir: Path,
    started_at: str,
    *,
    clock_ok: bool,
    monotonic_ns: int,
) -> Path:
    """Collision-free verdict filename (RTL-P3.637).

    With a trustworthy clock the ISO timestamp is unique and sorts by
    wall-clock. Without one (RTC-less board, all stamps == 1970), fall back
    to ``nortc-<seq>-up<monotonic_ns>.json`` — *seq* orders runs by creation
    even across reboots, and the monotonic suffix breaks any intra-sequence
    tie so two runs can never overwrite each other.
    """
    if clock_ok:
        return out_dir / f"{started_at}.json"
    seq = _next_nortc_seq(out_dir)
    return out_dir / f"nortc-{seq:04d}-up{monotonic_ns}.json"


def testbench_git_rev(repo_root: Optional[Path] = None) -> Optional[str]:
    """Short git rev of the testbench source tree, or None when unavailable.

    On a deployed board the rootfs has no .git, so this returns None and the
    caller falls back to the deploy stamp's recorded sw git_rev.
    """
    repo_root = repo_root or Path(__file__).resolve().parent
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, errors="replace", timeout=5, check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    rev = res.stdout.strip()
    return rev or None


def read_deploy_stamp(path: Optional[Path] = None) -> Optional[dict]:
    """Parse the deploy manifest dropped by `rr linux sdcard`.

    With an explicit *path*, reads that file. Otherwise walks
    ``DEPLOY_STAMP_CANDIDATES`` (rootfs /etc, then the boot FAT partition)
    and returns the first that parses. Returns None when the board predates
    RTL-P3.637 (no stamp anywhere) or every candidate is unreadable/corrupt —
    never raises into the harness.
    """
    candidates = (path,) if path is not None else DEPLOY_STAMP_CANDIDATES
    for cand in candidates:
        try:
            if cand.is_file():
                return json.loads(cand.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
    return None


def abi_verdict(hw_abi_ver: int, expected: int = CONTRACT_ABI_VER) -> dict:
    """Classify the RTL↔PS ABI pairing (RTL-P2.654).

    Returns ``{"state": ..., "detail": ..., "hw": ..., "expected": ...}``:

    * ``"match"``      — slot equals the PS contract constant. Proceed.
    * ``"unversioned"``— slot reads 0: a bitstream that predates contract
      versioning. We cannot enforce a contract the RTL doesn't carry, so the
      caller WARNS and proceeds (don't brick existing boards on rollout).
    * ``"mismatch"``   — slot is non-zero and != the PS constant: the
      testbench and bitstream are a mismatched pair. The caller reports
      ERROR (harness-broken, exit 2), NEVER FAIL — version-skew is not a DUT
      verdict, and a harness that can't tell the difference fabricates bugs.
    """
    if hw_abi_ver == 0:
        return {"state": "unversioned", "hw": hw_abi_ver, "expected": expected,
                "detail": "bitstream predates ABI versioning (CONTRACT_ABI_VER slot = 0)"}
    if hw_abi_ver != expected:
        return {"state": "mismatch", "hw": hw_abi_ver, "expected": expected,
                "detail": (f"RTL contract {hw_abi_ver:#010x} != PS driver "
                           f"{expected:#010x} — mismatched bitstream/software pair")}
    return {"state": "match", "hw": hw_abi_ver, "expected": expected,
            "detail": f"contract {hw_abi_ver:#010x}"}


def gather_provenance(
    bitstream_identity: dict,
    *,
    deploy_stamp: Optional[dict],
    tb_git_rev: Optional[str],
    clock_ok: bool,
) -> dict:
    """Assemble the verdict-JSON provenance header (RTL-P3.636 + P3.637).

    Records BOTH halves of the matched pair so an archived verdict fully
    self-describes: the bitstream identity (read live from the regbank) and
    the testbench identity (git rev + ABI contract), plus the deploy stamp
    and the trustworthiness of the wall clock that named the file.
    """
    sysver = bitstream_identity.get("system_version", 0) or 0
    return {
        "bitstream": {
            **bitstream_identity,
            # Low byte of system_version is the synth-time dirty bit
            # (RTL-P2.653); surface it explicitly so a reader needn't decode.
            "dirty": bool(sysver & 0xFF),
        },
        "testbench": {
            "git_rev": tb_git_rev or (deploy_stamp or {}).get("sw_git_rev"),
            "contract_abi_ver": f"{CONTRACT_ABI_VER:#010x}",
        },
        "deploy_stamp": deploy_stamp,
        "lock_digest": (deploy_stamp or {}).get("linux_lock_digest"),
        "clock_trustworthy": clock_ok,
    }
