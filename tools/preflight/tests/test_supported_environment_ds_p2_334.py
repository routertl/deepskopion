# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.334: the declaration, its prose and the preflight agree on the host OS,
WSL and the targets.

The clean-machine rehearsal of the export built the Zybo bitstream on Ubuntu
24.04 under WSL2, while the preflight warned "ubuntu 24.04 vs declared 22.04"
and the declaration still listed WSL and DE25 as unsupported. Ubuntu 24.04 is now
declared with the run that measured it and the scope that run covered; the three
product targets are declared with their silicon evidence; the preflight passes a
declared version and names the declaration when it warns.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))

from preflight import ds_preflight as P  # noqa: E402

DECL = yaml.safe_load((ROOT / "tools/preflight/supported_environment.yml").read_text(encoding="utf-8"))
DOC = (ROOT / "docs/SUPPORTED_ENVIRONMENT.md").read_text(encoding="utf-8")


def _os_result(os_id: str | None, version: str | None):
    return P._check_host_os(P.Probe(os_id=os_id, os_version=version), DECL, ROOT)


# ── the preflight verdict, pinned against the declaration ────────────────────

def test_a_24_04_host_passes_the_os_check():
    r = _os_result("ubuntu", "24.04")
    assert r.ok and not r.blocking
    assert r.found == "ubuntu 24.04"


def test_the_reference_22_04_host_passes_the_os_check():
    assert _os_result("ubuntu", "22.04").ok


@pytest.mark.parametrize("os_id, version", [("ubuntu", "20.04"), ("debian", "12"), (None, None)])
def test_an_undeclared_host_warns_and_names_the_declaration(os_id, version):
    r = _os_result(os_id, version)
    assert not r.ok
    assert r.severity == "warn" and not r.blocking
    assert "tools/preflight/supported_environment.yml" in r.fix
    assert "ubuntu 22.04" in r.requirement and "ubuntu 24.04" in r.requirement


def test_the_check_reads_the_declaration_not_a_constant():
    """Mutant: drop 24.04 from the declaration and the same host warns again."""
    decl = yaml.safe_load(yaml.safe_dump(DECL))
    decl["host"]["os"]["also_supported"] = []
    assert not P._check_host_os(P.Probe(os_id="ubuntu", os_version="24.04"), decl, ROOT).ok


# ── every claim carries its evidence ─────────────────────────────────────────

def test_each_extra_os_version_names_its_scope_and_evidence():
    extras = DECL["host"]["os"]["also_supported"]
    assert [e["version"] for e in extras] == ["24.04"]
    for e in extras:
        assert e["scope"].strip() and e["evidence"].strip(), e


def test_the_declared_targets_are_the_product_targets():
    shipped = sorted(p.stem for p in (ROOT / "targets").glob("*.yml"))
    assert sorted(DECL["targets"]) == shipped == ["de25_datalogger", "pfsoc_datalogger", "zybo_datalogger"]
    for name, t in DECL["targets"].items():
        assert t["status"] in ("supported", "preview"), name
        assert t["evidence"].strip() and t["tool"].strip(), name


def test_the_unsupported_list_no_longer_excludes_wsl_or_de25():
    joined = " ".join(DECL["unsupported"]).lower()
    assert "de25" not in joined and "altera" not in joined
    assert "windows" in joined and "macos" in joined
    assert "wsl2 past the bitstream" in joined


# ── the prose says the same ──────────────────────────────────────────────────

def test_the_doc_lists_each_declared_os_version():
    for v, name in P._declared_os_versions(DECL["host"]["os"]):
        assert v in DOC and name.split(" (")[0] in DOC, v


def test_the_doc_tabulates_each_target_with_its_tool_and_status():
    for name, t in DECL["targets"].items():
        row = next((line for line in DOC.splitlines() if line.startswith(f"| `{name}` |")), None)
        assert row is not None, name
        assert f"| {t['tool']} |" in row and f"| {t['status']} |" in row, row


def test_the_doc_no_longer_calls_wsl_or_de25_unsupported():
    section = DOC.split("## What is NOT supported", 1)[1].split("\n## ", 1)[0]
    assert "DE25" not in section and "Altera" not in section
    assert "Windows, macOS, WSL" not in section
    assert "WSL2 past the bitstream" in section
