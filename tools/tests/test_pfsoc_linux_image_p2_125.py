# rr unknown-llm (AI-generated)
"""PF-L0 (DS-P2.125): the pfsoc image path composes ds_ip, and the checker
accepts what it produces — closing the "detector nobody can call" refusal.

``tools/check_bench_ip.py --target pfsoc`` reads
``build/pfsoc_datalogger/linux/system.dtb`` for the declared ds_ip token; the
land refusal was that no in-tree tool wrote that token into any pfsoc artifact,
so the accept test could only pass against a fixture DTB it wrote itself. This
module proves ``tools/pfsoc_linux_image.py compose`` is that missing producer:
the /chosen/bootargs writer round-trips through the same FDT parser the rest of
the tree uses, and the composed artifact is one ``check_bench_ip`` ACCEPTS.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "pfsoc_linux_image.py"
CHECKER = ROOT / "tools" / "check_bench_ip.py"

_spec = importlib.util.spec_from_file_location(
    "pfsoc_linux_image", TOOL)
pfsoc_linux_image = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pfsoc_linux_image)
de25_linux_image = pfsoc_linux_image.de25_linux_image

DECLARED_ADDR = "198.51" + ".100.40"
BASE_BOOTARGS = (
    "console=ttyS0,115200 earlycon root=/dev/mmcblk0p2 rootwait rw mem=512M "
    "uio_pdrv_genirq.of_id=generic-uio ds_source=ddr ds_mmio_base=0x60000000 "
    "ds_clk_hz=100000000"
)


def _fdt(props: dict[str, dict[str, bytes]]) -> bytes:
    """A minimal FDT. ``props`` maps node name ('' = root) -> {prop: value}."""
    strings = bytearray()

    def stroff(name: str) -> int:
        off = len(strings)
        strings.extend(name.encode("ascii") + b"\x00")
        return off

    def pad(b: bytes) -> bytes:
        return b + b"\x00" * (-len(b) % 4)

    body = bytearray()

    def emit_node(name: str, children: dict) -> None:
        body.extend(struct.pack(">I", 1))
        body.extend(pad(name.encode("ascii") + b"\x00"))
        for prop, val in children.get("props", {}).items():
            body.extend(struct.pack(">III", 3, len(val), stroff(prop)))
            body.extend(pad(val))
        for cname, cval in children.get("nodes", {}).items():
            emit_node(cname, cval)
        body.extend(struct.pack(">I", 2))

    emit_node("", props)
    body.extend(struct.pack(">I", 9))

    off_rsvmap = 40
    off_struct = off_rsvmap + 16
    off_strings = off_struct + len(body)
    total = off_strings + len(strings)
    header = struct.pack(">10I", 0xD00DFEED, total, off_struct, off_strings,
                         off_rsvmap, 17, 16, 0, len(strings), len(body))
    return header + b"\x00" * 16 + bytes(body) + bytes(strings)


def _chosen_bootargs(blob: bytes) -> str | None:
    for node_path, name, value in de25_linux_image._iter_fdt_props(blob):
        if node_path == "chosen" and name == "bootargs":
            return value.rstrip(b"\x00").decode()
    return None


# ── the FDT writer, all three placement cases, round-tripped ────────────────

def test_replaces_an_existing_chosen_bootargs():
    base = _fdt({"nodes": {"chosen": {
        "props": {"bootargs": b"OLD ARGS\x00", "stdout-path": b"serial0\x00"}}}})
    out = pfsoc_linux_image.set_chosen_bootargs(base, "NEW ARGS here")
    assert _chosen_bootargs(out) == "NEW ARGS here"
    # sibling property survives
    got = {n: (name, v) for n, name, v in de25_linux_image._iter_fdt_props(out)}
    assert any(name == "stdout-path" for _n, name, _v
               in de25_linux_image._iter_fdt_props(out))


def test_adds_bootargs_to_a_chosen_without_it():
    base = _fdt({"nodes": {"chosen": {"props": {"stdout-path": b"serial0\x00"}}}})
    out = pfsoc_linux_image.set_chosen_bootargs(base, "ADDED")
    assert _chosen_bootargs(out) == "ADDED"


def test_creates_chosen_when_absent():
    base = _fdt({"nodes": {"soc": {"props": {"foo": b"bar\x00"}}}})
    out = pfsoc_linux_image.set_chosen_bootargs(base, "CREATED")
    assert _chosen_bootargs(out) == "CREATED"
    # the pre-existing node is untouched
    assert any(np == "soc" for np, _n, _v
               in de25_linux_image._iter_fdt_props(out))


# ── compose bakes the declared ds_ip and the checker accepts it ─────────────

def _write_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "targets").mkdir(parents=True)
    (root / "targets/pfsoc_datalogger.yml").write_text(
        yaml.safe_dump({"linux": {"bootargs": BASE_BOOTARGS}}), encoding="utf-8")
    return root


def _write_hw(tmp_path: Path) -> Path:
    doc = {"bench": {
        "resources": {"pfsoc_address": {
            "kind": "bench_ip", "addr": DECLARED_ADDR, "prefix": 24}},
        "targets": {"pfsoc_bench": {
            "board": "polarfire-soc-discovery",
            "bench_ip": "pfsoc_address", "cmdline_ip_key": "ds_ip"}},
    }}
    path = tmp_path / "hw.yml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def test_compose_bakes_ds_ip_and_checker_accepts_it(tmp_path, monkeypatch):
    root = _write_root(tmp_path)
    hw = _write_hw(tmp_path)
    monkeypatch.setenv("RR_BENCH_CONFIG", str(hw))

    base = tmp_path / "base.dtb"
    base.write_bytes(_fdt({"nodes": {"chosen": {
        "props": {"bootargs": BASE_BOOTARGS.encode() + b"\x00"}}}}))
    out = tmp_path / "system.dtb"

    bootargs = pfsoc_linux_image.compose(root, base, out)
    assert f"ds_ip={DECLARED_ADDR}/24" in bootargs
    assert f"ds_ip={DECLARED_ADDR}/24" in _chosen_bootargs(out.read_bytes())

    # The produced artifact is one the checker ACCEPTS — the loop the refusal
    # said did not exist.
    proc = subprocess.run(
        ["python3", str(CHECKER), "--target", "pfsoc", "--root", str(root),
         "--hw", str(hw), "--dtb", str(out)],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ACCEPTED" in proc.stdout


def test_compose_fails_closed_when_base_dtb_is_absent(tmp_path):
    root = _write_root(tmp_path)
    _write_hw(tmp_path)
    with pytest.raises(RuntimeError, match="base PolarFire DTB missing"):
        pfsoc_linux_image.compose(
            root, tmp_path / "nope.dtb", tmp_path / "out.dtb")
