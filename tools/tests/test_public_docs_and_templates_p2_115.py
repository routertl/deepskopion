# rr unknown-llm (AI-generated)
"""DS-P2.115 — Regression guard for public README rewrite, CONTRIBUTING.md,
ATTRIBUTIONS.md, and .github templates.

MEASURED DEFECT:
The public README contained internal developer noise (ticket citations like DS-*,
pre-commit test file names, cocotb dependency floor maintenance blocks, and Makefile
shims). The architecture diagram was obsolete (named logic-trace viewer instead of
RouteWave, and only covered Zynq PS without Agilex HPS). The repo lacked a
CONTRIBUTING.md specifying the DCO sign-off model and hardware-validated PR rule,
lacked ATTRIBUTIONS.md with openlogicbit and sigrok/PulseView entries, and lacked
.github issue and PR templates.

This test asserts all requirements at the caller-reachable surfaces (file existence,
content contracts, and link resolution).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
ATTRIBUTIONS = ROOT / "ATTRIBUTIONS.md"
GITHUB_DIR = ROOT / ".github"
BUG_REPORT_TEMPLATE = GITHUB_DIR / "ISSUE_TEMPLATE" / "bug_report.md"
PROTOCOL_PROPOSAL_TEMPLATE = GITHUB_DIR / "ISSUE_TEMPLATE" / "new_protocol_proposal.md"
PR_TEMPLATE = GITHUB_DIR / "PULL_REQUEST_TEMPLATE.md"

LINK_RE = re.compile(r"\[(?:[^\[\]]|\[[^\]]*\])*\]\(([^)]+)\)")
TICKET_CITE_RE = re.compile(r"\b(?:DS|RTL|LT|TIC)-[PTD]\d+\.\d+\b")


def _resolve_link(raw_target: str, src_file: Path) -> Path | None:
    if raw_target.startswith(("http://", "https://", "mailto:", "#")):
        return None
    target_path = raw_target.split("#", 1)[0].strip()
    if not target_path:
        return None
    return (src_file.parent / target_path).resolve()


# --------------------------------------------------------------------------- #
# README.md: noise removal & clean tone
# --------------------------------------------------------------------------- #
def test_readme_has_no_internal_ticket_citations() -> None:
    text = README.read_text(encoding="utf-8")
    citations = sorted(set(TICKET_CITE_RE.findall(text)))
    assert not citations, (
        f"README.md contains internal ticket citations: {citations}"
    )


def test_readme_has_no_internal_developer_noise() -> None:
    text = README.read_text(encoding="utf-8")
    noise_markers = [
        "cocotb_dependency_floor.py",
        "guard-install",
        "test_pkg_install_clean_clone_p3_150.py",
        "hooks/pre-commit.local",
        "make lint",
        "make simulation",
    ]
    found = [m for m in noise_markers if m in text]
    assert not found, (
        f"README.md contains internal developer noise: {found}"
    )


# --------------------------------------------------------------------------- #
# README.md: architecture diagram & adoption framing
# --------------------------------------------------------------------------- #
def test_readme_architecture_diagram_corrected() -> None:
    text = README.read_text(encoding="utf-8")
    assert "RouteWave" in text, "README.md architecture must name RouteWave"
    assert "logic-trace" not in text, "README.md architecture must not mention logic-trace"
    # Diagram or architecture text must cover both Zynq PS and Agilex HPS
    assert "Zynq" in text and "Agilex" in text, (
        "README.md architecture must cover both Zynq PS and Agilex HPS"
    )
    assert "HPS" in text and "PS" in text, (
        "README.md architecture must reference both PS and HPS"
    )


def test_readme_adoption_framing_and_protocols() -> None:
    text = README.read_text(encoding="utf-8")
    # Adoption framing leads: boards people already own
    assert "Zybo Z7-20" in text, "README.md must feature Zybo Z7-20"
    assert "DE25" in text, "README.md must feature DE25"

    # Supported protocols listed
    for proto in ["UART", "I2C", "SPI", "CAN", "Edge"]:
        assert proto in text, f"README.md must mention protocol {proto}"


def test_readme_when_not_right_tool_references() -> None:
    text = README.read_text(encoding="utf-8")
    assert "sigrok" in text, "README.md must reference sigrok"
    assert "PulseView" in text, "README.md must reference PulseView"
    # Owner 2026-11-02: openlogicbit is neither shipped nor used, so it is not named.
    assert "openlogicbit" not in text


# --------------------------------------------------------------------------- #
# README.md: self-test section with block diagram
# --------------------------------------------------------------------------- #
def test_readme_self_test_section_and_block_diagram() -> None:
    text = README.read_text(encoding="utf-8")
    assert "Self-Test" in text or "self-test" in text, (
        "README.md must have a self-test section"
    )
    # Zybo EMIO routing details
    assert "hw/bd/ds_emio_uart_i2c_spi.tcl" in text
    assert "hw/bd/ds_emio_can.tcl" in text
    assert "hw/rtl/emio/ds_emio_mux.vhd" in text
    assert "hw/rtl/emio/glitch_filter.vhd" in text
    # DE25 RTL stimulus details
    assert "hw/rtl/stimulus/ds_stimulus_gen.vhd" in text
    assert "STIM_" in text or "stimulus" in text
    assert "project_regbank.yml" in text
    # Block diagrams are committed SVG files (owner 2026-11-02: Mermaid did not render).
    for image in ("docs/images/architecture.svg", "docs/images/self_test.svg"):
        assert f"]({image})" in text, f"README.md must show {image}"
        svg = (ROOT / image).read_text(encoding="utf-8")
        assert svg.startswith("<svg") and "</svg>" in svg
    assert "mermaid" not in text
    assert "gather" in text.lower()
    assert "ddr" in text.lower()


# --------------------------------------------------------------------------- #
# README.md: RouteRTL build instructions & tool floors
# --------------------------------------------------------------------------- #
def test_readme_build_with_routertl_and_floors() -> None:
    text = README.read_text(encoding="utf-8")
    assert "pip install routertl" in text
    assert "rr pkg install" in text
    assert "rr synth run" in text
    # The Zybo bitstream reaches the board inside the SD image (DS-P1.68).
    assert "rr linux build" in text
    assert "rr linux flash-sd" in text

    # Tool version floors from routertl.lock and supported_environment.yml SSOT
    assert "2024.1" in text, "README.md must state Vivado version floor 2024.1"
    assert "25.3" in text, "README.md must state Quartus version floor 25.3"
    assert "1.19.3" in text, "README.md must state NVC simulator version floor 1.19.3"

    ssot_path = ROOT / "tools" / "preflight" / "supported_environment.yml"
    assert ssot_path.is_file(), "supported_environment.yml missing"
    env_data = yaml.safe_load(ssot_path.read_text(encoding="utf-8"))
    rr_floor = f"routertl >= {env_data['routertl']['min']}"
    assert rr_floor in text, (
        f"README.md must state the RouteRTL floor from supported_environment.yml ({rr_floor})"
    )
    py_spec = env_data["python"]
    expected_py = f">= {py_spec['min']}, < {py_spec['max_exclusive']}"
    assert expected_py in text, (
        f"README.md must state Python version floor matching SSOT supported_environment.yml ({expected_py})"
    )


# --------------------------------------------------------------------------- #
# README.md: CLI configurator & on-board software
# --------------------------------------------------------------------------- #
def test_readme_cli_configurator_and_software() -> None:
    text = README.read_text(encoding="utf-8")
    assert "configurator.py" in text
    assert "tools/datalogger-builder" in text

    # On-board software details
    assert "7475" in text, "README.md must mention TCP port 7475"
    assert "wave_stream_v1" in text, "README.md must name wire format wave_stream_v1"
    assert "sw/telemetry" in text or "telemetry" in text, (
        "README.md must mention the telemetry exporter"
    )
    assert "docs/software" in text, "README.md must link to docs/software"


# --------------------------------------------------------------------------- #
# README.md: links validity
# --------------------------------------------------------------------------- #
def test_readme_relative_links_resolve() -> None:
    text = README.read_text(encoding="utf-8")
    broken = []
    for match in LINK_RE.finditer(text):
        target = match.group(1).strip()
        resolved = _resolve_link(target, README)
        if resolved is not None and not resolved.exists():
            broken.append(f"{target} (resolved to {resolved})")
    assert not broken, f"README.md contains broken relative links: {broken}"


def test_readme_relative_links_resolve_in_exported_tree() -> None:
    """DS-P2.117: Every relative link in README.md must point to a file that survives the export gate (not in DROP_LIST)."""
    import importlib.util
    hygiene_path = ROOT / "tools" / "public_artifact_hygiene.py"
    spec = importlib.util.spec_from_file_location("public_artifact_hygiene", hygiene_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    text = README.read_text(encoding="utf-8")
    dropped_links = []
    for match in LINK_RE.finditer(text):
        target = match.group(1).strip()
        resolved = _resolve_link(target, README)
        if resolved is not None and resolved.exists():
            rel_path = resolved.relative_to(ROOT).as_posix()
            matched = mod._matches_drop_list(rel_path)
            if matched:
                dropped_links.append(f"{target} matches DROP_LIST item '{matched}'")
    assert not dropped_links, f"README.md links to files excluded by export DROP_LIST:\n{dropped_links}"


# --------------------------------------------------------------------------- #
# CONTRIBUTING.md
# --------------------------------------------------------------------------- #
def test_contributing_md_exists_and_conforms() -> None:
    assert CONTRIBUTING.is_file(), "CONTRIBUTING.md must exist at repo root"
    text = CONTRIBUTING.read_text(encoding="utf-8")

    # DCO sign-off requirement
    assert "Developer Certificate of Origin" in text or "DCO" in text
    assert "Signed-off-by:" in text

    # Dual licensing (CERN-OHL-W-2.0 + MPL-2.0) and NO CLA
    assert "CERN-OHL-W-2.0" in text
    assert "MPL-2.0" in text
    assert "CLA" in text or "Contributor License Agreement" in text

    # Hardware-validated PR rule
    assert "hardware" in text.lower()
    assert "rr hw run" in text, "CONTRIBUTING.md must require 'rr hw run'"
    assert "simulation" in text.lower() or "regression" in text.lower()

    # Next goals: other boards and new protocols (EtherCAT first)
    assert "EtherCAT" in text
    assert "boards" in text.lower()


# --------------------------------------------------------------------------- #
# ATTRIBUTIONS.md
# --------------------------------------------------------------------------- #
def test_attributions_md_exists_and_conforms() -> None:
    assert ATTRIBUTIONS.is_file(), "ATTRIBUTIONS.md must exist at repo root"
    text = ATTRIBUTIONS.read_text(encoding="utf-8")
    assert "openlogicbit" not in text
    assert "sigrok" in text or "PulseView" in text


# --------------------------------------------------------------------------- #
# .github issue and PR templates
# --------------------------------------------------------------------------- #
def test_github_templates_exist_and_conform() -> None:
    assert BUG_REPORT_TEMPLATE.is_file(), (
        f"Bug report template must exist at {BUG_REPORT_TEMPLATE}"
    )
    bug_text = BUG_REPORT_TEMPLATE.read_text(encoding="utf-8")
    assert "board" in bug_text.lower()
    assert "reproduce" in bug_text.lower()

    assert PROTOCOL_PROPOSAL_TEMPLATE.is_file(), (
        f"Protocol proposal template must exist at {PROTOCOL_PROPOSAL_TEMPLATE}"
    )
    proto_text = PROTOCOL_PROPOSAL_TEMPLATE.read_text(encoding="utf-8")
    assert "protocol" in proto_text.lower()
    assert "framing" in proto_text.lower() or "packet" in proto_text.lower()

    assert PR_TEMPLATE.is_file(), f"PR template must exist at {PR_TEMPLATE}"
    pr_text = PR_TEMPLATE.read_text(encoding="utf-8")
    # Hardware validation checklist
    assert "- [ ]" in pr_text or "* [ ]" in pr_text
    assert "hardware" in pr_text.lower()
    assert "rr hw run" in pr_text, "PR template must require 'rr hw run'"
    assert "board" in pr_text.lower()
    assert "Signed-off-by:" in pr_text or "DCO" in pr_text


def _fenced_rr_commands(text: str) -> set[tuple[str, str]]:
    """Each rr verb and subcommand shown in a fenced code block."""
    found = set()
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S):
        for line in block.splitlines():
            line = line.split("#", 1)[0]
            match = re.match(r"\s*(?:[A-Z_]+=\S+\s+)*rr\s+([a-z][\w-]*)(?:\s+([a-z][\w-]*))?", line)
            if match:
                found.add((match.group(1), match.group(2) or ""))
    return found


def test_every_rr_command_in_the_public_docs_exists():
    """CONTRIBUTING once required a command rr does not have."""
    commands = set()
    for name in ("README.md", "CONTRIBUTING.md"):
        commands |= _fenced_rr_commands((ROOT / name).read_text(encoding="utf-8"))
    assert ("linux", "build") in commands
    missing = []
    for verb, sub in sorted(commands):
        argv = ["rr", verb, *([sub] if sub else []), "--help"]
        proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            missing.append(" ".join(argv))
    assert missing == []
