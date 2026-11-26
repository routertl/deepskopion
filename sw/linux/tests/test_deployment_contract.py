# rr unknown-llm (AI-generated)
"""SW-C0 (DS-P1.46) — the deployment contract, and proof its check can go RED.

The defect this guards is not "a doc got out of date". It is that the repo
described its own boot behaviour in three places that disagreed — a tracked
init script in `rootfs_overlay/`, two systemd units in `docs/` labelled
"examples", and a design decision (DS-D.7) saying both were staged — so
"how does the capture daemon start on the board?" had no answer a reader could
trust. The version of that defect which actually shipped: the units really were
in the overlay, `rr linux rootfs` copied one into every image, and the rootfs
has no systemd in it (DS-P2.82).

A check that only ever runs against a clean tree proves nothing, so every
failure mode below is CONSTRUCTED in a throwaway copy of the repo and the
checker must reject it, naming the file. The two that matter most:

* `test_a_stowaway_systemd_unit_in_the_overlay_fails` plants exactly the file
  that shipped for months, and
* `test_a_doc_that_disagrees_with_the_overlay_fails` makes the document and the
  declaration disagree about which autostart files ship, which is the failure
  the ticket requires be demonstrated rather than assumed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_LINUX_DIR = Path(__file__).resolve().parents[1]
_ROOT = _LINUX_DIR.parents[1]
sys.path.insert(0, str(_LINUX_DIR))
import deployment_contract as C                            # noqa: E402

_DOC = _ROOT / "docs" / "DEPLOYMENT_CONTRACT.md"
_OVERLAY = _LINUX_DIR / "rootfs_overlay"


@pytest.fixture(scope="module")
def decl():
    return C.load_declaration()


# ---------------------------------------------------------------------------
# 1. The repo, as it stands, satisfies its own contract
# ---------------------------------------------------------------------------
def test_the_repo_matches_the_contract(decl):
    fails = C.check_all(decl, _ROOT)
    assert not fails, "deployment contract violated:\n  " + "\n  ".join(
        str(f) for f in fails
    )


def test_every_getting_started_guide_is_bound(decl):
    """The README's board and Linux instructions live in docs/getting_started/ (DS-P2.397). Every page there hands a
    reader commands, so each is bound and this check, which the commit hook runs, scans it; a guide added in a
    subdirectory or with an upper-case extension counts too."""
    guides = sorted(p.relative_to(_ROOT).as_posix() for p in (_ROOT / "docs" / "getting_started").rglob("*")
                    if p.is_file() and p.suffix.lower() in (".md", ".markdown"))
    assert guides, "no guide under docs/getting_started/"
    assert sorted(set(guides) - set(decl["docs_bound_by_this_contract"])) == []


def test_the_check_is_wired_into_the_one_script_every_linux_target_runs():
    """A check nobody invokes is a document with extra steps.

    `stage_daemon.sh` is `linux.rootfs.stage_command` for every Linux target
    (asserted by sw/stream/tests/test_overlay_in_sync.py), and `rr linux
    rootfs` aborts if it fails (RTL-P1.117) — so this one line is what makes
    the contract gate the IMAGE rather than only the test suite.
    """
    stage = (_LINUX_DIR / "stage_daemon.sh").read_text(encoding="utf-8")
    assert "deployment_contract.py" in stage, (
        "stage_daemon.sh no longer runs the deployment-contract check, so an "
        "overlay that disagrees with docs/DEPLOYMENT_CONTRACT.md would be "
        "baked into an image with nothing objecting"
    )


# ---------------------------------------------------------------------------
# 2. The declaration says the things the ticket requires it to say
# ---------------------------------------------------------------------------
def test_the_declaration_answers_every_question_the_ticket_asks(decl):
    """Delete any one of these and every other test still passes, because
    there is nothing left to check. So they are asserted explicitly."""
    assert decl["boot"]["init_system"], "which autostart mechanism is authoritative"
    assert decl["boot"]["targets"], "…for which targets"
    assert decl["daemon"]["source"], "where the daemon source lives"
    assert decl["daemon"]["staged_by"], "how it reaches the rootfs"
    assert decl["daemon"]["provenance"] == "generated", "what is generated"
    assert decl["provenance_rule"], "how a reader tells staged from source"
    assert all(e.get("provenance") for e in decl["autostart"]), \
        "every autostart file must say whether it is tracked or staged"


def test_there_is_exactly_one_autostart_mechanism(decl):
    """The whole point of SW-C0. Two mechanisms is the state we came from."""
    assert decl["boot"]["init_system"] == "busybox"
    assert {m["mechanism"] for m in decl["prohibited"]} >= {"systemd", "petalinux"}


def test_the_daemon_is_declared_as_starting_from_the_init_script(decl):
    """"How does the capture daemon start on the board?" must be answerable
    from the declaration alone, without reading a shell script or git log."""
    starters = [e for e in decl["autostart"] if "sw.stream" in e["starts"]]
    assert len(starters) == 1, (
        "exactly one declared file may start the capture daemon; found "
        f"{[e['path'] for e in starters]}"
    )
    assert starters[0]["path"] == "etc/init.d/S99deepskopion-stream"


def test_the_petalinux_justification_is_resolved_in_the_tree(decl):
    """The systemd variant's only stated reason was a forbidden flow, so the
    variant is gone — files and all. A relabelled example is not a resolution:
    it is the same two answers with a nicer sign on one of them."""
    assert not (_ROOT / "docs" / "software" / "systemd-examples").exists(), (
        "the systemd 'examples' are back. Either they ship (declare them in "
        "deployment_contract.yml and stage them in the overlay) or they do "
        "not exist — a third state is what SW-C0 removed."
    )
    petalinux = next(m for m in decl["prohibited"] if m["mechanism"] == "petalinux")
    assert "python_overlay" in petalinux["reason"], (
        "the PetaLinux entry must say what replaces it, or the next reader "
        "reinvents the workaround it was there to justify"
    )


# ---------------------------------------------------------------------------
# 3. The document is discoverable, and it is the only answer
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("index", [
    "README.md",
    "docs/README.md",
    "sw/linux/README.md",
    "docs/software/README.md",
])
def test_the_contract_is_reachable_from_every_index_a_reader_lands_on(index):
    text = (_ROOT / index).read_text(encoding="utf-8")
    assert "DEPLOYMENT_CONTRACT.md" in text, (
        f"{index} does not link the deployment contract. 'One discoverable "
        "document' means reachable from wherever a reader actually starts."
    )


def test_the_document_names_the_daemon_start_path_in_prose():
    text = _DOC.read_text(encoding="utf-8")
    for needed in ("S99deepskopion-stream", "python3 -m sw.stream",
                   "/var/log/deepskopion-stream.log"):
        assert needed in text, f"the contract document never mentions {needed}"


# ---------------------------------------------------------------------------
# 4. Every failure mode, CONSTRUCTED. This is the part that matters.
# ---------------------------------------------------------------------------
def _bench_tools(root: Path) -> list[dict]:
    """The tool images bench/deployment_contract.yml declares (DS-P1.103); none without bench/."""
    sidecar = root / "bench" / "deployment_contract.yml"
    if not sidecar.is_file():
        return []
    return (yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}).get("tool_images") or []


#: DS-P1.103: the tool-image rules are exercised on a synthetic image the sandbox builds, so they run in
#: every tree, the public export (no bench/) included. The real bench image (de25_p2writer) is judged by
#: the real bench sidecar in the live checks.
FIXTURE_TOOL = "fixture_tool_image"


def _plant_fixture_tool_image(root: Path) -> None:
    """A rootfs-building tool target, its own PID 1 overlay, declared in the sandbox's public contract."""
    target = root / "targets" / f"{FIXTURE_TOOL}.yml"
    target.write_text("linux:\n  rootfs:\n    overlay: sw/linux/fixture_tool/overlay\n", encoding="utf-8")
    init = root / "sw" / "linux" / "fixture_tool" / "overlay" / "init"
    init.parent.mkdir(parents=True)
    init.write_text("#!/bin/sh\nexec /bin/sh\n", encoding="utf-8")
    decl_path = root / "sw" / "linux" / "deployment_contract.yml"
    decl = yaml.safe_load(decl_path.read_text(encoding="utf-8"))
    decl.setdefault("tool_images", []).insert(0, {
        "target": FIXTURE_TOOL, "overlay": "sw/linux/fixture_tool/overlay",
        "pid1": "init", "autostart": "none"})
    decl_path.write_text(yaml.safe_dump(decl, sort_keys=False), encoding="utf-8")


