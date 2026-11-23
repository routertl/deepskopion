"""DS-C1: public artifacts carry neither bench identity nor access state.

The failure fixtures are assembled from fragments so the tracked test module
is itself clean.  Each class of the original defect is seeded in a temporary
artifact and must make the production scanner fail.
"""
from __future__ import annotations

import gzip
import importlib.util
import io
import os
import site
from pathlib import Path
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "public_artifact_hygiene.py"
SPEC = importlib.util.spec_from_file_location("public_artifact_hygiene", TOOL)
assert SPEC and SPEC.loader
H = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(H)


def _run(path: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["python3", str(TOOL), "--no-index", str(path)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.mark.parametrize(("kind", "seed"), [
    ("developer home", "/ho" + "me/build-user/project"),
    ("developer removable-media home", "/med" + "ia/build-user/card/image"),
    ("bench cable serial", "21" + "F" * 10),
    ("private-subnet assumption", ".".join(str(n) for n in (192, 168, 45, 7))),
    ("host TFTP root", "/sr" + "v/tftp"),
    ("private key", "-----BEGIN OPENSSH " + "PRIVATE KEY-----"),
    ("preinstalled SSH authorized key",
     "ssh-" + "ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureOnlyNotARealKey1234 user@example"),
])
def test_each_seeded_forbidden_fixture_fails(tmp_path, kind, seed):
    artifact = tmp_path / "captured-transcript.txt"
    artifact.write_text(f"before\n{seed}\nafter\n", encoding="utf-8")
    rc, output = _run(artifact)
    assert rc == 1
    assert kind in output
    assert str(artifact) in output


def test_clean_generated_rtl_provenance_and_transcript_pass(tmp_path):
    for name, body in {
        "generated.vhd": "constant C_BUILD_ROOT : string := \"project-relative\";\n",
        "provenance.json": '{"builder":"release","artifact":"design.bit"}\n',
        "console.log": "boot complete; DHCP lease acquired\n",
    }.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    rc, output = _run(tmp_path)
    assert rc == 0, output


def test_only_exact_upstream_runtime_reference_is_not_identity(tmp_path):
    runtime = tmp_path / "rootfs/bin/busybox"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"upstream example " + ".".join(str(n) for n in (192, 168, 0, 20)).encode())
    rc, output = _run(tmp_path / "rootfs")
    assert rc == 0, output

    runtime.write_bytes(b"deployed topology " + ".".join(str(n) for n in (192, 168, 0, 21)).encode())
    rc, output = _run(tmp_path / "rootfs")
    assert rc == 1
    assert "private-subnet assumption" in output


def test_linux_image_archive_is_opened_not_only_string_scanned(tmp_path):
    key = ("ssh-" + "ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureOnlyNotARealKey1234 "
           "user@example\n").encode()
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as archive:
        info = tarfile.TarInfo("root/.ssh/authorized_keys")
        info.size = len(key)
        archive.addfile(info, io.BytesIO(key))
    image = tmp_path / "rootfs.tar.gz"
    image.write_bytes(gzip.compress(tar_bytes.getvalue()))

    # The key is compressed and absent from the outer bytes.  Passing proves
    # nothing unless the scanner actually expands the Linux image.
    assert key not in image.read_bytes()
    rc, output = _run(image)
    assert rc == 1
    assert "preinstalled SSH authorized key" in output
    assert "root/.ssh/authorized_keys" in output


def test_missing_artifact_is_not_a_clean_scan(tmp_path):
    rc, output = _run(tmp_path / "missing.bit")
    assert rc == 1
    assert "SCAN INCOMPLETE" in output


def test_the_committed_index_is_clean():
    findings, errors = H.scan([], include_index=True, root=ROOT)
    assert not errors, errors
    assert not findings, "\n".join(findings)


def test_the_scan_is_armed_in_the_commit_gate():
    hook = (ROOT / "hooks" / "pre-commit.local").read_text(encoding="utf-8")
    assert "tools/public_artifact_hygiene.py" in hook
    assert "DS-C1" in hook


# ---------------------------------------------------------------------------
# DS-P1.45: the DE-G0 evidence carve-out must not blunt the cable-serial rule
# ---------------------------------------------------------------------------


def test_an_allowed_timestamp_in_ANOTHER_file_still_fails():
    """MUTATION, the other axis: the same literal elsewhere is still a finding.
    The carve-out is keyed on file AND value, so it cannot travel."""
    value = next(iter(H._EVIDENCE_TIMESTAMPS[
        "2026-08-30_ds_p1_45_deg0_hw_leg/cap_uart.live.json"]))
    findings = H.inspect_bytes("some/other/file.json", b'{"x": ' + value + b'}')
    assert any("bench cable serial" in f for f in findings), (
        f"{value!r} was allowed outside the file it was carved out for")


# ---------------------------------------------------------------------------
# DS-P1.45: the same carve-out, for the UNATTENDED run's edge capture
# ---------------------------------------------------------------------------
_P145_KEY = "2026-08-30_ds_p1_45_deg0_p145_hw_leg/cap_edge.live.json"


def test_a_p145_allowed_timestamp_in_ANOTHER_file_still_fails():
    """MUTATION, the other axis: the literal cannot travel to another file."""
    value = next(iter(H._EVIDENCE_TIMESTAMPS[_P145_KEY]))
    findings = H.inspect_bytes("some/other/file.json", b'{"x": ' + value + b'}')
    assert any("bench cable serial" in f for f in findings), (
        f"{value!r} was allowed outside the file it was carved out for")


# ---------------------------------------------------------------------------
# DS-P2.113: GitHub-export gate rules & drop list
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restricted_names_fixture(tmp_path_factory, monkeypatch):
    """A names file for tests that are not about finding one: a public clone has
    none, and the gate correctly refuses to run without it."""
    names = tmp_path_factory.mktemp("names") / "names.yml"
    names.write_text("- name: FixtureOnly" + "ClientName\n", encoding="utf-8")
    monkeypatch.setenv("RESTRICTED_NAMES_FILE", str(names))


def _run_export(path: Path, *extra_args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["python3", str(TOOL), "--export-gate", str(path), *extra_args],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_clean_export_fixture_tree_passes(tmp_path):
    tree = tmp_path / "clean_export"
    tree.mkdir()
    # Code files with ticket IDs are permitted (559 files carry them; stripping breaks tests)
    vhdl = tree / "hw" / "rtl" / "core.vhd"
    vhdl.parent.mkdir(parents=True)
    vhdl.write_text(
        "-- DS-P2.113: public core design\n"
        "library ieee;\n"
        "use ieee.std_logic_1164.all;\n"
        "entity core is end entity;\n",
        encoding="utf-8",
    )
    py = tree / "sw" / "driver.py"
    py.parent.mkdir(parents=True)
    py.write_text(
        "# DS-T1.13: write-plane arming contract\n"
        "def run_driver():\n"
        "    return 42\n",
        encoding="utf-8",
    )
    # Docs with clean narrative (no ticket IDs or ticket narrative)
    doc = tree / "docs" / "guide.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "# Public User Guide\n\n"
        "This guide describes the deepskopion capture system and its protocols.\n",
        encoding="utf-8",
    )
    rc, output = _run_export(tree)
    assert rc == 0, output
    assert "PUBLIC EXPORT GATE PASS" in output


@pytest.mark.parametrize(("kind", "rel_path", "seed", "expected_marker"), [
    # Agent names
    ("agent name " + "cl" + "aude", "docs/guide.md", "Authored by " + "cl" + "aude 3.5 Sonnet", "agent identity reference"),
    ("agent name " + "co" + "dex", "sw/driver.py", "Reviewed by " + "co" + "dex lane", "agent identity reference"),
    ("agent name " + "a" + "gy", "sw/driver.py", "Generated in " + "a" + "gy session", "agent identity reference"),
    ("agent name " + "fa" + "ble", "docs/guide.md", "Audited by " + "fa" + "ble 5.1", "agent identity reference"),
    ("agent name " + "op" + "us", "hw/rtl/core.vhd", "Signed off by " + "op" + "us-4-8", "agent identity reference"),
    ("agent name " + "gem" + "ini", "sw/driver.py", "Run with " + "gem" + "ini 3.8", "agent identity reference"),
    # Absolute home paths
    ("home path /home", "docs/guide.md", "Path: /ho" + "me/build-user/deepskopion", "developer home"),
    ("home path /Users", "sw/driver.py", "Path: /Use" + "rs/builder/project", "developer home"),
    # Internal tich references
    ("ti" + "ch-super reference", "docs/guide.md", "See " + "ti" + "ch-super repository", "internal " + "ti" + "ch reference"),
    ("ti" + "ch-pm reference", "sw/driver.py", "Depends on " + "ti" + "ch-pm package", "internal " + "ti" + "ch reference"),
    ("tl" + "og reference", "sw/driver.py", "Log with python tools/" + "tl" + "og.py add", "internal " + "ti" + "ch reference"),
    # Retired sn + iffer reference
    ("sn" + "iffer reference in code", "hw/rtl/core.vhd", "entity uart_" + "sn" + "iffer is", "retired " + "sn" + "iffer reference"),
    ("sn" + "iffer reference in docs", "docs/guide.md", "The " + "sn" + "iffer captures traffic", "retired " + "sn" + "iffer reference"),
    # Dropped artifacts
    ("drop list " + "docs/" + "in" + "ternal/", "docs/" + "in" + "ternal/conv/meeting.md", "# Meeting notes", "dropped artifact in export"),
    ("drop list ." + "ag" + "ent/", "." + "ag" + "ent/rules/sim.md", "Rule text", "dropped artifact in export"),
    ("drop list screencast", "demo/config1/screencast.md", "# Screencast script", "dropped artifact in export"),
    ("drop list private terms list", H.PRIVATE_TERMS_REL, "term\n", "dropped artifact in export"),
    ("drop list stage_tftp_deploy", "sw/linux/stage_tftp_deploy.sh", "#!/bin/sh\nexit 0\n", "dropped artifact in export"),
    ("drop list DE25_BOARD_FACTS", "docs/hardware/DE25_BOARD_FACTS.md", "# Board facts", "dropped artifact in export"),
    ("drop list libs/ctu-fee/", "libs/ctu-fee/ctucanfd_ip_core/src/dummy.vhd", "-- dummy\n", "dropped artifact in export"),
    # Ticket narrative in docs
    ("ticket narrative in docs (DS-P)", "docs/guide.md", "Fixed in DS-P2.113 narrative", "ticket narrative in docs"),
    ("ticket narrative in docs (DS-T)", "docs/guide.md", "Symptom of DS-T2.24 was wedged", "ticket narrative in docs"),
    ("ticket narrative in docs (DS-D)", "docs/notes.md", "Owner decision DS-D.7 amended", "ticket narrative in docs"),
    ("ticket narrative in docs (Haz)", "docs/notes.md", "Respect Haz.56 loop", "ticket narrative in docs"),
    ("ticket narrative in docs (RTL-P)", "README.md", "Follows RTL-P3.937 contract", "ticket narrative in docs"),
    # Internal paths
    ("internal sibling escape", "sw/driver.py", "Escape ../" + "route" + "rtl/build", "internal sibling escape"),
    ("internal bench path", "docs/guide.md", "Checkout " + "git_" + "repo/" + "ti" + "ch-super/deepskopion", "internal bench workspace path"),
    ("internal admin reference", "sw/driver.py", "Config in " + "admin/" + "restricted_" + "names.yml", "internal admin reference"),
])
def test_export_fixture_tree_seeded_with_each_violation_is_refused(tmp_path, kind, rel_path, seed, expected_marker):
    tree = tmp_path / "violation_tree"
    tree.mkdir()
    target_file = tree / rel_path
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(f"prefix text\n{seed}\nsuffix text\n", encoding="utf-8")
    rc, output = _run_export(tree)
    assert rc == 1, f"Expected refusal for {kind}, got rc={rc}\nOutput:\n{output}"
    assert "PUBLIC EXPORT GATE FAIL" in output
    assert expected_marker in output


def test_restricted_client_name_via_admin_restricted_names_pattern_refused(tmp_path):
    tree = tmp_path / "export_tree"
    tree.mkdir()
    doc = tree / "docs" / "guide.md"
    doc.parent.mkdir(parents=True)
    restricted_word = "FakeRestrictedCorp"
    doc.write_text(f"Customer deployment for {restricted_word}.\n", encoding="utf-8")
    names_file = tmp_path / "admin" / ("restricted_" + "names.yml")
    names_file.parent.mkdir(parents=True)
    names_file.write_text(f"- name: {restricted_word}\n  codename: CodenameCorp\n", encoding="utf-8")
    rc, output = _run_export(tree, "--restricted-names", str(names_file))
    assert rc == 1, output
    assert "restricted client name" in output
    assert restricted_word not in output
    assert restricted_word[0] + "*" in output


def test_ticket_ids_in_code_stay_and_are_permitted(tmp_path):
    tree = tmp_path / "code_tree"
    tree.mkdir()
    vhdl = tree / "hw" / "rtl" / "core.vhd"
    vhdl.parent.mkdir(parents=True)
    vhdl.write_text("-- DS-P2.113: ticket id in VHDL\n-- RTL-P3.937: upstream ref\n", encoding="utf-8")
    py = tree / "sw" / "driver.py"
    py.parent.mkdir(parents=True)
    py.write_text("# DS-T1.13: ticket id in Python\n# DS-D.41: decision ref\n", encoding="utf-8")
    rc, output = _run_export(tree)
    assert rc == 0, output
    assert "PUBLIC EXPORT GATE PASS" in output


def test_export_to_drops_drop_list_files_and_gates_cleanly(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    # Clean files
    (source / "hw" / "rtl").mkdir(parents=True)
    (source / "hw" / "rtl" / "core.vhd").write_text("-- DS-P2.113: core\n", encoding="utf-8")
    (source / "docs").mkdir()
    (source / "docs" / "guide.md").write_text("# Guide\nClean public docs\n", encoding="utf-8")
    # Dropped files
    (source / "docs" / "internal" / "conv").mkdir(parents=True)
    (source / "docs" / "internal" / "conv" / "secret.md").write_text("internal", encoding="utf-8")
    (source / ".agent" / "rules").mkdir(parents=True)
    (source / ".agent" / "rules" / "r.md").write_text("agent rule", encoding="utf-8")
    (source / "demo" / "config1").mkdir(parents=True)
    (source / "demo" / "config1" / "screencast.md").write_text("screencast", encoding="utf-8")
    (source / "sw" / "linux").mkdir(parents=True)
    (source / "sw" / "linux" / "stage_tftp_deploy.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (source / "docs" / "hardware").mkdir(parents=True)
    (source / "docs" / "hardware" / "DE25_BOARD_FACTS.md").write_text("# Facts\n", encoding="utf-8")
    (source / "libs" / "ctu-fee").mkdir(parents=True)
    (source / "libs" / "ctu-fee" / "dummy.vhd").write_text("-- dummy\n", encoding="utf-8")

    dest = tmp_path / "exported"
    proc = subprocess.run(
        ["python3", str(TOOL), "--export-to", str(dest), str(source)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PUBLIC EXPORT GATE PASS" in proc.stdout
    assert (dest / "hw" / "rtl" / "core.vhd").is_file()
    assert (dest / "docs" / "guide.md").is_file()
    assert not (dest / "docs" / "internal").exists()
    assert not (dest / ".agent").exists()
    assert not (dest / "demo" / "config1" / "screencast.md").exists()
    # DS-P1.123: the public drop list names no private term (the documents it would name are in the dropped tree).
    assert not [e for e in H.DROP_LIST if any(t in e.lower() for t in H.load_private_terms())]
    assert not (dest / "sw" / "linux" / "stage_tftp_deploy.sh").exists()
    assert not (dest / "docs" / "hardware" / "DE25_BOARD_FACTS.md").exists()
    assert not (dest / "libs" / "ctu-fee").exists()


def test_export_gate_fails_closed_when_restricted_names_file_absent(tmp_path):
    tree = tmp_path / "export_tree"
    tree.mkdir()
    doc = tree / "docs" / "guide.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("Public guide content.\n", encoding="utf-8")

    # 1. Explicit missing file via --restricted-names
    dest1 = tmp_path / "should_not_exist_1"
    missing_file = tmp_path / "nonexistent" / "admin" / ("restricted_" + "names.yml")
    proc1 = subprocess.run(
        ["python3", str(TOOL), "--export-to", str(dest1), "--restricted-names", str(missing_file), str(tree)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert proc1.returncode != 0, proc1.stdout + proc1.stderr
    assert "restricted names file not found" in (proc1.stdout + proc1.stderr)
    assert str(missing_file) in (proc1.stdout + proc1.stderr)
    assert not dest1.exists()


def test_export_gate_refuses_offbench_host_without_registry(tmp_path):
    """Auto-discovery fails closed off-bench.  Production discovery walks every
    ancestor of the tool's own checkout (load_restricted_names), so a checkout
    nested in an umbrella that carries the registry legitimately finds it and
    exports (DS-P2.140).  The off-bench host is therefore a copy of the tool
    with no registry reachable from HOME, the environment, or its ancestors;
    the umbrella here is synthetic so the case does not depend on where the
    suite happens to be checked out."""
    tree = tmp_path / "export_tree"
    doc = tree / "docs" / "guide.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("Public guide content.\n", encoding="utf-8")

    nested_tool, _env = _umbrella_tool(tmp_path / "umbrella")
    dest = tmp_path / "should_not_exist"
    offbench_tool, offbench_env = _offbench_tool(nested_tool, tmp_path / "offbench")
    assert _reachable_restricted_names(offbench_tool, offbench_env) == [], (
        "off-bench fixture must not reach any restricted-names registry"
    )
    proc = subprocess.run(
        ["python3", str(offbench_tool), "--export-to", str(dest), str(tree)],
        cwd=offbench_tool.parents[1], capture_output=True, text=True,
        check=False, env=offbench_env,
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "restricted names file not found" in (proc.stdout + proc.stderr)
    assert ("admin/" + "restricted_" + "names.yml") in (proc.stdout + proc.stderr)
    assert not dest.exists()


def _scrubbed_env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # A new HOME moves the user site-packages too; keep the interpreter's
    # packages (markdown-it-py, the routertl editable install) where they are.
    # Only the restricted-names discovery is meant to see the scrubbed HOME.
    env.setdefault("PYTHONUSERBASE", site.getuserbase())
    env["HOME"] = str(home)
    env.pop("RESTRICTED_NAMES_FILE", None)
    env.pop("TI" + "CH_SUPER", None)
    return env


def _umbrella_tool(umbrella: Path) -> tuple[Path, dict[str, str]]:
    """A copy of the gate in a checkout nested in an umbrella that carries a
    restricted-names registry, as the bench layout does."""
    tool = umbrella / "checkout" / "tools" / TOOL.name
    tool.parent.mkdir(parents=True)
    tool.write_bytes(TOOL.read_bytes())
    registry = umbrella / "admin" / ("restricted_" + "names.yml")
    registry.parent.mkdir()
    client_name = bytes([67, 108, 105, 101, 110, 116, 81]).decode()
    registry.write_text(f"- name: {client_name}\n", encoding="utf-8")
    return tool, _scrubbed_env(umbrella / "home")


def _offbench_tool(source: Path, host: Path) -> tuple[Path, dict[str, str]]:
    """The gate as an off-bench host runs it: a copy in ``host`` with HOME and
    the environment scrubbed, so no registry is reachable from its ancestors."""
    tool = host / "checkout" / "tools" / source.name
    tool.parent.mkdir(parents=True)
    tool.write_bytes(source.read_bytes())
    return tool, _scrubbed_env(host / "home")


def _reachable_restricted_names(tool: Path, env: dict[str, str]) -> list[Path]:
    """Every registry the gate's own discovery would find."""
    rel = Path("admin") / ("restricted_" + "names.yml")
    checkout = tool.resolve().parents[1]
    home = Path(env["HOME"])
    cands = [checkout / rel, home / ("git_" + "repo") / ("ti" + "ch-super") / rel]
    for parent in checkout.parents:
        cands += [parent / rel, parent / ("ti" + "ch-super") / rel]
    return [c for c in cands if c.is_file()]


def test_offbench_fixture_reaches_no_registry(tmp_path):
    nested_tool, _env = _umbrella_tool(tmp_path / "umbrella")
    offbench_tool, offbench_env = _offbench_tool(nested_tool, tmp_path / "offbench")
    assert _reachable_restricted_names(offbench_tool, offbench_env) == []


def test_export_gate_discovers_restricted_names_in_checkout_ancestor(tmp_path):
    """Positive control for the off-bench fixture: the same copied gate, nested
    in an umbrella that carries the registry, finds it through the ancestor
    walk and gates the export on its names.  This is why the off-bench case
    must not run the gate from inside the umbrella."""
    tool, env = _umbrella_tool(tmp_path / "umbrella")
    registry = tmp_path / "umbrella" / "admin" / ("restricted_" + "names.yml")
    assert _reachable_restricted_names(tool, env) == [registry]
    client_name = bytes([67, 108, 105, 101, 110, 116, 81]).decode()

    tree = tmp_path / "export_tree"
    (tree / "docs").mkdir(parents=True)
    (tree / "docs" / "guide.md").write_text("Public guide content.\n", encoding="utf-8")
    clean_dest = tmp_path / "clean_export"
    clean = subprocess.run(
        ["python3", str(tool), "--export-to", str(clean_dest), str(tree)],
        cwd=tool.parents[1], capture_output=True, text=True, check=False, env=env,
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert (clean_dest / "docs" / "guide.md").is_file()

    (tree / "docs" / "client.md").write_text(f"Built for {client_name}.\n", encoding="utf-8")
    leaky_dest = tmp_path / "leaky_export"
    leaky = subprocess.run(
        ["python3", str(tool), "--export-to", str(leaky_dest), str(tree)],
        cwd=tool.parents[1], capture_output=True, text=True, check=False, env=env,
    )
    assert leaky.returncode != 0, leaky.stdout + leaky.stderr
    assert "client.md" in (leaky.stdout + leaky.stderr)


def test_export_subdirectory_honours_drop_list(tmp_path):
    tree = tmp_path / "repo"
    tree.mkdir()
    docs = tree / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Public Guide\nClean public docs\n", encoding="utf-8")
    internal_conv = docs / ("in" + "ternal") / "conv"
    internal_conv.mkdir(parents=True)
    (internal_conv / "meeting.md").write_text("# Meeting notes\nSecret internal.\n", encoding="utf-8")

    names_file = tree / "admin" / ("restricted_" + "names.yml")
    names_file.parent.mkdir(parents=True)
    client_name = bytes([67, 108, 105, 101, 110, 116, 65]).decode()
    names_file.write_text(f"- name: {client_name}\n", encoding="utf-8")

    dest = tmp_path / "exported_docs"
    proc = subprocess.run(
        ["python3", str(TOOL), "--export-to", str(dest), "--root", str(tree),
         "--restricted-names", str(names_file), str(docs)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PUBLIC EXPORT GATE PASS" in proc.stdout
    assert (dest / "guide.md").is_file()
    assert not (dest / "internal").exists()
    assert not (dest / "meeting.md").exists()


def test_the_export_gate_is_armed_in_export_target_and_documented():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "tools/public_artifact_hygiene.py" in makefile
    assert "--export-gate" in makefile
    assert "--export-to" in makefile
    assert "export-gate:" in makefile
    assert "export:" in makefile

    hook = (ROOT / "hooks" / "pre-commit.local").read_text(encoding="utf-8")
    assert "tools/export_github.py --staged" in hook

    # The golden path document is private; the public export drops it.
    golden_path_file = ROOT / "docs" / "GOLDEN_PATH.md"
    if golden_path_file.is_file():
        golden_path = golden_path_file.read_text(encoding="utf-8")
        assert "export-gate" in golden_path
        assert "export-to" in golden_path


def test_export_gate_passes_on_hygiene_module_and_its_tests(tmp_path):
    names_file = tmp_path / "admin" / ("restricted_" + "names.yml")
    names_file.parent.mkdir(parents=True)
    client_name = bytes([67, 108, 105, 101, 110, 116, 66]).decode()
    names_file.write_text(f"- name: {client_name}\n", encoding="utf-8")

    rc, output = _run_export(TOOL, "--restricted-names", str(names_file))
    assert rc == 0, f"tools/public_artifact_hygiene.py must pass export gate:\n{output}"
    rc, output = _run_export(Path(__file__), "--restricted-names", str(names_file))
    assert rc == 0, f"tools/tests/test_public_artifact_hygiene.py must pass export gate:\n{output}"


def test_libs_ctu_fee_in_drop_list_and_dropped_on_export(tmp_path):
    """DS-P2.121 (DS-P2.116): libs/ctu-fee is in DROP_LIST and dropped during export."""
    assert "libs/ctu-fee/" in H.DROP_LIST
    source = tmp_path / "src"
    source.mkdir()
    (source / "hw" / "rtl").mkdir(parents=True)
    (source / "hw" / "rtl" / "core.vhd").write_text("-- core\n", encoding="utf-8")
    (source / "libs" / "ctu-fee" / "ctucanfd_ip_core").mkdir(parents=True)
    (source / "libs" / "ctu-fee" / "ctucanfd_ip_core" / "can_core.vhd").write_text("-- ctu\n", encoding="utf-8")

    dest = tmp_path / "exported"
    proc = subprocess.run(
        ["python3", str(TOOL), "--export-to", str(dest), str(source)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (dest / "hw" / "rtl" / "core.vhd").is_file()
    assert not (dest / "libs" / "ctu-fee").exists()


def test_upstream_vendored_references_allowed_in_exact_files_only():
    """DS-P2.130: Upstream vendored references in _UPSTREAM_REFERENCES are permitted
    ONLY in their exact designated paths, and trigger violations anywhere else."""
    internal_docs_ref = b"docs/" + b"in" + b"ternal/"
    client_name = bytes([115, 111, 110, 111, 118, 97]).decode()
    sn_const = bytes([83, 78, 73, 70, 70, 69, 82])
    sn_comment = bytes([115, 110, 105, 102, 102, 101, 114])

    # 1. ddr_stream_pkg.vhd: allowed in exact file, refused elsewhere
    pkg_path = "libs/routertl/rr_ddr_stream/rtl/ddr_stream_pkg.vhd"
    pkg_content = sn_const + b"\n" + sn_comment + b"\n"
    assert H.inspect_bytes(pkg_path, pkg_content, export_gate=True) == []
    other_pkg = "hw/rtl/other_pkg.vhd"
    findings_pkg = H.inspect_bytes(other_pkg, pkg_content, export_gate=True)
    assert len(findings_pkg) == 2, findings_pkg
    assert all("retired" in f for f in findings_pkg)

    # 2. axi4w.vhd: allowed in exact file, refused elsewhere
    axi_path = "libs/routertl/rr_ddr_stream/rtl/axi4w.vhd"
    axi_content = f"-- client: {client_name}\n".encode()
    assert H.inspect_bytes(axi_path, axi_content, export_gate=True, restricted_names=[client_name]) == []
    other_axi = "hw/rtl/axi.vhd"
    findings_axi = H.inspect_bytes(other_axi, axi_content, export_gate=True, restricted_names=[client_name])
    assert len(findings_axi) == 1, findings_axi
    assert "restricted client name" in findings_axi[0]

    # 3. ddr_stream_axi.vhd: allowed in exact file, refused elsewhere
    axi_rtl_path = "libs/routertl/rr_ddr_stream/rtl/ddr_stream_axi.vhd"
    axi_rtl_content = b"-- see " + internal_docs_ref + b"\n"
    assert H.inspect_bytes(axi_rtl_path, axi_rtl_content, export_gate=True) == []
    other_vhd = "hw/rtl/other.vhd"
    findings_vhd = H.inspect_bytes(other_vhd, axi_rtl_content, export_gate=True)
    assert len(findings_vhd) == 1, findings_vhd
    assert "internal docs reference" in findings_vhd[0]

    # 4. ddr_stream_cdc_scoped.sdc: allowed in exact file, refused elsewhere
    sdc_path = "libs/routertl/rr_ddr_stream/constraints/ddr_stream_cdc_scoped.sdc"
    sdc_content = b"# see " + internal_docs_ref + b"\n"
    assert H.inspect_bytes(sdc_path, sdc_content, export_gate=True) == []
    other_sdc = "constraints/other.sdc"
    findings_sdc = H.inspect_bytes(other_sdc, sdc_content, export_gate=True)
    assert len(findings_sdc) == 1, findings_sdc
    assert "internal docs reference" in findings_sdc[0]


def test_path_blobs_ignores_git_metadata_directory(tmp_path):
    """DS-P2.117: _path_blobs must not scan .git directories.

    Git object stores and packfiles contain compressed binary data that can
    randomly match token or agent regexes (false positives) and are not public artifacts.
    """
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    (repo / "clean.txt").write_text("clean public text\n", encoding="utf-8")
    git_dir = repo / ".git" / "objects" / "5c"
    git_dir.mkdir(parents=True)
    (git_dir / "fake_obj").write_bytes(b"binary \x00 " + b"AG" + b"Y \xff data")

    blobs = H._path_blobs(repo)
    blob_names = [name for name, _ in blobs]
    assert any("clean.txt" in b for b in blob_names)
    assert not any(".git" in b for b in blob_names)

    findings, errors = H.scan([repo], include_index=False, export_gate=True)
    assert not findings
    assert not errors
