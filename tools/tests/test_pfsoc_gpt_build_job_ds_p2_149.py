# rr unknown-llm (AI-generated)
"""DS-P2.149 — the deterministic GPT build job (deliverable 2) contract.

The prior landing refused this branch partly because deliverable (2) — *rebuild
the DS-P2.146 payload onto GPT* — existed only as stub-blob tests, with no
committed way to produce the real image. This guard pins the committed build job
``sw/linux/pfsoc/build_gpt_sdcard.sh`` so it cannot silently rot back into an
ad-hoc, stub-only, or MBR recipe:

  * it resolves the real riscv64 payload baseline (fetch_python_overlay.sh);
  * it stages the daemon through the deployment contract (stage_daemon.sh),
    never a hand-copied binary;
  * it produces the image through ``pfsoc_boot_media.py assemble`` (the GPT
    assembler), never petalinux/bitbake and never a bare ``mkfs`` of its own;
  * it is a real script (parses under ``bash -n``, executable bit set).

The first four checks are file+regex over the committed script — no board, no
network — so they run identically under cron/CI and a fresh clone.

``test_from_fast_path_lands_daemon_on_p3`` goes further: it RUNS the advertised
``--from`` fast path against a synthetic staging tree and asserts the DEPLOYED
EFFECT — that the daemon (``S99deepskopion-stream`` + ``sw/stream``) is
physically resident on the P3 ext4 of the image the job emits. It exists because
the prior landing shipped a ``--from`` path that only *looked* wired: it ran
``stage_daemon.sh`` (which writes the overlay to ``sw/linux/pfsoc/rootfs_overlay``)
and then ``assemble --from`` (which packs ``<staging>/rootfs``) with no step
bridging the two — so on a base staging tree without a pre-merged daemon the
assemble REFUSES ("dropped the overlay: rootfs is missing
etc/init.d/S99deepskopion-stream") and the fast path emits no image at all. A
file+regex guard cannot see that; only running the job does.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JOB = ROOT / "sw" / "linux" / "pfsoc" / "build_gpt_sdcard.sh"
INSPECTOR = ROOT / "tools" / "pfsoc_boot_media.py"

# The daemon marker whose presence on P3 proves the fast path bridged the
# staged overlay into the packed rootfs (boot_media.yml rootfs_markers).
DAEMON_P3_MARKER = "ROOTFS etc/init.d/S99deepskopion-stream"

_STUB_HSS_GENERATOR = r"""#!/usr/bin/env python3
import sys
from pathlib import Path
MAGIC = bytes.fromhex("dec007b0")
args = sys.argv[1:]
cfg = Path(args[args.index("-c") + 1])
out = Path(args[-1])
next_path = None
lines = cfg.read_text(encoding="utf-8").splitlines()
for i, line in enumerate(lines):
    if line.startswith("payloads:") and i + 1 < len(lines):
        next_path = Path(lines[i + 1].strip().rstrip(":"))
        break
