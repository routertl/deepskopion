# rr unknown-llm (AI-generated)
"""PF-L0 (DS-P2.125): the PolarFire SoC image must carry the ds_ip it declares.

The third deepskopion board (PolarFire SoC Discovery Kit, riscv64) deploys with
the same DS-D.39 split the DE25 uses: the public target
``targets/pfsoc_datalogger.yml`` names NO bench address, and the deployed
``ds_ip=<addr>/<prefix>`` is declared at deploy time in ``runtimes/hw.local.yml`` and
baked into the image.  The ticket's hands-off boot requires ds_ip to be set —
the PolarFire kit bench has no DHCP for it to fall back to — so an image that
ships WITHOUT the token comes up with eth0 unconfigured and looks exactly like
a dead NIC.

This module proves ``tools/check_bench_ip.py --target pfsoc`` is the guard that
refuses that image, against the SAME resolver
(``de25_linux_image.deployment_bootargs``) the DE25 checker uses — so a
descriptor this checker accepts is one an image builder would also accept.

INPUT DOMAIN: any (pfsoc descriptor, pfsoc image) pair a bench can hold at
deploy time — including an image built before the token existed, a descriptor
edited after the image was built, and a descriptor declaring no address.

OUTPUT CONTRACT: the CLI exits 0 / prints ACCEPTED only when the image carries
exactly one ``ds_ip=`` token equal to the declared one; otherwise exit 1 with
the reasons on stdout.

Assertions are on the CLI exit code + printed diagnosis (the surface the bench
operator reaches), not a helper return value.  Fixture addresses are TEST-NET
(RFC 5737), so this module carries no bench identity (DS-C1).
"""
from __future__ import annotations

from pathlib import Path
import struct
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "check_bench_ip.py"

DECLARED_ADDR = "198.51" + ".100.40"
OTHER_ADDR = "198.51" + ".100.41"
# The real pfsoc_datalogger bootargs shape (console + root + the ds_* trio),
# minus any ds_ip — DS-D.39 keeps the public target address-free.
BASE_BOOTARGS = (
    "console=ttyS0,115200 earlycon root=/dev/mmcblk0p2 rootwait rw mem=512M "
    "uio_pdrv_genirq.of_id=generic-uio ds_source=ddr ds_mmio_base=0x60000000 "
    "ds_clk_hz=100000000"
)


def _fdt(bootargs: str | None) -> bytes:
    """A minimal well-formed FDT carrying (or omitting) /chosen/bootargs."""
    strings = bytearray()

    def stroff(name: str) -> int:
        off = len(strings)
        strings.extend(name.encode("ascii") + b"\x00")
        return off

    def pad(blob: bytes) -> bytes:
        return blob + b"\x00" * (-len(blob) % 4)

    body = bytearray()
    body += struct.pack(">I", 1) + pad(b"\x00")          # root node, empty name
    body += struct.pack(">I", 1) + pad(b"chosen\x00")    # /chosen
    if bootargs is not None:
        value = bootargs.encode("utf-8") + b"\x00"
        body += struct.pack(">III", 3, len(value), stroff("bootargs"))
        body += pad(value)
    body += struct.pack(">I", 2)                          # end /chosen
    body += struct.pack(">I", 2)                          # end root
    body += struct.pack(">I", 9)                          # FDT_END

    header_len = 40
    off_rsvmap = header_len
    off_struct = off_rsvmap + 16
    off_strings = off_struct + len(body)
    total = off_strings + len(strings)
    header = struct.pack(
        ">10I", 0xD00DFEED, total, off_struct, off_strings, off_rsvmap,
        17, 16, 0, len(strings), len(body),
    )
    return header + b"\x00" * 16 + bytes(body) + bytes(strings)


def _write_root(tmp_path: Path, target_bootargs: str = BASE_BOOTARGS) -> Path:
    """A project root holding ONLY the pfsoc target — no de25 target at all, so a
    pass here proves --target pfsoc reads the PolarFire file, not the DE25 one."""
    root = tmp_path / "root"
    (root / "targets").mkdir(parents=True)
    (root / "targets/pfsoc_datalogger.yml").write_text(
        yaml.safe_dump({"linux": {"bootargs": target_bootargs}}), encoding="utf-8")
    return root


