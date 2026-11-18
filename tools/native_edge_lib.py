#!/usr/bin/env python3
"""Build, install and check libds_edge_drain.so in the Zynq and PolarFire images (DS-P1.87).

The daemon's native edge path (sw/stream/native, DS-P1.74) runs only where
sw/stream/native/edge_frames.py finds the library: at SYSTEM_LIB,
/usr/local/lib/deepskopion/libds_edge_drain.so. The DE25 image builds it in
tools/de25_linux_image.py (DS-P2.210). This tool does the same for the two
Buildroot images, whose per-arch python_overlay trees hold the CPython the
library is loaded into:

  profile  overlay                        compiler                ELF
  zybo     sw/linux/python_overlay        Buildroot arm gnueabi   ELF32 ARM, EABI5 soft-float
  pfsoc    sw/linux/pfsoc/python_overlay  Buildroot riscv64       ELF64 RISC-V, RVC, double-float

The Zynq Buildroot toolchain is soft-float (ld-linux.so.3): a hard-float
build does not load next to that Python, so the check compares the
library's ELF flags with the overlay's own libpython.

  install  compiles edge_drain.c from source into <overlay>/usr/local/lib/
           deepskopion/ and writes <overlay>/etc/deepskopion-native-edge
           (library and source sha256, compiler name, version and sha256).
           sw/linux/extract_python_overlay.sh and
           sw/linux/pfsoc/build_python_overlay.sh run it with their Buildroot gcc.
  check    run by each ddr target's linux.rootfs.stage_command: fails the
           image build when the library is missing, is not the image's ELF
           kind, or was built from other sources than the checkout's. It
           prints the install command.

No binary is committed: the library is built from the checkout every time.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
LIB_REL = "usr/local/lib/deepskopion/libds_edge_drain.so"
STAMP_REL = "etc/deepskopion-native-edge"
SOURCE = "sw/stream/native/edge_drain.c"
SOURCES = (SOURCE, "sw/stream/native/edge_drain.h")
BASE_CFLAGS = ("-O2", "-std=c11", "-D_POSIX_C_SOURCE=199309L",
               "-fPIC", "-shared", "-Wall", "-Wextra", "-Werror")
# DS-P2.262: link without debug sections. The library's own compile already maps
# its source paths (-ffile-prefix-map), but the libgcc objects it links carry the
# toolchain's build directory in .debug_str / .debug_line_str.
LDFLAGS = ("-Wl,--strip-debug",)
# tools/de25_linux_image.py SOURCE_DATE_EPOCH: the same fixed build time.
SOURCE_DATE_EPOCH = 1758780332

EM_ARM = 40
EM_RISCV = 243
ELFCLASS32 = 1
ELFCLASS64 = 2
ET_DYN = 3
PT_INTERP = 3
SHT_RISCV_ATTRIBUTES = 0x70000003
EF_ARM_ABI_FLOAT_SOFT = 0x200
EF_ARM_ABI_FLOAT_HARD = 0x400
EF_RISCV_RVC = 0x1
EF_RISCV_FLOAT_ABI_MASK = 0x6
EF_RISCV_FLOAT_ABI_DOUBLE = 0x4

INTERIM = "interim, not the Buildroot toolchain"


@dataclasses.dataclass(frozen=True)
class Profile:
    name: str
    overlay: str        # repo-relative per-arch python_overlay
    buildroot_cc: str   # repo-relative Buildroot host gcc
    cflags: Tuple[str, ...]
    machine: int
    elfclass: int
    eflags: int         # the image's e_flags; the library must carry the same
    describe: str


PROFILES: Dict[str, Profile] = {
    "zybo": Profile(
        "zybo", "sw/linux/python_overlay",
        "sw/linux/buildroot/output/host/bin/arm-buildroot-linux-gnueabi-gcc",
        ("-mcpu=cortex-a9",), EM_ARM, ELFCLASS32, 0x05000000 | EF_ARM_ABI_FLOAT_SOFT,
        "ELF32 ARM, EABI5, soft-float ABI"),
    "pfsoc": Profile(
        "pfsoc", "sw/linux/pfsoc/python_overlay",
        "sw/linux/pfsoc/buildroot/output/host/bin/riscv64-buildroot-linux-gnu-gcc",
        ("-march=rv64gc", "-mabi=lp64d"), EM_RISCV, ELFCLASS64,
        EF_RISCV_RVC | EF_RISCV_FLOAT_ABI_DOUBLE, "ELF64 RISC-V, RVC, double-float ABI"),
}


# ---- ELF ---------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Elf:
    elfclass: int
    machine: int
    e_type: int
    eflags: int
    interp: Optional[str]
    riscv_arch: Optional[str]


def read_elf(data: bytes) -> Optional[Elf]:
    """Header fields of a little-endian ELF32/ELF64, or None for anything else."""
    if len(data) < 52 or data[:4] != b"\x7fELF" or data[5] != 1:
        return None
    cls = data[4]
    u16 = lambda o: int.from_bytes(data[o:o + 2], "little")  # noqa: E731
    u32 = lambda o: int.from_bytes(data[o:o + 4], "little")  # noqa: E731
    u64 = lambda o: int.from_bytes(data[o:o + 8], "little")  # noqa: E731
    if cls == ELFCLASS32:
        phoff, shoff, flags = u32(28), u32(32), u32(36)
        phentsize, phnum, shentsize, shnum = u16(42), u16(44), u16(46), u16(48)
        word, ph_off_at, ph_sz_at, sh_type_at, sh_off_at, sh_sz_at = u32, 4, 16, 4, 16, 20
    elif cls == ELFCLASS64 and len(data) >= 64:
        phoff, shoff, flags = u64(32), u64(40), u32(48)
        phentsize, phnum, shentsize, shnum = u16(54), u16(56), u16(58), u16(60)
        word, ph_off_at, ph_sz_at, sh_type_at, sh_off_at, sh_sz_at = u64, 8, 32, 4, 24, 32
    else:
        return None
    interp = None
    for i in range(phnum):
        off = phoff + i * phentsize
        if off + phentsize > len(data):
            break
        if u32(off) == PT_INTERP:
            start, size = word(off + ph_off_at), word(off + ph_sz_at)
            interp = data[start:start + size].split(b"\0", 1)[0].decode("ascii", "replace")
    arch = None
    for i in range(shnum):
        off = shoff + i * shentsize
        if off + shentsize > len(data):
            break
        if u32(off + sh_type_at) == SHT_RISCV_ATTRIBUTES:
            start, size = word(off + sh_off_at), word(off + sh_sz_at)
            arch = _riscv_arch(data[start:start + size])
    return Elf(cls, u16(18), u16(16), flags, interp, arch)


def _riscv_arch(section: bytes) -> Optional[str]:
    """Tag_RISCV_arch from a .riscv.attributes section: its "rv32..."/"rv64..." string."""
    for base in (b"rv64", b"rv32"):
        at = section.find(base)
        if at >= 0:
            return section[at:section.index(b"\0", at)].decode("ascii", "replace")
    return None


def riscv_extensions(arch: str) -> set:
    """The extension names of an ISA string, versions dropped:
    rv64i2p1_m2p0_..._zicsr2p0 -> {i, m, ..., zicsr}."""
    parts = arch[4:].split("_")
    out = set()
    for part in parts:
        name = part.rstrip("0123456789p")
        if len(name) == 1 or name[:1] in ("z", "s", "x"):
            out.add(name)
        else:   # single-letter run without separators, e.g. "imafdc"
            out.update(c for c in name if c.isalpha())
    return out


def problems(lib: bytes, profile: Profile, image_lib: Optional[bytes]) -> List[str]:
    """Why *lib* is not a library the profile's image can load; empty when it is."""
    elf = read_elf(lib)
    if elf is None:
        return ["not a little-endian ELF32/ELF64 file"]
    out = []
    if elf.elfclass != profile.elfclass or elf.machine != profile.machine:
        out.append(f"ELF class {elf.elfclass} machine {elf.machine}, want class "
                   f"{profile.elfclass} machine {profile.machine} ({profile.describe})")
    if elf.e_type != ET_DYN or elf.interp is not None:
        out.append(f"not a shared object (e_type {elf.e_type}, interpreter {elf.interp})")
    if elf.eflags != profile.eflags:
        out.append(f"e_flags 0x{elf.eflags:x}, want 0x{profile.eflags:x} ({profile.describe})")
    if image_lib is not None:
        img = read_elf(image_lib)
        if img is None:
            out.append("the overlay's libpython is not an ELF file")
        else:
            if img.eflags != elf.eflags:
                out.append(f"e_flags 0x{elf.eflags:x} differ from the overlay libpython's "
                           f"0x{img.eflags:x}")
            if profile.machine == EM_RISCV:
                if elf.riscv_arch is None or img.riscv_arch is None:
                    out.append("no Tag_RISCV_arch in the library or the overlay libpython")
                else:
                    extra = riscv_extensions(elf.riscv_arch) - riscv_extensions(img.riscv_arch)
                    if extra:
                        out.append(f"ISA {elf.riscv_arch} uses {sorted(extra)}, which the "
                                   f"overlay libpython ({img.riscv_arch}) does not")
    return out


