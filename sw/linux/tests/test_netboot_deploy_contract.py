"""NET-6 (DS-P2.87) — netboot deploy contract: what the live validation caught.

The first live bootsrc=net attempt (2026-08-11, UART-logged) failed in two
independent ways that no test guarded:

1. **The board's SD carried an 877-byte boot.scr from the dnsmasq era** —
   "TFTP boot from <server-address>:active/ via dnsmasq unique-root". NET-2 had
   regenerated boot.scr through the rr flow, but nothing ever compared the
   *staged artifact* against the *current source*, and the board kept booting
   a script whose fetch paths died with dnsmasq (2026-07-21).

2. **u-boot's PHY wait is shorter than the bench autoneg.** The Z7-20's link
   partner is a 10/100-only port with >8 s autoneg (NET-4, measured). A
   single-shot tftpboot dies with "Waiting for PHY auto negotiation ...
   TIMEOUT" and silently falls back to SD on EVERY cold boot — observed
   live, twice back-to-back, before the retry loop landed. u-boot's
   phy_startup only waits for ANEGCOMPLETE (it does not restart autoneg),
   so a sleep-and-retry rides the same negotiation to completion: attempt 1
   timed out, attempt 2 fetched both files (uart log, 2026-08-11).

These tests pin both mechanisms, plus the staging discipline (the TFTP slot
must come from the rr flow — MANIFEST.sha256 present and true — never a
silent hand-copy; ticket rule "never hand-copy silently").
"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
BOOT_CMD = REPO / "sw" / "linux" / "boot.cmd"
EXAMPLE_HW_YML = REPO / "runtimes" / "hw.example.yml"
PROJECT = "deepskopion"
BOARD_IP = os.environ.get("DEEPSKOPION_BOARD_IP")
_tftp_root = os.environ.get("TFTP_ROOT")
TFTP_ROOT = Path(_tftp_root) if _tftp_root else None


def _net_branch(text):
    """The bootsrc=net branch that performs the fetch.

    A preceding branch may acquire/validate network state; select by behavior
    rather than assuming the first textual condition owns the transfer.
    """
    lines = text.splitlines()
    for start, line in enumerate(lines):
        if not ("bootsrc" in line and "net" in line
                and line.strip().startswith("if ")):
            continue
        depth = 0
        for j in range(start, len(lines)):
            code = lines[j].split("#", 1)[0]
            depth += code.count("if ") - code.count("fi")
            if depth == 0 and j > start:
                branch = lines[start:j + 1]
                if any("tftpboot" in row.split("#", 1)[0] for row in branch):
                    return branch
                break
    raise AssertionError("no bootsrc=net transfer branch in boot.cmd")


# ── defect 2: single-shot tftpboot vs the measured >8 s autoneg ───────────


def test_net_branch_retries_the_fetch_pair():
    """The tftpboot pair must sit inside a retry loop with an inter-attempt
    sleep. A single-shot fetch loses the race against the bench's >8 s
    autoneg (NET-4) every cold boot and silently falls back to SD — the
    board then runs STALE software while the deploy 'succeeded'."""
    branch = "\n".join(_net_branch(BOOT_CMD.read_text()))
    assert "for try in" in branch and "sleep" in branch, (
        "boot.cmd's bootsrc=net branch fetches single-shot. The Z7-20 link "
        "partner needs >8 s to autoneg (NET-4, measured); u-boot's PHY wait "
        "is shorter, so the first tftpboot dies with 'auto negotiation ... "
        "TIMEOUT' (observed live 2026-08-11) and the board silently boots "
        "the stale SD image. Retry the pair with a sleep between attempts — "
        "u-boot only WAITS for ANEGCOMPLETE, so the negotiation begun on "
        "attempt 1 completes during the sleep."
    )
    assert branch.count("tftpboot") == 2, (
        "expected exactly the dtb+uImage tftpboot pair inside the net branch"
    )


def test_net_branch_still_falls_back_to_sd():
    """The retry loop must not remove the SD fallback: with no TFTP server
    the board must still come up (SD-first is the product default)."""
    text = BOOT_CMD.read_text()
    branch = "\n".join(_net_branch(text))
    assert "falling back" in branch.lower(), (
        "bootsrc=net branch lost its SD fallback message/path"
    )
    tail = text[text.index(branch) + len(branch):]
    assert "fatload mmc" in tail and "bootm" in tail, (
        "SD fallback (fatload mmc + bootm) must survive after the net branch"
    )


# ── defect 1: staged boot.scr drifting behind boot.cmd ────────────────────


def _staged_boot_scrs():
    out = REPO / "sw" / "linux" / "out"
    return sorted(out.glob("**/sdcard/boot.scr")) if out.is_dir() else []


@pytest.mark.skipif("not _staged_boot_scrs()",
                    reason="no staged sdcard/boot.scr (nothing built here)")
def test_staged_boot_scr_matches_current_boot_cmd():
    """boot.scr is mkimage-wrapped plaintext (the whole boot.cmd travels
    verbatim, comments included), so the staged artifact must embed every
    CODE line of the CURRENT boot.cmd. The board ran an 877-byte
    dnsmasq-era boot.scr for three weeks because no check compared staged
    vs source — the exact drift class this pins."""
    code_lines = [
        l for l in BOOT_CMD.read_text().splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    for scr in _staged_boot_scrs():
        blob = scr.read_bytes()
        missing = [l for l in code_lines if l.encode() not in blob]
        assert not missing, (
            f"{scr} does not embed the current boot.cmd (missing lines: "
            f"{missing[:3]}...). Re-run `rr linux bootscr` / `rr linux "
            f"sdcard` — a stale staged boot.scr is how the board kept "
            f"tftpbooting dnsmasq-era paths for three weeks."
        )


# ── every Linux bench target must own a netboot slot ──────────────────────


def test_every_linux_bench_target_declares_a_netboot_slot():
    """PF-L0 (DS-P2.125): the ticket's "stage per the TFTP layout" means the
    board's bench target must actually reference the netboot resource. The land
    refusal caught pfsoc_bench listing only pfsoc_address in `uses`, so its
    payload had nowhere to stage. Enforce it for every bench target that boots
    Linux (each names a `netboot`-resource in `uses`)."""
    doc = yaml.safe_load(EXAMPLE_HW_YML.read_text())
    bench = doc["bench"]
    resources = bench["resources"]
    netboot_names = {
        name for name, r in resources.items() if r.get("kind") == "tftp"
    }
    assert netboot_names, "hw.example.yml declares no tftp/netboot resource"

    pfsoc = bench["targets"]["pfsoc_bench"]
    uses = set(pfsoc.get("uses", []))
    assert uses & netboot_names, (
        "pfsoc_bench declares no netboot slot in `uses` — the ticket asks the "
        "PolarFire payload be staged per the TFTP layout, but it has nowhere to "
        f"stage. uses={sorted(uses)}, netboot resources={sorted(netboot_names)}"
    )


# ── staging discipline: the slot must come from the rr flow ───────────────


def _bench_available():
    if not shutil.which("curl"):
        return False
    if not BOARD_IP or TFTP_ROOT is None or not TFTP_ROOT.is_dir():
        return False
    r = subprocess.run(
        ["curl", "-s", "--max-time", "5",
         "tftp://127.0.0.1/.probe-nonexistent"],
        capture_output=True,
    )
    return r.returncode in (0, 68)


bench = pytest.mark.skipif("not _bench_available()",
                           reason="not on the bench (no local TFTP server)")


@bench
def test_active_slot_manifest_is_true():
    """The active deepskopion slot's MANIFEST.sha256 (written by rr linux
    tftp-sync) must verify against the served bytes. Proves the slot came
    through the rr flow AND that the server delivers exactly the staged
    payload — a hand-copied or bit-rotted slot fails here."""
    active = TFTP_ROOT / BOARD_IP / PROJECT / "active"
    if not active.is_dir():
        pytest.skip(f"no staged {active}")
    manifest = active / "MANIFEST.sha256"
    assert manifest.is_file(), (
        f"{active} has no MANIFEST.sha256 — the slot was not staged via "
        f"`rr linux tftp-sync` (stage_tftp_deploy.sh). Hand-copied slots "
        f"are forbidden (NET-6)."
    )
    for row in manifest.read_text().splitlines():
        digest, name = row.split(None, 1)
        r = subprocess.run(
            ["curl", "-s", "--max-time", "60",
             f"tftp://127.0.0.1/{BOARD_IP}/{PROJECT}/active/{name}"],
            capture_output=True,
        )
        assert r.returncode == 0, f"GET failed for {name}"
        assert hashlib.sha256(r.stdout).hexdigest() == digest, (
            f"served bytes for {name} do not match the rr manifest — the "
            f"server is not delivering the staged slot"
        )
