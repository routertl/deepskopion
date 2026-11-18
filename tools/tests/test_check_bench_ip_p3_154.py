"""DS-P3.154: the deployed DE25 image must carry the address it declares.

DS-P3.152 split one fact into two artifacts.  Before it, the DE25's address
lived in ``targets/de25_datalogger.yml`` and shipped with the tree; after it, the
address is declared in a deploy-time ``runtimes/hw.local.yml`` and injected into the
image at build time.  Nothing then compared the two again, so an image built
against one descriptor and deployed under another comes up on the wrong
address — and because the DE25 bench has no DHCP, that is indistinguishable at
the console from a dead NIC.

INPUT DOMAIN: any (descriptor, image) pair a bench can hold at deploy time —
including a descriptor whose address was edited after the image was built, an
image built before the token existed at all, and a descriptor that declares no
address.

OUTPUT CONTRACT: ``tools/check_bench_ip.py`` exits 0 and prints ACCEPTED only
when the image carries exactly one ``ds_ip=`` token equal to the declared one.

PROHIBITION: it must never accept an image whose token is absent, duplicated,
or different from the declaration, and never accept on a descriptor the image
builder itself would refuse.

Assertions are on the CLI's exit code and printed diagnosis — the surface the
bench operator and the witness runbook actually reach — not on a helper's
return value.  Fixture addresses are TEST-NET (RFC 5737), so this module
carries no bench identity of its own (DS-C1).
"""
from __future__ import annotations

from pathlib import Path
import struct
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "check_bench_ip.py"

DECLARED_ADDR = "198.51" + ".100.25"
OTHER_ADDR = "198.51" + ".100.26"
BASE_BOOTARGS = "console=ttyS1,115200 root=/dev/mmcblk0p2 rw init=/init"


# ── fixtures ────────────────────────────────────────────────────────────────

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
    """A project root shaped like the parts deployment_bootargs() reads."""
    root = tmp_path / "root"
    (root / "targets").mkdir(parents=True)
    (root / "targets/de25_datalogger.yml").write_text(
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
            "resources": {"de25_address": resource},
            "targets": {"de25_bench": {
                "board": "de25-standard",
                "bench_ip": "de25_address",
                "cmdline_ip_key": key,
            }},
        }
    }
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _run(root: Path, hw: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["python3", str(TOOL), "--root", str(root), "--hw", str(hw), *args],
        cwd=ROOT, capture_output=True, text=True, check=False,
        # The bench exports RR_BENCH_CONFIG; --hw must win over it, or a
        # deployment check silently grades against the wrong descriptor.
        env={**dict(__import__("os").environ),
             "RR_BENCH_CONFIG": str(root / "does-not-exist.yml")},
    )
    return proc.returncode, proc.stdout + proc.stderr


# ── the accepting case ──────────────────────────────────────────────────────

def test_image_carrying_the_declared_token_is_accepted(tmp_path):
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "hw.yml", DECLARED_ADDR)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24"))

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 0, out
    assert "ACCEPTED" in out
    assert f"ds_ip={DECLARED_ADDR}/24" in out


def test_live_cmdline_carrying_the_declared_token_is_accepted(tmp_path):
    """The /proc/cmdline surface — what the KERNEL received, not what an
    artifact on the host claims it should have received."""
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "hw.yml", DECLARED_ADDR)
    live = tmp_path / "live_cmdline.txt"
    live.write_text(f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24\n", encoding="utf-8")

    rc, out = _run(root, hw, "--cmdline-file", str(live))
    assert rc == 0, out
    assert "ACCEPTED" in out


# ── the refusals ────────────────────────────────────────────────────────────

def test_wrong_declared_address_is_refused(tmp_path):
    """The witness's refusal case: a deliberately mis-declared hw.yml."""
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "wrong.yml", OTHER_ADDR)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24"))

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 1, out
    assert "REFUSED" in out
    # Both sides must be named, or the operator cannot tell which to fix.
    assert f"ds_ip={DECLARED_ADDR}/24" in out
    assert f"ds_ip={OTHER_ADDR}/24" in out


def test_wrong_declared_prefix_is_refused(tmp_path):
    """A /16 declaration against a /24 image is a different network."""
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "wrong.yml", DECLARED_ADDR, prefix=16)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24"))

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 1, out
    assert f"ds_ip={DECLARED_ADDR}/16" in out


def test_image_without_the_token_is_refused(tmp_path):
    """A pre-DS-P3.152 image: boots, but eth0 never gets an address."""
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "hw.yml", DECLARED_ADDR)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(BASE_BOOTARGS))

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 1, out
    assert "no ds_ip= token" in out


def test_duplicate_tokens_are_refused(tmp_path):
    """Two tokens make the address ordering-dependent, not declared."""
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "hw.yml", DECLARED_ADDR)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(
        f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24 ds_ip={OTHER_ADDR}/24"))

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 1, out
    assert "2 ds_ip= tokens" in out


@pytest.mark.parametrize(("name", "kwargs", "needle"), [
    ("no address declared", {"addr": None}, "valid addr"),
    ("wrong cmdline key", {"addr": DECLARED_ADDR, "key": "bench_ip"},
     "cmdline_ip_key"),
])
def test_unusable_descriptor_is_refused(tmp_path, name, kwargs, needle):
    """A descriptor the IMAGE BUILDER would refuse must not be graded as a
    pass by the checker — the two must agree on what is deployable."""
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "hw.yml", **kwargs)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24"))

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 1, out
    assert needle in out


def test_missing_descriptor_is_refused(tmp_path):
    root = _write_root(tmp_path)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24"))

    rc, out = _run(root, tmp_path / "absent.yml", "--dtb", str(dtb))
    assert rc == 1, out
    assert "deployment descriptor missing" in out


def test_target_carrying_the_address_is_refused(tmp_path):
    """DS-D.39 in the other direction: if the public target ever regains a
    literal token, the checker refuses instead of grading it a match."""
    root = _write_root(tmp_path,
                       target_bootargs=f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24")
    hw = _write_hw(tmp_path, "hw.yml", DECLARED_ADDR)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24"))

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 1, out
    assert "DS-D.39" in out


def test_dtb_without_bootargs_is_refused(tmp_path):
    """No kernel command line at all is a refusal, not a silent pass."""
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "hw.yml", DECLARED_ADDR)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(None))

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 1, out
    assert "no /chosen/bootargs" in out


def test_corrupt_dtb_is_refused(tmp_path):
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path, "hw.yml", DECLARED_ADDR)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(b"not a devicetree at all, just bytes" * 4)

    rc, out = _run(root, hw, "--dtb", str(dtb))
    assert rc == 1, out
    assert "REFUSED" in out
