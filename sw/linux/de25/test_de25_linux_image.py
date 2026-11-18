"""Executable DE-L0 image contract at the CLI/configuration surfaces."""
from __future__ import annotations

import fcntl
import functools
import importlib.util
import io
import os
import json
from pathlib import Path
import shutil
import struct
import subprocess
import re
import sys
import tarfile
import tempfile
import zlib

import pytest
import yaml
from sdk.cocotb_helpers import requires


ROOT = Path(os.environ.get("DE25_TEST_ROOT", Path(__file__).resolve().parents[3])).resolve()
TARGET = ROOT / "targets/de25_datalogger.yml"
TOOL = ROOT / "tools/de25_linux_image.py"

# Loaded directly from the worktree file (never a shared/editable `rr`
# install) so tests can reach the same partition-offset constants the CLI
# hard-codes, instead of duplicating them.
_spec = importlib.util.spec_from_file_location("de25_linux_image", TOOL)
de25_linux_image = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(de25_linux_image)

# DS-P3.154: the shared FDT fixture builder (tools/tests has no __init__.py,
# so it is loaded by path, matching this module's own convention above).
_fdt_spec = importlib.util.spec_from_file_location(
    "fdt_fixture", ROOT / "tools/tests/fdt_fixture.py")
_fdt_fixture = importlib.util.module_from_spec(_fdt_spec)
_fdt_spec.loader.exec_module(_fdt_fixture)


