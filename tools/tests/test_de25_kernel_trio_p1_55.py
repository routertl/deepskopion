"""DS-P1.55 regression guards: the DE25 kernel trio (Image + dtb + bootargs)
must be the silicon-proven set, and the builder must actually run the step
that patches the dtb.

**Measured defect** (2026-08-23 A/B on DE25 silicon, boot4/5/6 transcripts):
with identical ``de25_datalogger_hps.sof``, identical ``u-boot.itb`` and an
identical card layout, the builder-staged Image (46768640 B, linux 6.12.33
from the GSRD-25.3 kernel tag ``QPDS25.3_REL_GSRD_PR``) + dtb (24776 B) die
BEFORE earlycon prints — even with proven bootargs sourced by hand. Swapping
ONLY the kernel trio for the rr-ddr-stream de25_agilex5_ddr_validator's
silicon-proven set (linux 6.18.2-lts, tag
``rel_socfpga-6.18.2-lts_26.04.01_pr``) boots to a shell in 1.5 s.

Post-mortem of the shipped dtb (decompiled from the frozen-lock artifact)
found a second live defect: it was the RAW SoCDK dtb — ``rr linux dtb``
(the only step that applies the console retarget, ``dtb_disable_nodes`` and
every ``linux.dt_nodes`` declaration) was never part of the build sequence,
so chosen/bootargs were still the SoCDK defaults, serial0 pointed at UART0,
and zero deepskopion nodes (uio, CAN, PHY fix) existed on the card.

Third defect, observed on the PROVEN boot: ``mmc0: error -110 whilst
initialising SD card`` — the SoCDK ``sd_io_1v8_reg`` regulator-gpio state
map drives the SD I/O rail gpio LOW when the mmc core requests 3.3 V (same
family as rr-ddr-stream commit 1143589's silicon-validated AXE5-Eagle
U-Boot fix), so the card is invisible to Linux. The fix mirrors that state
remap into the kernel DTB via ``linux.dt_nodes``.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("DE25_TEST_ROOT",
                           Path(__file__).resolve().parents[2])).resolve()
TARGET_YML = ROOT / "targets/de25_datalogger.yml"

#: The silicon-proven kernel tree (rr-ddr-stream de25_agilex5_ddr_validator,
#: altera-opensource/linux-socfpga, linux 6.18.2-lts, commit be94a41df).
PROVEN_KERNEL_REF = "rel_socfpga-6.18.2-lts_26.04.01_pr"

#: What rr's kernel step falls back to without an explicit pin — the
#: GSRD-25.3 kernel tag, i.e. linux 6.12.33 / commit 3234b1ed8, the tree
#: CONVICTED by the A/B of dying pre-earlycon on DE25 silicon.
CONVICTED_GSRD_KERNEL_TAG = "QPDS25.3_REL_GSRD_PR"


def _linux_cfg() -> dict:
    return yaml.safe_load(TARGET_YML.read_text())["linux"]


def _load_fdt_fixture():
    """DS-P3.154's shared FDT fixture builder (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "fdt_fixture_p1_55", ROOT / "tools/tests/fdt_fixture.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_fdt_fixture = _load_fdt_fixture()