@pytest.fixture
def sandbox(tmp_path):
    """A throwaway git repo holding the real overlay, docs, targets and YAML.

    A copy rather than the live tree: these tests plant defects, and a test
    that mutates the checkout it is validating is one interrupted run away
    from committing its own fixture.
    """
    root = tmp_path / "repo"
    (root / "sw" / "linux").mkdir(parents=True)
    (root / "docs").mkdir()
    shutil.copytree(_OVERLAY, root / "sw" / "linux" / "rootfs_overlay")
    shutil.copytree(_ROOT / "targets", root / "targets")
    # DS-P1.103: the bench targets and their contract sidecar, like targets/ and the sw/linux sidecars,
    # when this checkout has them (the public export does not).
    if (_ROOT / "bench" / "deployment_contract.yml").is_file():
        shutil.copytree(_ROOT / "bench" / "targets", root / "bench" / "targets")
        shutil.copy(_ROOT / "bench" / "deployment_contract.yml", root / "bench")
    shutil.copy(_LINUX_DIR / "deployment_contract.yml", root / "sw" / "linux")
    for sidecar in _LINUX_DIR.glob("*/deployment_contract.yml"):
        dst = root / "sw" / "linux" / sidecar.parent.name
        dst.mkdir()
        shutil.copy(sidecar, dst)
    shutil.copy(_DOC, root / "docs")
    # DS-P2.264: each declared tool image's own overlay (its PID 1 is checked); DS-P1.103: the bench
    # tool images are declared in bench/deployment_contract.yml.
    for tool in _bench_tools(_ROOT):
        shutil.copytree(_ROOT / tool["overlay"], root / tool["overlay"])
    _plant_fixture_tool_image(root)
    shutil.copy(_ROOT / ".gitignore", root)
    for rel in C.load_declaration()["docs_bound_by_this_contract"]:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy(_ROOT / rel, dst)

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "fixture"],
        cwd=root, check=True,
    )
    return root


