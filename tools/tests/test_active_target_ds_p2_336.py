# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.336: every tool resolves the active target the way rr does.

rr picks the target from $RR_TARGET, then the untracked
.routertl/local/active-target that `rr target set` writes (routertl
RTL-P2.2179), then project.yml's `target:`. Five readers here skipped the middle
step, so after `rr target set zybo_datalogger` on a clone whose project.yml says
de25_datalogger they resolved DE25: the clean-machine rehearsal of the export
found the Zybo build's rom_info stage stamp under build/de25_datalogger and none
under build/zybo_datalogger.

Each scenario below is the state `rr target set` leaves: project.yml names one
target, the local file names the other. The assertions are hard-coded names.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import active_target  # noqa: E402
import de25_linux_image  # noqa: E402
import generated_source_contract as contract  # noqa: E402
from preflight import ds_preflight  # noqa: E402
from sim import pre_sim_seed_libs  # noqa: E402

REL = "hw/rtl/pkg/rom_info_pkg.vhd"
_ENV = ("RR_TARGET", "STAGE_STAMP", "RR_STAGE_STAMP", "RR_JOB_ID", "BUILD_DIR", "RR_BUILD_DIR")
TARGETS = {
    "de25_datalogger": "altera",
    "zybo_datalogger": "xilinx",
}


def _project(root: Path, *, default: str, selected: str | None) -> Path:
    """A clone as `rr target set <selected>` leaves it: project.yml still says *default*."""
    (root / "targets").mkdir(parents=True)
    for stem, vendor in TARGETS.items():
        (root / "targets" / f"{stem}.yml").write_text(
            f"project:\n  name: {stem}\n  output_dir: build/{stem}\n"
            f"hardware:\n  vendor: {vendor}\n",
            encoding="utf-8",
        )
    (root / "project.yml").write_text(f"target: targets/{default}.yml\n", encoding="utf-8")
    if selected:
        local = root / ".routertl" / "local"
        local.mkdir(parents=True)
        (local / "active-target").write_text(f"targets/{selected}.yml\n", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _no_ambient_target(monkeypatch):
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def zybo_selected(tmp_path):
    return _project(tmp_path / "clone", default="de25_datalogger", selected="zybo_datalogger")


@pytest.fixture
def de25_selected(tmp_path):
    return _project(tmp_path / "clone", default="zybo_datalogger", selected="de25_datalogger")


# ── the helper ───────────────────────────────────────────────────────────────

def test_the_local_choice_beats_project_yml(zybo_selected):
    assert active_target.active_target_pointer(zybo_selected) == (
        "targets/zybo_datalogger.yml", ".routertl/local/active-target")
    assert active_target.active_target_stem(zybo_selected) == "zybo_datalogger"
    assert active_target.active_target_path(zybo_selected) == zybo_selected / "targets/zybo_datalogger.yml"


def test_rr_target_beats_the_local_choice(zybo_selected, monkeypatch):
    monkeypatch.setenv("RR_TARGET", "targets/de25_datalogger.yml")
    assert active_target.active_target_pointer(zybo_selected) == ("targets/de25_datalogger.yml", "RR_TARGET")


def test_project_yml_is_the_default_without_a_local_choice(tmp_path):
    root = _project(tmp_path / "clone", default="de25_datalogger", selected=None)
    assert active_target.active_target_pointer(root) == ("targets/de25_datalogger.yml", "project.yml")


def test_a_bare_stem_names_a_file_under_targets(tmp_path, monkeypatch):
    root = _project(tmp_path / "clone", default="de25_datalogger", selected=None)
    monkeypatch.setenv("RR_TARGET", "zybo_datalogger")
    assert active_target.active_target_path(root) == root / "targets/zybo_datalogger.yml"


def test_no_pointer_resolves_to_nothing(tmp_path):
    (tmp_path / "project.yml").write_text("project:\n  name: x\n", encoding="utf-8")
    assert active_target.active_target_pointer(tmp_path) == (None, None)
    assert active_target.load_active_target(tmp_path) == (None, {})


def test_the_cli_prints_what_the_shell_scripts_read(zybo_selected):
    env = {k: v for k, v in os.environ.items() if k not in _ENV}
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools/active_target.py"), "--root", str(zybo_selected), "--stem"],
        capture_output=True, text=True, env=env, check=True,
    ).stdout.strip()
    assert out == "zybo_datalogger"


# ── 1. the rom_info stage stamp ──────────────────────────────────────────────

def _rom_info(tmp_path: Path) -> str:
    out = tmp_path / "gen"
    env = {k: v for k, v in os.environ.items() if k not in _ENV}
    env["RR_BUILD_DIR"] = str(tmp_path / "gen_build")
    r = subprocess.run(
        ["bash", "tools/regen_regbank.sh", "--rom-info-only", "--output-dir", str(out),
         "--regbank-dir", "hw/rtl/regbank", "--library", "tich"],
        cwd=ROOT, capture_output=True, text=True, env=env)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    return (out / "rom_info_pkg.vhd").read_text(encoding="utf-8")


def test_the_stamp_path_is_under_the_selected_target(zybo_selected, monkeypatch):
    monkeypatch.setattr(contract, "ROOT", zybo_selected)
    assert contract.resolve_stage_stamp() == zybo_selected / "build/zybo_datalogger/.rom_info_stage_stamp"


