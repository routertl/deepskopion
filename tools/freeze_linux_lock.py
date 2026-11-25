#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Refresh / diff ``linux.lock.frozen`` without importing this bench.

Why this exists (DS-P1.27 / DS-X1)
----------------------------------
``linux.lock`` records ``build.kernel_initramfs_source`` verbatim from the
built kernel's ``.config``, where ``CONFIG_INITRAMFS_SOURCE`` is written as
an ABSOLUTE path into the build tree. The committed ``linux.lock.frozen``
must not name this bench, so the tracked value is project-root-relative.

Those two facts collide in the sanctioned refresh: ``cp out/linux.lock
linux.lock.frozen`` re-imports the absolute form, and the DS-X1 pre-commit
guard then refuses the commit. This tool is the refresh that does not:

    tools/freeze_linux_lock.py            # neutralize + write the frozen lock
    tools/freeze_linux_lock.py --check    # diff live vs frozen, prefix-blind

``--check`` neutralizes the live lock the same way before comparing, so a
machine prefix is not reported as drift while a real input change still is.

Known limitation, deliberately not worked around here: ``rr linux lock
--diff`` and ``rr doctor``'s frozen-lock gate compare the RAW live lock, so
they still report ``build.kernel_initramfs_source`` as drift on any machine
whose project root differs from the recorded relative form — i.e. all of
them. Relativizing that field belongs in routertl's ``build_lock`` (it
already does exactly this for ``build.kernel_config_fragments``), which is
not this repo's file. Until then ``--check`` is deepskopion's answer to "has
the Linux build drifted", and the rr-side delta on that one field is
expected rather than informative.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DEFAULT_LIVE = _REPO_ROOT / "sw" / "linux" / "out" / "linux.lock"
_FROZEN = _REPO_ROOT / "linux.lock.frozen"

_BANNER = (
    "# Frozen Linux build lock — the known-good provenance reference.\n"
    "#\n"
    "# Written by tools/freeze_linux_lock.py, which strips the project-root\n"
    "# prefix so no tracked file names the bench it was built on (DS-P1.27 /\n"
    "# DS-X1). Refresh with that tool, never `cp out/linux.lock` — a raw copy\n"
    "# re-imports absolute build-tree paths and the pre-commit guard will\n"
    "# refuse the commit. Diff with `tools/freeze_linux_lock.py --check`.\n"
    "#\n"
    "# `rr linux lock --diff` compares the RAW live lock, so it reports\n"
    "# build.kernel_initramfs_source as drift on every machine (the kernel\n"
    "# .config records it absolute). That one delta is the machine prefix,\n"
    "# not an input change; --check is prefix-blind and is the real answer.\n"
)


def neutralize(text: str, project_root: pathlib.Path | None = None) -> str:
    """Rewrite absolute paths under ``project_root`` to relative ones.

    The default resolves at CALL time, not at def time: a default argument
    bound to the module global would silently ignore any caller (or test)
    that points the tool at a different root.
    """
    root = str(_REPO_ROOT if project_root is None else project_root)
    return text.replace(root + "/", "").replace(root, ".")


def _load(path: pathlib.Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse as a YAML mapping")
    return data


def _deltas(current, frozen, path: tuple[str, ...] = ()) -> list[str]:
    """Field-by-field diff, ignoring the per-emit ``generated_at`` stamp."""
    if path == ("generated_at",):
        return []
    if isinstance(current, dict) and isinstance(frozen, dict):
        out: list[str] = []
        for key in sorted(set(current) | set(frozen)):
            out += _deltas(
                current.get(key, "<absent>"), frozen.get(key, "<absent>"),
                path + (str(key),),
            )
        return out
    if current == frozen:
        return []
    return [f"{'.'.join(path) or '<root>'}: live={current!r} frozen={frozen!r}"]


def _residual_home(text: str, home: str | None = None) -> str | None:
    """An absolute $HOME path the project-root strip could not neutralize.

    $HOME counts only as a whole path: with HOME=/root (a container run as
    root) the neutral ``sw/linux/out/rootfs`` must not match.
    """
    home = (home or str(pathlib.Path.home())).rstrip("/")
    if not home:
        return None
    pattern = r"(?<![\w./-])" + re.escape(home) + r"(?![\w.-])"
    return home if re.search(pattern, text) else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lock", type=pathlib.Path, default=_DEFAULT_LIVE,
                    help=f"live lock to read (default: {_DEFAULT_LIVE})")
    ap.add_argument("--frozen", type=pathlib.Path, default=_FROZEN,
                    help=f"frozen lock to write/compare (default: {_FROZEN})")
    ap.add_argument("--check", action="store_true",
                    help="compare instead of writing; exit 1 on drift")
    args = ap.parse_args(argv)

    if not args.lock.exists():
        print(f"❌ No live lock at {args.lock} — run `rr linux lock` first. "
              f"(Nothing was compared; this is not a pass.)", file=sys.stderr)
        return 2

    neutral = neutralize(args.lock.read_text(encoding="utf-8"))
    residual = _residual_home(neutral)
    if residual is not None:
        print(f"❌ The live lock names {residual} outside the project root, so "
              f"stripping the root prefix is not enough to neutralize it. "
              f"Fix the build input that recorded it; do not hand-edit the "
              f"frozen lock.", file=sys.stderr)
        return 2

    if not args.check:
        args.frozen.write_text(_BANNER + neutral, encoding="utf-8")
        print(f"🔒 Wrote {args.frozen.name} from {args.lock} "
              f"(project-root prefix stripped). Review and `git add` it.")
        return 0

    if not args.frozen.exists():
        print(f"❌ No {args.frozen.name} to compare against — create one by "
              f"running this tool without --check.", file=sys.stderr)
        return 2

    drift = _deltas(yaml.safe_load(neutral), _load(args.frozen))
    if not drift:
        print(f"✅ Live lock matches {args.frozen.name} — no drift.")
        return 0
    print(f"❌ Live lock drifted from {args.frozen.name} "
          f"({len(drift)} field(s)):", file=sys.stderr)
    for line in drift:
        print(f"   {line}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