def _check(root: Path) -> list[C.Failure]:
    return C.check_all(C.load_declaration(root / "sw" / "linux" / "deployment_contract.yml"), root)


def _tracked_add(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=root, check=True)


def test_the_sandbox_itself_is_clean(sandbox):
    """Without this, every RED below could be the fixture, not the defect."""
    assert not _check(sandbox), [str(f) for f in _check(sandbox)]


def test_a_stowaway_systemd_unit_in_the_overlay_fails(sandbox):
    """THE defect. This exact file shipped in every image for months."""
    _tracked_add(
        sandbox,
        "sw/linux/rootfs_overlay/etc/systemd/system/deepskopion-stream.service",
        "[Service]\nExecStart=/usr/bin/python3 -m sw.stream\n",
    )
    fails = _check(sandbox)
    assert any("etc/systemd/system/deepskopion-stream.service" in f.what for f in fails), \
        f"a systemd unit in the overlay was accepted: {[str(f) for f in fails]}"


def test_an_undeclared_init_script_in_the_overlay_fails(sandbox):
    """Not systemd-specific: ANY new boot step must be declared first."""
    _tracked_add(sandbox, "sw/linux/rootfs_overlay/etc/init.d/S50surprise",
                 "#!/bin/sh\necho surprise\n")
    fails = _check(sandbox)
    assert any("S50surprise" in f.what for f in fails), \
        f"an undeclared init.d script was accepted: {[str(f) for f in fails]}"
    assert any("NOT declared" in f.detail for f in fails)


def test_a_declared_autostart_file_that_is_missing_fails(sandbox):
    """The other direction: the document promising a boot step no image does."""
    (sandbox / "sw/linux/rootfs_overlay/etc/init.d/S99deepskopion-stream").unlink()
    fails = _check(sandbox)
    assert any("S99deepskopion-stream" in f.what for f in fails), \
        f"a missing daemon launcher was accepted: {[str(f) for f in fails]}"


def test_a_doc_that_disagrees_with_the_overlay_fails(sandbox):
    """The ticket's headline requirement, demonstrated rather than assumed.

    The declaration and the overlay both gain a unit; the document is left
    alone. The reader's answer and the board's behaviour now differ, and the
    check must say so — the generated section is the coupling that makes this
    detectable at all.
    """
    yml = sandbox / "sw/linux/deployment_contract.yml"
    decl = yaml.safe_load(yml.read_text(encoding="utf-8"))
    decl["autostart"].append({
        "path": "etc/init.d/S60extra",
        "provenance": "tracked",
        "starts": "an extra service nobody wrote down in the prose",
        "note": "planted by the regression guard",
    })
    yml.write_text(yaml.safe_dump(decl, sort_keys=False), encoding="utf-8")
    _tracked_add(sandbox, "sw/linux/rootfs_overlay/etc/init.d/S60extra",
                 "#!/bin/sh\nexit 0\n")

    fails = _check(sandbox)
    assert any("DEPLOYMENT_CONTRACT.md" in f.what for f in fails), (
        "the overlay now ships an autostart file the document does not "
        f"describe and the check stayed green: {[str(f) for f in fails]}"
    )


