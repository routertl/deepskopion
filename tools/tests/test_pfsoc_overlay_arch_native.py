# rr unknown-llm (AI-generated)
"""PF-L0 (DS-P2.125): the PolarFire rootfs must not carry the ARM dropbear.

The land refusal: ``pfsoc_datalogger`` joins the shared busybox contract
(S49dropbear), but the shared dropbear is staged from the ARM Buildroot tree
(``sw/linux/stage_dropbear_overlay.sh``). A riscv64 kernel cannot exec an armhf
ELF — S49 fails closed and port 22 is dead — and nothing per-arch addressed it.

The fix is that the PolarFire kit's dropbear/scp are the rv64gc binaries the
pfsoc overlay build extracts into ``sw/linux/pfsoc/python_overlay`` (the
per-arch tree the target already declares), never the shared ARM binary. These
are static, source-level guards (the overlay itself is a gitignored 1–3 h build
artifact, absent here), so they check the STAGING RECIPE and the CONTRACT, which
is where the wrong-arch dropbear would be re-introduced.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BUILD_SH = ROOT / "sw" / "linux" / "pfsoc" / "build_python_overlay.sh"
DEFCONFIG = ROOT / "sw" / "linux" / "pfsoc" / "buildroot_pfsoc_defconfig"
# DS-P1.118: the PolarFire declaration moved from the sidecar into the public contract.
CONTRACT = ROOT / "sw" / "linux" / "deployment_contract.yml"
SHARED_DROPBEAR_STAGE = ROOT / "sw" / "linux" / "stage_dropbear_overlay.sh"


def test_pfsoc_overlay_build_extracts_its_own_dropbear():
    """The riscv64 overlay build must place dropbear in the pfsoc overlay tree,
    so a riscv64-native sshd exists without the shared ARM one."""
    body = BUILD_SH.read_text(encoding="utf-8")
    assert 'cp -a "$TARGET/usr/sbin/dropbear"  "$OVERLAY/usr/sbin/"' in body, (
        "build_python_overlay.sh no longer extracts a riscv64 dropbear into the "
        "pfsoc overlay — S49dropbear would fall back to the shared ARM binary a "
        "riscv64 kernel cannot exec"
    )
    # It must fail closed if the build produced no dropbear, rather than shipping
    # a rootfs with a dead S49.
    assert 'if [ ! -x "$TARGET/usr/sbin/dropbear" ]; then' in body, (
        "build_python_overlay.sh must abort if the Buildroot tree has no "
        "dropbear, not silently ship an overlay missing it"
    )


def test_pfsoc_defconfig_builds_dropbear_and_scp():
    """The extraction above only works if the defconfig actually builds them."""
    cfg = DEFCONFIG.read_text(encoding="utf-8")
    assert "BR2_PACKAGE_DROPBEAR=y" in cfg
    assert "BR2_PACKAGE_OPENSSH_CLIENT=y" in cfg
    # dropbear's own sshd must be the only sshd — OpenSSH server off, or two
    # daemons collide on :22 (DS-P3.79).
    assert "# BR2_PACKAGE_OPENSSH_SERVER is not set" in cfg


def test_pfsoc_target_uses_the_per_arch_overlay_not_the_shared_arm_tree():
    """The pfsoc dropbear rides in the per-arch python_overlay the target
    declares; it must be the pfsoc tree, not the armhf sw/linux/python_overlay."""
    target = yaml.safe_load(
        (ROOT / "targets" / "pfsoc_datalogger.yml").read_text(encoding="utf-8"))
    overlay = target["linux"]["rootfs"]["python_overlay"]
    assert overlay == "sw/linux/pfsoc/python_overlay", overlay


def test_shared_dropbear_stage_is_declared_armhf_only():
    """The shared stage script must stay honestly armhf-scoped so nobody wires
    it into the PolarFire build."""
    body = SHARED_DROPBEAR_STAGE.read_text(encoding="utf-8")
    assert "buildroot/output/target/usr/sbin/dropbear" in body, (
        "the shared stage no longer sources from the armhf Buildroot tree — "
        "if this changed, revisit the per-arch split"
    )


def test_deployment_contract_dropbear_note_is_arch_aware():
    """The land refusal quoted the contract still calling dropbear an ARM
    artefact after adding pfsoc to the same init list. The declaration must say
    the binary is per-arch, and it must NOT unconditionally call it ARM."""
    decl = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    entry = next(e for e in decl["autostart"]
                 if e["path"] == "etc/init.d/S49dropbear")
    starts = " ".join(str(entry["starts"]).split())
    assert "per-arch" in starts and "riscv64" in starts, (
        "S49dropbear declaration must state the binary is per-arch (armhf for "
        f"Zynq, riscv64 for PolarFire); got: {starts!r}"
    )
    # DS-P1.118 review: each board paired with its own architecture (a swapped
    # mapping fails), now that this text is in the public contract.
    assert "armhf for the Zynq targets from sw/linux/stage_dropbear_overlay.sh," in starts and \
        "riscv64 for the PolarFire kit from its own python_overlay)" in starts, (
        f"S49dropbear must pair armhf with the Zynq targets and riscv64 with the PolarFire kit; got: {starts!r}"
    )
    assert "its ARM binary is a staged artefact" not in starts, (
        "the declaration still calls dropbear unconditionally ARM (DS-P2.125 "
        "land refusal)"
    )
