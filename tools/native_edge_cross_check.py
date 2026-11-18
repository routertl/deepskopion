#!/usr/bin/env python3
"""The native edge path on another ABI, byte for byte against the Python one (DS-P1.87).

The DS-P1.74 wire test (sw/stream/tests/test_native_edge_frames_ds_p1_74.py)
runs the host's build. This runs the same three dumps at the same four
clocks through a cross-built libds_edge_drain.so, loaded by the image's own
CPython under qemu-user, and compares every wire byte with the host's
pure-Python daemon output:

  host     ring_dump.daemon_wire(dump, clk, None)            the reference
  target   python -m sw.stream.native.ring_dump --native so  the image's Python + library
  target   python -m sw.stream.native.ring_dump              the image's Python alone

  python3 tools/native_edge_cross_check.py --lib arm.so \\
      --root <unpacked python_overlay> --qemu qemu-arm-static

Without --qemu the target is this interpreter (the harness's own test).
The overlay must sit on a filesystem whose inode numbers fit 32 bits for a
32-bit guest (a tmpfs does; ext4 makes the guest's readdir fail), and
PYTHONHOME is the host path of <root>/usr because qemu 6.2 does not remap
statx() under -L. Exit 0 when every comparison matches.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sw.stream.native import ring_dump  # noqa: E402

# test_native_edge_frames_ds_p1_74.CLOCKS and its three dumps.
CLOCKS = (100_000_000, 125_000_000, 80_000_000, 33_000_000)
DUMPS = {
    "edge_dump_4096": lambda: ring_dump.edge_dump(4096),
    "mixed_dump": ring_dump.mixed_dump,
    "interleaved_dump": ring_dump.interleaved_dump,
}


def _target(args) -> tuple:
    """(argv prefix, env) that runs the target Python."""
    if args.qemu is None:
        return [sys.executable], dict(os.environ, PYTHONPATH=str(ROOT))
    root = args.root.resolve()
    env = {"PATH": "/usr/bin:/bin", "LD_LIBRARY_PATH": "/lib:/usr/lib",
           "PYTHONHOME": str(root / "usr"), "PYTHONPATH": str(ROOT),
           "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C"}
    return [str(args.qemu), "-L", str(root), str(root / "usr/bin/python3")], env


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="tools/native_edge_cross_check.py")
    p.add_argument("--lib", type=Path, required=True, help="libds_edge_drain.so to check")
    p.add_argument("--root", type=Path, default=None, help="unpacked python_overlay (with --qemu)")
    p.add_argument("--qemu", type=Path, default=None, help="qemu-user binary (qemu-arm-static, ...)")
    p.add_argument("--no-target-python", action="store_true",
                   help="skip the image-Python-alone leg (slow under emulation)")
    args = p.parse_args(argv)
    if args.qemu is not None and args.root is None:
        p.error("--qemu needs --root")
    prefix, env = _target(args)
    probe = subprocess.run(
        prefix + ["-c", "import platform,struct,sys;print(platform.machine(),"
                        "struct.calcsize('P')*8,sys.version.split()[0])"],
        env=env, capture_output=True, text=True, errors="replace", timeout=300)
    if probe.returncode != 0:
        print(f"target Python does not start:\n{probe.stderr[-2000:]}", file=sys.stderr)
        return 2
    print(f"target: {probe.stdout.strip()} (machine, pointer bits, Python); "
          f"lib sha256 {hashlib.sha256(args.lib.read_bytes()).hexdigest()}")
    failures = 0
    with tempfile.TemporaryDirectory(prefix="native-cross-") as tmp:
        tmp = Path(tmp)
        for name, make in DUMPS.items():
            dump = make()
            dump_path = ring_dump.write(tmp / f"{name}.bin", dump)
            for clk in CLOCKS:
                want = ring_dump.daemon_wire(dump, clk, None)
                legs = [("native", ["--native", str(args.lib.resolve())])]
                if not args.no_target_python:
                    legs.append(("python", []))
                for leg, extra in legs:
                    out = tmp / f"{name}.{clk}.{leg}.wire"
                    run = subprocess.run(
                        prefix + ["-m", "sw.stream.native.ring_dump", str(dump_path), str(clk),
                                  "--repeat", "1", "--out", str(out), *extra],
                        env=env, capture_output=True, text=True, errors="replace", timeout=1800)
                    got = out.read_bytes() if run.returncode == 0 and out.is_file() else None
                    ok = got == want
                    failures += not ok
                    sha = hashlib.sha256(got).hexdigest()[:16] if got is not None else "-"
                    print(f"{'MATCH' if ok else 'DIFF '} {name:17s} {clk // 1_000_000:4d} MHz "
                          f"target-{leg:6s} {len(got) if got is not None else 0:8d} B sha256 {sha} "
                          f"(host Python {len(want)} B {hashlib.sha256(want).hexdigest()[:16]})")
                    if run.returncode != 0:
                        print(run.stderr[-2000:], file=sys.stderr)
    print(f"{'PASS' if failures == 0 else 'FAIL'}: {failures} mismatch(es)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
