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

import html
import re
import subprocess
from pathlib import Path

import yaml
import pytest
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
# The board guides, self-test details and long instructions moved to docs/getting_started/ (owner 2026-11-25).
GUIDES_DIR = ROOT / "docs" / "getting_started"
SELF_TEST = GUIDES_DIR / "self_test.md"
ZYBO_GUIDE = GUIDES_DIR / "zybo_z7_20.md"
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
def _readme_and_guides() -> str:
    """The README and the guides it links: the instructions moved there keep the README's rules (T1 rv-011e9bc77c)."""
    return "\n".join(p.read_text(encoding="utf-8") for p in [README, *sorted((p for p in GUIDES_DIR.rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".markdown")))])


def test_readme_has_no_internal_ticket_citations() -> None:
    text = _readme_and_guides()
    citations = sorted(set(TICKET_CITE_RE.findall(text)))
    assert not citations, (
        f"README.md contains internal ticket citations: {citations}"
    )


def test_readme_has_no_internal_developer_noise() -> None:
    text = _readme_and_guides()
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
    assert "logic-trace" not in _readme_and_guides(), "README.md and its guides must not mention logic-trace"
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


def test_an_encoded_export_command_is_refused() -> None:
    """Control (T1 rv-70ae358262): 'export&#45;sr' renders as export-sr."""
    with pytest.raises(AssertionError, match="export command"):
        _check_no_pulseview_export("Run routewave_cli export&#45;sr.\n")
    with pytest.raises(AssertionError, match="export command"):
        _check_no_pulseview_export("Run routewave_cli export-**sr**.\n")
    with pytest.raises(AssertionError, match="export command"):
        _check_no_pulseview_export("```bash\nroutewave_cli 'export-'sr capture.sr\n```\n")
    with pytest.raises(AssertionError, match="PulseView"):
        _check_no_pulseview_export("Open it in **Pulse**View.\n")


def test_readme_when_not_right_tool_references() -> None:
    text = _readme_and_guides()
    # Each file parsed on its own: an unclosed fence in one cannot turn the next file's prose into code
    # (T1 rv-3b05fb442d).
    for doc in _readme_and_guide_paths():
        _check_no_pulseview_export(doc.read_text(encoding="utf-8"))
    # Owner 2026-11-02: openlogicbit is neither shipped nor used, so it is not named.
    assert "openlogicbit" not in text


def _check_no_pulseview_export(raw: str) -> None:
    # Checked in the raw text and in the rendered text, where entities are decoded (T1 rv-70ae358262).
    # Tags are removed without a gap, so emphasis inside a word ('export-**sr**', '**Pulse**View') reads as the word
    # (T1 rv-59f510b8fd).
    rendered = html.unescape(re.sub(r"<[^>]*>", "", MarkdownIt("commonmark").render(raw)))
    # ... and with shell quoting removed, so a quoted word ('export-'sr) reads as the argument it becomes
    # (T1 rv-14f0886228).
    unquoted = re.sub(r"[\'\"\\]", "", rendered)
    for text in (raw, rendered, unquoted):
        # Owner 2026-11-25: "Remove the pulseview export"; the README names neither (ATTRIBUTIONS.md keeps the row).
        assert "sigrok" not in text, "README.md and its guides must not reference sigrok"
        assert "PulseView" not in text, "README.md and its guides must not reference PulseView"
        # ... nor the export command itself (T1 rv-a58ad4bdbb).
        assert "export-sr" not in text, "README.md and its guides must not give the PulseView export command"


# --------------------------------------------------------------------------- #
# README.md: self-test section with block diagram
# --------------------------------------------------------------------------- #
def test_readme_self_test_section_and_block_diagram() -> None:
    text = README.read_text(encoding="utf-8")
    assert "Self-Test" in text or "self-test" in text, (
        "README.md must have a self-test section"
    )
    # The traffic-source details are in the self-test guide the README links.
    st = SELF_TEST.read_text(encoding="utf-8")
    # Zybo EMIO routing details
    assert "hw/bd/ds_emio_uart_i2c_spi.tcl" in st
    assert "hw/bd/ds_emio_can.tcl" in st
    assert "hw/rtl/emio/ds_emio_mux.vhd" in st
    assert "hw/rtl/emio/glitch_filter.vhd" in st
    # DE25 RTL stimulus details
    assert "hw/rtl/stimulus/ds_stimulus_gen.vhd" in st
    assert "STIM_" in st or "stimulus" in st
    assert "project_regbank.yml" in st
    # Block diagrams are committed SVG files (owner 2026-11-02: Mermaid did not render).
    for image in ("docs/images/architecture.svg", "docs/images/self_test.svg"):
        assert f"]({image})" in text, f"README.md must show {image}"
        svg = (ROOT / image).read_text(encoding="utf-8")
        assert svg.startswith("<svg") and "</svg>" in svg
    assert "mermaid" not in _readme_and_guides()
    assert "gather" in text.lower()
    assert "ddr" in text.lower()


# --------------------------------------------------------------------------- #
# README.md: RouteRTL build instructions & tool floors
# --------------------------------------------------------------------------- #
def test_readme_build_with_routertl_and_floors() -> None:
    text = README.read_text(encoding="utf-8")
    assert "pip install routertl" in text
    assert "rr pkg install" in text
    # The build steps are in the board guides the README links.
    zybo = ZYBO_GUIDE.read_text(encoding="utf-8")
    assert "rr synth run" in zybo
    # The Zybo bitstream reaches the board inside the SD image (DS-P1.68).
    assert "rr linux build" in zybo
    assert "rr linux flash-sd" in zybo

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
    # The configurator steps moved to the datalogger-mix guide; they are checked there (T1 rv-011e9bc77c).
    mix = (GUIDES_DIR / "datalogger_mix.md").read_text(encoding="utf-8")
    assert "configurator.py" in mix
    assert "tools/datalogger-builder" in mix
    # The README keeps a configurator example on the front page (owner 2026-11-25), checked there too (T5 rv-cff2f0dbd7).
    assert "tools/datalogger-builder/configurator.py" in text, "README.md lost its configurator example"
    assert "--board" in text, "README.md's configurator example no longer names a board"

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
def _readme_and_guide_paths() -> list[Path]:
    return [README, *sorted(p for p in GUIDES_DIR.rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".markdown"))]


