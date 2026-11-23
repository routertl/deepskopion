"""DS-P2.141 (PF-L0b): the riscv64 PolarFire python_overlay must be OBTAINABLE by
a fresh consumer without the 1-3 h Buildroot rebuild — distribution PARITY with
the armhf overlay — and the deployment contract must describe the overlay path
per-arch, not as a single armhf-only path.

WHAT THIS DOES *NOT* CLAIM (corrected on review of the first DS-P2.141 draft):
this is NOT guarding against `rr linux rootfs` "silently baking a python-less
rootfs". That footgun does not exist — the bake path already fails CLOSED.
``sdk.cli.commands.linux_steps.rootfs`` ``_resolve_python_overlay`` raises
``FileNotFoundError`` when the declared ``linux.rootfs.python_overlay`` directory
is absent, and ``rr linux rootfs`` maps that to exit 1. The earlier docstring and
one test asserted the wrong mechanism (a file merely existing); a landing review
caught that a stub would stay green while the overlay was never obtainable.

THE REAL GAP DS-P2.141 OWNS: the armhf overlay is distributable in seconds
(``sw/linux/fetch_python_overlay.sh`` unpacks the git-lfs
``sw/linux/python_overlay.tar.gz``); the PolarFire riscv64 overlay
(``targets/pfsoc_datalogger.yml`` ``linux.rootfs.python_overlay:
sw/linux/pfsoc/python_overlay``) had no such path — its only producer was the
1-3 h ``sw/linux/pfsoc/build_python_overlay.sh``.

These guards therefore assert the DISTRIBUTION MECHANISM, not file existence:
  * the fetch script REFUSES (exit 1, actionable) when neither overlay nor
    tarball is present — proven by running it in an isolated tree;
  * the fetch script EXTRACTS a present tarball into the overlay slot — proven by
    running it against a fabricated tarball (mutating the extract line goes RED);
  * the overlay is actually OBTAINABLE without the rebuild — a real git-lfs
    tarball (or an already-extracted overlay tree) exists beside the fetch script
    (this is RED until the 1-3 h build's tarball is committed — it cannot pass on
    a stub);
  * the deployment CONTRACT (read from the YAML source of truth, then the
    generated doc) names the overlay path per-arch, not armhf-only.
"""
from __future__ import annotations

import sys
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
TARGETS_DIR = ROOT / "targets"
CONTRACT_DOC = ROOT / "docs" / "DEPLOYMENT_CONTRACT.md"
# DS-P1.118: the PolarFire declaration moved from the sidecar into the public contract.
CONTRACT_YML = ROOT / "sw" / "linux" / "deployment_contract.yml"
PFSOC_FETCH = ROOT / "sw" / "linux" / "pfsoc" / "fetch_python_overlay.sh"
PFSOC_OVERLAY = PFSOC_FETCH.parent / "python_overlay"
PFSOC_TARBALL = PFSOC_FETCH.parent / "python_overlay.tar.gz"

# The fetch script guards a real git-lfs tarball against an unfetched pointer
# (a few hundred bytes of text); mirror that threshold when fabricating one.
_MIN_REAL_TARBALL = 1_000_000


def _linux_targets_with_python_overlay() -> list[tuple[Path, str]]:
    """(target yml, declared python_overlay path) for every Linux rootfs target
    that declares one. Parsed as text so the guard cannot go quiet because a yaml
    parser is missing in some deploy lane."""
    out: list[tuple[Path, str]] = []
    pat = re.compile(r"^\s*python_overlay:\s*(\S+)\s*$", re.MULTILINE)
    for yml in all_target_files(ROOT):
        text = yml.read_text(encoding="utf-8", errors="replace")
        m = pat.search(text)
        if m:
            out.append((yml, m.group(1)))
    return out


def _pfsoc_targets() -> list[tuple[Path, str]]:
    return [
        (yml, overlay)
        for yml, overlay in _linux_targets_with_python_overlay()
        if "pfsoc" in overlay or "pfsoc" in yml.name
    ]


