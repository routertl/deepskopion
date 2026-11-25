# rr unknown-llm (AI-generated)
"""DS-P1.96: build-metadata dirt does not make the REA design stamp dirty.

regbank-regen rewrites the tracked ``hw/rtl/pkg/rom_info_pkg.vhd`` on every
build, but only the lines ``tools/generated_sources.yml`` declares as
``volatile_lines`` (file date, git hash, system version, build date: "the
build invocation, not the generated design"). DE-G0 builds in two queued rr
invocations, synth then bitstream, and the ``rr_rea_build_id`` pre_build hook
runs in both. The second saw the rewritten file as a dirty tree and stamped a
different BUILD_ID than the netlist carries, so `rr hw bit-sha` reported a
MISMATCH on a board running the right build (DS-P2.164 run 4, 2026-11-07).

:func:`stamp_clean_if_only_build_metadata_changed` is called by
``tools/hooks/run_sdk_generator.py`` before rr's generator. When the worktree
differs from HEAD ONLY in declared volatile lines it writes rr's own clean
design identity, ``compute_design_identity(head, key, dirty=False)``, in rr's
formats. Any other change (another line of that file, any other file, an
untracked file, a rename) returns None and rr's dirty stamp applies.

DS-P2.231 made rom_info_pkg.vhd a generated, gitignored file, so a build no
longer dirties the tree: git status never lists it, and this policy finds
nothing to excuse (it returns None on a clean tree, and on any tracked dirt
that is not a declared volatile line). It is kept, deliberately, as a narrow
guard: tools/generated_sources.yml still declares volatile_lines (for the
within-build stage stamp), and should a declared volatile file ever be tracked
again the stamp stays the clean design identity instead of splitting across
queued stages. Its call site is tools/hooks/run_sdk_generator.py, gate
machinery that the owner edited for DS-P1.96; removing a call that is inert
does not justify reopening that guard.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent


def _git_env() -> dict:
    # A hook run inside a git hook inherits GIT_DIR/GIT_INDEX_FILE; drop them so
    # git answers for the project's own worktree.
    return {k: v for k, v in os.environ.items()
            if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}


def _contract():
    sys.path.insert(0, str(_TOOLS))
    try:
        return importlib.import_module("generated_source_contract")
    finally:
        sys.path.pop(0)


def only_build_metadata_changed(root: str | Path) -> bool:
    """True when the worktree differs from HEAD ONLY in declared volatile lines."""
    root = Path(root)
    env = _git_env()
    status = subprocess.run(["git", "status", "--porcelain", "-z"], cwd=root,
                            env=env, capture_output=True)
    if status.returncode != 0:
        return False
    entries = [e for e in status.stdout.decode("utf-8", "replace").split("\0") if e]
    if not entries:
        return False
    contract = _contract()
    manifest = contract.load_manifest()
    volatile = set(manifest.get("volatile_lines") or {})
    for entry in entries:
        code, rel = entry[:2], entry[3:]
        if code not in (" M", "M ", "MM") or rel not in volatile:
            return False
        head = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=root, env=env,
                              capture_output=True)
        if head.returncode != 0:
            return False
        try:
            work = (root / rel).read_bytes()
            if (contract.canonical_bytes(rel, head.stdout, manifest)
                    != contract.canonical_bytes(rel, work, manifest)):
                return False
        except (OSError, contract.ContractError):
            return False
    return True


def _arg(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv[:-1] else None


def stamp_clean_if_only_build_metadata_changed(mod, argv: list[str]) -> int | None:
    """Write the clean design identity when the only dirt is build metadata.

    *mod* is ``sdk.scripts.rr_rea_build_id``; *argv* its arguments. Returns 0
    after writing, or None to let rr's generator run unchanged.
    """
    if "--design-identity" not in argv or "--out" not in argv:
        return None
    out = Path(_arg(argv, "--out"))
    root = mod._design_project_root(out, _arg(argv, "--project-root"))
    if not only_build_metadata_changed(root):
        return None
    bit_sha = importlib.import_module("sdk.engine.bit_sha")
    head = _arg(argv, "--git-head")
    if not head:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=_git_env(),
                           capture_output=True, text=True)
        head = r.stdout.strip() if r.returncode == 0 else ""
    key = bit_sha.usr_access_target_key(root, _arg(argv, "--design-identity"))
    if not head or not key:
        return None  # rr's generator reports the missing HEAD/key itself
    value = mod.compute_design_identity(head, key, dirty=False)
    text = mod.render_package(value)
    out.parent.mkdir(parents=True, exist_ok=True)
    changed = not (out.is_file() and out.read_text(encoding="utf-8") == text)
    if changed:
        out.write_text(text, encoding="utf-8")
    sidecar = _arg(argv, "--sidecar")
    if sidecar:
        Path(sidecar).parent.mkdir(parents=True, exist_ok=True)
        Path(sidecar).write_text(f"{value:08x}", encoding="utf-8")
    print(f"rr_rea_build_id: C_REA_BUILD_ID = 0x{value:08X} → {out} "
          f"({'written' if changed else 'unchanged'})"
          + (f"; sidecar → {sidecar}" if sidecar else "")
          + " — clean identity: the only changes are declared volatile build "
            "metadata (DS-P1.96)")
    return 0
