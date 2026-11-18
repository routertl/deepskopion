"""Regression guards for DS-X2 deterministic generated-source reconstruction."""

from __future__ import annotations

import sys
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from tools import generated_source_contract as contract


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files, per_tree  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)


@pytest.fixture(scope="module")
def generated_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    destination = tmp_path_factory.mktemp("ds_x2_generated")
    contract.run_generation(destination)
    return destination


def _copy_observed_tree(destination: Path) -> None:
    for outputs in contract.tracked_outputs().values():
        for item in outputs:
            target = destination / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / item["path"], target)


def _flip_one_stable_byte(path: Path) -> None:
    body = bytearray(path.read_bytes())
    for index, value in enumerate(body):
        if value in b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
            body[index] = ord("Z") if value != ord("Z") else ord("Y")
            path.write_bytes(body)
            return
    raise AssertionError(f"no mutable byte in generated fixture {path}")


def test_clean_scratch_reconstruction_matches_every_tracked_output(generated_tree: Path):
    assert contract.compare_outputs(generated_tree) == {}


@pytest.mark.parametrize("family", sorted(contract.tracked_outputs()))
def test_one_byte_drift_in_each_tracked_family_is_detected(
    family: str, generated_tree: Path, tmp_path: Path
):
    _copy_observed_tree(tmp_path)
    victim = contract.tracked_outputs()[family][0]["path"]
    _flip_one_stable_byte(tmp_path / victim)

    drift = contract.compare_outputs(generated_tree, actual_root=tmp_path)

    assert family in drift
    assert victim in drift[family]


def test_two_clean_runs_are_identical_modulo_declared_volatility(
    generated_tree: Path, tmp_path: Path
):
    second = tmp_path / "second"
    contract.run_generation(second)
    manifest = contract.load_manifest()

    declared_fields = {
        rule["field"]
        for rule in manifest["volatile_lines"]["hw/rtl/pkg/rom_info_pkg.vhd"]
    }
    assert declared_fields == {
        "generated_at",
        "git_hash",
        "system_version_dirty_bit",
        "build_date",
    }

    for outputs in contract.tracked_outputs(manifest).values():
        for item in outputs:
            rel = item["path"]
            first_body = (generated_tree / item["candidate"]).read_bytes()
            second_body = (second / item["candidate"]).read_bytes()
            assert contract.canonical_bytes(rel, first_body, manifest) == contract.canonical_bytes(
                rel, second_body, manifest
            ), rel


# DS-T1.24: every auto-resolved stage stamp in these tests lives in the test's
# own tmp dir. With no override, resolve_stage_stamp() picks the checkout's
# active target (project.yml target -> build/<target>/.rom_info_stage_stamp),
# which every generator run in the checkout shares; under the nightly's -n 8
# the sibling tests refreshed it between this file's two installs, so the
# second install no longer saw "same build" and churned the volatile lines.
# RR_BUILD_DIR is the resolver's own env step ahead of the active target.
_STAMP_ENV = ("STAGE_STAMP", "RR_STAGE_STAMP", "RR_JOB_ID", "BUILD_DIR", "RR_BUILD_DIR", "RR_TARGET")


def _isolated_env(build_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STAMP_ENV}
    env["RR_BUILD_DIR"] = str(build_dir)
    return env