# ---- build / install / check -----------------------------------------

def source_sha256(root: Path = ROOT) -> str:
    """sha256 over the sources, each as path NUL bytes NUL
    (tools/de25_linux_image.native_edge_source_sha256)."""
    digest = hashlib.sha256()
    for rel in SOURCES:
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update((root / rel).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def image_libpython(overlay: Path) -> Optional[Path]:
    found = sorted((overlay / "usr/lib").glob("libpython3*.so.1.0"))
    return found[0] if found else None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compiler_identity(cc: Path) -> Dict[str, str]:
    real = Path(os.path.realpath(cc))
    version = subprocess.run([str(cc), "--version"], capture_output=True, text=True,
                             errors="replace", check=False).stdout.splitlines()
    first = version[0].strip() if version else "unknown"
    return {
        # DS-P2.262: the name, not the path: the record ships in the image, and a
        # path names the build host's directory layout.
        "native_edge_compiler": Path(cc).name,
        "native_edge_compiler_version": first,
        "native_edge_compiler_sha256": _sha256(real),
        # A Buildroot-built gcc names its Buildroot release in --version.
        "native_edge_toolchain": "buildroot" if "(Buildroot " in first else INTERIM,
    }


def build(root: Path, profile: Profile, cc: Path, output: Path,
          image_lib: Optional[bytes] = None) -> str:
    """Compile the library with *cc*; its sha256. Refuses a result the image cannot load."""
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(cc), *BASE_CFLAGS, *profile.cflags, f"-ffile-prefix-map={root}=.",
           *LDFLAGS, "-o", str(output), SOURCE, "-lm"]
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(SOURCE_DATE_EPOCH), LC_ALL="C")
    proc = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True,
                          errors="replace", check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"native edge library build failed ({' '.join(cmd)}):\n"
                           f"{proc.stdout}{proc.stderr}")
    bad = problems(output.read_bytes(), profile, image_lib)
    if bad:
        raise RuntimeError(f"{cc} built a library the {profile.name} image cannot load: "
                           + "; ".join(bad))
    return _sha256(output)


