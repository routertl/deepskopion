"""Kernel config fragments must be DECLARED, not merely shipped (DS-X5).

WHAT ESCAPED. ``sw/linux/kernel_emio.config-fragment`` (CONFIG_SPI_SPIDEV=y)
and ``sw/linux/kernel_ctucanfd.config-fragment`` (CONFIG_CAN_CTUCANFD_PLATFORM=y)
were tracked in-tree, each with a header explaining which feature it enables —
and NO target referenced either one. ``rr linux kernel`` resolves its fragment
list from ``$KERNEL_CONFIG_FRAGMENTS``, ``project.yml``, and auto-discovery of
``sw/linux/out/*.config-fragment`` (a gitignored build directory), so a
canonical build from a clean checkout applied neither. Read from the deployed
board's own ``/proc/config.gz`` on 2026-08-13:

    # CONFIG_SPI_SPIDEV is not set
    # CONFIG_CAN_CTUCANFD_PLATFORM is not set

The consequences were exactly the features the fragments exist for. No
``/dev/spidev1.0``, so ``SelfTestBench()`` raised ``FileNotFoundError`` and
PS-LOOP-REQ-7 / REQ-9 could not run at all — the SKIP that DS-X5 forbids. And
no ``ctucanfd`` binding for the CTU CAN FD cores the target is named after.

WHY A TEST AND NOT JUST A FIX. The fragments were correct the whole time; the
missing thing was the DECLARATION, which nothing looked at. This guard fails
when a fragment exists but no target names it, and when a named fragment loses
the symbol it exists to set.

Parsed as text, not YAML: the assertion is the literal presence of the
declaration, and it must not go quiet because a parser is unavailable.
"""
from __future__ import annotations

import sys
import re
from pathlib import Path

# tests/ -> linux/ -> sw/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "tools"))
from target_files import all_target_files  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
_LINUX_DIR = _REPO_ROOT / "sw" / "linux"
_TARGETS_DIR = _REPO_ROOT / "targets"

# The symbol(s) each tracked fragment exists to set, and the runtime artefact
# that disappears without each. Keyed by fragment filename; the value is a list
# so a fragment that is load-bearing for more than one runtime surface pins
# EVERY such symbol — a single-symbol pin let a mutation delete the OTHER
# load-bearing line and stay green (DS-P2.125 land refusal: deleting both
# CONFIG_UIO_PDRV_GENIRQ and CONFIG_MACB from the pfsoc fragment left the whole
# suite green).
_REQUIRED_SYMBOLS = {
    "kernel_emio.config-fragment": [
        ("CONFIG_SPI_SPIDEV=y",
         "/dev/spidev1.0 — PS-LOOP-REQ-7 and REQ-9 cannot run without it"),
    ],
    "kernel_ctucanfd.config-fragment": [
        ("CONFIG_CAN_CTUCANFD_PLATFORM=y",
         "the CTU CAN FD cores never bind to SocketCAN"),
    ],
    # DE25 (socfpga, aarch64): the ring's UIO watermark device and eth0.
    "kernel_de25.config-fragment": [
        ("CONFIG_UIO_PDRV_GENIRQ=y",
         "no /dev/uio* for the ring watermark — the SKIP-shaped failure DS-X5 "
         "forbids"),
        ("CONFIG_DWMAC_SOCFPGA=y",
         "the Agilex EMAC never binds, eth0 never appears and S40network fails"),
        # DS-P2.227: the image loads no modules, so a =m protocol is absent.
        ("CONFIG_CAN_RAW=y",
         "socket(AF_CAN, SOCK_RAW, CAN_RAW) fails with EPROTONOSUPPORT, "
         "\"request_module (can-proto-1) failed\" (DE25 silicon, DS-P3.211)"),
        ("CONFIG_CAN_BCM=y",
         "socket(AF_CAN, SOCK_DGRAM, CAN_BCM), the broadcast manager, is "
         "unavailable for the same reason"),
    ],
    # PolarFire SoC (riscv64): the two load-bearing symbols named in the
    # fragment's own header. Both are pinned so a mutation cannot delete either
    # and stay green.
    "kernel_pfsoc.config-fragment": [
        ("CONFIG_UIO_PDRV_GENIRQ=y",
         "no /dev/uio* — the daemon finds no watermark device and the ring is "
         "unreadable (the SKIP-shaped failure DS-X5 forbids)"),
        ("CONFIG_MACB=y",
         "the MPFS Cadence GEM never binds, eth0 never appears, S40network's "
         "`ip link set eth0 up` fails and the ds_ip= address is unreachable"),
    ],
}


