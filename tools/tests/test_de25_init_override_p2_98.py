"""DS-P2.98 desk-side guard for the DE25 image's init= override.

DE25 boots straight to its real disk root: boot.cmd's
``booti ${kernel_addr_r} - ${fdt_addr_r}`` — the bare ``-`` is an explicit
no-initrd — so the kernel's ``rdinit=/init`` default never applies (that only
fires when an initrd is present). With no ``init=`` on the cmdline the kernel
falls back to its bare search order, which finds ``/sbin/init`` (a busybox
symlink) BEFORE ever trying the deepskopion rootfs overlay's ``/init`` at the
root. busybox then runs the stock ``/etc/inittab`` (mount + respawn
``/bin/sh``) instead of the overlay ``/init``'s boot chain, so
``/etc/init.d/S*`` — S40network (the DS-P2.97 ``ds_ip=`` consumer),
S99deepskopion-stream — never runs on a cold boot.

MEASURED on silicon (DS-P2.98, 2026-08-25): three clean cold boots with
``ds_ip=`` already correctly declared in bootargs all landed at a bare shell
with eth0 down and no deepskopion-stream process (kernel print:
``check access for rdinit=/init failed: -2, ignoring``, then
``Run /sbin/init as init process``). Running ``/etc/init.d/S40network`` BY
HAND at the console then worked immediately, proving the script logic was
fine and the missing boot-chain wiring was the actual defect. Adding
``init=/init`` to bootargs and re-testing on the same silicon flipped the
kernel print to ``Run /init as init process``, and the console showed the
overlay's banner, ``[init] /etc/init.d/S40network start``, and a
``network: eth0 uses user-supplied static address`` line naming the
declared ``ds_ip=`` address (see ``targets/de25_datalogger.yml``), with zero
console intervention.

Zynq targets (classic_eye, canfd_demo) don't need this token: their boot uses
a real initrd, where the kernel's rdinit default already finds /init.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml


ROOT = Path(
    os.environ.get("DE25_TEST_ROOT", Path(__file__).resolve().parents[2])
).resolve()
TARGET_YML = ROOT / "targets/de25_datalogger.yml"

EXPECTED_INIT_TOKEN = "init=/init"


def test_de25_bootargs_override_init_so_the_overlay_init_runs() -> None:
    """DE25 has no initrd, so without an explicit init= token the kernel
    picks busybox's stock /sbin/init (stock /etc/inittab) over the
    deepskopion overlay's /init, and S40network/S99deepskopion-stream never
    run on a cold boot."""
    target = yaml.safe_load(TARGET_YML.read_text(encoding="utf-8"))
    tokens = target["linux"]["bootargs"].split()

    assert tokens.count(EXPECTED_INIT_TOKEN) == 1, (
        "DE25 bootargs must contain exactly one "
        f"{EXPECTED_INIT_TOKEN!r} — without it the kernel's default init "
        "search finds /sbin/init (busybox stock inittab) before the "
        "deepskopion overlay's /init, so /etc/init.d/S* (S40network, "
        "S99deepskopion-stream) never runs on a cold boot"
    )