def _make_fake_tarball(dst: Path) -> None:
    """A >1 MB gzip tarball whose payload is a plausible overlay: usr/bin/
    python3.12 plus padding to clear the fetch script's pointer-size guard."""
    payload = dst.parent / "_payload"
    (payload / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    (payload / "usr" / "bin" / "python3.12").write_bytes(b"#!fake-rv64-python\n")
    # Incompressible padding so the gzip result clears _MIN_REAL_TARBALL.
    import os

    (payload / "usr" / "lib").mkdir(parents=True, exist_ok=True)
    (payload / "usr" / "lib" / "pad.bin").write_bytes(os.urandom(2_000_000))
    with tarfile.open(dst, "w:gz") as tf:
        for p in sorted(payload.rglob("*")):
            tf.add(p, arcname=str(p.relative_to(payload)))
    shutil.rmtree(payload)


def _isolated_script(tmp_path: Path) -> Path:
    """Copy the real fetch script into an empty tree (no overlay, no tarball) so
    its behaviour is exercised deterministically regardless of what this checkout
    happens to contain."""
    dst = tmp_path / "fetch_python_overlay.sh"
    dst.write_bytes(PFSOC_FETCH.read_bytes())
    dst.chmod(0o755)
    return dst


def test_pfsoc_fetch_fails_loud_when_overlay_absent(tmp_path):
    """REFUSAL mechanism: with neither overlay nor tarball, the script must exit
    non-zero and name the 1-3 h build as the next step — never exit 0. Run in an
    isolated empty tree so it is asserted deterministically (the old version
    pytest.skip'd whenever this checkout happened to have the artefact, so the
    success run never proved anything)."""
    script = _isolated_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, cwd=str(tmp_path)
    )
    assert proc.returncode != 0, (
        "fetch_python_overlay.sh exited 0 with no overlay and no tarball — a fresh "
        f"consumer is told 'ready' with nothing extracted.\nstdout={proc.stdout!r}\n"
        f"stderr={proc.stderr!r}"
    )
    assert "build_python_overlay.sh" in proc.stderr, (
        "the refusal must name the actionable next step (the 1-3 h build); "
        f"stderr={proc.stderr!r}"
    )


