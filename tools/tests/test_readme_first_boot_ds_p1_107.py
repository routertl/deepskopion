# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.107: the README quick-start gives the first-boot steps a fresh board needs, in order, and they match the image.

Measured 2026-11-15 on the shipped Zybo image (7041ae2e): with no DHCP server the board boots to a serial-console shell
with no address ("network: no DHCP lease; set ds_ip=<address>/<prefix> or create /etc/deepskopion/network.conf") and SSH
closed ("dropbear: no owner key provisioned; SSH remains disabled"). The README named only the DHCP / ds_ip case.

The README's "First boot" section must state, in this order: the address (ds_ip on the kernel command line or
/etc/deepskopion/network.conf), `deepskopion-authorize-key` with the user's public key, and the dropbear start. Each
statement is checked against the rootfs overlay scripts that implement it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
OVERLAY = ROOT / "sw" / "linux" / "rootfs_overlay"
S40 = OVERLAY / "etc" / "init.d" / "S40network"
S49 = OVERLAY / "etc" / "init.d" / "S49dropbear"
AUTH = OVERLAY / "usr" / "local" / "bin" / "deepskopion-authorize-key"


def _first_boot_section() -> str:
    text = README.read_text(encoding="utf-8")
    m = re.search(r"^#{2,4} First boot\b.*?$(.*?)(?=^#{1,4} |\Z)", text, re.M | re.S)
    assert m, "README has no 'First boot' section"
    return m.group(1)


def test_first_boot_section_gives_the_three_steps_in_order():
    sec = _first_boot_section()
    pos = {
        "address": min((sec.find(k) for k in ("ds_ip=", "network.conf") if k in sec), default=-1),
        "authorize": sec.find("deepskopion-authorize-key"),
        "dropbear": sec.find("/etc/init.d/S49dropbear start"),
    }
    missing = [k for k, v in pos.items() if v < 0]
    assert not missing, f"README 'First boot' lacks: {missing}"
    assert pos["address"] < pos["authorize"] < pos["dropbear"], f"README 'First boot' step order {pos}"
    for needed in ("ds_ip=<address>/<prefix>", "/etc/deepskopion/network.conf", "address=<address>/<prefix>",
                   "/etc/init.d/S40network restart", "serial console"):
        assert needed in sec, f"README 'First boot' lacks {needed!r}"


def test_the_steps_match_the_overlay_scripts():
    sec = _first_boot_section()
    s40, s49, auth = (p.read_text(encoding="utf-8") for p in (S40, S49, AUTH))
    # address: both sources the README names are the ones S40network reads, with the README's key names
    assert "cmdline_value ds_ip" in s40 and "/etc/deepskopion/network.conf" in s40 and "address=<address>/<prefix>" in s40
    assert "restart" in s40.split("case")[-1], "S40network has no restart verb"
    # key: the command the README names exists, is executable, and takes a file or stdin
    assert AUTH.exists() and os.access(AUTH, os.X_OK), f"{AUTH} missing or not executable"
    assert 'SOURCE="${1:--}"' in auth
    # dropbear: S49dropbear refuses without a key and names the provisioner; it accepts a build-time key file
    assert "no owner key provisioned" in s49 and "deepskopion-authorize-key" in s49
    assert "/etc/deepskopion/authorized_keys" in s49 and "/etc/deepskopion/authorized_keys" in sec
    # password logins are off (dropbear -s), which the README states
    assert re.search(r'"\$DROPBEAR"[^\n]* -s ', s49) and "password" in sec.lower()
