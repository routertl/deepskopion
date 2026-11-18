"""DS-T2.75 regression guard: the de25_datalogger ``rr_rea_build_id`` pre_build
hook must stamp the commit+target DESIGN IDENTITY into the REA BUILD_ID
register, NOT the REA source-hash — so ``rr hw bit-sha`` can reach CONFIRMED on
Agilex silicon instead of a guaranteed MISMATCH.

THE DEFECT (measured 2026-08-28, DS-P1.59): the hook ran with ``--ip-yml
libs/local/rr_rea/ip.yml``, so ``compute_rea_build_id_declared`` baked the REA
source-tree hash ``0x9406F737`` into BUILD_ID (0xD4). That value is a CONSTANT
across every design built on this REA fork version (the "design hash is
insufficient" trap, Haz.165 shape). But ``rr hw bit-sha``
(``sdk.engine.bit_sha.expected_rea_build_id``) expects the vendor-neutral
``identity_usr_access(git HEAD, project.name)`` — the same stamp the Xilinx leg
bakes into USR_ACCESS. Source-hash ≠ design-identity, so a REA readback could
only ever MISMATCH → UNCONFIRMED, blocking DS-P1.44 / DS-P1.59 clause 1.

THE FIX (this ticket): the hook runs ``--design-identity deepskopion --sidecar
build/de25_datalogger/bitstream/deepskopion.rea_build_id``. ``deepskopion`` is
``project.name`` — what ``rr hw bit-sha`` resolves ``name`` to
(``sdk.cli.commands.hw.hw_bit_sha``) — so the baked BUILD_ID equals what bit-sha
expects on BOTH the sidecar path and the git-head fallback. The target stem
``de25_datalogger`` would only self-match via the sidecar and MISMATCH the
fallback (deepskopion→0x3551f7f0 vs de25_datalogger→0x36a21337).

POWER (why this test is not a green rubber-stamp):
  * BASELINE — RED against the pre-fix yml (``--ip-yml …``): no
    ``--design-identity`` arg → the structural assertion fails, and the baked
    value would be the 0x9406F737 source-hash, which ``!= identity`` and
    ``== the source-hash`` — the reverse of both value assertions.
  * MUTATION — restoring ``--ip-yml`` (or setting ``--design-identity
    de25_datalogger``) turns this RED: the first via the missing-arg / wrong-name
    assertion, the second because 0x36a21337 ≠ the deepskopion identity bit-sha
    expects.

This is build-free: it runs the pre_build generator (a sub-second pure hash),
never a synth, and drives the exact ``bit_sha`` resolution surface
``rr hw bit-sha`` reaches — not a helper's private return value.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET_YML = ROOT / "targets/de25_datalogger.yml"
IP_YML = ROOT / "libs/routertl/rea/ip.yml"

# project.name — the value `rr hw bit-sha` resolves `name` to and therefore the
# value the hook MUST stamp (NOT the target stem "de25_datalogger").
PROJECT_NAME = "deepskopion"
# The Haz.165-shaped REA source-tree hash the OLD --ip-yml hook baked. It is a
# hash over the rr_rea ip.yml file list, so it moves whenever the REA RTL
# changes: DS-T2.75 added the BUILD_ID (0xD4) register + rr_rea_build_id_pkg
# `use` to rr_rea_regbank/rr_rea_pkg, so the pin moved 0x9406F737 -> 0x3E97B079.
# In routertl/rea v1.11.1 the source hash is 0x9E0AC225. The REA-P2.24
# re-vendor (30c53d04) moved it to 0xB02E4DFF and the 1.12.3 repin (a78f2598,
# DS-P2.193: rr_rea_cdc.vhd +2 pragma comments, ip.yml) to 0x8BC718C0; neither
# re-pinned it here, so the test stayed red until DS-P1.75.
# This canary asserts only that the source-hash is DISTINCT from the git-head
# design identity — the whole point of the fix; the exact value is incidental.
OLD_SOURCE_HASH = 0x8BC718C0  # routertl/rea 1.12.3 source-hash

_C_BUILD_ID_RE = re.compile(
    r'constant\s+C_REA_BUILD_ID\s*:.*?x"([0-9A-Fa-f]{8})"', re.DOTALL
)


def _repo_head() -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(ROOT), check=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return r.stdout.strip() or None


def _rea_build_id_hook_args() -> list[str]:
    target = yaml.safe_load(TARGET_YML.read_text(encoding="utf-8"))
    hooks = {h["name"]: h for h in target["hooks"]["pre_build"]}
    assert "rr_rea_build_id" in hooks, "de25_datalogger must keep the rr_rea_build_id hook"
    args = list(hooks["rr_rea_build_id"]["args"])
    assert args and args[0] == "rea-build-id", args
    return args[1:]  # drop the generator selector


def _flag_value(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def test_hook_is_wired_to_design_identity_not_source_hash():
    """Structural guard: the configured args select DESIGN identity for the
    project, and go RED the instant --ip-yml / --rtl-dir is restored."""
    args = _rea_build_id_hook_args()

    assert "--ip-yml" not in args, (
        "de25_datalogger rr_rea_build_id must NOT use --ip-yml — that bakes the REA "
        "source-hash (0x9406F737), a constant `rr hw bit-sha` can never confirm "
        "(DS-T2.75)."
    )
    assert "--rtl-dir" not in args, "the legacy glob mode is likewise a source-hash"

    dn = _flag_value(args, "--design-identity")
    assert dn == PROJECT_NAME, (
        f"--design-identity must be {PROJECT_NAME!r} (= project.name, what "
        f"`rr hw bit-sha` resolves `name` to), got {dn!r}. The target stem "
        f"'de25_datalogger' would MISMATCH the git-head fallback."
    )

    # The sidecar must land exactly where `expected_rea_build_id` reads it:
    # <output_dir>/bitstream/<project.name>.rea_build_id.
    sidecar = _flag_value(args, "--sidecar")
    assert sidecar == f"build/de25_datalogger/bitstream/{PROJECT_NAME}.rea_build_id", (
        f"--sidecar must be the path `rr hw bit-sha` reads "
        f"(<output_dir>/bitstream/{PROJECT_NAME}.rea_build_id), got {sidecar!r}."
    )


def test_baked_build_id_is_the_identity_bit_sha_expects_and_confirms():
    """POWER guard at the `rr hw bit-sha` surface: run the hook as configured,
    then prove the resolution `rr hw bit-sha` performs would CONFIRM — the baked
    BUILD_ID equals the identity bit-sha expects, matches the sidecar it reads,
    and is NOT the old source-hash."""
    from sdk.engine.bit_sha import (
        compare,
        design_build_id,
        expected_rea_build_id,
        identity_usr_access_v3,
        normalize_usr_access,
    )
    from sdk.scripts.rr_rea_build_id import compute_rea_build_id_declared, main

    head = _repo_head()
    if head is None:
        pytest.skip("no git HEAD available (design identity needs a commit hash)")

    args = _rea_build_id_hook_args()
    dn = _flag_value(args, "--design-identity")

    # Reproduce the exact output layout `rr hw bit-sha` inspects:
    # <output_dir>/bitstream/<project.name>.rea_build_id, sibling of the .sof.
    tmp = Path(pytest_tmp())
    bitstream_dir = tmp / "bitstream"
    bitstream_dir.mkdir(parents=True)
    out = tmp / "rr_rea_build_id_pkg.vhd"
    sidecar = bitstream_dir / f"{PROJECT_NAME}.rea_build_id"

    rc = main([
        "--design-identity", dn,
        "--git-head", head,
        "--sidecar", str(sidecar),
        "--out", str(out),
    ])
    assert rc == 0

    m = _C_BUILD_ID_RE.search(out.read_text(encoding="utf-8"))
    assert m, "generator did not emit a C_REA_BUILD_ID constant"
    baked = normalize_usr_access(m.group(1))

    # (1) the device value equals the identity `rr hw bit-sha` expects (clean
    #     stamp — the generator always bakes dirty=False).
    expected_identity = identity_usr_access_v3(head, PROJECT_NAME, dirty=False)
    assert baked == expected_identity, (
        f"baked BUILD_ID 0x{baked} != identity bit-sha expects 0x{expected_identity} "
        f"— a wrong --design-identity (e.g. the target stem) would land here."
    )

    # (2) it is NOT the Haz.165 source-hash the defect baked.
    src_hash = normalize_usr_access(compute_rea_build_id_declared(IP_YML))
    assert baked != src_hash, (
        f"baked BUILD_ID is still the REA source-hash 0x{src_hash} — the exact "
        f"defect DS-T2.75 fixes."
    )
    assert normalize_usr_access(OLD_SOURCE_HASH) == src_hash, (
        f"sanity: the REA source-hash is 0x{src_hash}, not the pinned "
        f"0x{normalize_usr_access(OLD_SOURCE_HASH)} — if you changed the rr_rea "
        f"RTL, re-pin OLD_SOURCE_HASH (it is a hash over the ip.yml file list)."
    )

    # (3) the resolution surface: `rr hw bit-sha` reads the sidecar we wrote and
    #     its verdict against the baked device value is MATCH (== CONFIRMED).
    exp, source = expected_rea_build_id(
        tmp, PROJECT_NAME, bitstream_dir=str(bitstream_dir)
    )
    assert source.startswith("sidecar:"), source
    assert exp == baked
    assert compare(exp, baked)["status"] == "match"


def test_hook_generates_on_a_fresh_worktree_without_git_head_flag():
    """Fresh-clone guard: the pre_build hook must generate the package on a
    build where ``generated/`` does not exist yet and no ``--git-head`` is
    passed — exactly the pre_commit/synth path on a fresh dispatch worktree.

    THE DEFECT (measured this session): ``--design-identity`` resolves the git
    HEAD by running ``git rev-parse HEAD`` with cwd = the ``--out`` PARENT dir,
    which the generator only creates AFTER that resolution. On a fresh worktree
    ``generated/`` is absent, so the subprocess fails and the hook dies with
    "--design-identity needs a git HEAD" — a fail-closed build the legacy
    ``--ip-yml`` mode never hit. ``tools/hooks/run_sdk_generator.py`` now
    pre-creates the ``--out`` parent so HEAD resolves inside the worktree.

    BASELINE: RED without the shim's mkdir (the generator raises SystemExit).
    MUTATION: removing the pre-create turns this RED again.

    This drives the SHIM the hook actually invokes, with a NON-EXISTENT out
    parent and NO --git-head — the real fresh-build surface."""
    import importlib.util

    from sdk.engine.bit_sha import design_build_id, normalize_usr_access

    head = _repo_head()
    if head is None:
        pytest.skip("no git HEAD available")

    spec = importlib.util.spec_from_file_location(
        "run_sdk_generator", ROOT / "tools/hooks/run_sdk_generator.py"
    )
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)

    tmp = Path(pytest_tmp())
    # a --out whose parent does NOT exist yet, INSIDE this git worktree (so the
    # generator's cwd-based HEAD resolution can find the repo once the parent
    # is created). Use a nested path under ROOT that we clean up.
    fresh_parent = ROOT / "generated" / f"_ds_t2_75_fresh_{tmp.name}"
    out = fresh_parent / "rr_rea_build_id_pkg.vhd"
    sidecar = fresh_parent / "deepskopion.rea_build_id"
    assert not fresh_parent.exists()
    # The expectation must be derived BEFORE the shim creates `fresh_parent`:
    # `generated/_ds_t2_75_fresh_*` is not gitignored, so once it holds files the
    # tree reads dirty and design_build_id folds in the dirty-tree inputs hash,
    # while the generator baked the value it saw (the tree as it was before).
    expected = design_build_id(ROOT, PROJECT_NAME)
    try:
        rc = shim.main([
            "rea-build-id",
            "--design-identity", PROJECT_NAME,
            "--sidecar", str(sidecar),
            "--out", str(out),
        ])
        assert rc == 0, "shim must generate on a fresh worktree without --git-head"
        assert out.is_file() and sidecar.is_file()
        m = _C_BUILD_ID_RE.search(out.read_text(encoding="utf-8"))
        assert m and normalize_usr_access(m.group(1)) == expected
    finally:
        import shutil
        shutil.rmtree(fresh_parent, ignore_errors=True)


def test_debug_core_yml_loads_and_declares_the_rea_node():
    """The other half `rr hw bit-sha` needs: a resolvable REA debug core.

    `rr hw bit-sha` (Altera leg) resolves the on-chip node through
    `sdk.cli.commands.ila._resolve_debug_core` → `_load_debug_cores` →
    `sdk.cli.debug_core_yml.resolve_debug_cores()`. If `debug/de25_datalogger.yml`
    is missing or malformed there is NO node to address and the readback can
    only ever MISMATCH → UNCONFIRMED. This drives that exact loader against the
    real project root and pins the silicon parameters `debug/de25_datalogger.yml`
    must carry, so it goes RED the instant the file is deleted or drifts from
    the `u_rea` instantiation in deepskopion_de25_top.vhd.

    BASELINE: RED before DS-T2.75 (no debug/de25_datalogger.yml existed →
    resolve_debug_cores() returns no 'de25_datalogger' core).
    MUTATION: `rm debug/de25_datalogger.yml` → RED (KeyError on the missing core);
    corrupting sample_width/depth/jtag_chain → RED on the pinned-value asserts;
    an invalid type/HW-key → RED via DebugCoreError out of the loader itself."""
    from sdk.cli.debug_core_yml import resolve_debug_cores

    # The same resolution surface `rr hw bit-sha`/`rr ila` walk — project.yml
    # load + debug/ walk + HW-context merge — anchored at the real repo root.
    cores = resolve_debug_cores(project_root=ROOT)
    by_name = {c.get("name"): c for c in cores}
    assert "de25_datalogger" in by_name, (
        "debug/de25_datalogger.yml did not resolve to a debug core — `rr hw "
        f"bit-sha` would have no REA node to read. Resolved: {sorted(by_name)}"
    )
    core = by_name["de25_datalogger"]

    assert str(core.get("type", "")).lower() == "rea", (
        f"de25_datalogger core must be type 'rea', got {core.get('type')!r}"
    )
    # Pinned to the `u_rea` generic map (deepskopion_de25_top.vhd): a
    # sample_width/depth mismatch is a hard REAClient error (rea-issp Rung 4),
    # so drift here would break the live readback even with the file present.
    assert core.get("sample_width") == 32, core.get("sample_width")
    assert core.get("depth") == 4096, core.get("depth")
    assert core.get("jtag_chain") == 1, core.get("jtag_chain")

    # The HW context `rr ila identity` routes the SDM-JTAG transport from must
    # have merged in from project.yml/targets (vendor=altera, A5E… part →
    # part_is_sdm_jtag). A bare parse without this merge would leave them empty.
    assert str(core.get("vendor", "")).lower() in ("altera", "intel"), core.get("vendor")
    assert str(core.get("part", "")).startswith("A5E"), core.get("part")


def pytest_tmp() -> str:
    """A unique temp dir (avoids depending on the tmp_path fixture wiring so this
    file is copy-portable into other harnesses)."""
    import tempfile
    return tempfile.mkdtemp(prefix="ds_t2_75_")