def install(root: Path, profile: Profile, overlay: Path, cc: Path) -> Dict[str, str]:
    """Build into <overlay>/LIB_REL and write <overlay>/STAMP_REL; the stamp's fields."""
    if not cc.is_file():
        raise RuntimeError(f"compiler not found: {cc}")
    libpython = image_libpython(overlay)
    if libpython is None:
        raise RuntimeError(f"no usr/lib/libpython3*.so.1.0 in {overlay}: not a python_overlay")
    target = overlay / LIB_REL
    tmp = Path(tempfile.mkdtemp(prefix=".native-edge-", dir=overlay))
    try:
        built = tmp / target.name
        sha = build(root, profile, cc, built, libpython.read_bytes())
        target.parent.mkdir(parents=True, exist_ok=True)
        built.chmod(0o644)
        os.replace(built, target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    stamp = {
        "native_edge_profile": profile.name,
        "native_edge_lib": "/" + LIB_REL,
        "native_edge_lib_sha256": sha,
        "native_edge_source_sha256": source_sha256(root),
        **compiler_identity(cc),
        "native_edge_cflags": " ".join((*BASE_CFLAGS, *profile.cflags)),
        "native_edge_ldflags": " ".join(LDFLAGS),
    }
    (overlay / STAMP_REL).parent.mkdir(parents=True, exist_ok=True)
    (overlay / STAMP_REL).write_text("".join(f"{k}={v}\n" for k, v in stamp.items()))
    return stamp


def read_stamp(overlay: Path) -> Dict[str, str]:
    path = overlay / STAMP_REL
    if not path.is_file():
        return {}
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def install_command(profile: Profile, overlay: Path, root: Path) -> str:
    cc = root / profile.buildroot_cc
    line = (f"python3 tools/native_edge_lib.py install --profile {profile.name} "
            f"--overlay {overlay} --cc {cc}")
    if profile.name == "pfsoc" and not cc.is_file():
        line += (f"\n  (no Buildroot riscv64 toolchain here: the interim compiler is "
                 f"--cc $(command -v riscv64-linux-gnu-gcc), stamped '{INTERIM}')")
    return line


def check(root: Path, profile: Profile, overlay: Path) -> List[str]:
    """Why the overlay's library would not run the checkout's native edge path."""
    lib = overlay / LIB_REL
    if not lib.is_file():
        return [f"{lib} is missing: the daemon would run the Python edge path"]
    libpython = image_libpython(overlay)
    out = problems(lib.read_bytes(), profile, libpython.read_bytes() if libpython else None)
    stamp = read_stamp(overlay)
    if not stamp:
        out.append(f"{overlay / STAMP_REL} is missing")
    else:
        if stamp.get("native_edge_lib_sha256") != _sha256(lib):
            out.append("the library's sha256 is not the one its stamp records")
        if stamp.get("native_edge_source_sha256") != source_sha256(root):
            out.append(f"built from other sources (stamp {stamp.get('native_edge_source_sha256')}, "
                       f"checkout {source_sha256(root)}): {SOURCE} changed since the install")
        if stamp.get("native_edge_profile") != profile.name:
            out.append(f"stamp profile {stamp.get('native_edge_profile')}, want {profile.name}")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="tools/native_edge_lib.py", description=__doc__.split("\n")[0])
    p.add_argument("action", choices=("install", "check"))
    p.add_argument("--profile", required=True, choices=sorted(PROFILES))
    p.add_argument("--overlay", type=Path, default=None,
                   help="python_overlay tree (default: the profile's)")
    p.add_argument("--cc", type=Path, default=None,
                   help="cross gcc for install (default: the profile's Buildroot gcc)")
    p.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    profile = PROFILES[args.profile]
    root = args.root.resolve()
    overlay = args.overlay if args.overlay is not None else root / profile.overlay
    tag = f"[native-edge {profile.name}]"
    if args.action == "install":
        try:
            stamp = install(root, profile, overlay, args.cc or root / profile.buildroot_cc)
        except RuntimeError as exc:
            print(f"{tag} install failed: {exc}", file=sys.stderr)
            return 1
        print(f"{tag} installed {overlay / LIB_REL} sha256 {stamp['native_edge_lib_sha256']} "
              f"({stamp['native_edge_compiler_version']}; {stamp['native_edge_toolchain']})")
        return 0
    bad = check(root, profile, overlay)
    if bad:
        print(f"{tag} the image would run the Python edge path, refused (DS-P1.87):", file=sys.stderr)
        for line in bad:
            print(f"  - {line}", file=sys.stderr)
        print(f"  build it from source:\n  {install_command(profile, overlay, root)}", file=sys.stderr)
        return 1
    stamp = read_stamp(overlay)
    print(f"{tag} ok: {overlay / LIB_REL} sha256 {stamp['native_edge_lib_sha256']} "
          f"({stamp['native_edge_toolchain']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
