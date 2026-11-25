#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Compose the Zybo-Z7-20 deployment DTB: bake ds_ip into /chosen/bootargs.

DS-P2.216. The Zybo rootfs's S40network reads ``ds_ip=<addr>/<prefix>`` from the
kernel command line and otherwise asks DHCP, which the bench link does not
serve. DS-D.39 keeps the address out of the public target, so it is composed at
the image boundary from the deployment descriptor, as for the DE25 and the
PolarFire SoC:

  1. the bootargs come from ``de25_linux_image.deployment_bootargs`` with the
     Zybo pair (``targets/zybo_datalogger.yml`` linux.bootargs + the
     ``z7_20_primary`` bench_ip), the resolver ``tools/check_bench_ip.py
     --target zybo`` also uses, so the token written here is the token the
     checker demands;
  2. they replace ``/chosen/bootargs`` of a Zynq DTB, through the FDT writer
     ``pfsoc_linux_image.set_chosen_bootargs``.

The first choice is rr itself:

    rr linux dtb --refresh --bootargs "$(tools/check_bench_ip.py --target zybo --print-bootargs)"

That needs sdtgen from a registered Vitis 2025.1 or later. Where none is
registered, this tool patches an existing Zybo DTB (the one a TFTP slot or an
SD card already serves) instead. It exists only for that gap: RTL-P3.2385 asks
rr to set /chosen/bootargs in an existing DTB without sdtgen. Retire this tool
once that lands, as for tools/pfsoc_linux_image.py.

    tools/zybo_linux_image.py compose --base-dtb <slot>/system.dtb --output <new-slot>/system.dtb
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check_bench_ip = _load("check_bench_ip")
pfsoc_linux_image = _load("pfsoc_linux_image")

ZYBO = check_bench_ip.BOARDS["zybo"]
DEFAULT_OUTPUT_REL = ZYBO["default_dtb"]


def compose(root: Path, base_dtb: Path, output: Path) -> str:
    """Write ``base_dtb`` with the Zybo deployment bootargs to ``output``.

    Returns the bootargs. Raises ``RuntimeError`` for a missing or malformed
    base DTB and for a descriptor the resolver refuses."""
    if not base_dtb.is_file():
        raise RuntimeError(f"base Zybo DTB missing: {base_dtb}")
    bootargs = check_bench_ip.de25_linux_image.deployment_bootargs(
        root, target_rel=ZYBO["target_rel"], bench_target=ZYBO["bench_target"])
    try:
        patched = pfsoc_linux_image.set_chosen_bootargs(base_dtb.read_bytes(), bootargs)
    except ValueError as exc:
        raise RuntimeError(f"{base_dtb}: {exc}") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.write_bytes(patched)
    tmp.replace(output)
    return bootargs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("compose", help="bake the deployment ds_ip into a Zybo DTB")
    c.add_argument("--root", type=Path, default=ROOT)
    c.add_argument("--base-dtb", type=Path, required=True,
                   help="an existing Zybo-Z7-20 DTB (rr linux dtb output, or a slot's)")
    c.add_argument("--output", type=Path, default=None,
                   help=f"output DTB (default: {DEFAULT_OUTPUT_REL})")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    output = args.output if args.output is not None else root / DEFAULT_OUTPUT_REL
    try:
        bootargs = compose(root, args.base_dtb, output)
    except RuntimeError as exc:
        print(f"REFUSED: {exc}")
        return 1
    print(f"composed {output} with bootargs: {bootargs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
