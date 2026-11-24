"""DS-T1.15: an enabled ctucanfd node in the DE25 dtb is allowed ONLY when the
fabric BOUNDS a wedged CTU probe — the axil_demux3 completion watchdog armed in
deepskopion_de25_top (``wd_cycles_g /= 0``).

This RE-AIMS the DS-T1.11 interim guard (which refused ANY enabled ctucanfd node)
to the invariant the DS-T1.15 fix actually establishes.  MEASURED 2026-09-03
(DS-T1.11): a from-head de25 build with the ctu,ctucanfd nodes enabled hung the
kernel deterministically at ``[0.443] CAN device driver interface`` — the
mainline probe's MODE.RST-poll of the CTU aperture wedged the lwh2f master with
no bound.  DS-T1.15 lands the axil_demux3 completion watchdog: it force-completes
a slave response that never arrives, so the probe -ETIMEDOUTs and boot continues
instead of hanging.  With that bound armed, an enabled ctucanfd node is safe; a
build that ENABLES the node but DISARMS the watchdog is back to the unbounded
hang and must be refused.

The suite drives the REAL ``check-ctucanfd`` CLI (exit code + printed diagnosis)
and the real ``build()`` against RENDERED dtb blobs, over a repo tree whose top
carries the watchdog ARMED or DISARMED — so the discriminating axis is exactly
"is the fabric bound present", the thing the fix adds.  Assertions are on the CLI
exit code and the driven build's refusal, not a helper's return value.

BASELINE / MUTATION are built into the tests themselves: the same enabled-node
dtb is ACCEPTED with the watchdog armed (the shipped top) and REFUSED with the
watchdog disarmed (``wd_cycles_g => 0`` — the mutation that removes the fix),
which is what proves the guard is not vacuous.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import struct
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "de25_linux_image.py"
TOP_REL = "hw/rtl/de25/deepskopion_de25_top.vhd"

_fdt_spec = importlib.util.spec_from_file_location(
    "fdt_fixture", Path(__file__).with_name("fdt_fixture.py"))
_fdt_fixture = importlib.util.module_from_spec(_fdt_spec)
_fdt_spec.loader.exec_module(_fdt_fixture)

CAN0 = "/soc@0/can@20010000"
CAN1 = "/soc@0/can@20020000"
CAN_SPAN = 0x10000


def _dtb_with_can(tmp_path: Path, nodes: dict[str, bytes | None]) -> Path:
    tree: dict[str, dict[str, bytes]] = {}
    for i, (path, status) in enumerate(nodes.items()):
        base = 0x20010000 + i * 0x10000
        props: dict[str, bytes] = {
            "compatible": b"ctu,ctucanfd\x00",
            "reg": struct.pack(">II", base, CAN_SPAN),
        }
        if status is not None:
            props["status"] = status
        tree[path] = props
    dtb = tmp_path / "board.dtb"
    dtb.write_bytes(_fdt_fixture.build_fdt(tree))
    return dtb


def _root_with_watchdog(tmp_path: Path, cycles: int) -> Path:
    """A minimal repo root whose deepskopion_de25_top carries the axil_demux3
    watchdog set to ``cycles`` (0 == disarmed).  ``check-ctucanfd`` reads only
    this file to learn the fabric bound, so this is the whole discriminating
    surface."""
    root = tmp_path / f"root_wd{cycles}"
    (root / "hw/rtl/de25").mkdir(parents=True)
    top = (ROOT / TOP_REL).read_text()
    mutated, n = re.subn(r"wd_cycles_g\s*=>\s*\d+", f"wd_cycles_g => {cycles}", top, count=1)
    assert n == 1, "failed to locate/rewrite wd_cycles_g in the top fixture"
    (root / TOP_REL).write_text(mutated)
    return root


def _run(root: Path, dtb: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["python3", str(TOOL), "check-ctucanfd", "--root", str(root), "--dtb", str(dtb)],
        cwd=ROOT, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


# ── the shipped fabric really arms the watchdog (ties the guard to reality) ──

def test_shipped_top_arms_the_watchdog():
    spec = importlib.util.spec_from_file_location("de25_linux_image", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.demux_watchdog_cycles(ROOT) > 0, (
        "deepskopion_de25_top must arm the axil_demux3 completion watchdog "
        "(wd_cycles_g /= 0) — without it an enabled ctucanfd node is unbounded"
    )


# ── accept ──────────────────────────────────────────────────────────────────

def test_can_free_dtb_is_accepted(tmp_path):
    dtb = tmp_path / "clean.dtb"
    dtb.write_bytes(_fdt_fixture.build_fdt(
        {"/soc@0/serial@10c02100": {"status": b"okay\x00"}}))
    rc, out = _run(ROOT, dtb)
    assert rc == 0, out
    assert "probe-bound check passed" in out


def test_disabled_ctucanfd_node_is_accepted_even_without_watchdog(tmp_path):
    """A disabled node is never probed, so it is safe regardless of the fabric."""
    dtb = _dtb_with_can(tmp_path, {CAN0: b"disabled\x00"})
    rc, out = _run(_root_with_watchdog(tmp_path, 0), dtb)
    assert rc == 0, out


def test_enabled_ctucanfd_is_ACCEPTED_when_watchdog_armed(tmp_path):
    """The exit state: an enabled node is allowed because the demux watchdog
    bounds a wedged probe.  This is what the DS-T1.11 interim forbade."""
    dtb = _dtb_with_can(tmp_path, {CAN0: None, CAN1: None})
    rc, out = _run(_root_with_watchdog(tmp_path, 4096), dtb)
    assert rc == 0, out
    assert "probe-bound check passed" in out


# ── the defect this guard exists to catch (MUTATION: watchdog disarmed) ──────

def test_enabled_ctucanfd_is_REFUSED_when_watchdog_disarmed(tmp_path):
    dtb = _dtb_with_can(tmp_path, {CAN0: None})
    rc, out = _run(_root_with_watchdog(tmp_path, 0), dtb)
    assert rc == 1, out
    assert CAN0 in out
    assert "wd_cycles_g=0" in out


def test_both_nodes_enabled_refused_when_watchdog_disarmed(tmp_path):
    dtb = _dtb_with_can(tmp_path, {CAN0: None, CAN1: None})
    rc, out = _run(_root_with_watchdog(tmp_path, 0), dtb)
    assert rc == 1, out
    assert CAN0 in out and CAN1 in out


def test_okay_status_node_refused_when_watchdog_disarmed(tmp_path):
    dtb = _dtb_with_can(tmp_path, {CAN0: b"okay\x00"})
    rc, out = _run(_root_with_watchdog(tmp_path, 0), dtb)
    assert rc == 1, out
    assert CAN0 in out


# ── the guard must GATE the build, at the shipped-fabric surface ─────────────

def test_build_accepts_enabled_node_with_the_shipped_watchdog(tmp_path, monkeypatch):
    """The real build() with the SHIPPED top (watchdog armed) must NOT refuse an
    enabled ctucanfd dtb — that is the exit state the ticket requires.  Drive the
    real build() with a recording subprocess and a dtb the dtb step 'produced'."""
    import shutil
    spec = importlib.util.spec_from_file_location("de25_linux_image", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root = tmp_path / "tree"
    for rel in ("project.yml", "targets/de25_datalogger.yml",
                "sw/linux/de25/system_memory_map.yml",
                "hw/rtl/de25/deepskopion_de25_top.vhd",
                "hw/rtl/de25/axil_demux3.vhd"):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    out = root / "sw/linux/de25/out/de25_datalogger"
    out.mkdir(parents=True)
    realised = _fdt_fixture.dtb_realising(root / "targets/de25_datalogger.yml")
    nodes: dict[str, dict[str, bytes]] = {}
    for node_path, name, value in mod._iter_fdt_props(realised):
        entry = nodes.setdefault("/" + node_path, {})
        if name is not None:
            entry[name] = value
    nodes.pop("/", None)
    nodes[CAN0] = {"compatible": b"ctu,ctucanfd\x00",
                   "reg": struct.pack(">II", 0x20010000, CAN_SPAN)}
    (out / "socfpga_agilex5_socdk.dtb").write_bytes(_fdt_fixture.build_fdt(nodes))

    recorded: list[str] = []

    def _record(argv, **kwargs):
        recorded.append(" ".join(argv))
        class _Done:
            returncode = 0
        return _Done()

    monkeypatch.setattr(mod.subprocess, "run", _record)
    monkeypatch.setattr(mod, "deployment_bootargs", lambda _r: "console=ttyS1")
    monkeypatch.setattr(mod, "rebuild_uboot_with_emac0_phy_fix", lambda _r: None)
    # DS-P2.310: covered by test_de25_uboot_bootargs_ds_p2_310.py; the rebuild
    # stub leaves no itb.
    monkeypatch.setattr(mod, "assert_uboot_itb_default_bootargs", lambda *_a: None, raising=False)
    monkeypatch.setattr(mod, "_stage_target_stem_sof", lambda _r: None)
    monkeypatch.setattr(mod, "normalize_provenance", lambda _r, _o: None)
    monkeypatch.setattr(mod, "normalize_artifact_mtimes", lambda _o: None)

    # Watchdog armed in the shipped top → the enabled ctucanfd node is bounded →
    # build() runs through the dtb+bootscr+sdcard steps without refusing.
    monkeypatch.setattr(mod, "rootfs_image_problems", lambda *_a, **_k: [])  # DS-P3.316: build() now reads the card image; this test stubs every tool run
    mod.build(root)
    assert any("linux dtb" in c for c in recorded), recorded
    assert any("linux bootscr" in c for c in recorded), recorded
    assert any("linux sdcard" in c for c in recorded), recorded


def test_build_refuses_enabled_node_when_watchdog_disarmed(tmp_path, monkeypatch):
    """MUTATION at the build surface: disarm the watchdog in the copied top and
    the same enabled-node dtb must be refused AFTER `rr linux dtb`, before
    boot.scr / the SD image — the DS-T1.11 hang is unbounded again."""
    import shutil
    spec = importlib.util.spec_from_file_location("de25_linux_image", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root = tmp_path / "tree"
    for rel in ("project.yml", "targets/de25_datalogger.yml",
                "sw/linux/de25/system_memory_map.yml",
                "hw/rtl/de25/deepskopion_de25_top.vhd",
                "hw/rtl/de25/axil_demux3.vhd"):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    # Disarm the watchdog in the copied top (the mutation).
    top = (root / "hw/rtl/de25/deepskopion_de25_top.vhd")
    top.write_text(re.sub(r"wd_cycles_g\s*=>\s*\d+", "wd_cycles_g => 0",
                          top.read_text(), count=1))
    out = root / "sw/linux/de25/out/de25_datalogger"
    out.mkdir(parents=True)
    realised = _fdt_fixture.dtb_realising(root / "targets/de25_datalogger.yml")
    nodes: dict[str, dict[str, bytes]] = {}
    for node_path, name, value in mod._iter_fdt_props(realised):
        entry = nodes.setdefault("/" + node_path, {})
        if name is not None:
            entry[name] = value
    nodes.pop("/", None)
    nodes[CAN0] = {"compatible": b"ctu,ctucanfd\x00",
                   "reg": struct.pack(">II", 0x20010000, CAN_SPAN)}
    (out / "socfpga_agilex5_socdk.dtb").write_bytes(_fdt_fixture.build_fdt(nodes))

    recorded: list[str] = []

    def _record(argv, **kwargs):
        recorded.append(" ".join(argv))
        class _Done:
            returncode = 0
        return _Done()

    monkeypatch.setattr(mod.subprocess, "run", _record)
    monkeypatch.setattr(mod, "deployment_bootargs", lambda _r: "console=ttyS1")
    monkeypatch.setattr(mod, "rebuild_uboot_with_emac0_phy_fix", lambda _r: None)
    # DS-P2.310: covered by test_de25_uboot_bootargs_ds_p2_310.py; the rebuild
    # stub leaves no itb.
    monkeypatch.setattr(mod, "assert_uboot_itb_default_bootargs", lambda *_a: None, raising=False)
    monkeypatch.setattr(mod, "_stage_target_stem_sof", lambda _r: None)
    monkeypatch.setattr(mod, "normalize_provenance", lambda _r, _o: None)
    monkeypatch.setattr(mod, "normalize_artifact_mtimes", lambda _o: None)

    with pytest.raises(RuntimeError, match="DS-T1.15"):
        mod.build(root)
    assert any("linux dtb" in c for c in recorded), recorded
    assert not any("linux bootscr" in c for c in recorded), recorded
    assert not any("linux sdcard" in c for c in recorded), recorded
