#!/usr/bin/env python3
"""SW-C0 (DS-P1.46) — the executable half of the Linux deployment contract.

`sw/linux/deployment_contract.yml` declares how deepskopion software reaches a
board and starts there. This script CHECKS that the repo still matches it, and
it is wired into `sw/linux/stage_daemon.sh`, which every Linux target runs as
its `linux.rootfs.stage_command` — so an overlay that disagrees with the
declaration fails the image build instead of shipping.

The defect it exists to catch is not hypothetical and not "docs rot". The repo
carried, simultaneously: an init script in `rootfs_overlay/`, two systemd units
in `docs/` labelled examples, and a design decision (DS-D.7) recording that
BOTH were staged in the overlay. Before that they really were both in the
overlay, so `rr linux rootfs` copied a systemd unit into every image built —
into a rootfs with no systemd in it (DS-P2.82) — and the operator instructions
told people to read the daemon's output with `journalctl`, a command that
cannot work on any image this repo builds.

What is checked, and why each one can actually fail:

1.  **overlay ↔ declaration.** The set of autostart-shaped files present under
    the overlay must EQUAL the declared set. Not "contains" — equal. A file
    that starts something at boot and is not declared is the exact shape of the
    stowaway unit; a declared file that is missing means the image will not
    start what the document promises.
2.  **doc ↔ declaration.** The autostart section of
    `docs/DEPLOYMENT_CONTRACT.md` is generated from the YAML between
    markers. Any hand-edit or stale value fails, with `--write` as the fix.
3.  **targets ↔ declaration.** Every target that builds a Linux rootfs must be
    listed, and every listed target must exist. A new Linux target added
    without a line here is a board whose boot behaviour nobody declared.
4.  **provenance.** Every file under the overlay is either tracked (source) or
    gitignored (staged artefact). Neither = the state in which a build artefact
    gets swept into the next broad `git add` and becomes a second source of
    truth — how the staged daemon tree stayed committed for months (DS-P2.81).
5.  **prohibited mechanisms.** Forbidden directories may not exist in the
    overlay, and the FENCED CODE BLOCKS of the bound docs — the lines a reader
    copies and runs — may not invoke a forbidden command. Prose may discuss
    systemd; a copyable `journalctl -u deepskopion-stream` is the defect.

No check "skips". A check that cannot be evaluated is a failure with the reason
as its message: a contract gate that goes green because it could not look is
the false green this repo has already been bitten by (see docs/GOLDEN_PATH.md).

CLI:
    python3 sw/linux/deployment_contract.py [--check]     # exit 0 iff clean
    python3 sw/linux/deployment_contract.py --write       # regenerate the doc
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
#: Repo-relative, so every message reads the same whether the check runs
#: against the real checkout or a throwaway copy of it (the tests plant
#: defects in a copy; a message that resolved paths against the real ROOT
#: would crash there, which is a guard that only works on a clean tree).
DECLARATION_REL = "sw/linux/deployment_contract.yml"
DOC_REL = "docs/DEPLOYMENT_CONTRACT.md"
DECLARATION = ROOT / DECLARATION_REL
DOC = ROOT / DOC_REL

BEGIN_MARK = "<!-- BEGIN GENERATED: autostart — do not edit, see sw/linux/deployment_contract.yml -->"
END_MARK = "<!-- END GENERATED -->"


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------
def load_declaration(path: Path = DECLARATION) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass(frozen=True)
class Failure:
    """A named check failure. `what` is the thing that broke, not a category."""
    what: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - formatting
        return f"{self.what}: {self.detail}"


# ---------------------------------------------------------------------------
# What counts as "this file starts something at boot"
# ---------------------------------------------------------------------------
#: Path shapes, relative to the overlay root, that an init system would execute
#: at boot. Kept deliberately wider than the mechanism we use: the point is to
#: notice a mechanism arriving that nobody declared, so it must recognise the
#: shapes of init systems we do NOT run.
_AUTOSTART_EXACT = {"init", "linuxrc", "etc/inittab", "etc/rc.local"}
_AUTOSTART_PREFIXES = (
    "etc/init.d/",
    "etc/init/",
    "etc/rc.d/",
    "etc/rcS.d/",
    "etc/systemd/",
    "usr/lib/systemd/",
    "lib/systemd/",
    "etc/xdg/autostart/",
    "etc/s6/",
    "etc/runit/",
)


def is_autostart_shaped(rel: str) -> bool:
    return rel in _AUTOSTART_EXACT or rel.startswith(_AUTOSTART_PREFIXES)


# ---------------------------------------------------------------------------
# git helpers (the provenance rule is defined in terms of git, so ask git)
# ---------------------------------------------------------------------------
def _git(root: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, input=stdin,
        capture_output=True, text=True, check=False,
    )


def _tracked(root: Path, overlay_rel: str) -> set[str]:
    out = _git(root, "ls-files", "--", overlay_rel).stdout
    prefix = overlay_rel.rstrip("/") + "/"
    return {line[len(prefix):] for line in out.splitlines() if line.startswith(prefix)}


def _ignored(root: Path, overlay_rel: str, rels: list[str]) -> set[str]:
    """Subset of `rels` that .gitignore matches. Batched — one git call."""
    if not rels:
        return set()
    paths = [f"{overlay_rel}/{r}" for r in rels]
    res = _git(root, "check-ignore", "--stdin", stdin="\n".join(paths) + "\n")
    # rc 0 = some ignored, 1 = none ignored, >1 = error (a real failure, not a
    # quiet empty set: silently treating an error as "nothing is ignored" would
    # turn this guard into noise and then into a disabled guard).
    if res.returncode > 1:
        raise RuntimeError(f"git check-ignore failed: {res.stderr.strip()}")
    prefix = overlay_rel.rstrip("/") + "/"
    return {line[len(prefix):] for line in res.stdout.splitlines() if line.startswith(prefix)}


def _on_disk(overlay: Path) -> list[str]:
    return sorted(
        str(p.relative_to(overlay))
        for p in overlay.rglob("*")
        if p.is_file() or p.is_symlink()
    )


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_overlay(decl: dict, root: Path = ROOT) -> list[Failure]:
    """1, 4, 5a — the overlay on disk against the declaration."""
    fails: list[Failure] = []
    overlay_rel = decl["overlay_root"]
    overlay = root / overlay_rel
    if not overlay.is_dir():
        return [Failure(overlay_rel, "overlay root does not exist — nothing to check")]

    declared = {e["path"]: e for e in decl["autostart"]}
    present = _on_disk(overlay)
    found = {rel for rel in present if is_autostart_shaped(rel)}

    for rel in sorted(found - set(declared)):
        fails.append(Failure(
            f"{overlay_rel}/{rel}",
            "starts something at boot but is NOT declared in "
            f"{DECLARATION_REL}. Every image built from this "
            "overlay would run it while the deployment contract says it does "
            "not exist. Declare it, or delete it.",
        ))
    for rel in sorted(set(declared) - found):
        fails.append(Failure(
            f"{overlay_rel}/{rel}",
            "declared as an autostart file but missing from the overlay — the "
            "contract promises a boot step no image performs.",
        ))

    # DS-P2.262: bytecode is never part of an image. rr linux rootfs walks every
    # file, so a host-made __pycache__ in the staged tree ships, ignored or not.
    for rel in present:
        if rel.endswith(".pyc") or "/__pycache__/" in f"/{rel}":
            fails.append(Failure(
                f"{overlay_rel}/{rel}",
                "is bytecode in the overlay; rr linux rootfs would bake it into "
                "the image. sw/linux/stage_daemon.sh removes it under "
                "usr/local/share/deepskopion; elsewhere delete it. Then find what "
                "imported the overlay without -B.",
            ))

    # provenance: tracked = source, ignored = artefact, neither = a mistake.
    # The rule is defined in terms of git, so no git means the rule cannot be
    # evaluated — say that once, instead of reporting every file in the overlay
    # as unclassifiable and burying the real reason.
    if _git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        fails.append(Failure(
            str(root),
            "not a git work tree, so source cannot be told from staged "
            "artefact. Deepskopion is deployed by cloning it; a checkout "
            "without git history cannot be checked against this contract.",
        ))
        return fails

    tracked = _tracked(root, overlay_rel)
    unaccounted = [rel for rel in present if rel not in tracked]
    for rel in sorted(set(unaccounted) - _ignored(root, overlay_rel, unaccounted)):
        fails.append(Failure(
            f"{overlay_rel}/{rel}",
            "is neither tracked nor gitignored, so a reader cannot tell source "
            "from staged artefact and the next broad `git add` commits it "
            f"(DS-P2.81). Rule: {decl['provenance_rule']}",
        ))
    # owner-supplied private files (DS-P2.260): never tracked, and ignored by
    # an entry naming exactly the path. A wider pattern (the directory, a glob)
    # would also hide any other file placed next to it from the rule above.
    for entry in decl.get("owner_supplied") or []:
        rel = f"{overlay_rel}/{entry['path']}"
        if entry["path"] in tracked:
            fails.append(Failure(
                rel,
                "is an owner-supplied private file but git tracks it: it was "
                "committed, so every clone and every public image built from "
                "the tree carries it. Remove it from the index (git rm --cached).",
            ))
        res = _git(root, "check-ignore", "-v", "--no-index", rel)
        pattern = res.stdout.split("\t", 1)[0].split(":", 2)[-1] if res.returncode == 0 else None
        if pattern not in (rel, "/" + rel):
            fails.append(Failure(
                rel,
                "is declared owner-supplied, so .gitignore must match exactly "
                f"this path; the matching pattern is {pattern!r}. Add the exact "
                "path and remove any wider pattern.",
            ))
    for entry in decl["autostart"]:
        want_tracked = entry["provenance"] == "tracked"
        if want_tracked and entry["path"] not in tracked:
            fails.append(Failure(
                f"{overlay_rel}/{entry['path']}",
                "declared `provenance: tracked` but git does not track it — a "
                "boot step that exists only on this machine.",
            ))

    # prohibited mechanism directories
    for mech in decl["prohibited"]:
        for bad in mech.get("forbidden_paths") or []:
            if (overlay / bad).exists():
                fails.append(Failure(
                    f"{overlay_rel}/{bad}",
                    f"{mech['mechanism']} is not used by any image this repo "
                    f"builds ({mech['reason'].strip()}), yet the overlay has "
                    "this path — it would be baked into the initramfs.",
                ))
    return fails


_TOOL_IMAGE_KEYS = {"target", "overlay", "pid1", "autostart"}


def _tool_images(decl: dict, root: Path, source: str = "") -> tuple[set, list[Failure]]:
    """The declared bench tool images (DS-P2.264), and what is wrong with them.

    Each has exactly the keys target/overlay/pid1/autostart; its PID 1 file
    exists in its overlay; ``autostart`` is ``none`` and the overlay carries no
    autostart-shaped file (etc/init.d/S*, etc/rc*.d/, etc/inittab).
    """
    names: set = set()
    fails: list[Failure] = []
    for i, entry in enumerate(decl.get("tool_images") or []):
        where = f"{source + ': ' if source else ''}tool_images[{i}]"
        if not isinstance(entry, dict) or set(entry) != _TOOL_IMAGE_KEYS:
            fails.append(Failure(where, f"must have exactly the keys {sorted(_TOOL_IMAGE_KEYS)}"))
            continue
        name = str(entry["target"])
        names.add(name)
        overlay = root / str(entry["overlay"])
        if entry["autostart"] != "none":
            fails.append(Failure(f"{where} {name}", "autostart must be 'none': a tool "
                                 "image starts only its own PID 1"))
        if not (overlay / str(entry["pid1"])).is_file():
            fails.append(Failure(f"{where} {name}", f"PID 1 file "
                                 f"{entry['overlay']}/{entry['pid1']} does not exist"))
        if overlay.is_dir():
            starters = sorted(
                p.relative_to(root).as_posix() for p in overlay.rglob("*")
                if (p.parent.name == "init.d" and p.name.startswith("S"))
                or p.parent.name.startswith("rc") and p.parent.name.endswith(".d")
                or p.name == "inittab")
            for rel in starters:
                fails.append(Failure(rel, f"tool image {name} declares autostart: none, "
                                     "yet its overlay carries an autostart file"))
    return names, fails


#: DS-P1.103: the bench targets (bench/targets/, dropped from the public export)
#: declare their boot behaviour here, beside them, so the public contract names
#: only what ships.
BENCH_SIDECAR = Path("bench") / "deployment_contract.yml"


#: Where target files live, products first. The same two directories as
#: tools/target_files.py (PRODUCTS_DIR, BENCH_DIR); this checker also runs from
#: sandboxes that carry no tools/, so it keeps the pair itself and
#: tools/tests/test_target_files_ds_p1_103.py pins it to the helper.
TARGET_DIRS = (Path("targets"), Path("bench") / "targets")


def _target_files(root: Path) -> list[Path]:
    """Every target file, products and bench (DS-P1.103)."""
    return [p for d in TARGET_DIRS for p in sorted((root / d).glob("*.yml"))]


def _sidecars(root: Path) -> list[Path]:
    found = sorted(root.glob("sw/linux/*/deployment_contract.yml"))
    if (root / BENCH_SIDECAR).is_file():
        found.append(root / BENCH_SIDECAR)
    return found


def all_tool_images(decl: dict, root: Path = ROOT) -> tuple[set, list[Failure]]:
    """Tool images declared by the public contract and by every sidecar (DS-P1.103: the
    bench ones live in bench/deployment_contract.yml), and what is wrong with them."""
    names, fails = _tool_images(decl, root)
    for sidecar in _sidecars(root):
        extra = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
        more, more_fails = _tool_images(extra, root, sidecar.relative_to(root).as_posix())
        names |= more
        fails.extend(more_fails)
    return names, fails


def check_targets(decl: dict, root: Path = ROOT) -> list[Failure]:
    """3 — declared Linux targets against every target file (targets/*.yml and bench/targets/*.yml).

    Parsed as text, deliberately: this guard must not go quiet because a YAML
    dependency is missing somewhere, and what is asserted is the literal
    presence of a `rootfs:` declaration.
    """
    fails: list[Failure] = []
    where_is = {
        yml.stem: yml.relative_to(root).as_posix() for yml in _target_files(root)
        if "rootfs:" in yml.read_text(encoding="utf-8", errors="replace")
    }
    with_rootfs = set(where_is)
    declared = set(decl["boot"]["targets"])
    tools, tool_fails = _tool_images(decl, root)
    fails.extend(tool_fails)
    for sidecar in _sidecars(root):
        extra = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
        where = sidecar.relative_to(root).as_posix()
        if not extra.get("targets"):
            fails.append(Failure(
                where,
                "declares no targets — a contract sidecar exists only to "
                "declare the targets of a board kept out of the public export.",
            ))
            continue
        declared.update(extra["targets"])
        side_tools, side_fails = _tool_images(extra, root, where)
        overlap = tools & side_tools
        for name in sorted(overlap):
            fails.append(Failure(where, f"tool image {name} is declared twice"))
        tools |= side_tools
        fails.extend(side_fails)
    if not with_rootfs:
        return [Failure("targets/", "no target declares a linux rootfs — either "
                                    "they moved or this check is looking in the "
                                    "wrong place; either way it guards nothing")]
    for name in sorted(declared & tools):
        fails.append(Failure(
            where_is.get(name, f"targets/{name}.yml"),
            "is declared both in boot.targets and in tool_images — a target "
            "boots one way; declare it in exactly one.",
        ))
    for name in sorted(with_rootfs - declared - tools):
        fails.append(Failure(
            where_is[name],
            "builds a Linux rootfs but is not listed in boot.targets — a board "
            "whose boot behaviour the deployment contract does not describe.",
        ))
    for name in sorted(tools - with_rootfs):
        fails.append(Failure(
            f"tool_images: {name}",
            "declared as a tool image but no target file builds such a rootfs — the "
            "contract describes an image that is not built here.",
        ))
    for name in sorted(declared - with_rootfs):
        fails.append(Failure(
            f"boot.targets: {name}",
            "declared as a Linux target but no target file builds such a rootfs — the "
            "contract describes a board that is not built here.",
        ))
    return fails


_FENCE = re.compile(r"^\s*```")


def _fenced_command_lines(text: str) -> list[tuple[int, str]]:
    """Lines inside fenced code blocks — the ones a reader copies and runs."""
    out: list[tuple[int, str]] = []
    inside = False
    for n, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            inside = not inside
            continue
        if inside:
            out.append((n, line))
    return out


def check_bound_docs(decl: dict, root: Path = ROOT) -> list[Failure]:
    """5b — no bound doc may hand a reader a command from a forbidden flow."""
    fails: list[Failure] = []
    forbidden = {
        cmd: mech["mechanism"]
        for mech in decl["prohibited"]
        for cmd in mech.get("forbidden_commands") or []
    }
    for rel in decl["docs_bound_by_this_contract"]:
        path = root / rel
        if not path.is_file():
            fails.append(Failure(rel, "bound by this contract but does not exist"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in _fenced_command_lines(text):
            for cmd, mechanism in forbidden.items():
                if re.search(rf"(?<![\w./-]){re.escape(cmd)}(?![\w-])", line):
                    fails.append(Failure(
                        f"{rel}:{lineno}",
                        f"a copyable command invokes `{cmd}` ({mechanism} is not "
                        "used by any image this repo builds), so a reader who "
                        f"runs it gets nothing: {line.strip()!r}",
                    ))
    return fails


# ---------------------------------------------------------------------------
# The generated section of the prose document
# ---------------------------------------------------------------------------
def render_doc_section(decl: dict) -> str:
    boot, daemon = decl["boot"], decl["daemon"]
    lines = [
        BEGIN_MARK,
        "",
        f"**Init system: `{boot['init_system']}`. There is exactly one, on every "
        f"target that boots Linux ({', '.join(boot['targets'])}).**",
        "",
        f"PID 1 is `{decl['overlay_root']}/{boot['pid1']}`, from the overlay. "
        + " ".join(boot["runner"].split()),
        "",
        "| file in the overlay | tracked / staged | what it starts | why it is where it is |",
        "|---|---|---|---|",
    ]
    for e in decl["autostart"]:
        note = " ".join(e["note"].split())
        lines.append(
            f"| `{e['path']}` | {e['provenance']} | {e['starts']} | {note} |"
        )
    lines += [
        "",
        f"The daemon's source is **`{daemon['source']}/`** — the only on-disk "
        f"truth. `{daemon['staged_by']}` rsyncs it into "
        f"`{daemon['staged_to']}/` at `{daemon['staged_at']}` time; that copy is "
        f"**{daemon['provenance']}**, so it is gitignored and a fresh clone does "
        "not have it.",
        "",
        f"- `--source` comes from {daemon['source_selection']}.",
        f"- Listens on `{daemon['listen']}`; log at `{daemon['logs']}`; "
        f"pidfile `{daemon['pidfile']}`.",
        "",
        "Other **staged artefacts** in the overlay — gitignored, regenerated by "
        "their script, never edited in place:",
        "",
    ]
    for b in decl["staged_binaries"]:
        lines.append(f"- `{b['staged_to']}` ← `{b['staged_by']}`")
    owner = decl.get("owner_supplied") or []
    if owner:
        lines += [
            "",
            "**Owner-supplied private files** — placed by the image owner for a "
            "private build, gitignored by their exact path, never committed, "
            "never in a public image:",
            "",
        ]
        for e in owner:
            lines.append(f"- `{e['path']}` (read by `{e['read_by']}`) — "
                         f"{' '.join(e['note'].split())}")
    lines += [
        "",
        "**Not autostarted** (stated so \"there is no unit for it\" is an answer, "
        "not a gap):",
        "",
    ]
    for e in decl["not_autostarted"]:
        lines.append(f"- `{e['component']}` — {e['status']}. {' '.join(e['note'].split())}")
    lines += [
        "",
        "**Mechanisms this repo does not use:**",
        "",
    ]
    for m in decl["prohibited"]:
        lines.append(f"- **{m['mechanism']}** — {' '.join(m['reason'].split())}")
    lines += [
        "",
        f"Telling source from artefact, for any file under the overlay: "
        f"{' '.join(decl['provenance_rule'].split())}",
        "",
        END_MARK,
    ]
    return "\n".join(lines)


def _split_doc(text: str, rel: str) -> tuple[str, str, str]:
    if BEGIN_MARK not in text or END_MARK not in text:
        raise RuntimeError(
            f"{rel} has lost its generated markers — the prose and "
            f"{Path(DECLARATION_REL).name} can no longer be compared, which is the drift "
            "this contract exists to prevent"
        )
    head, rest = text.split(BEGIN_MARK, 1)
    _body, tail = rest.split(END_MARK, 1)
    return head, _body, tail


def check_doc(decl: dict, root: Path = ROOT) -> list[Failure]:
    """2 — the prose against the declaration."""
    doc = root / DOC_REL
    rel = DOC_REL
    if not doc.is_file():
        return [Failure(rel, "the deployment contract document does not exist")]
    text = doc.read_text(encoding="utf-8")
    try:
        _head, _body, _tail = _split_doc(text, rel)
    except RuntimeError as exc:
        return [Failure(rel, str(exc))]
    want = render_doc_section(decl)
    have = BEGIN_MARK + _body + END_MARK
    if have.strip() != want.strip():
        return [Failure(
            rel,
            "the generated autostart section no longer matches "
            f"{DECLARATION_REL} — the document and the overlay "
            "would tell a reader different things. Regenerate:\n"
            "    python3 sw/linux/deployment_contract.py --write",
        )]
    return []


def write_doc(decl: dict, root: Path = ROOT) -> bool:
    doc = root / DOC_REL
    rel = DOC_REL
    text = doc.read_text(encoding="utf-8")
    head, _body, tail = _split_doc(text, rel)
    new = head + render_doc_section(decl) + tail
    if new == text:
        return False
    doc.write_text(new, encoding="utf-8")
    return True


def check_all(decl: dict | None = None, root: Path = ROOT) -> list[Failure]:
    decl = decl if decl is not None else load_declaration()
    return (
        check_overlay(decl, root)
        + check_targets(decl, root)
        + check_doc(decl, root)
        + check_bound_docs(decl, root)
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true",
                    help="regenerate the generated section of the prose document")
    ap.add_argument("--check", action="store_true",
                    help="check the repo against the declaration (the default)")
    args = ap.parse_args(argv)

    decl = load_declaration()
    if args.write:
        changed = write_doc(decl)
        print(f"{'updated' if changed else 'unchanged'}: {DOC_REL}")

    fails = check_all(decl)
    if fails:
        print(f"DEPLOYMENT CONTRACT VIOLATED ({len(fails)}):", file=sys.stderr)
        for f in fails:
            print(f"  - {f}", file=sys.stderr)
        print(f"\nThe contract is {DECLARATION_REL} and its prose is "
              f"{DOC_REL} (SW-C0 / DS-P1.46).", file=sys.stderr)
        return 1
    print(f"✅ deployment contract holds: {len(decl['autostart'])} declared "
          f"autostart file(s), {len(decl['boot']['targets'])} Linux target(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
