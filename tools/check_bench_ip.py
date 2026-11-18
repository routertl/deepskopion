#!/usr/bin/env python3
"""Accept or refuse a Linux image by the bench identity it actually carries.

Boards: de25 (default), pfsoc (DS-P2.125) and zybo (DS-P2.216).

DS-P3.152 moved the DE25's network identity out of ``targets/de25_datalogger.yml``
(DS-D.39: the public target must name no bench address) and into the
deploy-time descriptor ``runtimes/hw.local.yml``, injected into ``/chosen/bootargs``
at the image boundary by ``tools/de25_linux_image.py``.

That move creates a gap nothing was checking.  The declaration and the shipped
image are now two separate artifacts, and the only place they were ever
compared was the build that wrote them in the same breath.  A card flashed from
an older ``out/`` tree, a descriptor edited after the image was built, or a
board handed to a second deployment all produce the same silent failure mode:
an image whose ``ds_ip=`` is not the address this deployment declares, which
comes up on the wrong address (or with no address at all, since the DE25 bench
has no DHCP) and looks exactly like a dead network.

So this is the check that closes the loop, at the bench, against the artifact
that ships:

  * the DECLARED side is resolved through ``de25_linux_image`` itself — the
    same function the builder uses — so a descriptor this checker accepts is
    one the builder would also accept, and a malformed descriptor is refused
    with the builder's own diagnosis rather than a second opinion;
  * the CARRIED side is read out of a real artifact: the DTB's
    ``/chosen/bootargs`` (what ``rr linux dtb`` bakes in and U-Boot boots), or
    a ``/proc/cmdline`` captured off the running board (``--cmdline-file``),
    which is the only surface that proves what the KERNEL received.

Exit 0 = accept, 1 = refuse (with the reasons on stdout), 2 = usage error.

    tools/check_bench_ip.py                      # built image vs runtimes/hw.local.yml
    tools/check_bench_ip.py --cmdline-file live_cmdline.txt
    tools/check_bench_ip.py --hw /tmp/wrong.yml  # the refusal case
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]

# tools/ has no __init__.py; load the sibling module by file path, matching
# de25_linux_image.py's own convention for reaching de25_toolchain_stage.
_spec = importlib.util.spec_from_file_location(
    "de25_linux_image", Path(__file__).with_name("de25_linux_image.py")
)
de25_linux_image = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(de25_linux_image)

CMDLINE_IP_KEY = de25_linux_image.CMDLINE_IP_KEY

#: Per-board resolution: which public target yml carries the base bootargs,
#: which bench.targets.<name> in hw.yml declares the address, and where the
#: built image DTB lands by default.  DS-P2.125 (PF-L0) adds the third board;
#: both resolve through the SAME de25_linux_image.deployment_bootargs, so a
#: descriptor this checker accepts is one an image builder would also accept.
BOARDS = {
    "de25": {
        "target_rel": "targets/de25_datalogger.yml",
        "bench_target": de25_linux_image.BENCH_TARGET,
        "default_dtb": "sw/linux/de25/out/de25_datalogger/socfpga_agilex5_socdk.dtb",
        # The bench-specific half of the missing-token diagnosis: which bench,
        # and the ticket that moved its identity out of the public target. Kept
        # per-board so `--target pfsoc` never prints the DE25's DHCP sentence
        # (DS-P2.125 land refusal: a wrong-board diagnosis unguarded).
        "no_dhcp_note": "The DE25 bench has no DHCP, so this image comes up "
                        "with eth0 unconfigured (DS-P3.152)",
    },
    "zybo": {
        # DS-P2.216: the Zybo-Z7-20 image. DS-P1.103: zybo_datalogger is the product
        # target (the bench CTU target carries byte-identical linux.bootargs).
        # `rr linux dtb` writes <project>/out/system.dtb; build it with
        #   rr linux dtb --refresh --bootargs \
        #     "$(tools/check_bench_ip.py --target zybo --print-bootargs)"
        # (sdtgen, Vitis 2025.1+), or patch an existing Zybo DTB with
        # tools/zybo_linux_image.py compose.
        "target_rel": "targets/zybo_datalogger.yml",
        "bench_target": "z7_20_primary",
        "default_dtb": "out/system.dtb",
        "no_dhcp_note": "The Zybo-Z7-20 bench link has no DHCP server, so this "
                        "image comes up with eth0 unconfigured (DS-P2.216)",
    },
    "pfsoc": {
        "target_rel": "targets/pfsoc_datalogger.yml",
        "bench_target": "pfsoc_bench",
        # Produced by tools/pfsoc_linux_image.py compose (DS-P2.125): it patches
        # the deployment ds_ip= into the MSS/HSS-staged base DTB's
        # /chosen/bootargs, the artifact U-Boot boots and this checker reads.
        "default_dtb": "build/pfsoc_datalogger/linux/system.dtb",
        "no_dhcp_note": "The PolarFire SoC Discovery Kit bench has no DHCP, so "
                        "this image comes up with eth0 unconfigured (DS-P2.125)",
    },
}
DEFAULT_BOARD = "de25"
#: Kept for backward compatibility with callers/imports predating --target.
DEFAULT_DTB = BOARDS[DEFAULT_BOARD]["default_dtb"]


def declared_token(root: Path, board: str = DEFAULT_BOARD) -> str:
    """The one ``ds_ip=<addr>/<prefix>`` token this deployment declares.

    Resolved by composing the real bootargs, so the declaration is read
    exactly as the image builder reads it — including its refusals for a
    missing, malformed or ambiguous descriptor.
    """
    spec = BOARDS[board]
    composed = de25_linux_image.deployment_bootargs(
        root, target_rel=spec["target_rel"], bench_target=spec["bench_target"])
    tokens = [t for t in composed.split() if t.startswith(f"{CMDLINE_IP_KEY}=")]
    if len(tokens) != 1:
        raise RuntimeError(
            f"deployment_bootargs produced {len(tokens)} {CMDLINE_IP_KEY}= "
            f"tokens, expected exactly 1: {composed!r}"
        )
    return tokens[0]


def dtb_bootargs(dtb: Path) -> str:
    """``/chosen/bootargs`` out of a flattened devicetree blob.

    Raises ``RuntimeError`` when the blob carries no such property — an image
    with no kernel command line is refused, never treated as "no address
    declared, therefore fine".
    """
    blob = dtb.read_bytes()
    for node_path, name, value in de25_linux_image._iter_fdt_props(blob):
        if node_path == "chosen" and name == "bootargs":
            return value.rstrip(b"\x00").decode("utf-8", "replace")
    raise RuntimeError(f"{dtb}: no /chosen/bootargs property in this dtb")


def check(root: Path, carried: str, source: str,
          board: str = DEFAULT_BOARD) -> list[str]:
    """Return every reason to refuse ``carried``.  Empty list == accept."""
    problems: list[str] = []
    want = declared_token(root, board)
    got = [t for t in carried.split() if t.startswith(f"{CMDLINE_IP_KEY}=")]
    if not got:
        problems.append(
            f"{source}: carries no {CMDLINE_IP_KEY}= token, but this "
            f"deployment declares {want}. {BOARDS[board]['no_dhcp_note']}"
        )
    elif len(got) > 1:
        problems.append(
            f"{source}: carries {len(got)} {CMDLINE_IP_KEY}= tokens "
            f"({' '.join(got)}); S40network consumes the first and the image's "
            "address becomes ordering-dependent"
        )
    elif got[0] != want:
        problems.append(
            f"{source}: carries {got[0]} but this deployment declares {want} "
            "— the image was built against a different descriptor, or the "
            "descriptor changed after the image was built (DS-D.39)"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Accept/refuse a Linux image against its declared bench IP.")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="project root (default: this checkout)")
    parser.add_argument("--target", choices=sorted(BOARDS), default=DEFAULT_BOARD,
                        help="which board's image to check (default: "
                             f"{DEFAULT_BOARD}). PF-L0 (DS-P2.125) added pfsoc.")
    parser.add_argument("--hw", type=Path, default=None,
                        help="deployment descriptor to check against "
                             "(default: $RR_BENCH_CONFIG or runtimes/hw.local.yml)")
    parser.add_argument("--dtb", type=Path, default=None,
                        help="image DTB to read (default: the board's built dtb)")
    parser.add_argument("--print-bootargs", action="store_true",
                        help="print the board's deployment bootargs (the public "
                             "target's linux.bootargs plus the declared ds_ip=) "
                             "for `rr linux dtb --bootargs`, and exit")
    parser.add_argument("--cmdline-file", type=Path, default=None,
                        help="a /proc/cmdline captured off the running board; "
                             "checks what the KERNEL received instead of what "
                             "the dtb carries")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    board = args.target
    if args.hw is not None:
        # _deployment_hw_path honours RR_BENCH_CONFIG; setting it is how the
        # checker aims at an alternative descriptor without a second resolver.
        os.environ["RR_BENCH_CONFIG"] = str(args.hw)

    if args.print_bootargs:
        spec = BOARDS[board]
        try:
            print(de25_linux_image.deployment_bootargs(
                root, target_rel=spec["target_rel"], bench_target=spec["bench_target"]))
        except RuntimeError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        if args.cmdline_file is not None:
            carried = args.cmdline_file.read_text(encoding="utf-8").strip()
            source = f"live cmdline {args.cmdline_file}"
        else:
            dtb = (args.dtb if args.dtb is not None
                   else root / BOARDS[board]["default_dtb"])
            carried = dtb_bootargs(dtb)
            source = f"image dtb {dtb}"
        problems = check(root, carried, source, board)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"REFUSED: {exc}")
        return 1

    if problems:
        print("REFUSED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"ACCEPTED: {source} carries {declared_token(root, board)}, "
          "matching this deployment's declaration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