def _generated_rom_info(tmp_path: Path) -> Path:
    """rom_info_pkg.vhd as the build writes it. DS-P2.231: the file is a
    gitignored build output (the runtime rom-info family), so tests generate
    it with the production generator instead of reading a tracked copy."""
    out = tmp_path / "generated_rom_info"
    r = subprocess.run(
        ["bash", "tools/regen_regbank.sh", "--rom-info-only", "--output-dir",
         str(out), "--regbank-dir", "hw/rtl/regbank", "--library", "tich"],
        cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    return out / "rom_info_pkg.vhd"

def test_volatile_metadata_does_not_churn_an_incremental_stage(tmp_path: Path, monkeypatch):
    manifest = contract.load_manifest()
    rel = "hw/rtl/pkg/rom_info_pkg.vhd"
    original_text = _generated_rom_info(tmp_path).read_text(encoding="utf-8")

    # Set up a clean baseline (dirty bit 00)
    dirty_rule = next(r for r in manifest["volatile_lines"][rel] if r["field"] == "system_version_dirty_bit")
    clean_text, count = re.subn(
        dirty_rule["pattern"],
        '  constant C_SYSTEM_VERSION  : std_logic_vector(C_ROM_DATA_WIDTH-1 downto 0) := x"0001b800";',
        original_text,
        count=1,
        flags=re.MULTILINE,
    )
    assert count == 1
    clean_bytes = clean_text.encode("utf-8")

    # The next stage in the same build sees a flipped dirty-bit (00 -> 01) because
    # stage 1 wrote hw/rtl/pkg/rom_info_pkg.vhd, as well as an advanced timestamp/date.
    changed = clean_text
    replacements = {
        "generated_at": "-- File created @Thu Jan  1 00:00:00 2099",
        "system_version_dirty_bit": (
            '  constant C_SYSTEM_VERSION  : std_logic_vector(C_ROM_DATA_WIDTH-1 downto 0) '
            ':= x"0001b801";'
        ),
        "build_date": (
            '  constant C_BUILD_DATE      : std_logic_vector(C_ROM_DATA_WIDTH-1 downto 0) '
            ':= x"FFFFFFFF";'
        ),
    }
    for field, rep in replacements.items():
        rule = next(r for r in manifest["volatile_lines"][rel] if r["field"] == field)
        changed, count = re.subn(
            rule["pattern"], rep, changed, count=1, flags=re.MULTILINE
        )
        assert count == 1

    output = tmp_path / "rom_info_pkg.vhd"
    candidate = tmp_path / "candidate.vhd"
    stage_stamp = tmp_path / "stage_stamp.vhd"
    output.write_bytes(clean_bytes)
    candidate.write_text(changed, encoding="utf-8")
    stage_stamp.write_bytes(clean_bytes)

    # 1. With explicit stage stamp
    wrote = contract.install_candidate(
        kind="rom-info", candidate=candidate, output=output, stage_stamp=stage_stamp
    )
    assert wrote is False
    assert output.read_bytes() == clean_bytes

    # 2. With auto-resolved stage stamp
    auto_dir = tmp_path / "auto_incremental"
    auto_dir.mkdir(parents=True, exist_ok=True)
    for var in _STAMP_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RR_BUILD_DIR", str(auto_dir))
    auto_out = auto_dir / "rom_info_pkg.vhd"
    auto_cand1 = auto_dir / "cand1.vhd"
    auto_cand1.write_bytes(clean_bytes)
    # First stage installs and establishes auto-resolved stamp
    wrote1 = contract.install_candidate(kind="rom-info", candidate=auto_cand1, output=auto_out)
    assert wrote1 is True
    assert auto_out.read_bytes() == clean_bytes

    # Second stage has dirty bit flip and date advance
    auto_cand2 = auto_dir / "cand2.vhd"
    auto_cand2.write_text(changed, encoding="utf-8")
    wrote2 = contract.install_candidate(kind="rom-info", candidate=auto_cand2, output=auto_out)
    assert wrote2 is False
    assert auto_out.read_bytes() == clean_bytes


def test_install_candidate_refuses_to_preserve_hash_from_different_head(tmp_path: Path):
    """DS-P2.156: install must never preserve an old C_GIT_HASH from a different HEAD."""
    manifest = contract.load_manifest()
    rel = "hw/rtl/pkg/rom_info_pkg.vhd"
    original = _generated_rom_info(tmp_path).read_bytes()
    changed = original.decode("utf-8")

    rule = next(r for r in manifest["volatile_lines"][rel] if r["field"] == "git_hash")
    changed, count = re.subn(
        rule["pattern"],
        '  constant C_GIT_HASH        : std_logic_vector(C_ROM_DATA_WIDTH-1 downto 0) := x"23280bf0";',
        changed,
        count=1,
        flags=re.MULTILINE,
    )
    assert count == 1

    # 1. With explicit stage stamp holding the old hash
    output = tmp_path / "rom_info_pkg.vhd"
    candidate = tmp_path / "candidate.vhd"
    stage_stamp = tmp_path / "stage_stamp.vhd"

    output.write_bytes(original)
    candidate.write_text(changed, encoding="utf-8")
    stage_stamp.write_bytes(original)

    wrote = contract.install_candidate(
        kind="rom-info", candidate=candidate, output=output, stage_stamp=stage_stamp
    )
    assert wrote is True, "install preserved a hash from a different HEAD"
    assert b'x"23280bf0"' in output.read_bytes(), "install preserved a hash from a different HEAD"

    # 2. With auto-resolved stage stamp holding the old hash
    auto_dir = tmp_path / "auto_hash"
    auto_dir.mkdir(parents=True, exist_ok=True)
    auto_out = auto_dir / "rom_info_pkg.vhd"
    auto_cand_old = auto_dir / "cand_old.vhd"
    auto_cand_old.write_bytes(original)
    # Establish stage stamp with old hash
    contract.install_candidate(kind="rom-info", candidate=auto_cand_old, output=auto_out)

    auto_cand_new = auto_dir / "cand_new.vhd"
    auto_cand_new.write_text(changed, encoding="utf-8")
    wrote_auto = contract.install_candidate(kind="rom-info", candidate=auto_cand_new, output=auto_out)
    assert wrote_auto is True, "auto-resolved install preserved a hash from a different HEAD"
    assert b'x"23280bf0"' in auto_out.read_bytes()


def test_regen_regbank_production_surface_intra_build_and_foreign_hash(tmp_path: Path):
    """tools/regen_regbank.sh pre_build hook preserves intra-build metadata without flags and rejects foreign hashes."""
    pkg_dir = tmp_path / "pkg"
    regbank_dir = tmp_path / "regbank"
    cmd = [
        "bash",
        str(ROOT / "tools/regen_regbank.sh"),
        "--output-dir",
        str(pkg_dir),
        "--regbank-dir",
        str(regbank_dir),
        "--library",
        "tich",
    ]
    env = _isolated_env(pkg_dir)
    # 1. First run generates rom_info_pkg.vhd for current HEAD
    subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    rom_info = pkg_dir / "rom_info_pkg.vhd"
    assert rom_info.is_file()
    content1 = rom_info.read_bytes()

    # 2. Second incremental run preserves intra-build metadata (no churn)
    time.sleep(1)
    subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    content2 = rom_info.read_bytes()
    assert content1 == content2, "regen_regbank.sh churned rom_info_pkg.vhd on an incremental pass"

    # 3. Simulate a stale package / stage stamp carrying an old hash from a different HEAD
    manifest = contract.load_manifest()
    rel = "hw/rtl/pkg/rom_info_pkg.vhd"
    rule = next(r for r in manifest["volatile_lines"][rel] if r["field"] == "git_hash")
    stale_text, count = re.subn(
        rule["pattern"],
        '  constant C_GIT_HASH        : std_logic_vector(C_ROM_DATA_WIDTH-1 downto 0) := x"deadbeef";',
        content1.decode("utf-8"),
        count=1,
        flags=re.MULTILINE,
    )
    assert count == 1
    rom_info.write_text(stale_text, encoding="utf-8")
    stage_stamp = pkg_dir / ".rom_info_stage_stamp"
    assert stage_stamp.is_file(), (
        "regen_regbank.sh left no stage stamp in the isolated build dir: the stale-stamp "
        "half of this check would be skipped")
    stage_stamp.write_text(stale_text, encoding="utf-8")

    # 4. Third run must refuse to preserve the foreign hash and restore live HEAD's hash
    subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    content3 = rom_info.read_bytes()
    head_hash = subprocess.run(
        ["git", "rev-parse", "--short=8", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert b'x"deadbeef"' not in content3, "regen_regbank.sh preserved an old hash from a different HEAD"
    assert f'x"{head_hash}"'.encode() in content3, "regen_regbank.sh did not restore current HEAD hash"
    assert contract.canonical_bytes(rel, content3, manifest) == contract.canonical_bytes(
        rel, content1, manifest
    )



def test_generated_text_contains_only_clone_relative_inputs(generated_tree: Path):
    for outputs in contract.tracked_outputs().values():
        for item in outputs:
            body = (generated_tree / item["candidate"]).read_bytes()
            contract.assert_clone_relative(item["path"], body)


def test_declared_rr_flow_owns_every_runtime_and_tracked_output():
    target = yaml.safe_load((ROOT / "targets/zybo_datalogger.yml").read_text(encoding="utf-8"))
    manifest = contract.load_manifest()
    hooks = {hook["name"]: hook for hook in target["hooks"]["pre_build"]}

    assert set(hooks["rr_rea_build_id"]["expected_outputs"]) == set(
        manifest["runtime_families"]["rea-build-id"]["outputs"]
    )
    assert set(hooks["datalogger-builder"]["expected_outputs"]) == {
        item["path"] for item in manifest["tracked_families"]["datalogger"]["outputs"]
    }
    regbank_outputs = {
        item["path"]
        for family in ("regbank-schema", "regbank-rtl")
        for item in manifest["tracked_families"][family]["outputs"]
    }
    # DS-P2.231: rom_info_pkg.vhd moved from the tracked regbank-rtl family to
    # the runtime rom-info family; regbank-regen still owns it.
    rom_info = set(manifest["runtime_families"]["rom-info"]["outputs"])
    assert manifest["runtime_families"]["rom-info"]["hook"] == "regbank-regen"
    assert set(hooks["regbank-regen"]["expected_outputs"]) == regbank_outputs | rom_info
    assert not regbank_outputs & rom_info
    assert manifest["runtime_families"]["block-design"]["tcl_hook"] in target["tcl_hooks"][
        "gen_project"
    ]
    assert len(manifest["runtime_families"]["block-design"]["outputs"]) >= 2


def test_every_target_generates_rom_info_before_sim_and_build():
    """DS-P2.231: rom_info_pkg.vhd is gitignored, so every target that builds
    it (pre_build regbank-regen) must also generate it before a sim."""
    rom_info = set(contract.load_manifest()["runtime_families"]["rom-info"]["outputs"])
    checked = 0
    for path in all_target_files(ROOT):
        target = yaml.safe_load(path.read_text(encoding="utf-8"))
        hooks = target.get("hooks") or {}
        build = {h["name"]: h for h in hooks.get("pre_build") or []}
        if "regbank-regen" not in build:
            continue
        sim = {h["name"]: h for h in hooks.get("pre_sim") or []}
        assert "rom-info-sim" in sim, f"{path.name}: no pre_sim rom-info-sim hook"
        hook = sim["rom-info-sim"]
        assert hook["script"] == build["regbank-regen"]["script"], path.name
        assert "--rom-info-only" in hook["args"], path.name
        assert set(hook["expected_outputs"]) == rom_info, path.name
        checked += 1
    assert checked >= per_tree(3, 8, ROOT)  # DS-P1.103: the three products in the export


def test_rom_info_is_gitignored_and_untracked():
    rel = "hw/rtl/pkg/rom_info_pkg.vhd"
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=ROOT,
                             capture_output=True, text=True)
    assert tracked.returncode != 0, f"{rel} is still tracked"
    ignored = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT)
    assert ignored.returncode == 0, f"{rel} is not gitignored"


def test_runtime_evidence_fails_closed_on_any_missing_output(tmp_path: Path):
    outputs = contract.load_manifest()["runtime_families"]["block-design"]["outputs"]
    for rel in outputs:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated", encoding="utf-8")
    assert contract.missing_runtime_outputs("block-design", tmp_path) == []

    (tmp_path / outputs[-1]).unlink()
    assert contract.missing_runtime_outputs("block-design", tmp_path) == [outputs[-1]]


def test_vivado_wrapper_declares_only_its_generated_date_volatile():
    manifest = contract.load_manifest()
    rel = manifest["runtime_families"]["block-design"]["outputs"][-1]
    rules = manifest["volatile_lines"][rel]
    assert [rule["field"] for rule in rules] == ["vivado_generated_at"]

    first = b"--Date        : Wed Aug 12 20:50:00 2026\nentity stable is\n"
    second = b"--Date        : Wed Aug 12 20:55:00 2026\nentity stable is\n"
    assert contract.canonical_bytes(rel, first, manifest) == contract.canonical_bytes(
        rel, second, manifest
    )


def test_every_declared_tracked_output_is_really_tracked():
    proc = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    )
    tracked = set(proc.stdout.decode("utf-8").split("\0"))
    declared = {
        item["path"]
        for outputs in contract.tracked_outputs().values()
        for item in outputs
    }
    assert declared <= tracked


def test_precommit_gate_runs_the_index_drift_check():
    hook = (ROOT / "hooks/pre-commit.local").read_text(encoding="utf-8")
    assert "generated_source_contract.py" in hook
    assert "check --actual index" in hook


def test_precommit_materializes_runtime_build_id_before_lint():
    """The clean-index gate must create the gitignored source lint consumes."""
    hook = (ROOT / "hooks/pre-commit.local").read_text(encoding="utf-8")
    command = "tools/hooks/run_sdk_generator.py rea-build-id"

    assert command in hook
    assert "--design-identity deepskopion" in hook
    assert "--out generated/rr_rea_build_id_pkg.vhd" in hook
    assert hook.index(command) > hook.index("check --actual index")
