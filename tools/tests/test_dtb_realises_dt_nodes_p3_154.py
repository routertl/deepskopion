"""DS-P3.154: the built board dtb must realise the dt_nodes it was built from.

DS-T2.71's crosscheck proves the CONFIGURATION is legal — every enabled
dt_node lands in a window the fabric decodes.  It says nothing about whether
the dtb that actually ships carries that configuration, and those two claims
came apart in practice.

MECHANISM: ``rr linux dtb`` applies ``linux.dt_nodes`` as a dtc ROOT OVERLAY
appended to the decompiled board dtb, and dtc merges by "later property wins".
A property the overlay does not mention SURVIVES from the base.  So re-patching
an already-patched dtb keeps the earlier revision's properties: deleting
``status: disabled`` from a dt_node to re-enable it leaves the shipped dtb with
``status = "disabled"`` still on the node.  The build prints "applied dt_nodes:
… can@20010000", the target says ENABLED, and the kernel skips the endpoint.

WHY IT MATTERS HERE: that is precisely how DS-T2.71's silicon half could be
witnessed GREEN while proving nothing — a ctucanfd probe that never runs cannot
wedge the NoC.  The bench's own shipped dtb (built 2026-08-26, before DS-T2.71
re-enabled the nodes) is a live instance of the defect.

INPUT DOMAIN: any (target dt_nodes, built dtb) pair the build can produce,
including one where the dtb was patched by an earlier revision of the target.

OUTPUT CONTRACT: ``check-dtb-nodes`` exits 0 only when every declared dt_node
exists in the dtb with the declared ``status`` and the declared ``reg``.

PROHIBITION: an enabled declaration must never be accepted against a dtb that
carries ``status = "disabled"``, and a declared ``reg`` must never be accepted
against a different window.

Assertions are on the CLI exit code and printed diagnosis.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "de25_linux_image.py"

# tools/ is not a package; load the shared FDT fixture builder by path.
_fdt_spec = importlib.util.spec_from_file_location(
    "fdt_fixture", Path(__file__).with_name("fdt_fixture.py"))
_fdt_fixture = importlib.util.module_from_spec(_fdt_spec)
_fdt_spec.loader.exec_module(_fdt_fixture)

CAN_PATH = "/soc@0/can@20010000"
CAN_BASE = 0x20010000
CAN_SPAN = 0x10000


def _can_dtb(tmp_path: Path, *, status: bytes | None,
             base: int = CAN_BASE) -> Path:
    props: dict[str, bytes] = {
        "compatible": b"ctu,ctucanfd\x00",
        "reg": struct.pack(">II", base, CAN_SPAN),
    }
    if status is not None:
        props["status"] = status
    dtb = tmp_path / "board.dtb"
    dtb.write_bytes(_fdt_fixture.build_fdt({CAN_PATH: props}))
    return dtb


def _write_target(tmp_path: Path, properties: dict) -> Path:
    root = tmp_path / "root"
    (root / "targets").mkdir(parents=True)
    (root / "targets/de25_datalogger.yml").write_text(
        yaml.safe_dump({"linux": {"dt_nodes": [
            {"path": CAN_PATH, "properties": properties}]}}),
        encoding="utf-8")
    # DS-P2.135: the tool resolves the ACTIVE target from project.yml.
    (root / "project.yml").write_text(
        "target: targets/de25_datalogger.yml\n", encoding="utf-8")
    return root


def _run(root: Path, dtb: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["python3", str(TOOL), "check-dtb-nodes",
         "--root", str(root), "--dtb", str(dtb)],
        cwd=ROOT, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


# ── accept ──────────────────────────────────────────────────────────────────

def test_enabled_declaration_against_an_enabled_dtb_is_accepted(tmp_path):
    root = _write_target(tmp_path, {"compatible": "ctu,ctucanfd",
                                    "reg": [CAN_BASE, CAN_SPAN]})
    rc, out = _run(root, _can_dtb(tmp_path, status=None))
    assert rc == 0, out
    assert "realisation check passed" in out


def test_disabled_declaration_against_a_disabled_dtb_is_accepted(tmp_path):
    root = _write_target(tmp_path, {"reg": [CAN_BASE, CAN_SPAN],
                                    "status": "disabled"})
    rc, out = _run(root, _can_dtb(tmp_path, status=b"disabled\x00"))
    assert rc == 0, out


# ── the defect this ticket exists to catch ──────────────────────────────────

def test_stale_disabled_status_in_the_dtb_is_refused(tmp_path):
    """The measured merge defect: the target re-enabled the node, the dtb was
    re-patched from an already-patched base, and the stale status survived."""
    root = _write_target(tmp_path, {"compatible": "ctu,ctucanfd",
                                    "reg": [CAN_BASE, CAN_SPAN]})
    rc, out = _run(root, _can_dtb(tmp_path, status=b"disabled\x00"))
    assert rc == 1, out
    assert CAN_PATH in out
    assert 'status = "disabled"' in out
    assert "vacuous" in out


def test_a_stale_disabled_status_on_a_declared_node_is_refused(tmp_path):
    """Not a hand-made fixture: reconstruct the merge shape against a node this
    repo currently SHIPS enabled.

    The DS-P3.154 defect is generic to any dt_node, not just CAN: a dtb patched
    from an already-patched base keeps a stale ``status: disabled`` after the
    target re-enables the node, so the kernel skips an endpoint the target says
    is live.  DS-P2.121 removed the CAN dt_nodes, so anchor the reconstruction
    on an ENABLED aperture node the target still declares today (the UIO
    ring-watermark on the lwh2f window).  Build a dtb where that node carries a
    stale ``status: disabled``, then grade it against the REPO's own current
    target; a guard that passes here would ship a card whose node is dark.
    """
    prior = yaml.safe_load(
        (ROOT / "targets/de25_datalogger.yml").read_text(encoding="utf-8"))
    victim = next(
        (n for n in prior["linux"]["dt_nodes"]
         if isinstance(n, dict) and "reg" in (n.get("properties") or {})
         and "status" not in (n.get("properties") or {})), None)
    assert victim is not None, (
        "the shipped target declares no enabled reg node to reconstruct")
    # Force ONLY the victim stale-disabled; every other node realises cleanly,
    # so the sole refusal is the vacuous-node one this test asserts on.
    victim["properties"]["status"] = "disabled"
    prior_yml = tmp_path / "prior_target.yml"
    prior_yml.write_text(yaml.safe_dump(prior), encoding="utf-8")
    dtb = tmp_path / "stale.dtb"
    dtb.write_bytes(_fdt_fixture.dtb_realising(prior_yml))

    rc, out = _run(ROOT, dtb)
    assert rc == 1, out
    assert victim["path"] in out, out
    assert 'status = "disabled"' in out


def test_declared_status_okay_against_a_disabled_dtb_is_refused(tmp_path):
    root = _write_target(tmp_path, {"reg": [CAN_BASE, CAN_SPAN],
                                    "status": "okay"})
    rc, out = _run(root, _can_dtb(tmp_path, status=b"disabled\x00"))
    assert rc == 1, out
    assert "declared status: 'okay'" in out


def test_wrong_reg_window_in_the_dtb_is_refused(tmp_path):
    """A node at an address the design never declared is the boot7/boot8
    configuration; the dtb, not just the yml, has to be checked."""
    root = _write_target(tmp_path, {"reg": [CAN_BASE, CAN_SPAN]})
    rc, out = _run(root, _can_dtb(tmp_path, status=None, base=0x20030000))
    assert rc == 1, out
    assert "declares reg" in out


def test_missing_node_in_the_dtb_is_refused(tmp_path):
    """The overlay silently not applying at all — the DS-P1.55 failure mode."""
    root = _write_target(tmp_path, {"reg": [CAN_BASE, CAN_SPAN]})
    dtb = tmp_path / "empty.dtb"
    dtb.write_bytes(_fdt_fixture.build_fdt(
        {"/soc@0/serial@10c02100": {"status": b"okay\x00"}}))
    rc, out = _run(root, dtb)
    assert rc == 1, out
    assert "no such node exists in the built dtb" in out


# ── the guard must GATE the build, not merely exist ─────────────────────────

def test_build_refuses_before_the_dtb_reaches_bootscr_or_the_sd_image(tmp_path,
                                                                      monkeypatch):
    """A detector wired in after the SD image is assembled would still ship a
    card whose CAN nodes are disabled, and the witness boot would prove
    nothing.  Drive the REAL ``build()`` with a recording ``subprocess.run``
    and require the refusal to land between ``rr linux dtb`` and everything
    downstream of it."""
    import importlib.util
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
    # The artifact the dtb step "produced": stale, CAN still disabled.
    shutil.copy2(_can_dtb(tmp_path, status=b"disabled\x00"),
                 out / "socfpga_agilex5_socdk.dtb")

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

    with pytest.raises(RuntimeError, match="DS-P3.154"):
        mod.build(root)

    assert any("linux dtb" in cmd for cmd in recorded), recorded
    assert not any("linux bootscr" in cmd for cmd in recorded), recorded
    assert not any("linux sdcard" in cmd for cmd in recorded), recorded
