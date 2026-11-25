# rr unknown-llm (AI-generated)
"""DS-P2.148: the pfsoc kernel console must actually reach the FlashPro5 UART.

Both defects guarded here were proven on silicon — the on-card image chained
HSS -> OpenSBI -> U-Boot -> "Starting kernel ..." and then the console went
SILENT with no login. A JTAG halt showed the boot hart at ``arch_cpu_idle``
(``satp`` set, MMU on): the kernel BOOTED fine; the only fault was that its
console never reached the physical UART. Fixing the two config defects below
reached a root shell on MMUART0 (login-shell witness:
``docs/evidence/pf-l0/DS-P2.148-pf-l0b-login-shell-witness.md``).

DEFECT 1 — SOC-NODE SHADOW (root cause).
``rr linux dtb`` applies ``linux.dt_nodes`` as a dtc ROOT OVERLAY. The pfsoc base
kernel DTB (linux4microchip ``mpfs.dtsi``) has its peripheral bus at ``/soc``
(NO unit address). A dt_node parented at ``/soc@0`` therefore does NOT merge into
``/soc``; it creates a SECOND top-level node whose node-name (the part before
``@``) is ALSO ``soc``. Linux and U-Boot resolve a path component by
node-name-before-``@`` and take the FIRST match, so the injected ``/soc@0``
SHADOWS ``/soc``: the ``serial4`` alias target ``/soc/serial@20106000`` and the
``stdout-path`` resolve to the serial-less shadow, ttyS4 never appears, and the
console is silent. (The DE25 target correctly uses ``/soc@0`` because ITS base
DTB's bus IS named ``soc@0`` — the anchor is board-specific.)

DEFECT 2 — CONSOLE LINE MUST REGISTER ON THE DEPLOYED KERNEL (DS-P2.150).
The physical FlashPro5 console is ``serial@20106000``, which the vendor DTB
aliases ``serial4`` -> ``/dev/ttyS4``. The 8250 driver only creates lines
0..NR_UARTS-1, and the DEPLOYED on-card Image ships the base defconfig
NR_UARTS=4, so line 4 (ttyS4) is REJECTED and the console is silent — proven on a
plain cold boot (DS-P2.150). DS-P2.148's fix (raise NR_UARTS>=5 so ttyS4
registers) needs a 19 MiB kernel reflash the bench cannot stage (card in J17, no
host reader) and was never proven on silicon. The mapping that WAS proven to
reach a root shell instead re-aliases the physical UART to a line that EXISTS
under NR_UARTS=4: ``dt_nodes`` set ``serial0 -> /soc/serial@20106000`` and boot
``console=ttyS0`` (line 0). The base ``serial4 -> /soc/serial@20106000`` alias is
repointed off the physical node (else of_alias_get_id, first-match, returns 4 and
line 4 is rejected). DS-P2.150 disabled MMUART1 @0x2010_0000 so an unaliased
node could not auto-grab line 0. DS-P2.126 needs that same MMUART (the one
routed to the fabric) to have a Linux tty, so it is enabled as ``serial1``
(ttyS1, line 1 — not the console, and not line 4) and ``serial4`` is moved onto
a disabled MMUART2. This guard pins that whole mapping.

INPUT DOMAIN: the committed pfsoc target + kernel fragment, as the image build
reads them. The shadow assertion is on the RENDERED merged DTB (base bus + the
target's dt_nodes), built with the repo's own FDT builder so no ``dtc`` is
needed under cron/CI.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"
FRAGMENT = ROOT / "sw" / "linux" / "pfsoc" / "kernel_pfsoc.config-fragment"

# Load the repo's pure-python FDT tooling by path (tools/ is not a package).
_fdt_spec = importlib.util.spec_from_file_location(
    "fdt_fixture", Path(__file__).with_name("fdt_fixture.py"))
fdt_fixture = importlib.util.module_from_spec(_fdt_spec)
_fdt_spec.loader.exec_module(fdt_fixture)

_img_spec = importlib.util.spec_from_file_location(
    "de25_linux_image", ROOT / "tools" / "de25_linux_image.py")
de25_linux_image = importlib.util.module_from_spec(_img_spec)
_img_spec.loader.exec_module(de25_linux_image)

# PolarFire SoC MMUART bases, index == the serial<N> alias index == ttyS<N>.
MMUART_BASES = (0x20000000, 0x20100000, 0x20102000, 0x20104000, 0x20106000)
# The physical FlashPro5 console: HSS/OpenSBI/U-Boot all drive serial@20106000
# and their output reaches the FlashPro5 if02 UART (DS-P2.148 hw witness).
PHYS_CONSOLE_BASE = 0x20106000


def _target() -> dict:
    return yaml.safe_load(TARGET.read_text(encoding="utf-8"))


def _dt_nodes() -> list[dict]:
    return (_target().get("linux") or {}).get("dt_nodes") or []


def _bootargs() -> str:
    return str((_target().get("linux") or {})["bootargs"])


def _console_index() -> int:
    m = re.search(r"\bconsole=ttyS(\d+)", _bootargs())
    assert m, f"no console=ttySN in bootargs: {_bootargs()!r}"
    return int(m.group(1))


def _nr_uarts() -> int | None:
    for line in FRAGMENT.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*CONFIG_SERIAL_8250_NR_UARTS\s*=\s*(\d+)", line)
        if m:
            return int(m.group(1))
    return None


# The physical FlashPro5 console node, and the base riscv/mpfs 8250 ceiling the
# DEPLOYED on-card Image ships (lines 0..3 exist with no fragment override).
PHYS_CONSOLE_PATH = "/soc/serial@20106000"
BASE_NR_UARTS = 4


def _dt_node_props(path: str) -> dict:
    """Return the properties map declared for a dt_node path, or {}."""
    for node in _dt_nodes():
        if isinstance(node, dict) and node.get("path") == path:
            return node.get("properties") or {}
    return {}


def _merged_top_level_nodes() -> set[str]:
    """Render the base pfsoc soc bus MERGED with the target's dt_nodes (exactly
    as rr's dtc root overlay would) and return the set of top-level node names."""
    def s(text: str) -> bytes:
        return text.encode("ascii") + b"\x00"

    # A minimal but faithful base: the /soc bus carrying the two enabled MMUARTs,
    # plus the serial4 alias + stdout-path the kernel resolves for the console.
    nodes: dict[str, dict[str, bytes]] = {
        "/aliases": {"serial4": s("/soc/serial@20106000")},
        "/chosen": {"stdout-path": s("serial4:115200n8")},
        "/soc/serial@20106000": {
            "compatible": s("ns16550a"), "status": s("okay"),
            "reg": fdt_fixture.encode_cells([0, 0x20106000, 0, 0x400]),
        },
        "/soc/serial@20100000": {
            "compatible": s("ns16550a"), "status": s("okay"),
            "reg": fdt_fixture.encode_cells([0, 0x20100000, 0, 0x400]),
        },
    }
    # Apply the target's dt_nodes as the overlay (path is what matters here).
    for node in _dt_nodes():
        if not isinstance(node, dict) or not node.get("path"):
            continue
        props = node.get("properties") or {}
        emitted: dict[str, bytes] = {}
        if "compatible" in props:
            emitted["compatible"] = s(str(props["compatible"]))
        cells = fdt_fixture.encode_cells(props["reg"]) if "reg" in props else None
        if cells is not None:
            emitted["reg"] = cells
        nodes[str(node["path"])] = emitted

    blob = fdt_fixture.build_fdt(nodes)
    top: set[str] = set()
    for path, _name, _value in de25_linux_image._iter_fdt_props(blob):
        if path and "/" not in path:
            top.add(path)
    return top