def test_a_hand_edited_generated_section_fails(sandbox):
    """The document is the reader's copy of the declaration; if a hand-edit
    survives, the two are back to being independent stories."""
    doc = sandbox / "docs" / "DEPLOYMENT_CONTRACT.md"
    doc.write_text(
        doc.read_text(encoding="utf-8").replace(
            "`etc/init.d/S99deepskopion-stream`", "`etc/init.d/S99something-else`"),
        encoding="utf-8",
    )
    assert any("DEPLOYMENT_CONTRACT.md" in f.what for f in _check(sandbox))


def test_deleting_the_generated_markers_fails(sandbox):
    """Removing the coupling must not be the way to make the check pass."""
    doc = sandbox / "docs" / "DEPLOYMENT_CONTRACT.md"
    text = doc.read_text(encoding="utf-8")
    doc.write_text(text.replace(C.BEGIN_MARK, "").replace(C.END_MARK, ""),
                   encoding="utf-8")
    fails = _check(sandbox)
    assert any("marker" in f.detail for f in fails), \
        f"a document with no generated section was accepted: {[str(f) for f in fails]}"


def test_a_new_linux_target_that_is_not_declared_fails(sandbox):
    """A board whose boot behaviour nobody wrote down — the ticket's root
    complaint, in its future tense."""
    src = (sandbox / "targets" / "zybo_datalogger.yml").read_text(encoding="utf-8")  # DS-P1.103
    _tracked_add(sandbox, "targets/newboard.yml", src)
    fails = _check(sandbox)
    assert any("newboard" in f.what for f in fails), \
        f"an undeclared Linux target was accepted: {[str(f) for f in fails]}"


def test_an_overlay_file_that_is_neither_tracked_nor_ignored_fails(sandbox):
    """How the staged daemon tree stayed committed for months (DS-P2.81): a
    build artefact that git had no opinion about, swept up by a broad add."""
    (sandbox / "sw/linux/rootfs_overlay/etc/leftover.conf").write_text("x\n")
    fails = _check(sandbox)
    assert any("leftover.conf" in f.what for f in fails), \
        f"an unclassifiable overlay file was accepted: {[str(f) for f in fails]}"


def test_a_journalctl_command_in_a_bound_doc_fails(sandbox):
    """The measured consequence of the old ambiguity: operator instructions
    telling people to run a command that cannot work on any image we build."""
    readme = sandbox / "sw" / "linux" / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\n## Checking the daemon\n\n```sh\njournalctl -u deepskopion-stream -f\n```\n",
        encoding="utf-8",
    )
    fails = _check(sandbox)
    assert any("journalctl" in f.detail for f in fails), \
        f"an impossible operator command was accepted: {[str(f) for f in fails]}"


def test_prose_about_systemd_is_still_allowed(sandbox):
    """The ban is on copyable commands, not on discussing the history. A rule
    that forbade the word would delete the explanation of why the rule exists,
    and the next reader would re-add the units."""
    readme = sandbox / "sw" / "linux" / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\nThere is no systemd here, so journalctl cannot work.\n",
        encoding="utf-8",
    )
    assert not _check(sandbox)


# ---------------------------------------------------------------------------
# 5. Owner-supplied private files (DS-P2.260)
# ---------------------------------------------------------------------------
# S49dropbear installs /etc/deepskopion/authorized_keys from a private image
# build. That file is the owner's, never a source file and never committed, so
# the contract names it and .gitignore matches exactly that path. The public
# image carries no key; tools/tests/test_public_artifact_hygiene.py
# (test_linux_image_archive_is_opened_not_only_string_scanned) refuses a key
# found inside a built Linux image archive.
_KEY = "etc/deepskopion/authorized_keys"


def _ignore_pattern(root: Path, rel: str) -> str | None:
    res = subprocess.run(["git", "check-ignore", "-v", "--no-index", rel],
                         cwd=root, capture_output=True, text=True)
    if res.returncode != 0:
        return None
    return res.stdout.split("\t", 1)[0].split(":", 2)[2]


def test_the_owner_supplied_key_is_declared(decl):
    paths = [e["path"] for e in decl.get("owner_supplied") or []]
    assert _KEY in paths, f"{_KEY} is not declared as owner-supplied: {paths}"


def test_the_real_ignore_matches_exactly_the_key_path():
    rel = f"sw/linux/rootfs_overlay/{_KEY}"
    assert _ignore_pattern(_ROOT, rel) == rel, \
        f"{rel} must be ignored by an entry naming exactly that path, got {_ignore_pattern(_ROOT, rel)!r}"
    other = "sw/linux/rootfs_overlay/etc/deepskopion/other.conf"
    assert _ignore_pattern(_ROOT, other) is None, \
        f"a sibling of the key is ignored too ({_ignore_pattern(_ROOT, other)!r}): the ignore is wider than the key"


