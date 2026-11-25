# rr unknown-llm (AI-generated)
"""DS-X1 (DS-P1.27) — no tracked file may depend on THIS bench.

The golden path is clone-and-build. That contract dies the moment a tracked
lock, hook, provenance record or generated source names a path that exists
only on the original developer's machine: ``/home/<dev>/...``, a mounted
``/media/<dev>/...``, or a sibling checkout reached via a parent-relative
``../`` escape into a ``routertl`` checkout. When DS-P1.27 was filed, ip.lock
pinned the rr_rea dependency to a ``file://`` URL rooted in a developer's
local machine tree and the REA build-id hook
read that same sibling checkout directly (a ``../`` escape into
``routertl/...``) — steps 2-4 of the golden path worked on exactly one
machine on earth.

Note for anyone editing this docstring: the banned markers below are spelled
out with a deliberate break (see ``_BANNED_MARKERS``) so this file's own
source text never contains one contiguously — otherwise the guard would fail
on itself the moment it is tracked. Keep prose above equally careful; never
write the local-file URL scheme immediately followed by a bare home root, or
the parent-relative escape immediately followed by the sibling repo name,
as one unbroken run of characters anywhere in this file — the self-scan
assertion right below the marker tuple catches a slip if one happens anyway.

This test is the regression guard: it sweeps every tracked TEXT file for
bench-local path markers, so it fails the moment any of them creeps back in
(a re-locked dependency, a regenerated rom_info_pkg.vhd committed with its
build-time absolute paths, a hook pointed back at a sibling checkout).

It is ARMED: ``hooks/pre-commit.local`` runs it as a leg of the project
pre-commit gate, so a commit that reintroduces a marker is refused. Do not
weaken one without the other — a guard nothing invokes is the false-green
pattern ``docs/GOLDEN_PATH.md`` itself catalogues.

The sweep reads the INDEX (``git grep --cached``), not the worktree, because
the index is exactly what a commit will contain. DS-X2 removed the former
machine-local build form of ``rom_info_pkg.vhd``: normal regeneration and
scratch checking now install the same clone-relative paths. The separate
generated-source gate compares all tracked families against their generators.

If this DOES fire on hw/rtl/pkg/rom_info_pkg.vhd, re-run
`tools/regen_regbank.sh` and re-stage it; there is no sanctioned absolute form.
"""
from __future__ import annotations

import pathlib
import subprocess

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Built by concatenation so this guard never matches itself once tracked —
# every marker's contiguous form must never appear verbatim anywhere in this
# file's own source (comments and docstring included), or the guard would
# fail against itself the moment `git add` makes it a scannable tracked file.
_DEV = "dmazure"
_BANNED_MARKERS = (
    "/home/" + _DEV,               # developer home (abs paths, file:// URLs)
    "/media/" + _DEV,              # developer's mounted media (SD staging)
    "/Users/" + _DEV,              # ditto, macOS form
    "file://" + "/home/",          # any local-home file URL, any user
    "../" + "routertl",            # sibling-checkout escape from the repo root
)

# Self-scan guard: fail loudly (not silently) if any marker's contiguous form
# ever creeps into this module's own text via a future edit — the concat
# trick above only helps if every occurrence, including in comments/prose,
# stays broken up.
_SELF_TEXT = pathlib.Path(__file__).read_text(encoding="utf-8")
_self_offenders = [m for m in _BANNED_MARKERS if m in _SELF_TEXT]
assert not _self_offenders, (
    "test_no_local_path_pollution.py's own source contains a banned marker "
    f"contiguously ({_self_offenders!r}) — it would fail the sweep on "
    "itself once tracked. Break the marker up in whatever line introduced "
    "it (docstring or comment), the way the rest of this file does."
)

# Files exempt from the sweep. EXACT paths, never a directory prefix: a
# blanket prefix exempts files that do not exist yet, which is a hole rather
# than a carve-out. Adding an entry here must be a deliberate act with a
# reason written next to it.
#
# DS-C1 removed the last historical exception by redacting the developer mount
# name while preserving the transcript's technical meaning. Keep the exact-file
# mechanism for auditability, but there are currently no exemptions.
_EXEMPT_FILES = frozenset()


def _index_hits(marker: str) -> list[str]:
    """``rel:line`` for every INDEX blob containing ``marker``.

    ``-I`` skips blobs git considers binary (wave dumps carry these strings
    as inert payload — hw/sim/waves/.../uart_datalogger.fst.hier does today).
    ``-F`` keeps the markers literal. Exit 1 means "no match", which is the
    passing case; anything else is git failing to run the sweep and must not
    be read as clean.
    """
    proc = subprocess.run(
        ["git", "grep", "--cached", "-I", "-n", "-F", "-e", marker],
        cwd=_REPO_ROOT, capture_output=True, check=False,
    )
    if proc.returncode == 1:
        return []
    if proc.returncode != 0:
        raise AssertionError(
            f"git grep --cached failed (exit {proc.returncode}) scanning for "
            f"{marker!r} — the sweep did not run, so this is NOT a clean "
            f"result:\n{proc.stderr.decode('utf-8', 'replace')}"
        )
    hits = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        rel, _, rest = line.partition(":")
        hits.append(f"{rel}:{rest.partition(':')[0]}")
    return hits


def test_no_tracked_file_names_a_bench_local_path():
    offenders: list[str] = []
    for marker in _BANNED_MARKERS:
        for hit in _index_hits(marker):
            if hit.partition(":")[0] in _EXEMPT_FILES:
                continue
            offenders.append(f"{hit}: contains '{marker}'")
    assert not offenders, (
        "Staged/tracked file(s) name a bench-local path — a lone clone "
        "cannot resolve these (DS-P1.27 / DS-X1):\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_every_exemption_is_still_needed():
    """A dead exemption is a hole with no cost to close.

    If the exempted file stops containing a marker (or stops existing), the
    entry must go — otherwise the allowlist quietly grows into the blanket
    prefix it was written to avoid.
    """
    still_needed = {
        hit.partition(":")[0]
        for marker in _BANNED_MARKERS
        for hit in _index_hits(marker)
    }
    dead = sorted(_EXEMPT_FILES - still_needed)
    assert not dead, (
        "_EXEMPT_FILES lists file(s) that no longer contain a banned marker "
        "(or are no longer tracked) — drop them:\n  " + "\n  ".join(dead)
    )