def test_dt_nodes_do_not_shadow_the_soc_bus():
    """No injected dt_node may create a second top-level node named "soc": that
    shadow makes the serial4 alias / stdout-path resolve to a serial-less node,
    and the kernel console goes silent (DS-P2.148 root cause)."""
    top = _merged_top_level_nodes()
    soc_named = sorted(n for n in top if n.split("@", 1)[0] == "soc")
    assert soc_named == ["soc"], (
        f"dt_nodes create more than one top-level 'soc' node {soc_named}: a "
        "'/soc@N' anchor shadows the base '/soc' bus, so /soc/serial@20106000 "
        "(the serial4 alias + stdout-path) resolves to the serial-less shadow "
        "and the console is silent. Anchor fabric nodes OFF the 'soc' name."
    )
    # And the real serial node must survive so the alias resolves (paths from
    # _iter_fdt_props carry no leading slash).
    all_paths = {
        p for p, _n, _v in de25_linux_image._iter_fdt_props(
            fdt_fixture.build_fdt({
                "/soc/serial@20106000": {"reg": fdt_fixture.encode_cells([0, 0x20106000, 0, 0x400])},
                **{str(n["path"]): {} for n in _dt_nodes() if n.get("path")},
            }))
    }
    assert "soc/serial@20106000" in all_paths, (
        f"the console serial node /soc/serial@20106000 was displaced: {sorted(all_paths)}"
    )