def test_an_owner_supplied_key_is_accepted_and_any_other_unaccounted_file_still_fails(sandbox):
    keydir = sandbox / "sw/linux/rootfs_overlay/etc/deepskopion"
    keydir.mkdir(parents=True, exist_ok=True)
    (keydir / "authorized_keys").write_text("ssh-ed25519 AAAA test\n")
    fails = _check(sandbox)
    assert not any("authorized_keys" in f.what for f in fails), \
        f"the owner-supplied key was refused: {[str(f) for f in fails]}"
    (keydir / "other.conf").write_text("x\n")
    fails = _check(sandbox)
    assert any("other.conf" in f.what for f in fails), \
        f"an unaccounted file next to the key was accepted: {[str(f) for f in fails]}"


def test_a_committed_owner_supplied_file_fails(sandbox):
    rel = f"sw/linux/rootfs_overlay/{_KEY}"
    path = sandbox / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ssh-ed25519 AAAA test\n")
    subprocess.run(["git", "add", "-f", rel], cwd=sandbox, check=True)
    fails = _check(sandbox)
    assert any("authorized_keys" in f.what and "committed" in f.detail for f in fails), \
        f"a committed owner key was accepted: {[str(f) for f in fails]}"


def test_a_wider_ignore_for_the_owner_supplied_path_fails(sandbox):
    gi = sandbox / ".gitignore"
    exact = f"sw/linux/rootfs_overlay/{_KEY}"
    text = gi.read_text(encoding="utf-8")
    assert exact + "\n" in text, "the sandbox .gitignore lacks the exact key entry"
    gi.write_text(text.replace(exact + "\n", "sw/linux/rootfs_overlay/etc/deepskopion/\n"), encoding="utf-8")
    fails = _check(sandbox)
    assert any("authorized_keys" in f.what and "exactly" in f.detail for f in fails), \
        f"a directory-wide ignore for the owner key was accepted: {[str(f) for f in fails]}"


def test_bytecode_in_the_overlay_fails(sandbox):
    """DS-P2.262: host-made bytecode under the overlay is baked into the image
    (rr linux rootfs walks every file); ignored or not, it must not be there."""
    pyc = sandbox / "sw/linux/rootfs_overlay/usr/local/share/deepskopion/sw/__pycache__/x.cpython-310.pyc"
    pyc.parent.mkdir(parents=True, exist_ok=True)
    pyc.write_bytes(b"\x00")
    fails = _check(sandbox)
    assert any("x.cpython-310.pyc" in f.what or "__pycache__" in f.what for f in fails), \
        f"bytecode in the overlay was accepted: {[str(f) for f in fails]}"


# ── DS-P2.264: bench tool images (own PID 1, no autostart) ──────────────────

def _decl_path(root: Path) -> Path:
    return root / "sw" / "linux" / "deployment_contract.yml"


def _edit_decl(root: Path, fn) -> None:
    path = _decl_path(root)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_a_tool_image_target_in_neither_list_fails(sandbox):
    _edit_decl(sandbox, lambda d: d.pop("tool_images"))
    fails = _check(sandbox)
    assert any(FIXTURE_TOOL in f.what and "not listed in boot.targets" in f.detail
               for f in fails), [str(f) for f in fails]


def test_a_target_in_both_lists_fails(sandbox):
    _edit_decl(sandbox, lambda d: d["boot"]["targets"].append(FIXTURE_TOOL))
    fails = _check(sandbox)
    assert any(FIXTURE_TOOL in f.what and "both" in f.detail for f in fails), \
        [str(f) for f in fails]


def test_a_tool_image_whose_pid1_file_is_missing_fails(sandbox):
    tool = C.load_declaration(_decl_path(sandbox))["tool_images"][0]
    (sandbox / tool["overlay"] / tool["pid1"]).unlink()
    fails = _check(sandbox)
    assert any("PID 1 file" in f.detail for f in fails), [str(f) for f in fails]


def test_a_tool_image_overlay_with_an_autostart_script_fails(sandbox):
    tool = C.load_declaration(_decl_path(sandbox))["tool_images"][0]
    _tracked_add(sandbox, f"{tool['overlay']}/etc/init.d/S50stowaway", "#!/bin/sh\n")
    fails = _check(sandbox)
    assert any("S50stowaway" in f.what and "autostart" in f.detail for f in fails), \
        [str(f) for f in fails]