def _load_de25_linux_image():
    """Import the builder by file path (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "de25_linux_image_p1_55", ROOT / "tools/de25_linux_image.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolve_kernel_ref(cfg: dict) -> str:
    """Mirror rr's kernel-ref precedence (run_socfpga_kernel,
    sdk.cli.commands.linux_steps._socfpga): an explicit
    ``linux.kernel_branch`` wins; otherwise the GSRD tag derived from
    ``linux.gsrd_version`` (``QPDS<ver>_REL_GSRD_PR``). Asserting on the
    RESOLVED ref means deleting the pin fails this test with the convicted
    tag in the message, not a bare KeyError."""
    branch = cfg.get("kernel_branch")
    if branch:
        return str(branch)
    ver = str(cfg.get("gsrd_version", "")).strip()
    return f"QPDS{ver}_REL_GSRD_PR" if ver else ""


def test_kernel_pinned_to_silicon_proven_tree():
    """Without ``linux.kernel_branch``, rr resolves the kernel to the
    GSRD-25.3 tag — the 6.12.33 tree the A/B convicted of pre-earlycon
    death. The pin must name the proven 6.18.2-lts tag."""
    resolved = _resolve_kernel_ref(_linux_cfg())
    assert resolved == PROVEN_KERNEL_REF, (
        f"DE25 kernel resolves to {resolved!r} — for the mute-death set this "
        f"was {CONVICTED_GSRD_KERNEL_TAG!r} (linux 6.12.33), convicted by "
        f"the 2026-08-23 A/B of dying before earlycon on DE25 silicon. Pin "
        f"linux.kernel_branch to {PROVEN_KERNEL_REF!r} (the validator's "
        f"silicon-proven 6.18.2-lts tree) in targets/de25_datalogger.yml."
    )


def test_partition_rootfs_bootargs_name_a_root_device():
    """``sdcard.rootfs: partition`` ships the rootfs on P2 ext4 — a cmdline
    with no ``root=`` can never mount it, so even a live kernel could not
    have reached a shell (the pre-DS-P1.55 bootargs named NO root device)."""
    cfg = _linux_cfg()
    assert cfg["sdcard"]["rootfs"] == "partition"
    bootargs = cfg["bootargs"]
    assert "root=/dev/mmcblk0p2" in bootargs.split(), (
        f"partition-mode rootfs but bootargs name no P2 root device: "
        f"{bootargs!r}"
    )
    assert "rootwait" in bootargs.split(), (
        "root=/dev/mmcblk0p2 without rootwait races SD probe ordering"
    )


def test_bootargs_carry_proven_agilex5_flags():
    """``nosmp kvm-arm.mode=nvhe`` are part of the only cmdline proven to
    boot this silicon (validator boot6; nosmp is the Intel GSRD bring-up
    default for the Agilex 5 A55+A76 cluster). 'Built like the validator's'
    means the proven set, not a novel subset."""
    tokens = _linux_cfg()["bootargs"].split()
    assert "nosmp" in tokens
    assert "kvm-arm.mode=nvhe" in tokens


def _vqmmc_node() -> dict | None:
    for node in _linux_cfg().get("dt_nodes", []):
        if node.get("path") == "/soc@0/regulator-1p8v":
            return node
    return None


def test_sd_vqmmc_regulator_override_declared():
    """The SoCDK ``sd_io_1v8_reg`` states map (3.3 V → gpio LOW) cuts the SD
    I/O rail when the mmc core requests 3.3 V → ``mmc0: error -110``, card
    invisible to Linux (observed on the proven boot6 too). The kernel-DTB
    override must pin min==max==3.3 V and remap 3.3 V → gpio HIGH, held via
    boot-on/always-on — the Linux mirror of rr-ddr-stream commit 1143589."""
    node = _vqmmc_node()
    assert node is not None, (
        "dt_nodes carries no /soc@0/regulator-1p8v override — the SoCDK SD "
        "vqmmc regulator states map leaves the SD I/O rail off in Linux "
        "(mmc0 -110, no mmcblk0)"
    )
    props = node["properties"]
    assert props["regulator-min-microvolt"] == 3300000
    assert props["regulator-max-microvolt"] == 3300000, (
        "min must equal max (3300000) or the regulator core never autosets "
        "the gpio at probe"
    )
    assert props["states"] == [3300000, 1, 1800000, 0], (
        "states must map 3300000 uV -> gpio HIGH (1); the stock map "
        "(3300000 -> 0) is exactly the defect"
    )
    # Empty string is the repo's dt_nodes convention for a boolean DT flag
    # (see no-map on the reserved-memory node).
    assert props["regulator-boot-on"] == ""
    assert props["regulator-always-on"] == ""


def test_vqmmc_override_renders_through_rr_injector():
    """Encode the declared node through the INSTALLED rr's own dt_nodes
    renderer (the dtc-fallback path of ``rr linux dtb``) and assert the DT
    fragment it would merge carries the remapped cells — proving the YAML
    spelling survives the real injector, not just this test's parser."""
    from sdk.cli.commands.linux_steps import dtb as rr_dtb

    node = _vqmmc_node()
    assert node is not None
    overlay = rr_dtb._dt_nodes_to_dts_overlay([node])
    assert "regulator-1p8v {" in overlay
    assert "states = <3300000 1 1800000 0>;" in overlay
    assert "regulator-min-microvolt = <3300000>;" in overlay
    assert "regulator-max-microvolt = <3300000>;" in overlay
    assert "regulator-boot-on;" in overlay
    assert "regulator-always-on;" in overlay