def _write_project_pointer(root: Path) -> None:
    """DS-P2.135: the tool resolves its ACTIVE target from project.yml."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.yml").write_text(
        "target: targets/de25_datalogger.yml\n", encoding="utf-8")

def _write_deployment_hw(
    root: Path, *, address: str = "198.51" + ".100.25", prefix=24,
    cmdline_ip_key: str = "ds_ip",
) -> Path:
    """Write the ignored deployment input used at the real build boundary."""
    path = root / "runtimes/hw.local.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({
            "bench": {
                "resources": {
                    "de25_address": {
                        "kind": "bench_ip", "addr": address, "prefix": prefix,
                    },
                },
                "targets": {
                    "de25_bench": {
                        "board": "de25-standard",
                        "bench_ip": "de25_address",
                        "cmdline_ip_key": cmdline_ip_key,
                        "uses": ["de25_address"],
                    },
                },
            },
        }, sort_keys=False),
        encoding="utf-8",
    )
    return path


@requires("DE-L0-IN-003", "DE-L0-BUILD-003", "DE-L0-NEG-007")
def test_deployment_address_is_injected_only_at_the_image_boundary(
    tmp_path, monkeypatch,
):
    """The rendered cmdline—not a helper field—carries the hw.yml address.

    BASELINE: the pre-DS-D.39 code has no deployment resolver, and its public
    target carries a fixed ds_ip token instead. MUTATION: deleting address
    composition or changing the consumed key makes this exact output fail.
    """
    root = tmp_path / "project"
    _write_project_pointer(root)
    (root / "targets").mkdir(parents=True)
    target = yaml.safe_load(TARGET.read_text(encoding="utf-8"))
    (root / "targets/de25_datalogger.yml").write_text(
        yaml.safe_dump(target, sort_keys=False), encoding="utf-8"
    )
    _write_deployment_hw(root)
    monkeypatch.delenv("RR_BENCH_CONFIG", raising=False)

    rendered = de25_linux_image.deployment_bootargs(root)
    tokens = rendered.split()
    expected = "ds_ip=198.51" + ".100.25/24"
    assert tokens.count(expected) == 1
    assert not any(token.startswith("bench_ip=") for token in tokens)


@requires("DE-L0-NEG-007")
@pytest.mark.parametrize("mutation", ["absent", "wrong-key", "bad-address", "bad-prefix"])
def test_deployment_address_mutations_refuse_image_build(
    tmp_path, monkeypatch, mutation,
):
    """Every unusable deployment declaration refuses before `rr` is called."""
    root = tmp_path / "project"
    _write_project_pointer(root)
    (root / "targets").mkdir(parents=True)
    target = yaml.safe_load(TARGET.read_text(encoding="utf-8"))
    (root / "targets/de25_datalogger.yml").write_text(
        yaml.safe_dump(target, sort_keys=False), encoding="utf-8"
    )
    if mutation != "absent":
        _write_deployment_hw(
            root,
            address=("not-an-ip" if mutation == "bad-address"
                     else "198.51" + ".100.25"),
            prefix="wide" if mutation == "bad-prefix" else 24,
            cmdline_ip_key="bench_ip" if mutation == "wrong-key" else "ds_ip",
        )
    monkeypatch.delenv("RR_BENCH_CONFIG", raising=False)

    with pytest.raises(RuntimeError, match="DS-D.39|cmdline_ip_key|valid addr"):
        de25_linux_image.deployment_bootargs(root)


@requires("DE-L0-NEG-007")
def test_public_target_refuses_a_reintroduced_address_token(tmp_path, monkeypatch):
    """A target mutation cannot override the deployment descriptor."""
    root = tmp_path / "project"
    _write_project_pointer(root)
    (root / "targets").mkdir(parents=True)
    target = yaml.safe_load(TARGET.read_text(encoding="utf-8"))
    target["linux"]["bootargs"] += " ds_ip=198.51" + ".100.99/24"
    (root / "targets/de25_datalogger.yml").write_text(
        yaml.safe_dump(target, sort_keys=False), encoding="utf-8"
    )
    _write_deployment_hw(root)
    monkeypatch.delenv("RR_BENCH_CONFIG", raising=False)

    with pytest.raises(RuntimeError, match=r"public targets/de25_datalogger\.yml bootargs contain deployment network identity"):
        de25_linux_image.deployment_bootargs(root)

_SECTOR = 512
# Mirrors routertl's SocfpgaSdLayout defaults (align_mib=4, fat_mib=256),
# which targets/de25_datalogger.yml's linux.sdcard does not override — the same
# assumption tools/de25_linux_image.py's SDCARD_*_OFFSET_BYTES constants make.
_ALIGN_MIB, _FAT_MIB, _TEST_EXT4_MIB = 4, 256, 8


def _write_socfpga_sdcard_image(
    sdcard: Path, fat_items: dict[str, Path], rootfs_dir: Path | None,
) -> None:
    """Build a real Agilex 5 GSRD-shaped raw image: MBR + P1 FAT32 (type
    ``c``) at the fixed offset ``SDCARD_FAT_OFFSET_BYTES`` + P2 ext4 (type
    ``83``) at the fixed offset ``SDCARD_ROOTFS_OFFSET_BYTES`` — the same
    layout ``rr linux sdcard`` (routertl ``_socfpga.py``) assembles for real,
    at test-friendly (mostly sparse) sizes. No A2 partition: this Agilex 5 /
    SDM chain has none (RTL-P2.668). ``rootfs_dir=None`` omits P2 entirely,
    reproducing the DS-P1.49 defect image (FAT-only, no rootfs).
    """
    spm = (1024 * 1024) // _SECTOR
    p1_start, p1_size = _ALIGN_MIB * spm, _FAT_MIB * spm
    p2_start, p2_size = p1_start + p1_size, _TEST_EXT4_MIB * spm
    assert p1_start * _SECTOR == de25_linux_image.SDCARD_FAT_OFFSET_BYTES
    assert p2_start * _SECTOR == de25_linux_image.SDCARD_ROOTFS_OFFSET_BYTES
    total_sectors = (p2_start + p2_size if rootfs_dir else p1_start + p1_size) + spm

    with open(sdcard, "wb") as f:
        f.truncate(total_sectors * _SECTOR)
    sfdisk_lines = [f"start={p1_start}, size={p1_size}, type=c, bootable"]
    if rootfs_dir is not None:
        sfdisk_lines.append(f"start={p2_start}, size={p2_size}, type=83")
    subprocess.run(
        [de25_linux_image.sbin_tool("sfdisk"), str(sdcard)], input="label: dos\n" + "\n".join(sfdisk_lines) + "\n",
        text=True, check=True, capture_output=True,
    )

    fat_img = sdcard.parent / "stage-fat.img"
    with open(fat_img, "wb") as f:
        f.truncate(_FAT_MIB * 1024 * 1024)
    subprocess.run(["mformat", "-i", str(fat_img), "::"], check=True, capture_output=True)
    for name, src in fat_items.items():
        subprocess.run(["mcopy", "-i", str(fat_img), str(src), f"::/{name}"],
                       check=True, capture_output=True)
    subprocess.run(["dd", f"if={fat_img}", f"of={sdcard}", f"bs={_SECTOR}",
                    f"seek={p1_start}", "conv=notrunc", "status=none"], check=True)

    if rootfs_dir is not None:
        ext4_img = sdcard.parent / "stage-rootfs.ext4"
        subprocess.run(
            [de25_linux_image.sbin_tool("mke2fs"), "-F", "-t", "ext4", "-b", "1024", "-L", "rootfs",
             "-d", str(rootfs_dir), str(ext4_img), str(_TEST_EXT4_MIB * 1024)],
            check=True, capture_output=True,
        )
        subprocess.run(["dd", f"if={ext4_img}", f"of={sdcard}", f"bs={_SECTOR}",
                        f"seek={p2_start}", "conv=notrunc", "status=none"], check=True)


@functools.lru_cache(maxsize=None)
def _shared_stage_inputs() -> tuple[Path, Path]:
    """One checksum-verified copy of the stage's pinned inputs and one unpacked
    toolchain, shared by every real-stage test of a session: (cache, toolchain).

    Each real-stage test used to download the 145 MB toolchain archive into its
    own tmp_path and unpack it (735 MB), about 1.3 GB per test. The directory
    is keyed by the toolchain sha256 under the session's TMPDIR, filled once
    under a lock (pytest-xdist workers share it), and every archive in it is
    verified by de25_linux_image._fetch_pinned, here and again by the stage.
    DE25_TEST_STAGE_INPUTS overrides the location (DS-P2.227)."""
    m = de25_linux_image
    root = Path(os.environ.get("DE25_TEST_STAGE_INPUTS") or Path(tempfile.gettempdir()) / (
        "de25-stage-inputs-" + m.de25_toolchain_stage.TOOLCHAIN_SHA256[:16]))
    root.mkdir(parents=True, exist_ok=True)
    cache, toolchain = root / "cache", root / "toolchain"
    with open(root / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name, url, sha in (
                (f"msgpack-{m.MSGPACK_VERSION}.tar.gz", m.MSGPACK_URL, m.MSGPACK_SHA256),
                (m.PYTHON_ARCHIVE, m.PYTHON_URL, m.PYTHON_SHA256),
                (m.de25_toolchain_stage.TOOLCHAIN_ARCHIVE, m.de25_toolchain_stage.TOOLCHAIN_URL,
                 m.de25_toolchain_stage.TOOLCHAIN_SHA256),
                (m.IPROUTE2_ARCHIVE, m.IPROUTE2_URL, m.IPROUTE2_SHA256),
                (m.CAN_UTILS_ARCHIVE, m.CAN_UTILS_URL, m.CAN_UTILS_SHA256)):
            m._fetch_pinned(cache / name, url=url, expected=sha, label=name)
        m.de25_toolchain_stage.stage(cache, toolchain)
    return cache, toolchain


def _private_cache(tmp_path: Path, copied: str) -> Path:
    """A per-test cache: hard links to the shared inputs, except *copied*, which
    is a real copy the test may damage without touching the shared one."""
    shared, _toolchain = _shared_stage_inputs()
    cache = tmp_path / "cache"
    cache.mkdir()
    for src in shared.iterdir():
        if not src.is_file():
            continue
        if src.name == copied:
            shutil.copy2(src, cache / src.name)
            continue
        try:
            os.link(src, cache / src.name)
        except OSError:
            shutil.copy2(src, cache / src.name)
    return cache


def _run_stage(output: Path, cache: Path | None = None) -> subprocess.CompletedProcess[str]:
    shared_cache, toolchain = _shared_stage_inputs()
    return subprocess.run(
        [sys.executable, str(TOOL), "stage", "--root", str(ROOT),
         "--output", str(output), "--cache", str(cache or shared_cache),
         "--python-output", str(output.parent / "python_overlay"),
         # the pinned toolchain the native edge library is built with (DS-P2.210)
         "--toolchain-dir", str(toolchain)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )


def _elf_needed(data: bytes) -> list[str]:
    """DT_NEEDED names of a 64-bit little-endian ELF shared object."""
    phoff = int.from_bytes(data[32:40], "little")
    phentsize = int.from_bytes(data[54:56], "little")
    phnum = int.from_bytes(data[56:58], "little")
    loads, dynamic = [], None
    for i in range(phnum):
        ph = data[phoff + i * phentsize: phoff + (i + 1) * phentsize]
        p_type = int.from_bytes(ph[0:4], "little")
        p_offset, p_vaddr = (int.from_bytes(ph[8:16], "little"),
                             int.from_bytes(ph[16:24], "little"))
        p_filesz = int.from_bytes(ph[32:40], "little")
        if p_type == 1:      # PT_LOAD
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == 2:    # PT_DYNAMIC
            dynamic = (p_offset, p_filesz)
    assert dynamic is not None, "no PT_DYNAMIC: not a shared object"
    entries = []
    for off in range(dynamic[0], dynamic[0] + dynamic[1], 16):
        tag, val = (int.from_bytes(data[off:off + 8], "little"),
                    int.from_bytes(data[off + 8:off + 16], "little"))
        if tag == 0:
            break
        entries.append((tag, val))
    strtab_vaddr = next(val for tag, val in entries if tag == 5)   # DT_STRTAB
    strtab = next(off + strtab_vaddr - vaddr for vaddr, off, size in loads
                  if vaddr <= strtab_vaddr < vaddr + size)
    return [data[strtab + val:].split(b"\0", 1)[0].decode()
            for tag, val in entries if tag == 1]                    # DT_NEEDED


def test_declarative_target_pins_architecture_map_and_boot_chain():
    """@requires DE-L0-IN-001 DE-L0-BUILD-001 DE-L0-MAP-001"""
    target = yaml.safe_load(TARGET.read_text(encoding="utf-8"))
    linux = target["linux"]
    rootfs = linux["rootfs"]

    assert target["memory_map"] == "sw/linux/de25/system_memory_map.yml"
    memory_map = yaml.safe_load(
        (ROOT / target["memory_map"]).read_text(encoding="utf-8")
    )
    assert memory_map["target"] == "agilex5-hps"
    # DS-T2.71 added canfd0/canfd1. This list is not decoration: it IS the
    # fabric decode map that tools/de25_linux_image.py crosschecks every
    # enabled linux.dt_nodes reg window against, so a window added here
    # without a fabric slave (or removed from under a live dt_node) is
    # exactly the boot7/boot8 NoC wedge. Pinned exhaustively and in order.
    assert [(d["base_addr"], d["span"]) for d in memory_map["devices"]] == [
        (0x20000000, 0x1000), (0xA0000000, 0x10000000),
        (0x20010000, 0x10000), (0x20020000, 0x10000),
    ]
    # The two CAN windows are the lwh2f fabric endpoints the ctucanfd driver
    # probes, not userspace UIO mappings.
    for name in ("canfd0", "canfd1"):
        device = next(d for d in memory_map["devices"] if d["name"] == name)
        assert device["requires_bridge"] == "lwh2f"
        assert device["uio"] is False
    assert linux["gsrd_version"] == "25.3"
    assert linux["sdcard"] == {
        "config_order": "fpga-first", "rootfs": "partition", "rootfs_mib": 0,
    }, (
        "sdcard.rootfs must be 'partition' so the image carries a real P2 ext4 "
        "rootfs (the embedded-initramfs default emits a single FAT32 partition "
        "with no rootfs at all — the DS-P1.49 defect); rootfs_mib: 0 autosizes "
        "it (the fixed 256 MiB default does not fit the staged rootfs, measured)"
    )
    assert linux["hps_fpga_bridges"] == "all"
    assert rootfs == {
        "python_overlay": "sw/linux/de25/python_overlay",
        "stage_command": (
            "./sw/linux/stage_daemon.sh && python3 tools/de25_linux_image.py stage"
        ),
    }
    assert rootfs["python_overlay"] != "sw/linux/python_overlay", (
        "the ARMv7 Xilinx overlay entered an AArch64 image"
    )
    assert "ds_mmio_base=0x20000000" in linux["bootargs"]


def test_dt_contract_reserves_ring_and_routes_live_peripherals():
    """@requires DE-L0-MAP-001 DE-L0-NEG-001"""
    linux = yaml.safe_load(TARGET.read_text(encoding="utf-8"))["linux"]
    nodes = {node["path"]: node["properties"] for node in linux["dt_nodes"]}

    assert nodes["/reserved-memory/deepskopion-ring@a0000000"] == {
        "reg": [0, 0xA0000000, 0, 0x10000000], "no-map": ""
    }
    assert nodes["/soc@0/ring-watermark@20000000"]["interrupts"] == [0, 17, 4]
    # DS-D.43: the default target is CAN-free; the CTU nodes live in the opt-in
    # de25_canfd target (the next test), so a default image never declares them.
    assert "/soc@0/can@20010000" not in nodes
    assert "/soc@0/can@20020000" not in nodes
    assert nodes["/soc@0/ethernet@10810000"]["status"] == "okay"
    assert nodes["/soc@0/ethernet@10810000/mdio0/ethernet-phy@0"]["reg"] == 1
    assert nodes["/soc@0/nand-controller@10b80000"]["status"] == "disabled"
    assert "ethernet@10830000" in linux["dtb_disable_nodes"]
    assert "nand-controller@10b80000" not in linux["dtb_disable_nodes"]


def test_stage_command_emits_self_contained_arch_neutral_userspace(tmp_path):
    """@requires DE-L0-IN-001 DE-L0-BUILD-001"""
    output = tmp_path / "rootfs_overlay"
    run = _run_stage(output)
    assert run.returncode == 0, run.stdout + run.stderr
    assert (output / "lib/python3.13/site-packages/msgpack/fallback.py").is_file()
    assert (output / "usr/local/share/deepskopion/sw/stream/__main__.py").is_file()
    assert (output / "usr/local/share/deepskopion/contracts/wave_stream_v1/wave_stream_contract.py").is_file()
    python = output.parent / "python_overlay/bin/python3"
    elf = python.read_bytes()[:20]
    assert elf[:4] == b"\x7fELF" and elf[4] == 2, "Python is not ELF64"
    assert int.from_bytes(elf[18:20], "little") == 183, "Python is not AArch64"
    init = (output / "etc/init.d/S99deepskopion-stream").read_text(encoding="utf-8")
    assert "ds_mmio_base=" in init and "--mmio-base" in init

    # A target-specific overlay must contain no native ARMv7 payload.  Python
    # itself is supplied later by rr's checksum-pinned AArch64 overlay; every
    # file emitted by this stage is script/data and therefore architecture-free.
    for path in output.rglob("*"):
        if path.is_file():
            assert not path.read_bytes().startswith(b"\x7fELF"), f"native binary leaked into DE25 overlay: {path}"

    # docs/REDISTRIBUTION.md lists every ELF file the image's Python overlay
    # ships, by image path; a binary a new upstream release adds turns this red.
    assert de25_linux_image.overlay_inventory_mismatch(
        output.parent / "python_overlay", output, ROOT / "docs/REDISTRIBUTION.md") == []


def test_stage_installs_the_native_edge_library_built_for_the_hps(tmp_path):
    """DS-P2.210: the daemon's native edge path ships in the image. The stage
    builds sw/stream/native/edge_drain.c with the pinned aarch64 toolchain and
    installs it in the Python overlay where sw/stream/native/edge_frames.py
    looks (SYSTEM_LIB), with its sha256 in etc/deepskopion-inputs."""
    import hashlib
    from sw.stream.native import edge_frames

    output = tmp_path / "rootfs_overlay"
    run = _run_stage(output)
    assert run.returncode == 0, run.stdout + run.stderr

    rel = de25_linux_image.NATIVE_EDGE_LIB_REL
    assert edge_frames.SYSTEM_LIB == Path("/" + rel), "the daemon would not find it"
    python_overlay = output.parent / "python_overlay"
    lib = python_overlay / rel
    assert lib.is_file(), f"{rel} is not installed in the Python overlay"
    data = lib.read_bytes()
    assert data[:4] == b"\x7fELF" and data[4] == 2, "not ELF64"
    assert int.from_bytes(data[16:18], "little") == 3, "not a shared object (ET_DYN)"
    assert int.from_bytes(data[18:20], "little") == 183, "not AArch64"
    needed = _elf_needed(data)
    assert set(needed) <= {"libc.so.6", "libm.so.6"}, needed
    for name in needed:
        assert (python_overlay / "lib" / name).is_file(), f"{name} is not in the image"

    inputs = dict(line.split("=", 1) for line in
                  (output / "etc/deepskopion-inputs").read_text(encoding="utf-8").splitlines())
    assert inputs["native_edge_lib"] == "/" + rel
    assert inputs["native_edge_lib_sha256"] == hashlib.sha256(data).hexdigest()
    assert inputs["native_edge_source_sha256"] == de25_linux_image.native_edge_source_sha256(ROOT)
    assert inputs["native_edge_toolchain_sha256"] == de25_linux_image.de25_toolchain_stage.TOOLCHAIN_SHA256


def test_stage_installs_the_socketcan_tools_built_for_the_hps(tmp_path):
    """DS-P2.227: de25_canfd ships two CTU CAN FD controllers, so the image
    carries the standard tools to configure and drive them: iproute2's ip (with
    CAN link support) and can-utils' cansend, candump and cangen, cross-built
    from the pinned sources with the pinned toolchain. Each loads with the
    image's own glibc, its sha256 is in etc/deepskopion-inputs, and the
    interpreter guard refuses it when the loader is missing."""
    import hashlib

    output = tmp_path / "rootfs_overlay"
    run = _run_stage(output)
    assert run.returncode == 0, run.stdout + run.stderr

    tools = de25_linux_image.CAN_TOOLS
    assert set(tools) == {"usr/sbin/ip", "usr/bin/cansend", "usr/bin/candump",
                          "usr/bin/cangen"}
    python_overlay = output.parent / "python_overlay"
    stamp = (output / "etc/deepskopion-inputs").read_text(encoding="utf-8").splitlines()
    for rel in tools:
        path = python_overlay / rel
        assert path.is_file(), f"/{rel} is not installed in the Python overlay"
        assert path.stat().st_mode & 0o777 == 0o755, oct(path.stat().st_mode)
        data = path.read_bytes()
        assert data[:4] == b"\x7fELF" and data[4] == 2, f"/{rel} is not ELF64"
        assert int.from_bytes(data[18:20], "little") == 183, f"/{rel} is not AArch64"
        assert de25_linux_image._elf_dyn_interp(data)[1] == "/lib/ld-linux-aarch64.so.1"
        needed = _elf_needed(data)
        assert needed == ["libc.so.6"], (rel, needed)
        assert (python_overlay / "lib/libc.so.6").is_file()
        assert (python_overlay / "lib/ld-linux-aarch64.so.1").is_file()
        assert f"can_tool=/{rel} {hashlib.sha256(data).hexdigest()}" in stamp
    # the full ip, with the CAN link type: busybox's ip applet has neither
    ip = (python_overlay / "usr/sbin/ip").read_bytes()
    assert b"dbitrate" in ip and b"ip utility, iproute2-%s" in ip
    assert b"\0" + de25_linux_image.IPROUTE2_VERSION.encode() + b"\0" in ip
    assert f"iproute2_sha256={de25_linux_image.IPROUTE2_SHA256}" in stamp
    assert f"can_utils_sha256={de25_linux_image.CAN_UTILS_SHA256}" in stamp

    # The interpreter guard covers these non-PIE (ET_EXEC) programs: the same
    # ip staged without its loader is refused.
    assert int.from_bytes(ip[16:18], "little") == 2, "ip is expected to be ET_EXEC here"
    bare = tmp_path / "bare"
    (bare / "usr/sbin").mkdir(parents=True)
    (bare / "usr/sbin/ip").write_bytes(ip)
    with pytest.raises(RuntimeError, match="needs ELF interpreter"):
        de25_linux_image.assert_dynamic_executables_have_interpreters(bare)

    # check_hps_executable refuses a library the image lacks and a foreign loader.
    de25_linux_image.check_hps_executable(ip, "ip")
    foreign_lib = ip.replace(b"libc.so.6\0", b"libq.so.6\0", 1)
    with pytest.raises(RuntimeError, match=r"needs \['libq.so.6'\]"):
        de25_linux_image.check_hps_executable(foreign_lib, "ip")
    foreign_loader = ip.replace(b"/lib/ld-linux-aarch64.so.1\0",
                                b"/lib/ld-linux-aarch64.so.9\0", 1)
    with pytest.raises(RuntimeError, match="ELF interpreter"):
        de25_linux_image.check_hps_executable(foreign_loader, "ip")


def test_check_hps_executable_refuses_a_host_binary():
    """DS-P2.227: a tool built by the host compiler by mistake fails the stage."""
    host = Path(sys.executable).resolve().read_bytes()
    assert int.from_bytes(host[18:20], "little") != 183
    with pytest.raises(RuntimeError, match="not an AArch64 ELF64"):
        de25_linux_image.check_hps_executable(host, "host python")


def test_socketcan_sources_are_pinned_by_url_and_sha256():
    """DS-P2.227: both source archives are fetched by exact URL and verified."""
    m = de25_linux_image
    assert m.IPROUTE2_URL.endswith("/" + m.IPROUTE2_ARCHIVE)
    assert m.IPROUTE2_URL.startswith("https://mirrors.edge.kernel.org/")
    assert m.CAN_UTILS_URL.startswith("https://github.com/linux-can/can-utils/")
    assert m.CAN_UTILS_URL.endswith(f"/v{m.CAN_UTILS_VERSION}.tar.gz")
    for digest in (m.IPROUTE2_SHA256, m.CAN_UTILS_SHA256):
        assert re.fullmatch(r"[0-9a-f]{64}", digest), digest


def test_corrupt_pinned_input_fails_without_replacing_previous_overlay(tmp_path):
    """@requires DE-L0-NEG-002"""
    output = tmp_path / "rootfs_overlay"
    cache = _private_cache(tmp_path, "msgpack-1.1.1.tar.gz")
    first = _run_stage(output, cache)
    assert first.returncode == 0, first.stdout + first.stderr
    sentinel = output / "last-known-good"
    sentinel.write_text("keep", encoding="utf-8")

    archive = cache / "msgpack-1.1.1.tar.gz"
    damaged = bytearray(archive.read_bytes())
    damaged[len(damaged) // 2] ^= 0xFF
    archive.write_bytes(damaged)
    second = _run_stage(output, cache)

    assert second.returncode == 1, second.stdout + second.stderr
    assert "corrupt pinned input" in second.stderr
    assert "Refusing to replace" in second.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_staged_overlay_passes_the_public_artifact_scanner(tmp_path):
    """@requires DE-L0-NEG-001"""
    output = tmp_path / "rootfs_overlay"
    staged = _run_stage(output)
    assert staged.returncode == 0, staged.stdout + staged.stderr
    scan = subprocess.run(
        [sys.executable, str(ROOT / "tools/public_artifact_hygiene.py"),
         "--no-index", str(output)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert scan.returncode == 0, scan.stdout + scan.stderr


def test_stage_end_to_end_produces_a_bootable_python_with_glibc_runtime(tmp_path):
    """DS-P1.57 end-to-end. MEASURED on silicon (boot21): the shipped overlay
    staged a dynamically linked (ET_DYN) python3 with no ELF interpreter and
    no libc anywhere in the rootfs — `find / -name "ld-linux*"` came back
    EMPTY on the board, so every dynamically linked binary failed exec with
    "not found", and only busybox (statically linked) worked. This is the
    real ``stage`` CLI, so it also exercises the new glibc-runtime download
    (~145 MB from the already-pinned ARM GNU toolchain archive) alongside the
    existing CPython/msgpack downloads — this test needs network access and
    cannot run in an offline/air-gapped lane, same as the pre-existing
    ``test_stage_command_emits_self_contained_arch_neutral_userspace`` above.
    """
    output = tmp_path / "rootfs_overlay"
    run = _run_stage(output)
    assert run.returncode == 0, run.stdout + run.stderr

    python_overlay = output.parent / "python_overlay"
    ld_linux = python_overlay / "lib/ld-linux-aarch64.so.1"
    assert ld_linux.is_file(), "no ELF interpreter staged — the DS-P1.57 defect"
    assert ld_linux.read_bytes()[:4] == b"\x7fELF"
    for name in ("libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2",
                 "librt.so.1", "libutil.so.1"):
        lib = python_overlay / "lib" / name
        assert lib.is_file(), f"{lib} missing — python3 would still fail to exec"
        assert lib.read_bytes()[:4] == b"\x7fELF"

    # The property that actually matters (the ticket's own bar): every
    # ET_DYN executable this stage produced can resolve its own PT_INTERP
    # from files THIS SAME overlay ships, exactly what the board merges onto
    # its rootfs (ticket-measured "ls /lib/libpython3.13.so* -> PRESENT").
    de25_linux_image.assert_dynamic_executables_have_interpreters(python_overlay)


def test_regression_guard_catches_the_measured_silicon_defect(tmp_path):
    """DS-P1.57 regression-guard power proof, hermetic (no network).

    BASELINE: reproduce the EXACT shipped defect state (an ET_DYN python3
    binary staged with no ELF interpreter anywhere under the overlay — the
    committed sw/linux/de25/python_overlay/ ticket described) and show the
    guard catches it.

    MUTATION: stage the interpreter (what the DS-P1.57 fix now does) and show
    the SAME guard goes quiet — proving it is not merely "always fails" but
    actually keys off the one thing that matters (the interpreter's presence
    at the exact path PT_INTERP names).

    A real statically linked ET_EXEC binary (busybox's own shape) is also
    checked to prove the guard does not merely flag every dynamic-looking
    file — it must leave a binary with no PT_INTERP segment alone.
    """
    root = tmp_path / "rootfs"
    dynamic = root / "bin/python3.13"
    dynamic.parent.mkdir(parents=True)
    dynamic.write_bytes(_make_elf64(is_dyn=True, interp="/lib/ld-linux-aarch64.so.1"))

    static = root / "bin/busybox"
    static.write_bytes(_make_elf64(is_dyn=False, interp=None))

    # BASELINE — the measured defect: ET_DYN staged, its interpreter is not.
    with pytest.raises(RuntimeError) as excinfo:
        de25_linux_image.assert_dynamic_executables_have_interpreters(root)
    assert "bin/python3.13" in str(excinfo.value)
    assert "ld-linux-aarch64.so.1" in str(excinfo.value)

    # A file-presence-only check (e.g. "does python3 exist") would have
    # passed on the shipped, broken overlay — that is precisely the class of
    # test the ticket says does NOT catch this. Confirm the file was there
    # all along; what was missing is its interpreter, not itself.
    assert dynamic.is_file()

    # MUTATION (the fix): stage the interpreter at the exact PT_INTERP path.
    interp_path = root / "lib/ld-linux-aarch64.so.1"
    interp_path.parent.mkdir(parents=True)
    interp_path.write_bytes(b"\x7fELF\x02\x01" + b"\x00" * 10)
    de25_linux_image.assert_dynamic_executables_have_interpreters(root)  # no raise

    # Un-staging it again (deleting the fix) reproduces the baseline exactly
    # — the guard is not vacuous once satisfied.
    interp_path.unlink()
    with pytest.raises(RuntimeError):
        de25_linux_image.assert_dynamic_executables_have_interpreters(root)


# DS-P2.94: the exact GSRD QPDS25.3_REL_GSRD_PR default for the DE25's EMAC0
# (gmac0), read directly from arch/arm/dts/socfpga_agilex5_socdk-u-boot.dtsi
# rather than guessed — the shipped defect state before any patch runs.
_UBOOT_GSRD_GMAC0_FIXTURE = """\
&gmac0 {
	status = "okay";
	phy-mode = "rgmii";
	phy-handle = <&emac0_phy0>;

	max-frame-size = <9000>;

	mdio0 {
		#address-cells = <1>;
		#size-cells = <0>;
		compatible = "snps,dwxgmac-mdio";
		emac0_phy0: ethernet-phy@0 {
			reg = <0>;
		};
	};
};
"""


def _write_uboot_control_dtsi_fixture(uboot_dir: Path) -> Path:
    dtsi = uboot_dir / "arch/arm/dts/socfpga_agilex5_socdk-u-boot.dtsi"
    dtsi.parent.mkdir(parents=True, exist_ok=True)
    dtsi.write_text(_UBOOT_GSRD_GMAC0_FIXTURE, encoding="utf-8")
    return dtsi


def _build_fdt_blob(tree: dict) -> bytes:
    """Minimal flattened-devicetree (FDT) blob builder for test fixtures —
    the write-side counterpart of ``de25_linux_image._iter_fdt_props``.
    ``tree`` is ``{"props": {name: bytes}, "children": {name: tree}}``. No
    ``dtc``/``libfdt`` dependency, so these tests stay hermetic.
    """
    strings = bytearray()
    string_offsets: dict[str, int] = {}

    def str_off(name: str) -> int:
        if name not in string_offsets:
            string_offsets[name] = len(strings)
            strings.extend(name.encode("ascii") + b"\x00")
        return string_offsets[name]

    struct_block = bytearray()

    def emit_node(name: str, node: dict) -> None:
        struct_block.extend(struct.pack(">I", 1))  # FDT_BEGIN_NODE
        raw_name = name.encode("utf-8") + b"\x00"
        struct_block.extend(raw_name)
        struct_block.extend(b"\x00" * ((-len(raw_name)) % 4))
        for prop_name, value in node.get("props", {}).items():
            off = str_off(prop_name)
            struct_block.extend(struct.pack(">I", 3))  # FDT_PROP
            struct_block.extend(struct.pack(">II", len(value), off))
            struct_block.extend(value)
            struct_block.extend(b"\x00" * ((-len(value)) % 4))
        for child_name, child in node.get("children", {}).items():
            emit_node(child_name, child)
        struct_block.extend(struct.pack(">I", 2))  # FDT_END_NODE

    emit_node("", tree)
    struct_block.extend(struct.pack(">I", 9))  # FDT_END

    off_mem_rsvmap = 40
    rsvmap = struct.pack(">QQ", 0, 0)
    off_dt_struct = off_mem_rsvmap + len(rsvmap)
    off_dt_strings = off_dt_struct + len(struct_block)
    header = struct.pack(
        ">10I", 0xD00DFEED, off_dt_strings + len(strings), off_dt_struct,
        off_dt_strings, off_mem_rsvmap, 17, 16, 0, len(strings),
        len(struct_block),
    )
    return header + rsvmap + bytes(struct_block) + bytes(strings)


def _nested_tree(path_parts: list[str], leaf: dict) -> dict:
    """Build the ``_build_fdt_blob`` tree shape for a single node at
    ``path_parts`` (e.g. ``["soc", "ethernet@10810000", "mdio0",
    "ethernet-phy@1"]``) carrying ``leaf`` (typically ``{"props": {...}}``)."""
    tree = leaf
    for part in reversed(path_parts):
        tree = {"children": {part: tree}}
    return tree


def _build_fake_uboot_itb_bytes(*, phy_reg: int = 1, external: bool = False) -> bytes:
    """A synthetic FIT shaped just enough like the real ``u-boot.itb`` for
    ``de25_linux_image._assert_uboot_itb_carries_emac0_phy_fix`` to inspect:
    an ``/images/fdt-1`` node embedding the U-Boot control DTB's EMAC0 phy
    node at the DE25's real node path
    (``de25_linux_image.UBOOT_EMAC0_PHY_NODE_PATH`` — built from that
    constant, not a hardcoded duplicate of it, so this fixture cannot drift
    out of sync with the path the production code actually searches).

    ``external=False`` (default) inlines the control DTB in a ``data``
    property, matching ``mkimage``'s non-external-data default.
    ``external=True`` instead uses ``data-offset``/``data-size`` — the shape
    a REAL binman-built FIT uses (measured: the real 937,888-byte de25
    ``u-boot.itb``'s own structure block is only 1,216 bytes; every image's
    bytes live after it, referenced by offset).
    """
    control_dtb = _build_fdt_blob(_nested_tree(
        de25_linux_image.UBOOT_EMAC0_PHY_NODE_PATH.strip("/").split("/"),
        {"props": {"reg": struct.pack(">I", phy_reg)}},
    ))
    if not external:
        return _build_fdt_blob({
            "children": {"images": {"children": {
                "fdt-1": {"props": {"data": control_dtb}},
            }}},
        })
    header_only = _build_fdt_blob({
        "children": {"images": {"children": {
            "fdt-1": {"props": {
                "data-offset": struct.pack(">I", 0),
                "data-size": struct.pack(">I", len(control_dtb)),
            }},
        }}},
    })
    (totalsize,) = struct.unpack_from(">I", header_only, 4)
    return header_only[:totalsize] + control_dtb


@requires("DE-L0-BUILD-004")
def test_patch_uboot_control_dtb_emac0_phy_overrides_ksz9031_mdio_address_ds_p2_94(
    tmp_path,
):
    """DS-P2.94 regression-guard power proof, hermetic (no network, no dtc).

    BASELINE: the fixture reproduces the exact measured defect state — the
    GSRD default control DTB source declares the EMAC0 MDIO child at
    ``ethernet-phy@0``/reg=<0>, the address U-Boot's phy_connect() then
    probes on real hardware ("addr 0 / phy_connect() failed" — the DE25's
    KSZ9031 answers at 1, not 0). Confirm that state BEFORE patching.

    MUTATION power: the assertion pins the exact renamed node text (unit
    address AND reg change together, to ``ethernet-phy@1``/reg=<1>), so a
    patch that targets the wrong node, renames only one of the two, or
    writes any reg value other than 1, fails this test — not merely "some
    text changed". The renamed shape (not an appended override fragment
    leaving ``ethernet-phy@0``/reg=<1> — a unit-address/reg mismatch) is
    itself the DS-P2.94 landing-refusal `unit_address_vs_reg` fix.
    """
    uboot_dir = tmp_path / "u-boot-socfpga"
    dtsi = _write_uboot_control_dtsi_fixture(uboot_dir)

    baseline = dtsi.read_text(encoding="utf-8")
    assert de25_linux_image._UBOOT_EMAC0_PHY_BASE_NODE in baseline, (
        "fixture must reproduce the shipped GSRD default"
    )
    assert de25_linux_image._UBOOT_EMAC0_PHY_PATCHED_NODE not in baseline

    de25_linux_image.patch_uboot_control_dtb_emac0_phy(uboot_dir)
    patched = dtsi.read_text(encoding="utf-8")

    assert de25_linux_image._UBOOT_EMAC0_PHY_PATCHED_NODE in patched, (
        "the patch must rename the DE25's real EMAC0 mdio child to "
        "ethernet-phy@1 with reg=1, mirroring the kernel-side dt_nodes fix "
        "for the same KSZ9031 strap address"
    )
    # In-place rewrite, not an additive override: the GSRD default text is
    # GONE, not merely superseded by a later stanza.
    assert de25_linux_image._UBOOT_EMAC0_PHY_BASE_NODE not in patched
    assert "ethernet-phy@0" not in patched, (
        "no ethernet-phy@0/reg=1 mismatch may remain anywhere in the file"
    )

    # Idempotent: re-running (e.g. a second `rr linux uboot --refresh`-style
    # rebuild) must not accumulate a second copy or corrupt the file.
    de25_linux_image.patch_uboot_control_dtb_emac0_phy(uboot_dir)
    reapplied = dtsi.read_text(encoding="utf-8")
    assert reapplied.count(de25_linux_image._UBOOT_EMAC0_PHY_PATCHED_NODE) == 1
    assert reapplied == patched


@requires("DE-L0-BUILD-004")
def test_patch_uboot_control_dtb_emac0_phy_fails_closed_without_source_ds_p2_94(
    tmp_path,
):
    """If `rr linux uboot` has never cloned u-boot-socfpga, the control-DTB
    source this patch needs simply is not there. Silently doing nothing
    would ship the unpatched GSRD default with no signal; this must fail
    closed instead.
    """
    uboot_dir = tmp_path / "u-boot-socfpga"
    with pytest.raises(RuntimeError, match="control DTB source missing"):
        de25_linux_image.patch_uboot_control_dtb_emac0_phy(uboot_dir)


@requires("DE-L0-BUILD-004", "DE-L0-NEG-006")
def test_rebuild_uboot_with_emac0_phy_fix_patches_then_rebuilds_ds_p2_94(
    monkeypatch, tmp_path,
):
    """BASELINE: before this ticket, `rr linux uboot` alone shipped u-boot.itb
    built from the unpatched GSRD control DTB (defect reproduced by the
    previous test). This test proves the caller-level rebuild step (1)
    patches the control DTB BEFORE invoking `make`, and (2) actually ships
    the rebuilt artifacts into out/ — not just a helper nobody called.

    No real aarch64 cross-compiler runs here (unavailable in this
    environment); `make` is faked to drop placeholder artifacts, matching
    this suite's existing convention (test_build_leaves_uboot_env_absent...
    fakes `rr`/`mkenvimage` the same way for the same reason).
    """
    root = tmp_path / "project"
    _write_project_pointer(root)
    uboot_dir = root / "sw/linux/de25/src/u-boot-socfpga"
    out_dir = root / "sw/linux/de25/out/de25_datalogger"
    out_dir.mkdir(parents=True)
    dtsi = _write_uboot_control_dtsi_fixture(uboot_dir)
    (uboot_dir / "bl31.bin").write_bytes(b"bl31")

    make_calls = []
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        if argv[0] == "make":
            make_calls.append(argv)
            # The real build only re-emits u-boot.itb etc if the dtsi was
            # already patched — assert the ordering the ticket requires.
            assert de25_linux_image._UBOOT_EMAC0_PHY_PATCHED_NODE in dtsi.read_text(
                encoding="utf-8"
            ), "make ran before the EMAC0 PHY fix was patched into the control DTB"
            for src_rel in de25_linux_image.UBOOT_REBUILD_COPIES:
                artifact = uboot_dir / src_rel
                artifact.parent.mkdir(parents=True, exist_ok=True)
                if src_rel == "u-boot.itb":
                    # A real `make` relinks the FIT from the just-patched
                    # control DTB — model that rather than an opaque
                    # placeholder, so this test can prove the SHIPPED FIT
                    # actually carries the fix, not just that a same-named
                    # file exists.
                    artifact.write_bytes(_build_fake_uboot_itb_bytes(phy_reg=1))
                else:
                    artifact.write_bytes(f"rebuilt:{src_rel}".encode())
            class Done:
                returncode = 0
            return Done()
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(de25_linux_image.subprocess, "run", fake_run)
    de25_linux_image.rebuild_uboot_with_emac0_phy_fix(root)

    assert len(make_calls) == 1
    assert de25_linux_image._UBOOT_EMAC0_PHY_PATCHED_NODE in dtsi.read_text(encoding="utf-8")
    for src_rel, dst_name in de25_linux_image.UBOOT_REBUILD_COPIES.items():
        shipped = out_dir / dst_name
        assert shipped.is_file(), f"{dst_name} not shipped into out/"
        if src_rel == "u-boot.itb":
            assert shipped.read_bytes() == _build_fake_uboot_itb_bytes(phy_reg=1)
        else:
            assert shipped.read_bytes() == f"rebuilt:{src_rel}".encode()


@requires("DE-L0-NEG-006")
def test_rebuild_uboot_with_emac0_phy_fix_fails_closed_without_bl31_ds_p2_94(
    monkeypatch, tmp_path,
):
    """MUTATION: if bl31.bin (the binman input `rr linux uboot` stages) is
    missing, rebuilding would either crash deep inside binman or silently
    reuse a stale FIT. Fail closed before `make` even runs, and before the
    control DTB is touched.
    """
    root = tmp_path / "project"
    _write_project_pointer(root)
    uboot_dir = root / "sw/linux/de25/src/u-boot-socfpga"
    (root / "sw/linux/de25/out/de25_datalogger").mkdir(parents=True)
    dtsi = _write_uboot_control_dtsi_fixture(uboot_dir)

    called = []
    monkeypatch.setattr(
        de25_linux_image.subprocess, "run",
        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
            AssertionError("make must not run without bl31.bin")
        ),
    )
    with pytest.raises(RuntimeError, match="bl31.bin"):
        de25_linux_image.rebuild_uboot_with_emac0_phy_fix(root)
    assert not called
    assert de25_linux_image._UBOOT_EMAC0_PHY_PATCHED_NODE not in dtsi.read_text(encoding="utf-8")


@requires("DE-L0-NEG-006")
def test_rebuild_uboot_with_emac0_phy_fix_fails_closed_on_noop_make_ds_p2_94(
    monkeypatch, tmp_path,
):
    """Reproduces the landing-refusal finding at (the old)
    tools/de25_linux_image.py:245: a `make` that exits 0 without actually
    relinking anything must not be trusted just because a PRIOR build left
    the expected artifacts sitting in the u-boot tree.

    BASELINE (pre-fix `rebuild_uboot_with_emac0_phy_fix`): this test fails —
    the stale `u-boot.itb` from a previous build is silently copied into
    out/ and reported as success, exactly the "stale reg=0 FIT shipped"
    defect the reviewer reproduced.
    """
    root = tmp_path / "project"
    _write_project_pointer(root)
    uboot_dir = root / "sw/linux/de25/src/u-boot-socfpga"
    out_dir = root / "sw/linux/de25/out/de25_datalogger"
    out_dir.mkdir(parents=True)
    _write_uboot_control_dtsi_fixture(uboot_dir)
    (uboot_dir / "bl31.bin").write_bytes(b"bl31")

    # Stale artifacts already present from a prior (real) build — the state
    # a no-op `make` would leave untouched.
    for src_rel in de25_linux_image.UBOOT_REBUILD_COPIES:
        artifact = uboot_dir / src_rel
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(f"stale:{src_rel}".encode())

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        if argv[0] == "make":
            class Done:
                returncode = 0
            return Done()
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(de25_linux_image.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="missing output"):
        de25_linux_image.rebuild_uboot_with_emac0_phy_fix(root)
    assert not (out_dir / "u-boot.itb").exists(), (
        "a no-op make must not result in a stale artifact being shipped "
        "into out/"
    )


def _fake_rebuild_setup(uboot_dir: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dtsi = _write_uboot_control_dtsi_fixture(uboot_dir)
    (uboot_dir / "bl31.bin").write_bytes(b"bl31")
    return dtsi


@requires("DE-L0-NEG-006")
def test_rebuild_uboot_with_emac0_phy_fix_fails_closed_on_invalid_fit_ds_p2_94(
    monkeypatch, tmp_path,
):
    """Reproduces the round-2 landing-refusal finding at (the old)
    tools/de25_linux_image.py:253: the prior check only asked "does a file
    named u-boot.itb exist", so ``rebuild_uboot_with_emac0_phy_fix`` shipped
    the mapped test's own fixture — an 18-byte ASCII placeholder,
    ``b"rebuilt:u-boot.itb"`` — as if it were a real FIT.

    BASELINE (pre-fix ``rebuild_uboot_with_emac0_phy_fix``, i.e. before the
    ``_assert_uboot_itb_carries_emac0_phy_fix`` guard existed): this exact
    18-byte fixture is shipped into ``out/`` and reported as success — the
    invalid-artifact defect the reviewer reproduced.

    MUTATION: the guard must be the thing that fails this — comment out the
    ``_assert_uboot_itb_carries_emac0_phy_fix(...)`` call in
    ``rebuild_uboot_with_emac0_phy_fix`` and this test goes green on a
    placeholder, which is exactly the escaped defect.
    """
    root = tmp_path / "project"
    _write_project_pointer(root)
    uboot_dir = root / "sw/linux/de25/src/u-boot-socfpga"
    out_dir = root / "sw/linux/de25/out/de25_datalogger"
    _fake_rebuild_setup(uboot_dir, out_dir)

    def fake_run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        assert argv[0] == "make"
        for src_rel in de25_linux_image.UBOOT_REBUILD_COPIES:
            artifact = uboot_dir / src_rel
            artifact.parent.mkdir(parents=True, exist_ok=True)
            # The exact 18-byte ASCII placeholder the landing refusal named.
            artifact.write_bytes(f"rebuilt:{src_rel}".encode())
        assert len((uboot_dir / "u-boot.itb").read_bytes()) == 18
        class Done:
            returncode = 0
        return Done()

    monkeypatch.setattr(de25_linux_image.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="not a valid FIT"):
        de25_linux_image.rebuild_uboot_with_emac0_phy_fix(root)
    assert not (out_dir / "u-boot.itb").exists(), (
        "an invalid (non-FIT) artifact must never be shipped into out/"
    )


@requires("DE-L0-NEG-006")
def test_rebuild_uboot_with_emac0_phy_fix_fails_closed_on_standalone_control_dtb_ds_p2_94(
    monkeypatch, tmp_path,
):
    """Reproduces the landing-refusal finding at (the prior)
    tools/de25_linux_image.py:317: the guard used to inspect the top-level
    blob itself as a control DTB before ever proving it was a FIT. A bare
    172-byte standalone FDT — no ``/images`` node, no FIT structure at all
    — that happens to plant ``UBOOT_EMAC0_PHY_NODE_PATH`` with ``reg=<1>``
    directly at its own top level used to pass validation once renamed
    ``u-boot.itb``, because the old check found the phy node/reg before it
    ever confirmed a FIT shape.

    BASELINE (pre-fix, i.e. before the ``/images``-node structural check
    existed): this standalone FDT is shipped as if it were a real FIT.

    MUTATION power, precisely stated (a prior version of this docstring
    overclaimed it — landing-refusal finding): deleting ONLY the
    ``has_images_node`` check does NOT turn this test green. The phy search
    below is scoped to resolved ``/images/<name>`` children
    (``_fit_image_data_blobs``); a standalone top-level tree has no node
    path starting with ``images/`` at all, so the search finds nothing
    regardless of ``has_images_node`` and this blob is still rejected (with
    a different message — "does not embed a control DTB" — asserted
    separately below). ``has_images_node`` is a structural check that gives
    a precise "not a FIT" diagnostic; the ``images/*``-scoped search is what
    actually makes the exploit impossible to pass. Deleting the WHOLE
    ``_assert_uboot_itb_carries_emac0_phy_fix`` call (the guard as a unit)
    is what turns this test green — asserted directly, not just claimed.
    """
    root = tmp_path / "project"
    _write_project_pointer(root)
    uboot_dir = root / "sw/linux/de25/src/u-boot-socfpga"
    out_dir = root / "sw/linux/de25/out/de25_datalogger"
    _fake_rebuild_setup(uboot_dir, out_dir)

    standalone_control_dtb = _build_fdt_blob({
        "children": {"soc": {"children": {
            "ethernet@10810000": {"children": {"mdio0": {"children": {
                "ethernet-phy@1": {
                    "props": {"reg": struct.pack(">I", 1)},
                },
            }}}},
        }}},
    })

    def fake_run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        assert argv[0] == "make"
        for src_rel in de25_linux_image.UBOOT_REBUILD_COPIES:
            artifact = uboot_dir / src_rel
            artifact.parent.mkdir(parents=True, exist_ok=True)
            if src_rel == "u-boot.itb":
                artifact.write_bytes(standalone_control_dtb)
            else:
                artifact.write_bytes(f"rebuilt:{src_rel}".encode())
        class Done:
            returncode = 0
        return Done()

    monkeypatch.setattr(de25_linux_image.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="no top-level /images node"):
        de25_linux_image.rebuild_uboot_with_emac0_phy_fix(root)
    assert not (out_dir / "u-boot.itb").exists(), (
        "a standalone control DTB with no FIT /images structure must "
        "never be shipped into out/ just because it happens to carry the "
        "phy node at its own top level"
    )

    # MUTATION power, isolated per-check rather than merely asserted in the
    # docstring above: deleting the has_images_node check on its own still
    # rejects this exact blob (the images/*-scoped search catches it), but
    # deleting the whole guard call ships it.
    itb_path = uboot_dir / "u-boot.itb"
    itb_path.write_bytes(standalone_control_dtb)
    top_level = list(de25_linux_image._iter_fdt_props(standalone_control_dtb))
    reg = None
    for value in de25_linux_image._fit_image_data_blobs(standalone_control_dtb, top_level):
        if len(value) >= 40 and value[:4] == struct.pack(">I", 0xD00DFEED):
            reg = de25_linux_image._find_emac0_phy_reg_cells(value)
    assert reg is None, (
        "has_images_node is NOT what rejects this blob: the images/*-scoped "
        "search alone already finds nothing on a standalone (non-FIT) tree"
    )


@requires("DE-L0-NEG-006")
def test_rebuild_uboot_with_emac0_phy_fix_fails_closed_on_unpatched_phy_reg_ds_p2_94(
    monkeypatch, tmp_path,
):
    """MUTATION of the guarded mechanism itself: a structurally VALID FIT
    whose embedded control DTB still has the GSRD default gmac0 phy
    reg=<0> — e.g. `make` relinked the FIT from a stale compiled `.dtb`
    that never picked up the source patch — must be rejected exactly like
    the invalid-artifact case. Ships nothing but a well-formed FDT and the
    ORIGINAL measured defect (DE-L0-NEG-006).
    """
    root = tmp_path / "project"
    _write_project_pointer(root)
    uboot_dir = root / "sw/linux/de25/src/u-boot-socfpga"
    out_dir = root / "sw/linux/de25/out/de25_datalogger"
    _fake_rebuild_setup(uboot_dir, out_dir)

    def fake_run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        assert argv[0] == "make"
        for src_rel in de25_linux_image.UBOOT_REBUILD_COPIES:
            artifact = uboot_dir / src_rel
            artifact.parent.mkdir(parents=True, exist_ok=True)
            if src_rel == "u-boot.itb":
                artifact.write_bytes(_build_fake_uboot_itb_bytes(phy_reg=0))
            else:
                artifact.write_bytes(f"rebuilt:{src_rel}".encode())
        class Done:
            returncode = 0
        return Done()

    monkeypatch.setattr(de25_linux_image.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="reg=0"):
        de25_linux_image.rebuild_uboot_with_emac0_phy_fix(root)
    assert not (out_dir / "u-boot.itb").exists(), (
        "a FIT whose control DTB still probes the KSZ9031 at address 0 "
        "must never be shipped into out/"
    )


@requires("DE-L0-BUILD-004", "DE-L0-NEG-006")
def test_assert_uboot_itb_carries_emac0_phy_fix_resolves_external_data_fit_ds_p2_94(
    tmp_path,
):
    """DS-P2.94 landing-refusal (round N+1): ``_build_fake_uboot_itb_bytes``'s
    ``external=True`` branch mints a FIT shaped like a REAL binman build
    (``data-offset``/``data-size``, the shape every real ``u-boot.itb`` uses
    — see ``_fit_image_data_blobs``'s docstring), but until this test no
    caller ever passed ``external=True``: every other guard test fed the
    ``data``-inline default, so deleting the external-data resolution at
    ``tools/de25_linux_image.py:367-379`` left the whole always-runnable
    suite green (the landing-refusal's exact finding). This test needs no
    gitignored ``u-boot-socfpga`` checkout or cross-compiler, so it cannot
    skip out of a fresh landed checkout the way
    ``test_rebuild_uboot_with_emac0_phy_fix_ships_a_real_binman_fit_ds_p2_94``
    below can.

    BASELINE: the pre-fix shape (``phy_reg=0``, the measured GSRD default)
    must still be rejected when resolved through ``data-offset``.
    MUTATION: the PHY-fixed shape (``phy_reg=1``) must be accepted — and
    only IS accepted if the external-data branches actually resolve the
    bytes; deleting them makes the guard find no control DTB at all, so this
    line goes red for the wrong reason (``does not embed a control DTB``
    instead of passing).
    """
    itb = tmp_path / "u-boot.itb"

    itb.write_bytes(_build_fake_uboot_itb_bytes(phy_reg=0, external=True))
    with pytest.raises(RuntimeError, match="did not ship"):
        de25_linux_image._assert_uboot_itb_carries_emac0_phy_fix(itb)

    itb.write_bytes(_build_fake_uboot_itb_bytes(phy_reg=1, external=True))
    de25_linux_image._assert_uboot_itb_carries_emac0_phy_fix(itb)  # no raise


@requires("DE-L0-BUILD-004")
def test_rebuild_uboot_with_emac0_phy_fix_passes_bl31_in_make_env_ds_p2_94(
    monkeypatch, tmp_path,
):
    """DS-P2.94 landing-refusal: the incremental rebuild's ``make`` needs
    ``BL31`` set explicitly (tools/de25_linux_image.py:492-495) — without it,
    binman's ``--allow-missing --fake-ext-blobs`` falls back to a bare
    ``bl31.bin`` lookup in the process's cwd, which only happens to resolve
    on a bench that already has a stale ``bl31.bin`` sitting there. Both
    existing rebuild tests faked ``subprocess.run`` in a way that ignores
    ``kwargs["env"]`` entirely, and the real-build test below runs from
    inside ``uboot_dir`` where ``bl31.bin`` is genuinely present either way
    — so neither test would notice ``BL31`` being dropped from the mapping.

    This test does NOT invoke a real ``make``/binman (still faked, same as
    the other hermetic rebuild tests) — it inspects the ``env`` mapping
    ``rebuild_uboot_with_emac0_phy_fix`` actually hands to ``subprocess.run``
    DIRECTLY, which is exactly the dict ``make_env()`` builds in production
    code, not a duplicate of it.

    MUTATION: deleting ``"BL31": str(bl31)`` from the ``make_env(...)`` call
    at tools/de25_linux_image.py:492-495 makes this assertion fail.
    """
    root = tmp_path / "project"
    _write_project_pointer(root)
    uboot_dir = root / "sw/linux/de25/src/u-boot-socfpga"
    out_dir = root / "sw/linux/de25/out/de25_datalogger"
    _fake_rebuild_setup(uboot_dir, out_dir)
    expected_bl31 = str(uboot_dir / "bl31.bin")

    captured_envs = []

    def fake_run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        assert argv[0] == "make"
        captured_envs.append(kwargs.get("env"))
        for src_rel in de25_linux_image.UBOOT_REBUILD_COPIES:
            artifact = uboot_dir / src_rel
            artifact.parent.mkdir(parents=True, exist_ok=True)
            if src_rel == "u-boot.itb":
                artifact.write_bytes(_build_fake_uboot_itb_bytes(phy_reg=1))
            else:
                artifact.write_bytes(f"rebuilt:{src_rel}".encode())
        class Done:
            returncode = 0
        return Done()

    monkeypatch.setattr(de25_linux_image.subprocess, "run", fake_run)
    de25_linux_image.rebuild_uboot_with_emac0_phy_fix(root)

    assert len(captured_envs) == 1
    env = captured_envs[0]
    assert env is not None, "make must run with an explicit env mapping"
    assert env.get("BL31") == expected_bl31, (
        f"make_env() must carry BL31={expected_bl31!r} so binman's "
        "atf-bl31-path input is explicit rather than falling back to a "
        f"bl31.bin found by chance in cwd; got {env.get('BL31')!r}"
    )


@requires("DE-L0-BUILD-004", "DE-L0-NEG-006")
def test_rebuild_uboot_with_emac0_phy_fix_ships_a_real_binman_fit_ds_p2_94(tmp_path):
    """DS-P2.94 landing-refusal (round-N): every other test in this module
    proves guard/patch POWER against a hand-built synthetic FIT
    (``_build_fake_uboot_itb_bytes``) for speed and hermeticity. That is not
    enough on its own: a real binman-built ``u-boot.itb`` never inlines
    image data in a top-level ``data`` property — it uses external
    ``data-offset``/``data-size`` (measured: the real 937,888-byte de25 FIT's
    own structure block is only 1,216 bytes) — and the landing-refusal
    reproduced exactly that gap: a real, PHY-fixed rebuild was rejected
    ("does not embed a control DTB") because the guard only ever read inline
    ``data``. This test performs an ACTUAL ``make`` build against the real,
    already-cloned u-boot-socfpga source tree (DS-P2.92's staged Vitis
    aarch64-linux-gnu-gcc toolchain, reused via the same ``make_env()`` this
    ticket's fix now calls) and asserts the guard on the REAL emitted bytes
    (staged into ``tmp_path`` via ``stage_uboot_build_tree``, DS-T3.36).

    BASELINE: a real rebuild of the UNPATCHED control DTB (the shipped GSRD
    default, reg=<0>) produces a real FIT the guard must still reject.

    MUTATION/fix: the full caller-level ``rebuild_uboot_with_emac0_phy_fix``
    — patch, real ``make``, guard, copy to out/ — must ship a real FIT the
    guard accepts, with the KSZ9031 fix confirmed by decoding the SHIPPED
    bytes directly (not merely "no exception raised").
    """
    root = tmp_path / "project"
    _write_project_pointer(root)
    uboot_dir = root / "sw/linux/de25/src/u-boot-socfpga"
    out_dir = root / "sw/linux/de25/out/de25_datalogger"
    out_dir.mkdir(parents=True)
    de25_linux_image.stage_uboot_build_tree(uboot_dir)
    assert not uboot_dir.is_symlink(), (
        "stage_uboot_build_tree must stage a writable copy, not a symlink"
    )

    dtsi = uboot_dir / "arch/arm/dts/socfpga_agilex5_socdk-u-boot.dtsi"
    text = dtsi.read_text(encoding="utf-8")
    if de25_linux_image._UBOOT_EMAC0_PHY_PATCHED_NODE in text:
        text = text.replace(
            de25_linux_image._UBOOT_EMAC0_PHY_PATCHED_NODE,
            de25_linux_image._UBOOT_EMAC0_PHY_BASE_NODE, 1,
        )
        dtsi.write_text(text, encoding="utf-8")
    assert de25_linux_image._UBOOT_EMAC0_PHY_BASE_NODE in dtsi.read_text(encoding="utf-8")

    from sdk.cli.commands.linux_steps._cross_compile import make_env
    for src_rel in de25_linux_image.UBOOT_REBUILD_COPIES:
        (uboot_dir / src_rel).unlink(missing_ok=True)
    env = make_env({
        "ARCH": de25_linux_image.UBOOT_ARCH,
        "CROSS_COMPILE": de25_linux_image.UBOOT_CROSS_COMPILE,
        "BL31": str(uboot_dir / "bl31.bin"),
    })
    subprocess.run(["make", f"-j{os.cpu_count() or 1}"], cwd=uboot_dir, env=env,
                    check=True, capture_output=True)
    pre_fix_itb = uboot_dir / "u-boot.itb"
    assert pre_fix_itb.stat().st_size > 100_000, (
        "expected a real, multi-hundred-KB FIT from a real make build, not "
        "a placeholder"
    )
    with pytest.raises(RuntimeError, match="does not embed a control DTB"):
        de25_linux_image._assert_uboot_itb_carries_emac0_phy_fix(pre_fix_itb)

    de25_linux_image.rebuild_uboot_with_emac0_phy_fix(root)

    shipped = out_dir / "u-boot.itb"
    assert shipped.is_file()
    shipped_bytes = shipped.read_bytes()
    assert len(shipped_bytes) > 100_000
    de25_linux_image._assert_uboot_itb_carries_emac0_phy_fix(shipped)  # no raise

    top_level = list(de25_linux_image._iter_fdt_props(shipped_bytes))
    has_images_node = any(
        prop_name is None and node_path == "images"
        for node_path, prop_name, _value in top_level
    )
    assert has_images_node, "shipped bytes must be a real FIT with /images"
    found_reg_1 = False
    for value in de25_linux_image._fit_image_data_blobs(shipped_bytes, top_level):
        if len(value) < 40 or value[:4] != struct.pack(">I", 0xD00DFEED):
            continue
        if de25_linux_image._find_emac0_phy_reg_cells(value) == struct.pack(">I", 1):
            found_reg_1 = True
    assert found_reg_1, (
        "shipped real FIT must embed a control DTB whose "
        f"{de25_linux_image.UBOOT_EMAC0_PHY_NODE_PATH} reg is <1>"
    )


def test_resolve_uboot_src_and_stage_build_tree_ds_t3_36(tmp_path):
    """DS-T3.36: resolve_uboot_src discovers the built u-boot-socfpga tree across
    worktrees (or via search_roots / DS_T3_36_UBOOT_SRC), refuses incomplete
    trees, and stage_uboot_build_tree copies a writable tree into tmp_path."""
    assert not hasattr(sys.modules[__name__], "requires_real_uboot_build"), (
        "requires_real_uboot_build skipif marker must be removed"
    )
    assert getattr(
        test_rebuild_uboot_with_emac0_phy_fix_ships_a_real_binman_fit_ds_p2_94,
        "pytestmark",
        [],
    ) == [], (
        "test_rebuild_uboot_with_emac0_phy_fix_ships_a_real_binman_fit_ds_p2_94 "
        "must not carry skip/skipif markers"
    )

    good_root = tmp_path / "good_repo"
    good_uboot = good_root / "sw/linux/de25/src/u-boot-socfpga"
    for rel in de25_linux_image._UBOOT_SRC_REQUIRED_FILES:
        p = good_uboot / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("fixture\n", encoding="utf-8")
    (good_uboot / ".git").mkdir()
    (good_uboot / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (good_uboot / "doc").mkdir()
    (good_uboot / "doc/README").write_text("docs\n", encoding="utf-8")

    resolved = de25_linux_image.resolve_uboot_src(env={}, search_roots=[good_root])
    assert resolved == good_uboot.resolve(), (
        "resolve_uboot_src did not return the verified u-boot-socfpga directory"
    )

    bad_root = tmp_path / "bad_repo"
    bad_uboot = bad_root / "sw/linux/de25/src/u-boot-socfpga"
    bad_uboot.mkdir(parents=True)
    (bad_uboot / "Makefile").write_text("all:\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing bl31.bin"):
        de25_linux_image.resolve_uboot_src(env={}, search_roots=[bad_root])

    with pytest.raises(RuntimeError, match="not a directory"):
        de25_linux_image.resolve_uboot_src(
            env={de25_linux_image.UBOOT_SRC_ENV: str(tmp_path / "missing_uboot")},
            search_roots=[good_root],
        )

    staged = tmp_path / "staged_uboot"
    out = de25_linux_image.stage_uboot_build_tree(staged, src=good_uboot)
    assert out == staged and staged.is_dir() and not staged.is_symlink()
    assert (staged / "Makefile").is_file()
    assert (staged / "tools/binman/binman").is_file()
    assert not (staged / ".git").exists()
    assert not (staged / "doc").exists()

    real_src = de25_linux_image.resolve_uboot_src(env={})
    assert (real_src / "Makefile").is_file(), real_src
    assert (real_src / "tools/binman/binman").is_file(), real_src


def test_precommit_gate_runs_de25_uboot_binman_fit_unskipped_ds_t3_36(tmp_path):
    """DS-T3.36: hooks/pre-commit.local must run the DE25 U-Boot binman FIT
    rebuild test in the private tree and fail closed on failures and skips."""
    hook_path = ROOT / "hooks/pre-commit.local"
    hook_text = hook_path.read_text(encoding="utf-8")
    m = re.search(
        r"^# ── DS-T3\.36 .*?(?=\n# ── |\Z)",
        hook_text,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None, "DS-T3.36 gate missing from hooks/pre-commit.local"
    snippet = m.group(0)
    assert (
        "sw/linux/de25/test_de25_linux_image.py::"
        "test_rebuild_uboot_with_emac0_phy_fix_ships_a_real_binman_fit_ds_p2_94"
        in snippet
    )
    assert "-rs" in snippet

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / "context.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "context.json"], cwd=str(repo), check=True)

    fake_py = tmp_path / "fake_py"
    fake_py.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$FAKE_PYTEST_OUT"\n'
        'exit "${FAKE_PYTEST_RC:-0}"\n',
        encoding="utf-8",
    )
    fake_py.chmod(0o755)

    def _run_gate(rc: int, out: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-e", "-c", snippet],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "DS_GUARD_PY": str(fake_py),
                "FAKE_PYTEST_RC": str(rc),
                "FAKE_PYTEST_OUT": out,
            },
        )

    ok = _run_gate(0, "3 passed in 0.12s")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "3 passed in 0.12s" in ok.stdout
    assert "✓ DE25 U-Boot binman FIT rebuild passes unskipped (DS-T3.36)" in ok.stdout

    skipped = _run_gate(0, "2 passed, 1 skipped in 0.12s")
    assert skipped.returncode == 1, skipped.stdout + skipped.stderr
    assert "5 = skipped" in skipped.stdout

    failed = _run_gate(1, "1 failed, 2 passed in 0.12s")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "DE25 U-Boot binman FIT rebuild failed (DS-T3.36)" in failed.stdout


def test_stage_glibc_runtime_maps_toolchain_members_and_fails_closed_on_a_short_archive(tmp_path):
    """Hermetic (no network) unit test for ``_stage_glibc_runtime``'s file
    mapping and its behaviour against an incomplete/corrupt toolchain
    archive, using a synthetic archive shaped like the real ARM GNU
    toolchain release instead of the real ~145 MB download."""
    prefix = de25_linux_image.de25_toolchain_stage.TOOLCHAIN_DIR_NAME
    archive = tmp_path / "toolchain.tar.xz"
    with tarfile.open(archive, "w:xz") as package:
        for rel, dest_rel in de25_linux_image.GLIBC_RUNTIME_MEMBERS.items():
            data = f"fake-{dest_rel}".encode()
            info = tarfile.TarInfo(name=f"{prefix}/{rel}")
            info.size = len(data)
            info.mode = 0o755
            package.addfile(info, io.BytesIO(data))
        # A decoy the mapping must ignore.
        decoy = b"decoy"
        info = tarfile.TarInfo(name=f"{prefix}/aarch64-none-linux-gnu/libc/lib64/libcrypt.so.1")
        info.size = len(decoy)
        package.addfile(info, io.BytesIO(decoy))

    destination = tmp_path / "out"
    de25_linux_image._stage_glibc_runtime(archive, destination)
    for rel, dest_rel in de25_linux_image.GLIBC_RUNTIME_MEMBERS.items():
        staged = destination / dest_rel
        assert staged.is_file()
        assert staged.read_bytes() == f"fake-{dest_rel}".encode()
    assert not (destination / "lib/libcrypt.so.1").exists()

    # MUTATION: an archive missing one of the required members must fail
    # closed, not silently ship a partial glibc runtime.
    short_archive = tmp_path / "toolchain-short.tar.xz"
    with tarfile.open(archive, "r:xz") as src, tarfile.open(short_archive, "w:xz") as dst:
        for member in src.getmembers():
            if member.name.endswith("ld-linux-aarch64.so.1"):
                continue
            dst.addfile(member, src.extractfile(member))
    with pytest.raises(RuntimeError, match="missing expected glibc runtime"):
        de25_linux_image._stage_glibc_runtime(short_archive, tmp_path / "out-short")


def test_stage_glibc_runtime_symlinks_soname_libs_into_lib64_ds_p2_102(tmp_path):
    """DS-P2.102 MEASURED on silicon: with the pre-fix code (glibc runtime
    staged only under lib/), ``python3 -c 'print(1)'`` on the physical DE25
    board failed ``error while loading shared libraries: libpthread.so.0:
    cannot open shared object file`` even though ``/lib/libpthread.so.0`` was
    proven present and byte-correct on the card (stat/md5sum both clean).
    ``LD_DEBUG=libs`` on the board pinned the cause: with no
    ``/etc/ld.so.cache`` (nothing stages one; the board's busybox has no
    ldconfig applet), this ARM GNU toolchain's dynamic loader falls back to
    its *compiled-in* default search path ``/lib64:/usr/lib64`` for every
    bare-SONAME ``DT_NEEDED`` entry (``libc.so.6``, ``libm.so.6``,
    ``libpthread.so.0``, ``libdl.so.2``, ``librt.so.1``, ``libutil.so.1`` —
    everything except the loader itself, which the kernel opens directly via
    PT_INTERP's literal ``/lib`` path, and libpython3.13.so.1.0, which
    python3's own DT_NEEDED names via an explicit ``$ORIGIN/../lib/`` path).
    ``LD_LIBRARY_PATH=/lib python3 -c 1`` succeeded on the same board,
    isolating the defect to the search path, not corruption or a missing
    file — a class of bug ``assert_dynamic_executables_have_interpreters``
    (which only checks PT_INTERP presence) cannot see at all.

    BASELINE (proves this is not vacuous): reset destination/lib64 by
    deleting it after a real stage and show python3's own bare-SONAME
    libraries are then absent from every directory this loader's default
    search path names — reproducing the exact silicon failure mode.

    MUTATION (the fix): stage for real and show every one of those SONAMEs
    resolves under lib64/ to the identical bytes staged under lib/ (a
    symlink, not a duplicate copy — same inode content, no drift possible
    between the two).
    """
    prefix = de25_linux_image.de25_toolchain_stage.TOOLCHAIN_DIR_NAME
    archive = tmp_path / "toolchain.tar.xz"
    with tarfile.open(archive, "w:xz") as package:
        for rel, dest_rel in de25_linux_image.GLIBC_RUNTIME_MEMBERS.items():
            data = f"fake-{dest_rel}".encode()
            info = tarfile.TarInfo(name=f"{prefix}/{rel}")
            info.size = len(data)
            info.mode = 0o755
            package.addfile(info, io.BytesIO(data))

    destination = tmp_path / "out"
    de25_linux_image._stage_glibc_runtime(archive, destination)

    bare_soname_libs = [
        name for rel, name in
        ((rel, Path(dest_rel).name)
         for rel, dest_rel in de25_linux_image.GLIBC_RUNTIME_MEMBERS.items())
        if not rel.endswith("/ld-linux-aarch64.so.1")
    ]
    assert bare_soname_libs, "no bare-SONAME libraries to check — test is vacuous"

    for name in bare_soname_libs:
        link = destination / "lib64" / name
        assert link.is_symlink(), f"{link} missing or not a symlink — the DS-P2.102 defect"
        assert link.resolve() == (destination / "lib" / name).resolve()
        assert link.read_bytes() == (destination / "lib" / name).read_bytes()
    # The loader's own interpreter path is PT_INTERP-literal (kernel-opened,
    # never searched), so it must NOT be duplicated into lib64/.
    assert not (destination / "lib64/ld-linux-aarch64.so.1").exists()

    # BASELINE: without lib64/ (the pre-fix shape), every one of these
    # SONAMEs is unresolvable via the board-measured default search path.
    shutil.rmtree(destination / "lib64")
    for name in bare_soname_libs:
        assert not (destination / "lib64" / name).exists()
        assert not (destination / "usr/lib64" / name).exists()


def _make_elf64(*, is_dyn: bool, interp: str | None) -> bytes:
    """Build a minimal, real, parseable ELF64 LE aarch64 file: an ET_DYN
    with a PT_INTERP program header (matching python3.13's actual shape), or
    an ET_EXEC with no program headers at all (matching busybox's — DS-P1.57
    measured it as a plain ET_EXEC static binary, ELF header only)."""
    e_type = 3 if is_dyn else 2  # ET_DYN / ET_EXEC
    phdrs = b""
    e_phnum = 0
    e_phoff = 0
    if interp is not None:
        interp_bytes = interp.encode("ascii") + b"\x00"
        e_phoff = 64
        p_offset = e_phoff + 56  # right after the single phdr
        phdrs = struct.pack(
            "<IIQQQQQQ",
            3,  # p_type = PT_INTERP
            4,  # p_flags = PF_R
            p_offset, 0, 0,
            len(interp_bytes), len(interp_bytes), 1,
        ) + interp_bytes
        e_phnum = 1
    header = struct.pack(
        "<4sBBBB8sHHIQQQIHHHHHH",
        b"\x7fELF", 2, 1, 1, 0, b"\x00" * 8,
        e_type, 183,  # e_machine = EM_AARCH64
        1, 0, e_phoff, 0, 0, 64, 56, e_phnum, 0, 0, 0,
    )
    return header + phdrs


_ARTIFACT_NAMES = (
    "bl31.bin", "u-boot.itb", "u-boot-spl-dtb.hex", "Image",
    "socfpga_agilex5_socdk.dtb", "boot.scr",
    de25_linux_image.SOF_HPS_ARTIFACT, "sdcard.img",
)


def _stage_lock_fixture(out: Path, sources: Path) -> dict[str, Path]:
    """Populate the non-sdcard boot artifacts + pinned source checkouts
    shared by the DE-L0 lock tests. Returns the FAT-partition item map."""
    out.mkdir()
    for name in _ARTIFACT_NAMES:
        if name != "sdcard.img":
            (out / name).write_bytes(("de25:" + name).encode())
    for name in ("arm-trusted-firmware", "u-boot-socfpga", "linux-socfpga"):
        repo = sources / name
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=DE-L0", "-c",
                        "user.email=de-l0@example.invalid", "commit",
                        "--allow-empty", "-qm", "pin"], cwd=repo, check=True)
    # No SOF_HPS_ARTIFACT here: routertl's real `run_socfpga_sdcard` never
    # stages a `.sof` into the FAT (DS-P3.147) — these are exactly the four
    # files the sanctioned fpga-first assembler and the witnessed boot17/
    # boot21 cards both carry.
    return {name: out / name for name in
            ("Image", "boot.scr", "socfpga_agilex5_socdk.dtb", "u-boot.itb")}


def _stage_rootfs_fixture(out: Path) -> Path:
    rootfs = out / "rootfs"
    for rel in de25_linux_image.SDCARD_ROOTFS_CONTENTS:
        path = rootfs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("de25-rootfs:" + rel).encode())
    return rootfs


@requires(
    "DE-L0-IN-002", "DE-L0-BUILD-003", "DE-L0-NEG-005",
    "DE-L0-BUILD-004", "DE-L0-NEG-006",
)
def test_build_leaves_uboot_env_absent_from_output_and_card(
    monkeypatch, tmp_path,
):
    """@requires DE-L0-IN-002 DE-L0-BUILD-003 DE-L0-NEG-005

    The caller-level build surface must preserve RouteRTL's environment-free
    FAT and remove stale output from the reverted emitter.

    BASELINE: commit 3a31852 fails because build() emits and copies uboot.env
    into both surfaces. MUTATION: restoring either emitter call fails the
    corresponding deployed-surface assertion below.
    """
    root = tmp_path / "project"
    _write_project_pointer(root)
    out = root / "sw/linux/de25/out/de25_datalogger"
    out.mkdir(parents=True)
    (root / "project.yml").write_text(
        "target: targets/de25_datalogger.yml\n", encoding="utf-8"
    )
    # DS-T2.71: build() refuses, before any rr step, a dt_node whose lwh2f
    # window the design's decode map does not cover. Stage the REPO's own
    # target, memory map and the two RTL files that check derives the real
    # decode windows from — not stubs, so this fixture keeps exercising the
    # shipped configuration rather than a hand-made one that always passes.
    # The first entry is TARGET, which DS-P3.152's deployment resolver also
    # needs staged before build().
    for rel in ("targets/de25_datalogger.yml",
                "sw/linux/de25/system_memory_map.yml",
                "hw/rtl/de25/deepskopion_de25_top.vhd",
                "hw/rtl/de25/axil_demux3.vhd"):
        staged = root / rel
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes((ROOT / rel).read_bytes())
    _write_deployment_hw(root)
    monkeypatch.delenv("RR_BENCH_CONFIG", raising=False)
    for name in (
        "bl31.bin", "u-boot.itb", "u-boot-spl-dtb.hex", "Image",
        "socfpga_agilex5_socdk.dtb", "boot.scr",
        de25_linux_image.SOF_HPS_ARTIFACT,
    ):
        (out / name).write_bytes(("de25:" + name).encode())
    # DS-P3.154: build() reads the board DTB back and refuses a boot set whose
    # dtb does not carry the target's dt_nodes. `rr linux dtb` is mocked here,
    # so nothing produces one — stage a REAL blob generated from the repo's own
    # target, keeping this fixture an exercise of the shipped configuration.
    (out / de25_linux_image.DTB_ARTIFACT).write_bytes(
        _fdt_fixture.dtb_realising(root / "targets/de25_datalogger.yml"))
    (out / "uboot.env").write_bytes(b"stale-environment")
    (out / "env.txt").write_text("stale recipe\n", encoding="ascii")

    # DS-P2.94: build() now rebuilds u-boot.itb with the EMAC0 PHY fix right
    # after `rr linux uboot` — it needs the cloned control-DTB source and the
    # bl31.bin binman input that real step leaves behind.
    uboot_dir = root / "sw/linux/de25/src/u-boot-socfpga"
    dtsi = _write_uboot_control_dtsi_fixture(uboot_dir)
    (uboot_dir / "bl31.bin").write_bytes(b"bl31")

    real_run = subprocess.run
    rr_steps = []
    rr_commands = []
    make_calls = []

    def fake_run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        if argv[0] == "make":
            make_calls.append(argv)
            assert de25_linux_image._UBOOT_EMAC0_PHY_PATCHED_NODE in dtsi.read_text(
                encoding="utf-8"
            ), "make ran before the DS-P2.94 EMAC0 PHY fix was patched in"
            for src_rel in de25_linux_image.UBOOT_REBUILD_COPIES:
                artifact = uboot_dir / src_rel
                artifact.parent.mkdir(parents=True, exist_ok=True)
                if src_rel == "u-boot.itb":
                    artifact.write_bytes(_build_fake_uboot_itb_bytes(phy_reg=1))
                else:
                    artifact.write_bytes(f"rebuilt:{src_rel}".encode())
            class Done:
                returncode = 0
            return Done()
        if argv[:2] == ["rr", "linux"]:
            rr_steps.append(argv[2])
            rr_commands.append(argv)
            if argv[2] == "sdcard":
                fat = out / "sdcard/boot.fat"
                fat.parent.mkdir(parents=True, exist_ok=True)
                with fat.open("wb") as stream:
                    stream.truncate(8 * 1024 * 1024)
                real_run(["mformat", "-i", str(fat), "::"], check=True,
                         capture_output=True)
                for name in ("u-boot.itb", "Image",
                             "socfpga_agilex5_socdk.dtb", "boot.scr"):
                    real_run(["mcopy", "-i", str(fat), str(out / name),
                              f"::/{name}"], check=True, capture_output=True)
                card = out / "sdcard.img"
                with card.open("wb") as stream:
                    stream.truncate(
                        de25_linux_image.SDCARD_FAT_OFFSET_BYTES
                        + fat.stat().st_size
                    )
                real_run(
                    ["dd", f"if={fat}", f"of={card}", "bs=512",
                     f"seek={de25_linux_image.SDCARD_FAT_OFFSET_BYTES // 512}",
                     "conv=notrunc", "status=none"],
                    check=True, capture_output=True,
                )
        elif Path(argv[0]).name == "mkenvimage":
            size = int(argv[argv.index("-s") + 1], 0)
            recipe = Path(argv[-1]).read_text(encoding="ascii").rstrip().encode()
            body = recipe + b"\0\0"
            body += b"\xff" * (size - 4 - len(body))
            Path(argv[argv.index("-o") + 1]).write_bytes(
                zlib.crc32(body).to_bytes(4, "little") + body
            )
        else:
            return real_run(cmd, **kwargs)

        class Done:
            returncode = 0

        return Done()

    monkeypatch.setattr(de25_linux_image.subprocess, "run", fake_run)
    monkeypatch.setattr(de25_linux_image, "_stage_target_stem_sof",
                        lambda _root: None)
    monkeypatch.setattr(de25_linux_image, "normalize_provenance",
                        lambda *_args: None)
    monkeypatch.setattr(de25_linux_image, "_resolve_mkenvimage",
                        lambda _root: Path("/test/mkenvimage"), raising=False)
    # DS-P2.310: record the shipped-itb bootargs check instead of running it on
    # the fake FIT (which has no default env); pinned below.
    checked = []
    monkeypatch.setattr(de25_linux_image, "assert_uboot_itb_default_bootargs",
                        lambda itb, ba: checked.append((itb, ba)))
    de25_linux_image.build(root)
    assert checked == [(out / "u-boot.itb", de25_linux_image.deployment_bootargs(root))]
    assert rr_steps.index("kernel") < rr_steps.index("dtb") < rr_steps.index("sdcard"), (
        "the canonical /chosen bootargs must be patched into the DTB shipped "
        "to the default distro boot flow"
    )
    dtb_call = next(command for command in rr_commands if command[2] == "dtb")
    assert dtb_call[3:] == [
        "--bootargs", de25_linux_image.deployment_bootargs(root)
    ]
    # DS-P2.94: the EMAC0 PHY rebuild must run once, after `rr linux uboot`
    # and before the card is assembled — a card built from the pre-rebuild
    # u-boot.itb would still carry the "addr 0" defect.
    assert len(make_calls) == 1
    assert rr_steps.index("uboot") < rr_steps.index("sdcard")
    rebuilt_uboot_itb = _build_fake_uboot_itb_bytes(phy_reg=1)
    assert (out / "u-boot.itb").read_bytes() == rebuilt_uboot_itb, (
        "sdcard assembly must ship the rebuilt (PHY-fixed) u-boot.itb, not "
        "the one `rr linux uboot` alone produced"
    )
    # DE-L0-NEG-006 names the SHIPPED FIT, not merely out/. Read back what
    # `rr linux sdcard`'s mcopy actually put on the FAT partition of
    # sdcard.img — this is the assertion that fails if the rebuild call is
    # ever moved to AFTER the "sdcard" rr step: out/u-boot.itb would still
    # read the rebuilt bytes (this module writes it directly), but the FAT
    # would have already been mcopy'd from the pre-rebuild bytes and this
    # readback would catch it.
    fat_uboot_itb = tmp_path / "fat-u-boot.itb"
    fat_readback = real_run(
        ["mtype", "-i",
         f"{out / 'sdcard.img'}@@{de25_linux_image.SDCARD_FAT_OFFSET_BYTES}",
         "::/u-boot.itb"],
        check=True, capture_output=True,
    )
    fat_uboot_itb.write_bytes(fat_readback.stdout)
    assert fat_uboot_itb.read_bytes() == rebuilt_uboot_itb, (
        "the u-boot.itb actually shipped on the FAT partition of "
        "sdcard.img must be the PHY-rebuilt one — this fails if the rebuild "
        "runs after `rr linux sdcard` instead of before it"
    )
    # DE-L0-NEG-006, asserted directly (not just byte-equality against our
    # own fixture): decode the SHIPPED FIT's embedded control DTB and pin
    # the actual EMAC0 phy `reg` cell rather than trusting it by
    # construction.
    shipped_control_dtb = None
    for _node_path, name, value in de25_linux_image._iter_fdt_props(
        fat_uboot_itb.read_bytes()
    ):
        if name == "data" and value[:4] == struct.pack(">I", 0xD00DFEED):
            shipped_control_dtb = value
            break
    assert shipped_control_dtb is not None, (
        "the FAT-shipped u-boot.itb must embed a control DTB blob"
    )
    assert de25_linux_image._find_emac0_phy_reg_cells(
        shipped_control_dtb
    ) == struct.pack(">I", 1), (
        f"the FAT-shipped u-boot.itb's control DTB must set "
        f"{de25_linux_image.UBOOT_EMAC0_PHY_NODE_PATH} reg=<1>"
    )

    readback = real_run(
        ["mtype", "-i",
         f"{out / 'sdcard.img'}@@{de25_linux_image.SDCARD_FAT_OFFSET_BYTES}",
         f"::/{de25_linux_image.UBOOT_ENV_ARTIFACT}"],
        check=False, capture_output=True,
    )
    assert readback.returncode != 0, (
        "build() put forbidden uboot.env on sdcard.img P1; it overrides the "
        "working default distro bootcmd"
    )
    assert not (out / "uboot.env").exists()
    assert not (out / "env.txt").exists()


def test_lock_cli_rejects_any_uboot_env_on_card(tmp_path):
    """@requires DE-L0-NEG-005

    Assert the prohibition at the CLI/raw-card boundary using the formerly
    accepted CRC-valid payload. Validity is immaterial to the new rule because
    any persistent env can hijack the known-good default boot flow.

    BASELINE: commit 3a31852 returns zero for this exact 128-KiB payload: the
    old validator certifies its own broken env, so this test fails. MUTATION:
    deleting the explicit FAT presence probe also makes the lock command
    return zero, so this guard goes red.
    """
    out = tmp_path / "out"
    sources = tmp_path / "src"
    fat_items = _stage_lock_fixture(out, sources)
    recipe = (
        b"bootcmd=fatload mmc 0:1 0x82000000 boot.scr; "
        b"source 0x82000000\0\0"
    )
    body = recipe + b"\xff" * (0x20000 - 4 - len(recipe))
    forbidden = out / de25_linux_image.UBOOT_ENV_ARTIFACT
    forbidden.write_bytes(zlib.crc32(body).to_bytes(4, "little") + body)
    fat_items[de25_linux_image.UBOOT_ENV_ARTIFACT] = forbidden
    rootfs = _stage_rootfs_fixture(out)
    sdcard = out / "sdcard.img"
    _write_socfpga_sdcard_image(sdcard, fat_items, rootfs)

    frozen = tmp_path / "frozen.json"
    create = subprocess.run(
        [sys.executable, str(TOOL), "lock", "--root", str(ROOT),
         "--out-dir", str(out), "--source-root", str(sources),
         "--output", str(frozen)], cwd=ROOT, capture_output=True, text=True,
    )
    assert create.returncode == 1, create.stdout + create.stderr
    assert "forbidden /uboot.env is present" in create.stderr
    assert not frozen.exists()


def test_lock_cli_rejects_artifact_drift(tmp_path):
    """@requires DE-L0-BUILD-001 DE-L0-NEG-002 DE-L0-NEG-003"""
    out = tmp_path / "out"
    sources = tmp_path / "src"
    fat_items = _stage_lock_fixture(out, sources)
    rootfs = _stage_rootfs_fixture(out)
    sdcard = out / "sdcard.img"
    _write_socfpga_sdcard_image(sdcard, fat_items, rootfs)

    # The emitted table is P1 FAT32 (type c) + P2 Linux/ext4 (type 83) only —
    # no type-A2 raw partition exists on this Agilex 5 / SDM chain (RTL-P2.668).
    table = subprocess.run([de25_linux_image.sbin_tool("sfdisk"), "-d", str(sdcard)], check=True,
                           capture_output=True, text=True).stdout
    types = [line.split("type=")[1].split(",")[0].strip()
             for line in table.splitlines() if "type=" in line]
    assert types == ["c", "83"], table
    assert "a2" not in [t.lower() for t in types], table

    frozen = tmp_path / "frozen.json"
    live = tmp_path / "live.json"
    create = subprocess.run(
        [sys.executable, str(TOOL), "lock", "--root", str(ROOT),
         "--out-dir", str(out), "--source-root", str(sources),
         "--output", str(frozen)], cwd=ROOT, capture_output=True, text=True,
    )
    assert create.returncode == 0, create.stdout + create.stderr
    manifest = json.loads(frozen.read_text(encoding="utf-8"))
    assert manifest["target"] == "de25_datalogger"
    assert manifest["source_date_epoch"] == 1758780332
    assert manifest["mtools_date_string"] == "2025-09-25 06:05:32"
    assert manifest["kernel_build_identity"] == {
        "user": "routertl", "host": "reproducible",
        "timestamp": "2025-09-25 06:05:32 +0000",
        "version": 1,
    }
    assert set(manifest["artifacts"]) == set(_ARTIFACT_NAMES)

    # Change Image consistently in BOTH the out_dir source and its FAT copy
    # (a real rebuild would do the same) so the new source<->card equality
    # check (added by this fix, guarding a different failure mode) stays
    # green and this exercises what it's meant to: content_sha256 drifting
    # from the frozen lock between builds (DE-L0-NEG-002).
    corrupt = tmp_path / "corrupt-Image"
    corrupt.write_bytes(b"corrupted")
    (out / "Image").write_bytes(b"corrupted")
    subprocess.run(["mcopy", "-o", "-i",
                    f"{sdcard}@@{de25_linux_image.SDCARD_FAT_OFFSET_BYTES}",
                    str(corrupt), "::/Image"], check=True)
    check = subprocess.run(
        [sys.executable, str(TOOL), "lock", "--root", str(ROOT),
         "--out-dir", str(out), "--source-root", str(sources),
         "--output", str(live), "--check", str(frozen)], cwd=ROOT,
        capture_output=True, text=True,
    )
    assert check.returncode == 1, check.stdout + check.stderr
    assert "provenance drift" in check.stderr


@requires("DE-L0-BUILD-002", "DE-L0-NEG-004")
def test_lock_cli_fails_closed_when_sof_hps_artifact_is_missing_ds_p1_50(tmp_path):
    """@requires DE-L0-BUILD-002 DE-L0-NEG-004

    Regression guard for the measured DS-P1.50 defect: an ``out/`` that never
    ran `rr linux sof` (the JTAG-programmable, SPL-embedded ``.sof`` `rr
    program` needs for this fpga-first target's out-of-band bring-up path —
    see SOF_HPS_ARTIFACT's docstring in tools/de25_linux_image.py, revised by
    DS-P3.147) must fail lock generation loudly, exactly like a missing
    u-boot.itb/Image/dtb would — not silently omit the one artifact this
    ticket exists to add. DS-P3.147 kept this requirement (BOOT_ARTIFACTS,
    the out/-presence + provenance-hash check) even after dropping the
    artifact from SDCARD_CONTENTS (the FAT-membership check, sibling test
    below) — the sof is still a real, pinned build output, just never an SD
    FAT member.

    BASELINE (verified by hand against tools/de25_linux_image.py at commit
    8ca97a9, the tip before DS-P1.50): ``BOOT_ARTIFACTS`` there has no
    SPL-bearing/fabric-config entry at all, so `lock` never looks for one and
    this exact fixture — every artifact present EXCEPT the sof — returns 0
    with a manifest that omits it entirely. A test only asserting
    ``returncode == 1`` here would have caught nothing on that commit; the
    defect this closes is precisely that the old code had no path that could
    ever fail this way.

    MUTATION check for this test itself: also `write_bytes` the sof into
    `out/` before calling `lock` and this test fails, because `lock` then
    finds every required artifact and returns 0 — confirming the assertion
    below exercises the missing-sof path specifically, not something
    unconditional.
    """
    out = tmp_path / "out"
    sources = tmp_path / "src"
    fat_items = _stage_lock_fixture(out, sources)
    (out / de25_linux_image.SOF_HPS_ARTIFACT).unlink()
    rootfs = _stage_rootfs_fixture(out)
    sdcard = out / "sdcard.img"
    _write_socfpga_sdcard_image(sdcard, fat_items, rootfs)

    frozen = tmp_path / "frozen.json"
    create = subprocess.run(
        [sys.executable, str(TOOL), "lock", "--root", str(ROOT),
         "--out-dir", str(out), "--source-root", str(sources),
         "--output", str(frozen)], cwd=ROOT, capture_output=True, text=True,
    )
    assert create.returncode == 1, create.stdout + create.stderr
    assert de25_linux_image.SOF_HPS_ARTIFACT in create.stderr
    assert not frozen.exists()


@requires("DE-L0-BUILD-002", "DE-L0-NEG-004")
def test_lock_cli_passes_when_sof_hps_artifact_is_absent_from_the_card_ds_p3_147(
    tmp_path,
):
    """@requires DE-L0-BUILD-002 DE-L0-NEG-004

    Regression guard for the measured DS-P3.147 defect: DS-P1.50 put
    SOF_HPS_ARTIFACT into SDCARD_CONTENTS (the FAT-readback set
    ``_sdcard_content_sha`` demands), but routertl's real
    ``run_socfpga_sdcard`` (``sdk/cli/commands/linux_steps/_socfpga.py``,
    ``fat_items``) never stages a ``.sof`` for a fpga-first target, and
    neither the silicon-proven boot17-era card nor the freshly rebuilt
    DS-P3.146 boot21 card ever carried one — both booted with exactly
    Image + boot.scr + dtb + u-boot.itb in the FAT. A card assembled by the
    sanctioned `build()` route therefore must pass `lock --check`, not fail
    on a file no assembler was ever going to write.

    BASELINE (verified by hand against tools/de25_linux_image.py at commit
    cc21c67, the DS-P1.50 tip immediately before this fix): SDCARD_CONTENTS
    there includes SOF_HPS_ARTIFACT, so this exact fixture — a realistic
    4-file FAT with no sof, matching every witnessed card — makes
    `_sdcard_content_sha` report "cannot verify" and `lock` return 1. A test
    only asserting ``returncode == 0`` on the OLD code would have shown that
    failure; this test is the one that pins the fix.

    MUTATION check: reinstate ``SOF_HPS_ARTIFACT`` in ``SDCARD_CONTENTS``
    (the DS-P1.50 shape) and this test fails again the same way, confirming
    the assertion below exercises exactly the FAT-membership expectation
    DS-P3.147 corrected, not something unconditional.
    """
    out = tmp_path / "out"
    sources = tmp_path / "src"
    fat_items = _stage_lock_fixture(out, sources)
    assert de25_linux_image.SOF_HPS_ARTIFACT not in fat_items
    rootfs = _stage_rootfs_fixture(out)
    sdcard = out / "sdcard.img"
    _write_socfpga_sdcard_image(sdcard, fat_items, rootfs)

    frozen = tmp_path / "frozen.json"
    create = subprocess.run(
        [sys.executable, str(TOOL), "lock", "--root", str(ROOT),
         "--out-dir", str(out), "--source-root", str(sources),
         "--output", str(frozen)], cwd=ROOT, capture_output=True, text=True,
    )
    assert create.returncode == 0, create.stdout + create.stderr
    manifest = json.loads(frozen.read_text(encoding="utf-8"))
    assert de25_linux_image.SOF_HPS_ARTIFACT in manifest["artifacts"], (
        "the sof is still a pinned build output even though it is not a FAT "
        "member"
    )


def test_lock_cli_fails_closed_on_a_fat_only_image_ds_p1_49(tmp_path):
    """@requires DE-L0-BUILD-001 DE-L0-NEG-003

    Regression guard for the measured DS-P1.49 defect: a single-FAT32,
    no-rootfs sdcard.img (what the builder emitted before this fix) must
    fail lock generation loudly, not produce a green lock file that looks
    like a complete boot set.

    BASELINE (proven by hand against tools/de25_linux_image.py as it stood
    at commit 132124a, before this change): ``_sdcard_content_sha`` took only
    ``path`` and never touched an ext4 partition at all, so this exact
    FAT-only image made ``lock`` return 0 — a new test that only asserted
    ``returncode == 0`` here would have caught nothing.

    MUTATION check for this test itself: temporarily pass ``rootfs_dir`` back
    into ``_write_socfpga_sdcard_image`` (i.e. build the good 2-partition
    image) and this test fails, because the current code now finds and
    verifies the rootfs partition successfully — confirming the assertion
    below is actually exercising the missing-partition path, not something
    unconditional.
    """
    out = tmp_path / "out"
    sources = tmp_path / "src"
    fat_items = _stage_lock_fixture(out, sources)
    sdcard = out / "sdcard.img"
    _write_socfpga_sdcard_image(sdcard, fat_items, rootfs_dir=None)

    frozen = tmp_path / "frozen.json"
    create = subprocess.run(
        [sys.executable, str(TOOL), "lock", "--root", str(ROOT),
         "--out-dir", str(out), "--source-root", str(sources),
         "--output", str(frozen)], cwd=ROOT, capture_output=True, text=True,
    )
    assert create.returncode == 1, create.stdout + create.stderr
    assert "rootfs" in create.stderr.lower()
    assert not frozen.exists()


def test_normalize_provenance_removes_wall_clock_and_worktree_identity(tmp_path):
    """@requires DE-L0-BUILD-001 DE-L0-NEG-001"""
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "usr/bin").mkdir(parents=True)
    stamp = rootfs / "etc/rr-sw-provenance"
    payload = rootfs / "usr/bin/daemon"
    payload.write_text("stable payload", encoding="utf-8")
    stamp.write_text("RR_SW_BUILD_DATE=wall-clock-one\n", encoding="utf-8")

    command = [sys.executable, str(TOOL), "normalize-provenance",
               "--root", str(ROOT), "--rootfs", str(rootfs)]
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    expected = stamp.read_text(encoding="utf-8")
    assert "2025-09-25T06:05:32Z" in expected
    assert "RR_SW_GIT_DIRTY=0" in expected
    assert str(ROOT) not in expected

    stamp.write_text("RR_SW_BUILD_DATE=wall-clock-two\n", encoding="utf-8")
    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert second.returncode == 0, second.stdout + second.stderr
    assert stamp.read_text(encoding="utf-8") == expected
