# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""Regression tests for Reference Design manifests (DS-P2.119).

Guards the requirements of DS-P2.119:
1. Both committed targets have manifests:
   - de25: ref_design.yml and ref_design_de25.yml (Agilex-5, Quartus Pro, exact version)
   - canfd_demo: ref_design_canfd_demo.yml (zybo-z7-20, Vivado, exact version)
   - pfsoc_datalogger: ref_design_pfsoc.yml (polarfire-soc-discovery, Libero SoC,
     exact version; DS-P3.156)
2. Schema validation: all manifests pass sdk.engine.ref_design_schema validation.
3. Exact tool versions: version ranges (>=, <=, >, <, commas) are rejected.
4. Canonical board IDs: board IDs must be canonical (de25-standard, zybo-z7-20).
5. Evidence-based verification status: DS-P2.118 evidence justifies sandbox-verified,
   never claimed-by-default.
6. Pinned IPs in uses: block match registry requirements (namespace/name).
7. Public GitHub source repo pointer with pinned commit.
8. CLI surface: `routertl ref publish` succeeds for valid manifests and fails for invalid.
"""
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from sdk.engine.ref_design_schema import (
    load_known_boards,
    load_manifest,
    parse_manifest,
    validate_manifest,
)
from tools.ref_design_validator import (
    CANONICAL_BOARDS,
    PENDING_CANONICAL_BOARDS,
    get_canonical_boards,
    validate_deepskopion_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
DE25_MANIFEST = ROOT / "ref_design.yml"
DE25_NAMED_MANIFEST = ROOT / "ref_design_de25.yml"
ZYBO_MANIFEST = ROOT / "ref_design_zybo.yml"  # DS-P1.103: the Zybo product design
PFSOC_MANIFEST = ROOT / "ref_design_pfsoc.yml"

# DS-P1.103 param split: the CTU bench manifest (path2) runs in the _bench.py sibling; the ids
# are the original ones, so no node id changes.
ALL_MANIFEST_PATHS = [pytest.param(DE25_MANIFEST, id="path0"), pytest.param(DE25_NAMED_MANIFEST, id="path1"),
                      pytest.param(ZYBO_MANIFEST, id="path3"), pytest.param(PFSOC_MANIFEST, id="path4")]


class TestRefDesignManifestExistence:
    """Verify that required reference design manifest files exist in project root."""

    @pytest.mark.parametrize("path", ALL_MANIFEST_PATHS)
    def test_manifest_file_exists(self, path: Path):
        assert path.exists(), f"Reference design manifest missing: {path.name}"
        assert path.stat().st_size > 0, f"Manifest file is empty: {path.name}"


class TestRefDesignSchemaConformance:
    """Verify schema conformance and Deepskopion-specific invariants."""

    @pytest.mark.parametrize("path", ALL_MANIFEST_PATHS)
    def test_schema_validation_passes(self, path: Path):
        rd = load_manifest(path)
        errors = validate_manifest(rd)
        assert errors == [], f"Schema validation failed for {path.name}: {errors}"

    @pytest.mark.parametrize("path", ALL_MANIFEST_PATHS)
    def test_deepskopion_validation_passes(self, path: Path):
        errors = validate_deepskopion_manifest(path)
        assert errors == [], f"Deepskopion validation failed for {path.name}: {errors}"

    def test_de25_target_declaration(self):
        rd = load_manifest(DE25_MANIFEST)
        assert rd.target.board == "de25-standard"
        assert rd.target.vendor == "altera"
        assert rd.target.family == "agilex-5"
        assert rd.target.part == "A5ED013BB32AE4SCS"
        assert rd.target.tool == "Quartus Pro"
        assert rd.target.tool_version == "25.3.1"
        assert rd.project.top_module == "deepskopion_de25_top"
        assert rd.project.language == "vhdl"

    def test_zybo_product_target_declaration(self):
        """DS-P1.103: the Zybo product design builds zybo_datalogger, with no CTU CAN FD core."""
        rd = load_manifest(ZYBO_MANIFEST)
        assert rd.target.board == "zybo-z7-20"
        assert rd.target.part == "xc7z020clg400-1"
        assert rd.target.tool_version == "2024.1"
        assert rd.project.top_module == "deepskopion_top"
        assert "rr target set zybo_datalogger" in rd.build.command
        assert rd.build.expected_bitstream == "build/zybo_datalogger/bitstream/deepskopion.bit"
        assert "ctu-fee/ctucanfd_ip_core" not in {u.ref for u in rd.uses}


    def test_pfsoc_target_declaration(self):
        """DS-P3.156: the PolarFire SoC Discovery Kit manifest names the
        pfsoc_datalogger target as targets/pfsoc_datalogger.yml declares it."""
        rd = load_manifest(PFSOC_MANIFEST)
        assert rd.target.board == "polarfire-soc-discovery"
        assert rd.target.vendor == "microchip"
        assert rd.target.family == "polarfire-soc"
        assert rd.target.part == "MPFS095T-1FCSG325E"
        assert rd.target.tool == "Libero SoC"
        assert rd.target.tool_version == "2025.2"
        assert rd.project.top_module == "deepskopion_pfsoc_top"
        assert rd.project.language == "vhdl"
        assert rd.build.command == "rr target set pfsoc_datalogger && rr synth run && rr bitstream"
        assert rd.build.expected_bitstream == "build/pfsoc_datalogger/bitstream/deepskopion.job"

        target = yaml.safe_load((ROOT / "targets" / "pfsoc_datalogger.yml").read_text())
        assert target["hardware"]["platform"] == rd.target.board
        assert target["hardware"]["vendor"] == rd.target.vendor
        assert target["hardware"]["part"] == rd.target.part
        assert str(target["hardware"]["tool_version"]) == rd.target.tool_version
        assert target["project"]["top_module"] == rd.project.top_module

    def test_pfsoc_uses_pinned_to_ip_lock(self):
        """Every uses: entry is an ip.lock package at its requested version."""
        lock = yaml.safe_load((ROOT / "ip.lock").read_text())
        rd = load_manifest(PFSOC_MANIFEST)
        refs = [ip.ref for ip in rd.uses]
        assert refs == [
            "open-logic/olo_base_fifo_sync",
            "open-logic/olo_base_fifo_async",
            "open-logic/olo_base_wconv_n2xn",
            "open-logic/olo_base_wconv_xn2n",
            "routertl/rea",
            "routertl/rr_ddr_stream",
        ]
        for ip in rd.uses:
            assert ip.ref in lock, ip.ref
            assert lock[ip.ref]["requested_version"] == ip.version, ip.ref

    def test_pfsoc_resources_are_the_provisional_compile_report(self):
        """Libero SoC 2025.2 compile report of 2026-11-05: 4LUT 43149, DFF 34845,
        LSRAM 59 x 20 Kb. Compile-only evidence: claimed with the blocker named,
        and no fmax (no timing report exists)."""
        rd = load_manifest(PFSOC_MANIFEST)
        assert rd.resources.luts == 43149
        assert rd.resources.ffs == 34845
        assert rd.resources.bram_kbits == 59 * 20
        assert rd.resources.fmax_mhz == 0.0
        raw = yaml.safe_load(PFSOC_MANIFEST.read_text())
        assert "fmax_mhz" not in raw["resources"]
        assert "provisional" in PFSOC_MANIFEST.read_text().lower()
        assert rd.verification.status == "claimed"
        assert "DS-P1.88" in raw["verification"]["blocker"]
        assert rd.verification.last_verified_tool_version == "2025.2"

    @pytest.mark.parametrize("path", [DE25_MANIFEST, DE25_NAMED_MANIFEST])
    def test_de25_verification_evidence_rule(self, path: Path):
        """DE25 qualification evidence in DS-P2.118 (commit cac8361) witnessed successful
        image build (job 71e7feee75aa, exit 0), programming on silicon, and RouteWave v2 accept,
        justifying sandbox-verified."""
        rd = load_manifest(path)
        assert rd.verification.status == "sandbox-verified"
        assert rd.verification.last_verified_commit == "cac8361a9c78df5d1952abf7bc7e9ed255a634fa"
        assert rd.verification.last_verified_tool_version == "25.3.1"
        assert rd.verification.last_verified_date != ""
        # Corroborating resources from witnessed build
        assert rd.resources.luts == 28129
        assert rd.resources.fmax_mhz == 100.4


    @pytest.mark.parametrize("path", ALL_MANIFEST_PATHS)
    def test_uses_ips_resolved_through_registry(self, path: Path):
        rd = load_manifest(path)
        assert len(rd.uses) > 0, f"Manifest {path.name} must declare IP dependencies"
        for ip in rd.uses:
            assert "/" in ip.ref, f"IP ref must be 'namespace/name' format: {ip.ref}"
            assert ip.version != "", f"IP {ip.ref} missing version constraint"

    @pytest.mark.parametrize("path", ALL_MANIFEST_PATHS)
    def test_source_repo_is_public_github(self, path: Path):
        rd = load_manifest(path)
        assert rd.source.repo == "https://github.com/routertl/deepskopion"
        assert rd.source.commit != ""


class TestRefDesignRegressions:
    """Power tests — assert that mutated or invalid manifests fail loudly."""

    @pytest.mark.parametrize("bad_version", [
        ">=2024.1",
        "<=2024.1",
        ">2024.1",
        "<2024.1",
        "2024.1,2024.2",
        ">=25.3",
    ])
    def test_tool_version_range_rejected(self, bad_version: str):
        with open(DE25_MANIFEST) as f:
            data = yaml.safe_load(f)
        data["target"]["tool_version"] = bad_version
        rd = parse_manifest(data)
        errors = validate_manifest(rd)
        assert any("must be exact" in err for err in errors), (
            f"Range '{bad_version}' was not rejected: {errors}"
        )

    @pytest.mark.parametrize("bad_board", [
        "de25",             # Non-canonical shorthand
        "zybo",             # Non-canonical shorthand
        "unknown-board-id", # Fabricated board
        "",                 # Missing board
    ])
    def test_non_canonical_board_rejected(self, bad_board: str):
        with open(DE25_MANIFEST) as f:
            data = yaml.safe_load(f)
        data["target"]["board"] = bad_board
        rd = parse_manifest(data)
        errors = validate_deepskopion_manifest(rd)
        assert len(errors) > 0, f"Bad board '{bad_board}' was not rejected"
        assert any("board" in err.lower() for err in errors)

    def test_claimed_by_default_rejected(self):
        """DS-P2.118 qualification evidence requires sandbox-verified, not claimed."""
        with open(DE25_MANIFEST) as f:
            data = yaml.safe_load(f)
        data["verification"]["status"] = "claimed"
        rd = parse_manifest(data)
        errors = validate_deepskopion_manifest(rd)
        assert any("claimed" in err.lower() for err in errors), (
            f"Default 'claimed' status was not rejected: {errors}"
        )


    def test_canonical_boards_anchored_on_routertl_boards_yml(self, monkeypatch):
        """Canonical board check must consult sdk.engine.ref_design_schema.load_known_boards().
        'zybo-z7-20' is in RouteRTL boards.yml.
        'de25-standard' is covered by a documented pending-addition allowance.
        Third boards from boards.yml (e.g. 'arty-a7-35t') must appear in get_canonical_boards().
        Dynamic updates from load_known_boards() must be reflected in get_canonical_boards().
        """
        known = load_known_boards()
        assert "zybo-z7-20" in known, "zybo-z7-20 must be in RouteRTL load_known_boards()"
        assert "de25-standard" in known, (
            "de25-standard is in RouteRTL boards.yml since ae29dd26e (RTL-P2.1498); "
            "the PENDING allowance retires under DS-P3.169"
        )
        assert "de25-standard" in PENDING_CANONICAL_BOARDS

        # (a) Assert a third boards.yml board the hardcode never contained
        # (e.g. 'arty-a7-35t', present at RouteRTL 36a25f1f) appears in get_canonical_boards()
        assert "arty-a7-35t" in known, "arty-a7-35t must be in RouteRTL load_known_boards()"
        canonical = get_canonical_boards()
        assert "de25-standard" in canonical
        assert "zybo-z7-20" in canonical
        assert "arty-a7-35t" in canonical
        assert "arty-a7-35t" in CANONICAL_BOARDS

        # (b) Monkeypatch load_known_boards to return a sentinel set and assert
        # get_canonical_boards() reflects it
        sentinel_board = "sentinel-mock-board-42"
        monkeypatch.setattr(
            "sdk.engine.ref_design_schema.load_known_boards",
            lambda: {sentinel_board},
        )
        monkeypatch.setattr(
            "tools.ref_design_validator.load_known_boards",
            lambda: {sentinel_board},
        )
        patched_canonical = get_canonical_boards()
        assert sentinel_board in patched_canonical, (
            "get_canonical_boards() must reflect load_known_boards() changes"
        )
        assert "zybo-z7-20" not in patched_canonical, (
            "get_canonical_boards() must not retain hardcoded RouteRTL boards when load_known_boards() changes"
        )
        assert "de25-standard" in patched_canonical, (
            "de25-standard pending allowance should be preserved alongside load_known_boards()"
        )


class TestCliSurface:
    """Assert at the caller-reachable CLI surface (rr ref publish and validator)."""

    def test_cli_ref_publish_de25(self):
        res = subprocess.run(
            ["rr", "ref", "publish", str(DE25_MANIFEST)],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        assert res.returncode == 0, f"CLI ref publish failed: {res.stdout}\n{res.stderr}"
        assert "Manifest valid" in res.stdout

    def test_cli_ref_publish_bare_defaults_to_ref_design_yml(self):
        res = subprocess.run(
            ["rr", "ref", "publish"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        assert res.returncode == 0, f"Bare CLI ref publish failed: {res.stdout}\n{res.stderr}"
        assert "Manifest valid" in res.stdout


    def test_cli_ref_publish_pfsoc(self):
        res = subprocess.run(
            ["rr", "ref", "publish", str(PFSOC_MANIFEST)],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        assert res.returncode == 0, f"CLI ref publish failed: {res.stdout}\n{res.stderr}"
        assert "Manifest valid" in res.stdout

    def test_cli_ref_design_validator_pfsoc(self):
        res = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "ref_design_validator.py"), str(PFSOC_MANIFEST)],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        assert res.returncode == 0, f"Validator CLI failed for pfsoc: {res.stdout}\n{res.stderr}"
        assert "Manifest valid" in res.stdout

    def test_cli_ref_design_validator(self):
        res = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "ref_design_validator.py"), str(DE25_MANIFEST)],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        assert res.returncode == 0, f"Validator CLI failed: {res.stdout}\n{res.stderr}"
        assert "Manifest valid" in res.stdout
