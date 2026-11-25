# rr unknown-llm (AI-generated)
"""DS-P2.310: U-Boot's default env bootargs is the composed deployment cmdline.

U-Boot writes its environment ``bootargs`` into ``/chosen`` at ``booti``
(boot/fdt_support.c), replacing the string ``rr linux dtb --bootargs`` put in
the dtb. The DE25 card carries no ``uboot.env``, so the compiled-in default
(CONFIG_BOOTARGS, set by ``rr linux uboot --bootargs``) IS the kernel command
line of a hands-off boot. The image builder gave the composed string (target
bootargs + the deployment ``ds_ip=``) to ``rr linux dtb`` only, so U-Boot
carried another string: on the DE25 C3 card (2026-11-15) one without
``ds_ring_word_bytes``, and the daemon reported every PL zero-pad word as
zs_resync.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from sdk.cocotb_helpers import requires

ROOT = Path(os.environ.get("DE25_TEST_ROOT",
                           Path(__file__).resolve().parents[3])).resolve()

COMPOSED = ("console=ttyS1,115200 root=/dev/mmcblk0p2 init=/init "
            "ds_ring_word_bytes=32 ds_ip=192.0.2.5/24")
# The string the C3 card's U-Boot printed (address replaced), no ring token.
CARD_C3 = ("console=ttyS1,115200 root=/dev/mmcblk0p2 init=/init "
           "ds_ip=192.0.2.5/24")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"tools/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_fdt_fixture():
    spec = importlib.util.spec_from_file_location(
        "fdt_fixture_p2_310", ROOT / "tools/tests/fdt_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@requires("DE-L0-BUILD-005")
def test_build_gives_rr_linux_uboot_the_same_bootargs_as_rr_linux_dtb(monkeypatch, tmp_path):
    image = _load("de25_linux_image")
    fdt = _load_fdt_fixture()
    calls: list[list[str]] = []

    def record(cmd, **_kwargs):
        calls.append(list(cmd))

        class _Done:
            returncode = 0
        return _Done()

    root = tmp_path
    (root / "project.yml").write_text("target: targets/de25_datalogger.yml\n", encoding="utf-8")
    for rel in ("targets/de25_datalogger.yml", "sw/linux/de25/system_memory_map.yml",
                "hw/rtl/de25/deepskopion_de25_top.vhd", "hw/rtl/de25/axil_demux3.vhd"):
        staged = root / rel
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes((ROOT / rel).read_bytes())
    out = root / "sw/linux/de25/out/de25_datalogger"
    out.mkdir(parents=True, exist_ok=True)
    (out / image.DTB_ARTIFACT).write_bytes(fdt.dtb_realising(root / "targets/de25_datalogger.yml"))
    monkeypatch.setattr(image.subprocess, "run", record)
    monkeypatch.setattr(image, "deployment_bootargs", lambda _root: COMPOSED)
    monkeypatch.setattr(image, "_stage_target_stem_sof", lambda _root: None)
    monkeypatch.setattr(image, "rebuild_uboot_with_emac0_phy_fix", lambda _root: None)
    monkeypatch.setattr(image, "normalize_provenance", lambda *_a, **_k: None)
    monkeypatch.setattr(image, "normalize_artifact_mtimes", lambda *_a, **_k: None)
    checked: list[tuple[Path, str]] = []
    monkeypatch.setattr(image, "assert_uboot_itb_default_bootargs",
                        lambda itb, bootargs: checked.append((itb, bootargs)), raising=False)
    monkeypatch.setattr(image, "rootfs_image_problems", lambda *_a, **_k: [])  # DS-P3.316: build() now reads the card image; this test stubs every tool run
    image.build(root)

    uboot = next(c for c in calls if c[:3] == ["rr", "linux", "uboot"])
    dtb = next(c for c in calls if c[:3] == ["rr", "linux", "dtb"])
    assert "--bootargs" in dtb and dtb[dtb.index("--bootargs") + 1] == COMPOSED
    assert "--bootargs" in uboot, (
        f"`rr linux uboot` ran without --bootargs ({uboot}): U-Boot's default env "
        "bootargs then differs from the dtb's and replaces it at booti")
    assert uboot[uboot.index("--bootargs") + 1] == COMPOSED
    assert checked == [(out / "u-boot.itb", COMPOSED)], (
        "the shipped u-boot.itb was not checked against the composed bootargs")


def _itb(*env_values: str) -> bytes:
    """Bytes shaped like the u-boot binary inside the FIT: a day-name table,
    then the default environment entries, each NUL-terminated."""
    body = b"\xd0\x0d\xfe\xed" + b"\x00" * 60 + b"SunMonTueWedThuFriSat"
    for v in env_values:
        body += b"bootargs=" + v.encode("ascii") + b"\x00"
    return body + b"bootcmd=load mmc 0:1 ${kernel_addr_r} Image\x00\x00" + b"\xff" * 64


@requires("DE-L0-NEG-008")
def test_itb_with_the_composed_bootargs_is_accepted(tmp_path):
    image = _load("de25_linux_image")
    itb = tmp_path / "u-boot.itb"
    itb.write_bytes(_itb(COMPOSED))
    image.assert_uboot_itb_default_bootargs(itb, COMPOSED)


@requires("DE-L0-NEG-008")
@pytest.mark.parametrize("values", [(CARD_C3,), (), (COMPOSED, COMPOSED), (COMPOSED + " extra",)],
                         ids=["c3-card-no-token", "no-bootargs", "two-entries", "longer"])
def test_itb_without_exactly_the_composed_bootargs_is_refused(tmp_path, values):
    image = _load("de25_linux_image")
    itb = tmp_path / "u-boot.itb"
    itb.write_bytes(_itb(*values))
    with pytest.raises(RuntimeError, match="DS-P2.310"):
        image.assert_uboot_itb_default_bootargs(itb, COMPOSED)