def test_build_sequence_runs_dtb_patch_step(monkeypatch, tmp_path):
    """The shipped card's dtb was the RAW SoCDK dtb because ``build()``
    never ran ``rr linux dtb`` — the only step that applies the console
    retarget, dtb_disable_nodes and dt_nodes (including the vqmmc fix
    above; a declared node that is never injected is a fix that does not
    exist). Assert at the command-sequence surface: kernel → dtb → sdcard,
    and the dtb step must NOT pass --refresh (that would trigger a second
    full kernel distclean+rebuild inside run_socfpga_dtb)."""
    image = _load_de25_linux_image()

    calls: list[list[str]] = []

    def record(cmd, **kwargs):
        calls.append(list(cmd))

        class _Done:
            returncode = 0

        return _Done()

    root = tmp_path
    (root / "project.yml").write_text(
        "target: targets/de25_datalogger.yml\n", encoding="utf-8"
    )
    # DS-T2.71: build() now refuses, before any rr step, a dt_node whose
    # lwh2f window the design's decode map does not cover. That check reads
    # the target, the memory map and the two RTL files it derives the real
    # decode windows from, so this fixture stages the REPO's own copies —
    # deliberately not a stub and deliberately not monkeypatched away, so
    # this ordering guard also witnesses that the shipped configuration
    # passes the crosscheck on the way through.
    for rel in ("targets/de25_datalogger.yml",
                "sw/linux/de25/system_memory_map.yml",
                "hw/rtl/de25/deepskopion_de25_top.vhd",
                "hw/rtl/de25/axil_demux3.vhd"):
        staged = root / rel
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes((ROOT / rel).read_bytes())
    # DS-P3.154: build() reads the board DTB back after `rr linux dtb` and
    # refuses a boot set whose dtb does not carry the target's dt_nodes. The
    # rr steps are recorded, not run, so stage a REAL blob generated from the
    # staged target — the shipped configuration, not a hand-made one.
    out = root / "sw/linux/de25/out/de25_datalogger"
    out.mkdir(parents=True, exist_ok=True)
    (out / image.DTB_ARTIFACT).write_bytes(
        _fdt_fixture.dtb_realising(root / "targets/de25_datalogger.yml"))
    monkeypatch.setattr(image.subprocess, "run", record)
    # DS-D.39 resolves deployment-only network identity before starting the
    # image sequence.  This guard deliberately owns only the command ordering,
    # so provide that boundary result directly rather than requiring its bare
    # tmp_path fixture to duplicate the independently tested hw.yml contract.
    monkeypatch.setattr(
        image,
        "deployment_bootargs",
        lambda _root: "console=ttyS1,115200 ds_ip=192.0.2.5/24",
    )
    monkeypatch.setattr(image, "_stage_target_stem_sof", lambda _root: None)
    # DS-P2.94: this test is about the kernel/dtb/sdcard ordering, not the
    # (separately tested) EMAC0 PHY rebuild — that step needs a real cloned
    # u-boot-socfpga source tree this fixture never stages.
    monkeypatch.setattr(image, "rebuild_uboot_with_emac0_phy_fix",
                        lambda _root: None)
    # DS-P2.310: the shipped-itb bootargs check is covered by
    # test_de25_uboot_bootargs_ds_p2_310.py; this test owns only command order
    # and its rebuild stub leaves no itb to read.
    monkeypatch.setattr(image, "assert_uboot_itb_default_bootargs",
                        lambda *_a: None, raising=False)
    monkeypatch.setattr(image, "normalize_provenance", lambda *_a, **_k: None)
    monkeypatch.setattr(image, "normalize_artifact_mtimes",
                        lambda *_a, **_k: None)
    # Compatibility with the pre-DS-T2.72 builder lets this independent DTB
    # sequence guard run against both sides of the uboot.env revert.
    monkeypatch.setattr(image, "_emit_uboot_env",
                        lambda _root, out, _env: out / "uboot.env",
                        raising=False)
    monkeypatch.setattr(image, "_stage_uboot_env_on_sdcard",
                        lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(image, "rootfs_image_problems", lambda *_a, **_k: [])  # DS-P3.316: build() now reads the card image; this test stubs every tool run
    image.build(root)

    rr_steps = [c[2] for c in calls if c[:2] == ["rr", "linux"]]
    assert "dtb" in rr_steps, (
        f"build() never runs `rr linux dtb` — the staged dtb ships as the "
        f"raw SoCDK tree (no console retarget, no dt_nodes). Steps run: "
        f"{rr_steps}"
    )
    assert rr_steps.index("kernel") < rr_steps.index("dtb") < \
        rr_steps.index("sdcard"), (
        f"dtb patch must run after the kernel stages the raw dtb and before "
        f"sdcard packages it. Steps run: {rr_steps}"
    )
    dtb_call = next(c for c in calls if c[:3] == ["rr", "linux", "dtb"])
    assert "--refresh" not in dtb_call, (
        "`rr linux dtb --refresh` re-runs a full kernel distclean+rebuild "
        "inside run_socfpga_dtb; the kernel step above already rebuilt the "
        "dtb"
    )