def test_console_serial_alias_resolves_to_the_physical_flashpro5_uart():
    """console=ttyS<N> must resolve, via the DTB serial<N> alias, to the physical
    FlashPro5 UART (/soc/serial@20106000). DS-P2.150 re-aliases serial0 -> that
    node so of_alias gives the physical console line 0/ttyS0 under NR_UARTS=4."""
    idx = _console_index()
    alias = _dt_node_props("/aliases")
    target = alias.get(f"serial{idx}")
    assert target == PHYS_CONSOLE_PATH, (
        f"console=ttyS{idx} but dt_nodes /aliases serial{idx}={target!r}, not the "
        f"physical FlashPro5 console {PHYS_CONSOLE_PATH!r}: the kernel console + "
        "login getty would land on the wrong/dead UART and stay silent"
    )


def test_base_serial4_alias_is_repointed_off_the_physical_console():
    """The vendor DTB aliases serial4 -> /soc/serial@20106000. Under NR_UARTS=4
    that makes of_alias_get_id (first match, DT order) return 4 for the physical
    node, and the 8250 driver rejects line 4 -> no tty. dt_nodes must repoint
    serial4 OFF the physical node so it resolves to the low re-aliased line."""
    alias = _dt_node_props("/aliases")
    serial4 = alias.get("serial4")
    assert serial4 not in (None, PHYS_CONSOLE_PATH), (
        f"dt_nodes /aliases serial4={serial4!r} still points at (or leaves the "
        f"vendor alias on) the physical console {PHYS_CONSOLE_PATH!r}: "
        "of_alias_get_id returns 4, line 4 is rejected under NR_UARTS=4, and the "
        "console is silent after 'Starting kernel ...'"
    )


def test_fabric_mmuart_is_a_tty_on_a_line_that_is_not_the_console():
    """MMUART1 @0x2010_0000 is the fabric UART (DS-P2.126). DS-P2.150 disabled
    it so an unaliased node could not steal ttyS0; that also left Linux with
    no tty on the logger's UART. It must be enabled, aliased to a line that
    exists under NR_UARTS=4, and that line must not be the console line.
    serial4 must not name it either: of_alias_get_id's first match would
    return 4 and the 8250 driver would reject the node."""
    props = _dt_node_props("/soc/serial@20100000")
    assert str(props.get("status", "")).strip() == "okay", (
        "dt_nodes must set /soc/serial@20100000 status=okay so Linux can drive "
        f"the fabric MMUART; got status={props.get('status')!r}"
    )
    alias = _dt_node_props("/aliases")
    assert alias.get("serial1") == "/soc/serial@20100000", (
        f"fabric MMUART must be serial1 (ttyS1); aliases={alias!r}"
    )
    console = _console_index()
    assert console != 1, (
        f"console=ttyS{console} collides with the fabric MMUART's serial1 alias"
    )
    assert alias.get("serial4") not in (
        "/soc/serial@20100000", PHYS_CONSOLE_PATH, None,
    ), (
        f"serial4={alias.get('serial4')!r} still names the fabric MMUART or the "
        "physical console"
    )


def test_console_line_registers_under_the_deployed_nr_uarts():
    """The console line index must be < the DEPLOYED kernel's line count so it
    registers on a PLAIN cold boot. The on-card Image ships the base defconfig
    NR_UARTS=4 (lines 0..3); the fix must NOT depend on an un-reflashable
    NR_UARTS bump, so the console line must fit under the base ceiling."""
    idx = _console_index()
    nr = _nr_uarts()
    floor = BASE_NR_UARTS if nr is None else min(nr, BASE_NR_UARTS)
    assert idx < floor, (
        f"console=ttyS{idx} but the deployed kernel creates only lines 0..{floor - 1} "
        f"(base defconfig NR_UARTS={BASE_NR_UARTS}"
        f"{'' if nr is None else f', fragment sets {nr}'}) — line {idx} is rejected "
        "and the console is silent. Re-alias the physical UART to a line below the "
        "base ceiling instead of relying on an un-reflashable NR_UARTS bump."
    )