def test_readme_relative_links_resolve() -> None:
    # The README and each guide it links, every link resolved from the file that holds it (T1 rv-6825d4f6e9).
    broken = []
    for doc in _readme_and_guide_paths():
        for match in LINK_RE.finditer(doc.read_text(encoding="utf-8")):
            target = match.group(1).strip()
            resolved = _resolve_link(target, doc)
            if resolved is not None and not resolved.exists():
                broken.append(f"{doc.relative_to(ROOT)}: {target} (resolved to {resolved})")
    assert not broken, f"README.md or a guide contains broken relative links: {broken}"


def test_readme_relative_links_resolve_in_exported_tree() -> None:
    """DS-P2.117: Every relative link in README.md must point to a file that survives the export gate (not in DROP_LIST)."""
    import importlib.util
    hygiene_path = ROOT / "tools" / "public_artifact_hygiene.py"
    spec = importlib.util.spec_from_file_location("public_artifact_hygiene", hygiene_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    dropped_links = []
    for doc in _readme_and_guide_paths():   # the guides carry the README's moved links (T1 rv-6825d4f6e9)
        for match in LINK_RE.finditer(doc.read_text(encoding="utf-8")):
            target = match.group(1).strip()
            resolved = _resolve_link(target, doc)
            if resolved is not None and resolved.exists():
                rel_path = resolved.relative_to(ROOT).as_posix()
                matched = mod._matches_drop_list(rel_path)
                if matched:
                    dropped_links.append(f"{doc.relative_to(ROOT)}: {target} matches DROP_LIST item '{matched}'")
    assert not dropped_links, f"README.md or a guide links to files excluded by export DROP_LIST:\n{dropped_links}"


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


def _rendered_code(text: str) -> list[str]:
    """The text of every code block a reader sees, as CommonMark renders it: fences (backtick or tilde, also
    inside block quotes and lists, also left open at the end of a file), and indented code blocks."""
    out = []
    for tok in MarkdownIt("commonmark").parse(text):
        if tok.type in ("fence", "code_block"):
            out.append(tok.content)
    return out


def _fenced_rr_commands(text: str) -> set[tuple[str, str]]:
    """Each rr verb and subcommand shown in a fenced code block, as CommonMark parses the fences (a fence left
    open at the end of a file still renders, and still counts)."""
    found = set()
    # The CommonMark code blocks and main's own fence reading together, so every block main read is still read
    # (T1 rv-a0a3114da9).
    for block in _rendered_code(text) + re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S):
        for line in [ln.split("#", 1)[0] for ln in block.splitlines()]:   # one command per line, as main reads it
            match = re.match(r"\s*(?:[A-Z_]+=\S+\s+)*rr\s+([a-z][\w-]*)(?:\s+([a-z][\w-]*))?", line)
            if match:
                found.add((match.group(1), match.group(2) or ""))
    return found


def test_an_unclosed_last_fence_is_still_read_for_rr_commands():
    """Control (T1 rv-e9a8685cd5): a command in a final fence with no closing marker renders, so it is read."""
    assert ("no-such-command", "check") in _fenced_rr_commands("# Guide\n\n```bash\nrr no-such-command check\n")
    # Indented code renders as copyable code too (T1 rv-f2fc046c84).
    assert ("no-such-command", "check") in _fenced_rr_commands("# Guide\n\n    rr no-such-command check\n")
    # A comment ending in a backslash does not swallow the next command (T1 rv-a00ad460bc).
    assert ("no-such-command", "check") in _fenced_rr_commands(
        "# Guide\n\n```bash\necho a  # note \\\nrr no-such-command check\n```\n")
    # T1 rv-bbbf510158: a quoted `&` inside an environment assignment is not a command boundary.
    assert ("no-such-command", "check") in _fenced_rr_commands(
        "# Guide\n\n```bash\nDEMO_URL='https://example.com/?a=b&c=d' rr no-such-command check\n```\n")
    # T1 rv-045fb119b4: an escaped `\\&` is not a command boundary either.
    assert ("no-such-command", "check") in _fenced_rr_commands("# Guide\n\n```bash\nDEMO=a\\&b rr no-such-command check\n```\n")
    # T1 rv-4e5f8b78e3: a ';' inside a command substitution is not a boundary that hides the command.
    assert ("no-such-command", "check") in _fenced_rr_commands(
        "# Guide\n\n```bash\nDEMO=$(true;true) rr no-such-command check\n```\n")


def test_every_rr_command_in_the_public_docs_exists():
    """CONTRIBUTING once required a command rr does not have."""
    commands = set()
    for path in (ROOT / "README.md", ROOT / "CONTRIBUTING.md", *sorted((p for p in GUIDES_DIR.rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".markdown")))):
        commands |= _fenced_rr_commands(path.read_text(encoding="utf-8"))
    assert ("linux", "build") in commands
    missing = []
    for verb, sub in sorted(commands):
        argv = ["rr", verb, *([sub] if sub else []), "--help"]
        proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            missing.append(" ".join(argv))
    assert missing == []
