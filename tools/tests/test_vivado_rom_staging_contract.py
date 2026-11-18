"""Contract for clone-portable regbank ROM paths in Vivado synthesis."""

import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
ROM_STAGING_SCRIPTS = (
    "hw/bd/configure_regbank_rom_staging.tcl",
    "hw/bd/stage_regbank_roms_pre.tcl",
)


def test_canfd_target_installs_a_synth_run_rom_staging_hook() -> None:
    target = yaml.safe_load((ROOT / "targets/zybo_datalogger.yml").read_text(encoding="utf-8"))
    assert target["tcl_hooks"]["synthesis"] == [
        "hw/bd/configure_regbank_rom_staging.tcl"
    ]

    configure = (ROOT / target["tcl_hooks"]["synthesis"][0]).read_text(encoding="utf-8")
    stage = (ROOT / "hw/bd/stage_regbank_roms_pre.tcl").read_text(encoding="utf-8")
    assert "STEPS.SYNTH_DESIGN.TCL.PRE" in configure
    assert "[pwd]" in stage
    assert "hw/rtl/regbank/regbank_schema.hex" in stage
    assert "hw/rtl/regbank/config_schema.hex" in stage
    assert "stage_regbank_roms_pre.tcl" in configure

    for rel in ROM_STAGING_SCRIPTS:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", rel],
            cwd=ROOT,
            check=False,
        )
        assert ignored.returncode == 1, f"{rel} is gitignored and cannot ship"