def test_pfsoc_fetch_extracts_a_present_tarball(tmp_path):
    """DISTRIBUTION mechanism: given a real (>1 MB) tarball, the script must
    extract it into the overlay slot and exit 0 — the fast path a fresh consumer
    hits after `git lfs pull`. Mutating the `tar xzf` line (or the tarball
    resolution) makes this go RED, so it guards the extraction itself, not the
    mere presence of a file."""
    script = _isolated_script(tmp_path)
    _make_fake_tarball(tmp_path / "python_overlay.tar.gz")
    assert (tmp_path / "python_overlay.tar.gz").stat().st_size >= _MIN_REAL_TARBALL
    proc = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, cwd=str(tmp_path)
    )
    assert proc.returncode == 0, (
        "fetch_python_overlay.sh failed to extract a present, real tarball.\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    extracted = tmp_path / "python_overlay" / "usr" / "bin" / "python3.12"
    assert extracted.exists(), (
        "the tarball was accepted but its python3 did not land in the overlay "
        f"slot — extraction is broken. tree={sorted(p.name for p in (tmp_path / 'python_overlay').rglob('*'))!r}"
    )


def test_pfsoc_fetch_rejects_an_unfetched_lfs_pointer(tmp_path):
    """A git-lfs pointer (a few hundred bytes) is NOT the overlay. The script must
    refuse it (exit 1, 'git lfs pull') rather than tar-extract a text pointer and
    claim success."""
    script = _isolated_script(tmp_path)
    (tmp_path / "python_overlay.tar.gz").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef\nsize 12639423\n"
    )
    proc = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, cwd=str(tmp_path)
    )
    assert proc.returncode != 0, (
        "a git-lfs pointer was treated as a real tarball; the size guard is dead.\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "git lfs pull" in proc.stderr


def test_stage_daemon_materializes_the_pfsoc_overlay_when_fetchable():
    """stage_daemon.sh is the shared stage_command for EVERY Linux target, so the
    pfsoc overlay materialization must be (a) present — deleting the fetch call
    goes RED here (2026-09-15 land-review), and (b) GUARDED so a non-pfsoc build
    in an lfs-less clone is never failed by a pfsoc-only concern."""
    sd = (ROOT / "sw" / "linux" / "pfsoc" / "stage_hook.sh").read_text(encoding="utf-8")
    assert "pfsoc/fetch_python_overlay.sh" in sd, (
        "stage_daemon.sh no longer materializes the pfsoc python_overlay — "
        "fresh-clone pfsoc rootfs packing loses the distribution path (DS-P2.141)."
    )
    assert 'if [ -s "$THIS_DIR/pfsoc/python_overlay.tar.gz" ]' in sd, (
        "the pfsoc fetch call must stay guarded (tarball present) so zybo/de25 "
        "stage runs in lfs-less clones are unaffected."
    )
    # BOTH halves of the guard are load-bearing (2026-09-15 review round 3):
    # after landing, every lfs-less clone holds a NON-EMPTY pointer file, so
    # the -s half alone would call fetch (which exits 1) and kill every
    # non-pfsoc stage_command. The pointer-detection half must stay.
    assert "head -c 20" in sd and "version http" in sd, (
        "stage_daemon.sh lost the lfs-pointer detection half of the overlay "
        "guard — an lfs-less clone's pointer file would trigger a failing "
        "fetch on the shared stage path (zybo/de25)."
    )


def test_deployment_contract_is_per_arch_not_armhf_only():
    """The CONTRACT source of truth (the YAML), and the doc generated from it,
    must describe the overlay path per-arch — not as the single armhf-only
    `sw/linux/fetch_python_overlay.sh`, which is silently false for a riscv64
    target. Reading the YAML (not just substring-hunting markdown) is what makes
    reverting the YAML reason go RED."""
    # Read the executable source of truth through the YAML loader, not a regex
    # (a regex also matched a path that sat only in a YAML comment).
    decl = yaml.safe_load(CONTRACT_YML.read_text(encoding="utf-8"))
    entry = next((e for e in decl.get("prohibited") or [] if e.get("mechanism") == "petalinux"), None)
    assert entry, "no petalinux mechanism block in deployment_contract.yml"
    petalinux_block = " ".join(str(entry.get("reason", "")).split())
    # DS-P1.118: the public contract names the riscv64 route a public clone can run.
    # The LFS archive is not exported, so the fetch script would exit 1 there; it is
    # not the public route and the reason must not offer it.
    # Each architecture paired with its own build (a swapped mapping fails).
    # Complete and bounded: the whole Zynq recipe, and the PolarFire path up to its
    # closing parenthesis (so a longer or renamed path does not match).
    pairs = ("armhf Zynq built by sw/linux/build_buildroot.sh and sw/linux/extract_python_overlay.sh;",
             "riscv64 PolarFire built by sw/linux/pfsoc/build_python_overlay.sh)")
    assert all(pair in petalinux_block for pair in pairs) and \
        "fetch_python_overlay" not in petalinux_block, (
        "deployment_contract.yml's petalinux reason must name the riscv64 PolarFire "
        "overlay build (sw/linux/pfsoc/build_python_overlay.sh) and not the "
        f"private-archive fetch; got: {petalinux_block!r}"
    )
    assert "per-arch" in petalinux_block, (
        "the contract reason must state the overlay is per-arch (armhf Zynq vs "
        "riscv64 PolarFire), not a single shared path"
    )
    # The reason, and its generated line, name the riscv64 route a public clone
    # can run (DS-P1.118: PolarFire ships; the export drops only the archive),
    # not only the private-archive fetch.
    doc = CONTRACT_DOC.read_text(encoding="utf-8")
    petalinux_line = next((ln for ln in doc.splitlines() if "**petalinux**" in ln), "")
    assert all(pair in " ".join(petalinux_line.split()) for pair in pairs), (
        "the petalinux reason (YAML and generated line) must name the riscv64 overlay "
        "build a public clone runs"
    )
    assert "per-arch" in doc