def _tracked_fragments() -> list[Path]:
    # rglob, not glob: fragments live under per-board subdirs too
    # (sw/linux/de25/, sw/linux/pfsoc/). A non-recursive glob never saw them,
    # so neither the declaration guard nor the symbol guard covered any
    # per-board fragment (DS-P2.125 land refusal).
    return sorted(_LINUX_DIR.rglob("*.config-fragment"))


def _declared_fragment_names() -> set[str]:
    """Every fragment filename named by any target yml or project.yml."""
    named: set[str] = set()
    ymls = list(all_target_files(_REPO_ROOT))  # DS-P1.103: products and bench
    project_yml = _REPO_ROOT / "project.yml"
    if project_yml.is_file():
        ymls.append(project_yml)
    for yml in ymls:
        body = yml.read_text(encoding="utf-8", errors="replace")
        # Only DECLARATIONS count — a mention inside a comment does not
        # configure anything. Strip comment tails before matching.
        code = "\n".join(
            line.split("#", 1)[0] for line in body.splitlines()
        )
        named.update(re.findall(r"[\w./-]+\.config-fragment", code))
    return {Path(n).name for n in named}


def test_every_tracked_kernel_fragment_is_declared_by_a_target():
    fragments = _tracked_fragments()
    assert fragments, (
        f"no *.config-fragment under {_LINUX_DIR} — either they moved or this "
        "guard is looking in the wrong place, and a guard that asserts nothing "
        "is worse than none."
    )
    declared = _declared_fragment_names()
    undeclared = [f.name for f in fragments if f.name not in declared]
    assert not undeclared, (
        "kernel config fragment(s) are tracked in-tree but declared by NO "
        "target — `rr linux kernel` will build from the bare defconfig and the "
        "features they enable will be silently absent from the image "
        "(measured on silicon, DS-X5):\n  " + "\n  ".join(sorted(undeclared))
        + "\nDeclare them in the target's linux.kernel_config."
    )


def test_declared_fragments_still_carry_the_symbol_they_exist_for():
    for fragment in _tracked_fragments():
        expected = _REQUIRED_SYMBOLS.get(fragment.name)
        if expected is None:
            continue
        body = fragment.read_text(encoding="utf-8", errors="replace")
        assignments = [
            line.strip() for line in body.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for symbol, consequence in expected:
            assert symbol in assignments, (
                f"{fragment.name} no longer sets {symbol}. Without it: "
                f"{consequence}."
            )


def test_every_load_bearing_fragment_has_its_symbols_pinned():
    """The symbol guard above is only as strong as _REQUIRED_SYMBOLS. A
    per-board fragment added with no entry here is a fragment whose contents no
    test constrains — exactly how the pfsoc fragment shipped unguarded. Every
    fragment that lives under a per-board subdir (i.e. is board-specific, not a
    shared feature toggle) must pin at least one symbol."""
    unpinned = [
        f.relative_to(_REPO_ROOT)
        for f in _tracked_fragments()
        if f.parent != _LINUX_DIR and f.name not in _REQUIRED_SYMBOLS
    ]
    assert not unpinned, (
        "board-specific kernel fragment(s) pin no required symbol, so a "
        "mutation deleting their load-bearing lines stays green — add each to "
        "_REQUIRED_SYMBOLS with the runtime artefact it exists for:\n  "
        + "\n  ".join(str(p) for p in unpinned)
    )