def test_the_hook_install_writes_the_stamp_under_the_selected_target_only(tmp_path, zybo_selected, monkeypatch):
    """install_candidate is the pre_build hook's write step (regen_regbank.sh calls it)."""
    monkeypatch.setattr(contract, "ROOT", zybo_selected)
    (zybo_selected / "hw/rtl/pkg").mkdir(parents=True)
    cand = tmp_path / "cand.vhd"
    cand.write_text(_rom_info(tmp_path), encoding="utf-8")
    assert contract.install_candidate(kind="rom-info", candidate=cand, output=zybo_selected / REL) is True
    stamp = zybo_selected / "build/zybo_datalogger/.rom_info_stage_stamp"
    assert stamp.read_bytes() == (zybo_selected / REL).read_bytes()
    assert not (zybo_selected / "build/de25_datalogger").exists()
    assert sorted(p.name for p in (zybo_selected / "build").iterdir()) == ["zybo_datalogger"]


# ── 2. the preflight ─────────────────────────────────────────────────────────

def test_the_preflight_without_routertl_judges_the_selected_target(zybo_selected, monkeypatch):
    monkeypatch.setattr(ds_preflight, "load_project_config", None)
    path, target = ds_preflight.load_target(zybo_selected)
    assert path == (zybo_selected / "targets/zybo_datalogger.yml").resolve()
    assert target["project"]["name"] == "zybo_datalogger"


def test_the_preflight_fallback_follows_the_reverse_choice_too(de25_selected, monkeypatch):
    monkeypatch.setattr(ds_preflight, "load_project_config", None)
    path, _ = ds_preflight.load_target(de25_selected)
    assert path.name == "de25_datalogger.yml"


def test_the_helper_agrees_with_routertl_where_it_is_installed(zybo_selected):
    rr = pytest.importorskip("sdk.cli.project_config")
    if not hasattr(rr, "local_active_target"):
        pytest.skip("routertl < 5.3.1 has no rr target set local state")
    cwd = Path.cwd()
    os.chdir(zybo_selected)
    try:
        pointer = rr.get_active_target("project.yml")
    finally:
        os.chdir(cwd)
    assert Path(pointer).stem == active_target.active_target_stem(zybo_selected) == "zybo_datalogger"


# ── 3. the simulation library seeder ─────────────────────────────────────────

def test_the_sim_seeder_reads_the_selected_vendor(zybo_selected, monkeypatch):
    monkeypatch.setattr(pre_sim_seed_libs, "ROOT", zybo_selected)
    assert pre_sim_seed_libs._active_target_vendor() == "xilinx"


def test_the_sim_seeder_reads_the_selected_vendor_in_reverse(de25_selected, monkeypatch):
    """Not vacuous: xilinx is also the seeder's fallback, so the reverse choice must read altera."""
    monkeypatch.setattr(pre_sim_seed_libs, "ROOT", de25_selected)
    assert pre_sim_seed_libs._active_target_vendor() == "altera"


# ── 4. the DE25 Linux image tool ─────────────────────────────────────────────

def test_the_de25_image_tool_serves_a_selected_de25_target(de25_selected):
    assert de25_linux_image.active_target_stem(de25_selected) == "de25_datalogger"


def test_the_de25_image_tool_refuses_when_zybo_is_selected(zybo_selected):
    """It serves DE25 targets only; with Zybo selected it must refuse, not build the project.yml default."""
    with pytest.raises(RuntimeError, match="active target 'zybo_datalogger' is not a DE25 target"):
        de25_linux_image.active_target_stem(zybo_selected)


# ── 5. the TFTP stage script ─────────────────────────────────────────────────

def test_the_tftp_stage_script_defaults_its_slot_through_the_helper():
    text = (ROOT / "sw/linux/stage_tftp_deploy.sh").read_text(encoding="utf-8")
    slot = next(line for line in text.splitlines() if line.startswith("SLOT="))
    assert "tools/active_target.py" in slot and "project.yml" not in slot


# ── the guard: no new direct reader ──────────────────────────────────────────

_TARGET_KEY_RE = re.compile(
    r"""get\(\s*["']target["']\s*\)|\[\s*["']target["']\s*\]|\^target:|s/\^target"""
)
# A file that names project.yml and reads a `target` key for another document.
EXEMPT = {
    "tools/lock_check_leg.py": "reads `target` from lock-check exemption entries, not from project.yml",
}


def _direct_readers(files: dict[str, str]) -> list[str]:
    return sorted(
        name for name, text in files.items()
        if "project.yml" in text and _TARGET_KEY_RE.search(text)
    )


def _scanned() -> dict[str, str]:
    names = subprocess.run(
        ["git", "ls-files", "--", "tools/*.py", "tools/*.sh", "sw/*.py", "sw/*.sh"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    keep = {}
    for name in names:
        parts = Path(name).parts
        if "tests" in parts or Path(name).name.startswith("test_") or name == "tools/active_target.py":
            continue
        keep[name] = (ROOT / name).read_text(encoding="utf-8", errors="replace")
    return keep


def test_no_tool_reads_the_project_yml_target_itself():
    readers = [n for n in _direct_readers(_scanned()) if n not in EXEMPT]
    assert readers == [], f"use tools/active_target.py, which follows rr target set: {readers}"


def test_every_exemption_is_still_needed():
    assert set(EXEMPT) <= set(_direct_readers(_scanned()))


@pytest.mark.parametrize("planted", [
    'cfg = yaml.safe_load(Path("project.yml").read_text())\npointer = cfg.get("target")\n',
    'cfg = yaml.safe_load(open("project.yml"))\npointer = cfg["target"]\n',
    'm = re.search(r"^target:\\s*targets/(\\w+)\\.yml", (root / "project.yml").read_text())\n',
    'SLOT="$(sed -n \'s/^target: targets\\/\\(.*\\)\\.yml/\\1/p\' "$REPO/project.yml")"\n',
])
def test_the_guard_sees_each_reader_shape_the_tree_had(planted):
    assert _direct_readers({"tools/planted.py": planted}) == ["tools/planted.py"]
