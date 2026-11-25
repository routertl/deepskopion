#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-P2.262: keep build-host data out of the Zynq python overlay.

  clean_python_overlay.py --check <overlay>
  clean_python_overlay.py --fix <overlay> --python <target-version python3>

--check refuses (exit 1, one line per file):
  * files only a build needs, which carry the Buildroot build paths:
      usr/lib/python3*/config-3*/Makefile (python3-config, extension builds) and
      usr/lib/*-gdb.py (gdb auto-load scripts; the image has no gdb);
  * any file under the overlay that contains an absolute build-host path, i.e.
    /home/<name>/ or /root/ (the _sysconfigdata .pyc, the native-edge record, a
    library's debug sections, ...).
  Upstream literals such as the Windows user path in CPython's ntpath docstring do
  not match.

--fix removes the build-only files and rewrites the _sysconfigdata .pyc in place:
every string value has the build-host part in front of 'buildroot/output/' removed,
so '/home/<name>/<checkout>/sw/linux/buildroot/output/build/x' becomes
'/buildroot/output/build/x'. Keys are unchanged. The .pyc is recompiled with the
interpreter given by --python, which must be the target's Python version (the
Buildroot host python3 is), so the bytecode stays loadable on the board.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HOST_PATH = re.compile(rb"/home/[^/\s\x00'\"]+/|(?<![A-Za-z0-9_.-])/root/")
BUILD_ONLY = ("usr/lib/python3*/config-3*/Makefile", "usr/lib/*-gdb.py")
SYSCONFIGDATA = "usr/lib/python3*/_sysconfigdata_*.pyc"

# Run by the --python interpreter (the target's version): load the module from the
# .pyc, rewrite the values, write a source next to it and compile it back over the .pyc.
_REWRITE = r'''
import importlib.machinery, importlib.util, py_compile, re, sys, os, tempfile
pyc = sys.argv[1]
loader = importlib.machinery.SourcelessFileLoader("_sysconfigdata_rw", pyc)
spec = importlib.util.spec_from_loader("_sysconfigdata_rw", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
pat = re.compile(r"(?:/home/[^/\s'\"]+|/root)/[^\s'\"]*?/?(?=buildroot/output/)")
def fix(v):
    return pat.sub("/", v) if isinstance(v, str) else v
new = {k: fix(v) for k, v in mod.build_time_vars.items()}
assert set(new) == set(mod.build_time_vars)
fd, src = tempfile.mkstemp(suffix=".py", dir=os.path.dirname(pyc))
with os.fdopen(fd, "w") as f:
    f.write("# system configuration generated and used by the sysconfig module\n")
    f.write("build_time_vars = " + repr(new) + "\n")
try:
    py_compile.compile(src, cfile=pyc, dfile="/usr/lib/" + os.path.basename(os.path.dirname(pyc))
                       + "/" + os.path.basename(pyc)[:-1], doraise=True)
finally:
    os.unlink(src)
print("rewrote", pyc, len(new), "keys")
'''


def problems(overlay: Path) -> list[str]:
    out = []
    for pattern in BUILD_ONLY:
        for p in sorted(overlay.glob(pattern)):
            out.append(f"{p.relative_to(overlay)}: build-only file (carries build paths)")
    for p in sorted(overlay.rglob("*")):
        if p.is_file() and not p.is_symlink():
            m = HOST_PATH.search(p.read_bytes())
            if m:
                out.append(f"{p.relative_to(overlay)}: build-host path at byte {m.start()}")
    return out


def fix(overlay: Path, python: str) -> None:
    for pattern in BUILD_ONLY:
        for p in sorted(overlay.glob(pattern)):
            p.unlink()
            print(f"removed {p.relative_to(overlay)}")
    for pyc in sorted(overlay.glob(SYSCONFIGDATA)):
        subprocess.run([python, "-c", _REWRITE, str(pyc)], check=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", metavar="OVERLAY")
    mode.add_argument("--fix", metavar="OVERLAY")
    ap.add_argument("--python", help="target-version python3 for --fix (the Buildroot host python3)")
    a = ap.parse_args(argv)
    if a.fix:
        if not a.python:
            ap.error("--fix needs --python")
        fix(Path(a.fix), a.python)
    overlay = Path(a.check or a.fix)
    bad = problems(overlay)
    for line in bad:
        print(f"  {line}")
    if bad:
        print(f"{overlay}: {len(bad)} file(s) carry build-host data. Repair with:\n"
              f"  python3 sw/linux/clean_python_overlay.py --fix {overlay} "
              "--python sw/linux/buildroot/output/host/bin/python3\n"
              "  (a stale libds_edge_drain.so or native-edge record also needs "
              "tools/native_edge_lib.py install, see extract_python_overlay.sh)")
        return 1
    print(f"{overlay}: no build-host data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