def _write_hw(tmp_path: Path, name: str, addr: str | None,
              prefix: int | None = 24, key: str = "ds_ip") -> Path:
    resource: dict = {"kind": "bench_ip"}
    if addr is not None:
        resource["addr"] = addr
    if prefix is not None:
        resource["prefix"] = prefix
    doc = {
        "bench": {
            "resources": {"pfsoc_address": resource},
            "targets": {"pfsoc_bench": {
                "board": "polarfire-soc-discovery",
                "bench_ip": "pfsoc_address",
                "cmdline_ip_key": key,
            }},
        }
    }
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _run(root: Path, hw: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["python3", str(TOOL), "--target", "pfsoc",
         "--root", str(root), "--hw", str(hw), *args],
        cwd=ROOT, capture_output=True, text=True, check=False,
        env={**dict(__import__("os").environ),
             "RR_BENCH_CONFIG": str(root / "does-not-exist.yml")},
    )
    return proc.returncode, proc.stdout + proc.stderr


# ── accept ──────────────────────────────────────────────────────────────────

def test_pfsoc_image_carrying_the_declared_token_is_accepted(tmp_path):
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "hw.yml", DECLARED_ADDR)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24"))

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 0, out
    assert "ACCEPTED" in out
    assert f"ds_ip={DECLARED_ADDR}/24" in out


# ── the load-bearing refusal: the ticket's regression guard ─────────────────

def test_pfsoc_image_without_the_token_is_refused(tmp_path):
    """The image the ticket forbids: boots, but eth0 never gets an address and
    the PolarFire bench has no DHCP to fall back to."""
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "hw.yml", DECLARED_ADDR)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(BASE_BOOTARGS))          # no ds_ip=

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 1, out
    assert "no ds_ip= token" in out
    # The diagnosis must name THIS board's bench, not the DE25's. The prior
    # delivery printed the DE25 DHCP sentence (DS-P3.152) verbatim for
    # --target pfsoc, and this test only asserted "no ds_ip= token", so the
    # wrong-board copy was unguarded (DS-P2.125 land refusal).
    assert "PolarFire" in out and "DS-P2.125" in out, out
    assert "DE25" not in out and "DS-P3.152" not in out, (
        "the pfsoc refusal is printing the DE25 bench's diagnosis:\n" + out
    )


def test_pfsoc_live_cmdline_without_the_token_is_refused(tmp_path):
    """Same refusal at the /proc/cmdline surface — what the KERNEL received."""
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "hw.yml", DECLARED_ADDR)
    live = tmp_path / "cmdline.txt"
    live.write_text(BASE_BOOTARGS + "\n", encoding="utf-8")

    rc, out = _run(root, hw, "--cmdline-file", str(live))
    assert rc == 1, out
    assert "no ds_ip= token" in out
    assert "PolarFire" in out and "DE25" not in out, out


def test_pfsoc_wrong_address_is_refused(tmp_path):
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "wrong.yml", OTHER_ADDR)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24"))

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 1, out
    assert f"ds_ip={DECLARED_ADDR}/24" in out       # what the image carries
    assert f"ds_ip={OTHER_ADDR}/24" in out           # what the descriptor wants


def test_pfsoc_public_target_carrying_the_address_is_refused(tmp_path):
    """DS-D.39 in reverse: a literal ds_ip creeping back into the public pfsoc
    target must be refused, not graded a match."""
    root = _write_root(tmp_path,
                       target_bootargs=f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24")
    hw = _write_hw(tmp_path, "hw.yml", DECLARED_ADDR)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24"))

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 1, out
    assert "DS-D.39" in out
    assert "pfsoc_datalogger.yml" in out             # names the offending file


def test_pfsoc_default_dtb_path_is_the_pfsoc_image_not_the_de25_one(tmp_path):
    """--target pfsoc must default to the pfsoc image path. With no --dtb and no
    such file, the refusal must name the pfsoc build dir, proving the board
    selection reached the dtb default (a de25 default here would be a silent
    cross-board mixup)."""
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "hw.yml", DECLARED_ADDR)

    rc, out = _run(root, hw)                          # no --dtb
    assert rc == 1, out
    assert "pfsoc_datalogger" in out
    assert "de25" not in out