body = next_path.read_bytes() if next_path and next_path.is_file() else b"WRAP"
out.write_bytes(MAGIC + body)
"""


_REQUIRED_SD_TOOLS = (
    "sfdisk",
    "dd",
    "mformat",
    "mcopy",
    "mtype",
    "mke2fs",
    "debugfs",
    "rsync",
)

_LFS_POINTER = (
    "version https://git-lfs.github.com/spec/v1\n"
    "oid sha256:f11405bcb31094e020b4c327a5a04c61c91e11d313c9f86b98feee43ef9b9221\n"
    "size 13941466\n"
)


def _sd_tool(tool: str) -> str | None:
    """Resolve an SD tool the way pfsoc_boot_media._require does (/sbin fallback)."""
    found = shutil.which(tool)
    if found:
        return found
    for d in ("/sbin", "/usr/sbin", "/usr/local/sbin"):
        cand = Path(d) / tool
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _sd_tools_present() -> bool:
    return all(_sd_tool(t) for t in _REQUIRED_SD_TOOLS)


def _require_sd_tools() -> None:
    for tool in _REQUIRED_SD_TOOLS:
        assert _sd_tool(tool) is not None, (
            f"missing required offline SD tool {tool!r} — "
            "test_pfsoc_gpt_build_job_ds_p2_149 must fail, not skip (DS-T3.40)"
        )


def _fresh_clone_without_lfs(tmp: Path) -> Path:
    """Materialize a fresh-clone tree in ``tmp`` where git-lfs objects have not
    been pulled (`sw/linux/pfsoc/python_overlay` is absent and
    `sw/linux/pfsoc/python_overlay.tar.gz` is an unfetched LFS pointer).
    """
    clone = tmp / "clone"
    clone.mkdir(parents=True, exist_ok=True)
    listing = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "-z",
            "sw/linux",
            "sw/stream",
            "contracts",
            "targets",
            "tools/pfsoc_boot_media.py",
            "tools/pfsoc_hss_lim_stage.py",
            "docs/*.md",
            "docs/software/*.md",
            "README.md",
            ".gitignore",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for rel in filter(None, listing.split("\0")):
        src = ROOT / rel
        if not src.is_file():
            continue
        dst = clone / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.stat().st_size > 1_000_000:
            dst.write_text(_LFS_POINTER, encoding="utf-8")
        else:
            shutil.copy2(src, dst)
    pfsoc_tb = clone / "sw" / "linux" / "pfsoc" / "python_overlay.tar.gz"
    pfsoc_tb.parent.mkdir(parents=True, exist_ok=True)
    pfsoc_tb.write_text(_LFS_POINTER, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    return clone


def _stub_hss_env(tmp: Path) -> dict[str, str]:
    """A stub hss-payload-generator so assemble produces a magic-carrying
    payload.bin without the 1-3 h HSS build (mirrors the assembler test)."""
    gen_dir = tmp / "hss" / "tools" / "hss-payload-generator"
    gen_dir.mkdir(parents=True)
    gen = gen_dir / "hss-payload-generator"
    gen.write_text(_STUB_HSS_GENERATOR, encoding="utf-8")
    gen.chmod(gen.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return {"PFSOC_HSS_DIR": str(tmp / "hss")}


def _base_staging_without_daemon(tmp: Path) -> Path:
    """A prebuilt-chain staging dir as ``--from`` receives one: the rr-linux
    chain artefacts + a base rootfs carrying the python_overlay (python3.12) but
    NOT the daemon overlay — exactly the tree that exposes whether the fast path
    bridges the daemon in. If it does not, assemble refuses and no image lands.
    """
    staging = tmp / "stage"
    (staging / "rootfs" / "usr" / "bin").mkdir(parents=True)
    (staging / "rootfs" / "usr" / "bin" / "python3.12").write_bytes(b"ELF-python-overlay-stub")
    (staging / "u-boot.bin").write_bytes(b"U-BOOT" * 512)
    (staging / "Image").write_bytes(b"IMAGE" * 256)
    dtb = staging / "microchip-mpfs-disco-kit.dtb"
    dtb.write_bytes(b"DTB-DISCO" * 32)
    (staging / "system.dtb").write_bytes(dtb.read_bytes())
    (staging / "boot.scr").write_bytes(b"BOOTSCR")
    return staging


def test_build_job_exists_and_is_executable() -> None:
    assert JOB.is_file(), f"missing deterministic GPT build job: {JOB}"
    assert os.access(JOB, os.X_OK), f"{JOB} must be executable"


def test_build_job_parses() -> None:
    proc = subprocess.run(["bash", "-n", str(JOB)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_build_job_drives_the_payload_baseline_onto_gpt() -> None:
    text = JOB.read_text(encoding="utf-8")
    # DS-P2.146 payload baseline, resolved through the committed contract.
    assert "fetch_python_overlay.sh" in text, "must resolve the riscv64 overlay baseline"
    assert "stage_daemon.sh" in text, "must stage the daemon via the deployment contract"
    # The image is produced by the in-tree GPT assembler, not a bespoke mkfs.
    assert re.search(r"pfsoc_boot_media\.py\s+assemble", text), (
        "must assemble via tools/pfsoc_boot_media.py (the GPT assembler)"
    )
    # It is a GPT rebuild that targets the p3 ext4 rootfs the deployed card uses.
    assert "mmcblk0p3" in text, "must reference the deployed GPT rootfs (root=p3)"


def test_build_job_uses_no_forbidden_flow() -> None:
    lowered = JOB.read_text(encoding="utf-8").lower()
    for banned in ("petalinux", "bitbake"):
        assert banned not in lowered, f"build job must not use {banned}"
    # It must not fabricate stub next-stage blobs (the exact thing the review
    # said stub-only tests could not stand in for) — no `echo ... > u-boot.bin`.
    assert not re.search(r">\s*['\"]?\S*u-boot\.bin", lowered), (
        "build job must consume the real rr linux chain, not fabricate u-boot.bin"
    )


def test_from_fast_path_lands_daemon_on_p3(tmp_path: Path) -> None:
    """RUN the ``--from`` fast path and assert the DEPLOYED EFFECT.

    The daemon (``S99deepskopion-stream`` + ``sw/stream``) must be physically
    resident on the P3 ext4 of the image the job emits. The job's own final
    ``pfsoc_boot_media.py inspect`` step reads the packed P3 back with debugfs
    and prints ``ROOTFS etc/init.d/S99deepskopion-stream`` — so this asserts on
    the job's real stdout, at the CLI surface a caller reaches, not a helper's
    return value.

    DS-T3.40: runs in a fresh-clone tree where git-lfs has not been pulled
    (``sw/linux/pfsoc/python_overlay`` is absent and
    ``sw/linux/pfsoc/python_overlay.tar.gz`` is an unfetched LFS pointer), and
    also verifies that when ``--from`` receives a staging rootfs without
    ``usr/bin/python3.12`` while ``python_overlay`` is materialized in the repo,
    ``build_gpt_sdcard.sh --from`` merges ``python_overlay`` into the staging
    rootfs before assembly.

    BASELINE (pre-fix ``--from``: stage the overlay, then ``assemble --from``
    with no bridge, or run ``fetch_python_overlay.sh`` unconditionally before
    checking ``--from``): the job exits non-zero and emits NO image → this
    assertion is RED with intended_assertion "did not emit a daemon-carrying
    GPT image".

    MUTATION (delete the ``rsync ... rootfs_overlay ... $STAGE_ROOTFS`` merge or
    revert the ``--from`` overlay-resolution guard in ``build_gpt_sdcard.sh``):
    reverts to the pre-fix behaviour → RED again.
    """
    _require_sd_tools()
    clone = _fresh_clone_without_lfs(tmp_path)
    clone_job = clone / "sw" / "linux" / "pfsoc" / "build_gpt_sdcard.sh"
    staging = _base_staging_without_daemon(tmp_path)
    out = staging / "sdcard.img"
    env = os.environ.copy()
    env.update(_stub_hss_env(tmp_path))
    proc = subprocess.run(
        [str(clone_job), "--from", str(staging), "--out", str(out)],
        cwd=str(clone), env=env, capture_output=True, text=True, check=False,
    )
    combined = proc.stdout + proc.stderr
    daemon_on_p3 = (
        proc.returncode == 0
        and out.is_file()
        and DAEMON_P3_MARKER in combined
    )
    assert daemon_on_p3, (
        "build_gpt_sdcard.sh --from did not emit a daemon-carrying GPT image "
        "(S99deepskopion-stream absent from the P3 ext4): the fast path must "
        "merge the staged daemon overlay into the packed rootfs before "
        f"assemble. rc={proc.returncode}\n{combined}"
    )
    # Independently confirm the payload package landed too (not just the init
    # script) — the full daemon must be reachable, per the deployment contract.
    insp = subprocess.run(
        ["python3", str(INSPECTOR), "inspect", str(out)],
        cwd=str(ROOT), capture_output=True, text=True, check=False,
    )
    insp_out = insp.stdout + insp.stderr
    assert insp.returncode == 0, insp_out
    assert "ROOTFS usr/local/share/deepskopion/sw/stream/__main__.py" in insp_out, insp_out

    # Also verify that when --from receives a base rootfs that does NOT yet
    # carry usr/bin/python3.12 and the repo has a materialized python_overlay,
    # build_gpt_sdcard.sh --from resolves and merges python_overlay into
    # $STAGE_ROOTFS so usr/bin/python3.12 lands on P3.
    staging_no_py = _base_staging_without_daemon(tmp_path / "no-py")
    (staging_no_py / "rootfs" / "usr" / "bin" / "python3.12").unlink()
    ovl_py = clone / "sw" / "linux" / "pfsoc" / "python_overlay" / "usr" / "bin" / "python3.12"
    ovl_py.parent.mkdir(parents=True, exist_ok=True)
    ovl_py.write_bytes(b"ELF-python-overlay-stub")
    ovl_py.chmod(0o755)
    out_no_py = staging_no_py / "sdcard.img"
    proc_no_py = subprocess.run(
        [str(clone_job), "--from", str(staging_no_py), "--out", str(out_no_py)],
        cwd=str(clone), env=env, capture_output=True, text=True, check=False,
    )
    combined_no_py = proc_no_py.stdout + proc_no_py.stderr
    assert (
        proc_no_py.returncode == 0
        and out_no_py.is_file()
        and DAEMON_P3_MARKER in combined_no_py
        and "ROOTFS usr/bin/python3.12" in combined_no_py
    ), (
        "build_gpt_sdcard.sh --from did not emit a daemon-carrying GPT image "
        "when bridging a materialized python_overlay into a staging rootfs "
        f"without python3.12. rc={proc_no_py.returncode}\n{combined_no_py}"
    )


def test_pfsoc_gpt_build_job_runs_unskipped_in_desk_ci_and_precommit_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-T3.40: test_pfsoc_gpt_build_job_ds_p2_149 carries no skipif markers,
    fails closed if any required SD tool is absent, and is wired into both
    desk-checks CI and the DS-P2.197 pre-commit gate."""
    marks = getattr(test_from_fast_path_lands_daemon_on_p3, "pytestmark", [])
    assert marks == [], (
        "test_from_fast_path_lands_daemon_on_p3 carries "
        f"pytestmark={marks!r}; PolarFire GPT build job guard must not skip"
    )

    suite_rel = "tools/tests/test_pfsoc_gpt_build_job_ds_p2_149.py"
    exclusions = (ROOT / "tools" / "ci" / "desk_test_exclusions.txt").read_text(
        encoding="utf-8"
    )
    assert suite_rel not in exclusions, (
        f"{suite_rel} must not be excluded in tools/ci/desk_test_exclusions.txt"
    )
    runner = (ROOT / "tools" / "ci" / "run_desk_tests.sh").read_text(encoding="utf-8")
    assert "tools/tests" in runner, "tools/ci/run_desk_tests.sh must run tools/tests"

    for wf_rel in (".github/workflows/ci.yml", ".forgejo/workflows/ci.yml"):
        wf_text = (ROOT / wf_rel).read_text(encoding="utf-8")
        for pkg in ("mtools", "e2fsprogs", "fdisk", "rsync"):
            assert pkg in wf_text, f"{wf_rel} must install {pkg} for {suite_rel}"

    for missing in _REQUIRED_SD_TOOLS:
        monkeypatch.setattr(
            __import__(__name__),
            "_sd_tool",
            lambda tool, missing_tool=missing: None if tool == missing_tool else f"/usr/bin/{tool}",
        )
        caught: AssertionError | None = None
        try:
            _require_sd_tools()
        except AssertionError as exc:
            caught = exc
        assert caught is not None and (
            f"missing required offline SD tool {missing!r}" in str(caught)
        ), (
            f"missing {missing} did not fail with AssertionError: "
            "test_pfsoc_gpt_build_job_ds_p2_149 must fail, not skip (DS-T3.40)"
        )

    hook = (ROOT / "hooks" / "pre-commit.local").read_text(encoding="utf-8")
    m = re.search(
        r"^# ── DS-P2\.197 — the public CI workflow.*?^echo \"✓ Public CI",
        hook,
        re.M | re.S,
    )
    assert m is not None, "hooks/pre-commit.local has no DS-P2.197 public CI leg"
    assert suite_rel in m.group(0), (
        f"hooks/pre-commit.local DS-P2.197 leg omits {suite_rel}"
    )
