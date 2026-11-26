#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""Re-runnable GitHub export script for Deepskopion.

Exports a clean orphan git repository from a Bitbucket checkout sha:
1. Exports tracked files at the given Bitbucket revision (default: HEAD).
2. Applies the DROP_LIST to exclude internal documentation, agent configurations,
   and unreleased components.
3. Regenerates files that embed host-specific paths (agilex_hps generation reports).
4. Executes tools/public_artifact_hygiene.py with --export-gate and refuses on
   ANY finding before any git commit is made.
5. Produces a single commit stamped with authoritative source provenance
   (Bitbucket source sha, commit date).
6. Re-runnable: subsequent exports to an existing destination update the working
   tree and create a new stamped commit cleanly.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HYGIENE_TOOL = ROOT / "tools" / "public_artifact_hygiene.py"

# One drop list. A failed import stops the export; a second copy here went stale.
sys.path.insert(0, str(ROOT / "tools"))
from public_artifact_hygiene import PRIVATE_TERMS_REL, _matches_drop_list  # noqa: E402


def managed_package_roots(treeish: str, root: Path) -> list[str]:
    """DS-P2.360: the local_path of every ip.lock package fetched from a remote (url not ``sdk://``) at ``treeish``.

    Those directories are filled by `rr workspace sync` / `rr pkg add` from the package's own repository, so a
    tracked copy under one is vendored registry IP, which the public export must not carry (owner ruling
    2026-11-18). ``sdk://`` units come from the RouteRTL SDK and are not fetched packages. No ip.lock: no roots."""
    import yaml

    shown = subprocess.run(["git", "-C", str(root), "show", f"{treeish}:ip.lock"],
                           capture_output=True, text=True, errors="replace", check=False)
    if shown.returncode != 0:
        return []
    lock = yaml.safe_load(shown.stdout) or {}
    roots = []
    for name, entry in lock.items():
        if name in ("version", "_meta") or not isinstance(entry, dict):
            continue
        url, local = str(entry.get("url") or ""), str(entry.get("local_path") or "").strip("/")
        if local and not url.startswith("sdk://"):
            roots.append(local)
    return sorted(roots)


def managed_package_refusals(treeish: str, root: Path) -> list[str]:
    """Tracked paths at ``treeish`` that lie under a managed package root, one line each."""
    roots = managed_package_roots(treeish, root)
    if not roots:
        return []
    listed = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only", treeish, "--", *roots],
                            capture_output=True, check=True).stdout
    paths = [p.decode("utf-8", "surrogateescape") for p in listed.split(b"\0") if p]
    return [f"{p}: tracked file under managed package dir "
            f"{next(r for r in roots if p == r or p.startswith(r + '/'))} (fetched by rr workspace sync)"
            for p in sorted(paths)]


_HOST_PATH_SUB_RES = (
    re.compile(rb"/(?:home|Users)/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*?/(ip/agilex_hps/)"),
    re.compile(rb"(?:\.\./)+(?:\.agent-wt/[^/]+/deepskopion/)?(ip/agilex_hps/)"),
)


def target_input_refusals(treeish: str, root: Path) -> list[str]:
    """DS-P1.113: tracked files that a SHIPPED target yml names, but the export drops, one line each.

    A target under targets/ that the export ships is built from the public tree, so every repository file its yml
    names (hardware.mss, system_config, constraint and hook paths, ...) has to ship with it. pfsoc_datalogger named
    hw/libero/pfsoc/mss/disco_mss.cxz while the drop list removed it, so a public clone could not build that target.
    A string value counts as a named file when it is exactly the path of a tracked file at ``treeish``."""
    import yaml

    listed = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only", treeish],
                            capture_output=True, check=True).stdout
    tracked = {q.decode("utf-8", "surrogateescape") for q in listed.split(b"\0") if q}

    def leaves(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from leaves(value)
        elif isinstance(node, list):
            for value in node:
                yield from leaves(value)
        elif isinstance(node, str):
            yield node.strip()

    out = []
    for target in sorted(t for t in tracked if re.fullmatch(r"targets/[^/]+\.yml", t)):
        if _matches_drop_list(target):
            continue
        shown = subprocess.run(["git", "-C", str(root), "show", f"{treeish}:{target}"],
                               capture_output=True, text=True, errors="replace", check=True).stdout
        try:
            data = yaml.safe_load(shown)
        except yaml.YAMLError as exc:
            out.append(f"{target}: unreadable yml, its inputs cannot be checked ({exc})")
            continue
        for named in sorted({v for v in leaves(data) if v in tracked}):
            rule = _matches_drop_list(named)
            if rule:
                out.append(f"{named}: named by shipped {target} but dropped by the export ({rule})")
    return out


#: The files whose code judges an export. The gate that judges an export must be the gate shipped with that source.
GATE_FILES = ("tools/export_github.py", "tools/public_artifact_hygiene.py")


def gate_checkout_refusal(full_sha: str, code_root: Path = ROOT) -> str | None:
    """DS-P2.396: why the gate running from ``code_root`` may not judge source ``full_sha``, or None.

    When the running checkout's repository holds the source commit, the checkout must be at that commit with no
    tracked change: an older or edited checkout would judge the source with a different gate. When it does not
    hold the commit (a separate source repository), every gate file the source ships must equal the running one."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(code_root), *args], capture_output=True, check=False)

    if git("cat-file", "-e", f"{full_sha}^{{commit}}").returncode == 0:
        head = git("rev-parse", "HEAD").stdout.decode().strip()
        if head != full_sha:
            return (f"the export tool runs from a checkout at {head[:8] or 'no commit'}, not at the source "
                    f"{full_sha[:8]}; run it from a worktree at {full_sha[:8]} so the gate is the one that source ships")
        dirty = git("status", "--porcelain", "--untracked-files=no").stdout.decode().strip()
        if dirty:
            return (f"the export tool's checkout at {full_sha[:8]} has tracked changes, so the running gate is not "
                    f"the one {full_sha[:8]} ships:\n{dirty}")
    return None


def gate_file_refusal(full_sha: str, root: Path, code_root: Path = ROOT) -> str | None:
    """A separate source repository that ships the gate (every file of GATE_FILES) must ship the same gate bytes as
    the running tool. A source that lacks any of them ships no gate, and there is nothing to compare."""
    shipped = {}
    for rel in GATE_FILES:
        shown = subprocess.run(["git", "-C", str(root), "show", f"{full_sha}:{rel}"], capture_output=True,
                               check=False)
        if shown.returncode != 0:
            return None
        shipped[rel] = shown.stdout
    for rel, data in shipped.items():
        running = (code_root / rel).read_bytes() if (code_root / rel).is_file() else b""
        if data != running:
            return (f"{rel} at the source {full_sha[:8]} differs from the running copy in {code_root}; "
                    f"run the export from a worktree at {full_sha[:8]}")
    return None


def _resolve_source_commit(sha_or_ref: str, root: Path) -> tuple[str, str, str]:
    """Resolve full sha, short sha, and iso date for the source commit."""
    proc_full = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", f"{sha_or_ref}^{{commit}}"],
        capture_output=True, text=True, check=False,
    )
    if proc_full.returncode != 0:
        raise ValueError(
            f"Invalid source revision '{sha_or_ref}': {proc_full.stderr.strip()}"
        )
    full_sha = proc_full.stdout.strip()

    proc_short = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", full_sha],
        capture_output=True, text=True, check=True,
    )
    short_sha = proc_short.stdout.strip()

    proc_date = subprocess.run(
        ["git", "-C", str(root), "show", "-s", "--format=%cI", full_sha],
        capture_output=True, text=True, check=True,
    )
    source_date = proc_date.stdout.strip()
    if not source_date:
        source_date = datetime.now(timezone.utc).isoformat()

    return full_sha, short_sha, source_date


def _export_tree_from_sha(
    dest_dir: Path,
    full_sha: str,
    root: Path,
    paths: list[str] | None = None,
) -> None:
    """Extract tracked files at full_sha into dest_dir, dropping DROP_LIST items."""
    # Enumerate tracked files at revision
    cmd = ["git", "-C", str(root), "ls-tree", "-r", "-z", "--full-name", full_sha]
    if paths:
        cmd.append("--")
        cmd.extend(paths)

    proc = subprocess.run(cmd, capture_output=True, check=True)

    # One git process for every blob: a process per file costs the pre-commit
    # hook (which exports on each commit) most of its time budget.
    wanted: list[tuple[Path, str, str]] = []
    dropped: list[str] = []
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        # Format: <mode> <type> <blob_sha>\t<file_path>
        header, _, raw_path = entry.partition(b"\t")
        file_path = raw_path.decode("utf-8", "surrogateescape")
        mode, _, rest = header.decode("ascii").partition(" ")
        kind, _, blob_sha = rest.partition(" ")
        if kind != "blob":
            continue
        if _matches_drop_list(file_path):
            dropped.append(file_path)
            continue
        wanted.append((dest_dir / file_path, mode, blob_sha))

    batch = subprocess.run(
        ["git", "-C", str(root), "cat-file", "--batch"],
        input="".join(f"{sha}\n" for _, _, sha in wanted).encode("ascii"),
        capture_output=True, check=True,
    ).stdout
    pos = 0
    for target_file, mode, blob_sha in wanted:
        eol = batch.index(b"\n", pos)
        fields = batch[pos:eol].split()
        if len(fields) != 3 or fields[1] != b"blob":
            raise ValueError(f"cannot read blob {blob_sha}: {batch[pos:eol]!r}")
        size = int(fields[2])
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_bytes(batch[eol + 1:eol + 1 + size])
        pos = eol + 1 + size + 1
        if mode == "100755":
            target_file.chmod(0o755)

    exported = [str(f.relative_to(dest_dir).as_posix()) for f, _, _ in wanted]
    publish_gitattributes(dest_dir, exported=exported, dropped=dropped)


def _gitattributes_regex(pattern: str) -> re.Pattern[str]:
    """A .gitattributes path pattern as a regex over a repository path.

    gitignore(5) rules, which gitattributes(5) uses: a pattern with no slash
    (other than a trailing one) matches a basename at any depth; otherwise it
    is anchored at the root. `**/`, `/**` and `**` cross directories; `*` and
    `?` stay inside one path component.
    """
    anchored = "/" in pattern.rstrip("/")
    body = pattern.lstrip("/")
    out = ""
    i = 0
    while i < len(body):
        if body.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif body.startswith("/**", i) and i + 3 == len(body):
            out += "/.*"
            i += 3
        elif body.startswith("**", i):
            out += ".*"
            i += 2
        elif body[i] == "*":
            out += "[^/]*"
            i += 1
        elif body[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(body[i])
            i += 1
    prefix = "" if anchored else "(?:.*/)?"
    return re.compile(f"^{prefix}{out}$")


def publish_gitattributes(dest_dir: Path, *, exported: list[str], dropped: list[str]) -> list[str]:
    """Drop .gitattributes rules that only name files the export leaves out.

    A rule is removed, with the comment lines directly above it, when it matches
    at least one dropped file and no exported file: the public repository must not
    route through git-lfs (or describe) a file it does not ship. A rule that
    matches no tracked file at all (generated output such as out/*.job) is kept.
    Returns the removed rule lines.
    """
    attrs = dest_dir / ".gitattributes"
    if not attrs.is_file():
        return []
    lines = attrs.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    removed: list[str] = []
    pending_comments: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            pending_comments.append(line)
            continue
        if not stripped:
            kept.extend(pending_comments)
            pending_comments = []
            kept.append(line)
            continue
        rx = _gitattributes_regex(stripped.split()[0])
        if any(rx.match(p) for p in dropped) and not any(rx.match(p) for p in exported):
            removed.append(line)
            pending_comments = []
            continue
        kept.extend(pending_comments)
        pending_comments = []
        kept.append(line)
    kept.extend(pending_comments)
    # A removed block leaves its separating blank line behind: collapse runs.
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept).strip("\n") + "\n")
    if removed:
        attrs.write_text(text, encoding="utf-8")
    return removed


def regenerate_host_paths(dest_dir: Path, full_sha: str, root: Path) -> list[str]:
    """Regenerate or sanitize files that embed machine/host paths."""
    modified: list[str] = []

    # 1. agilex_hps generation reports and bookkeeping
    hps_dir = dest_dir / "ip" / "agilex_hps" / "agilex_hps"
    if hps_dir.is_dir():
        for rpt_name in (
            "agilex_hps_generation.rpt",
            "agilex_hps_generation_previous.rpt",
            "agilex_hps.html",
            "agilex_hps.xml",
            "agilex_hps.sopcinfo",
            "agilex_hps.qip",
        ):
            rpt_file = hps_dir / rpt_name
            if rpt_file.is_file():
                try:
                    data = rpt_file.read_bytes()
                    new_data = data
                    for pattern in _HOST_PATH_SUB_RES:
                        new_data = pattern.sub(rb"\1", new_data)
                    if new_data != data:
                        rpt_file.write_bytes(new_data)
                        modified.append(str(rpt_file.relative_to(dest_dir)))
                except OSError:
                    pass

    return modified


class PrivateTermsMissing(Exception):
    """The private repository lost its private term list: the export is refused."""


def private_terms_for_tree(treeish: str, root: Path, scratch: Path) -> Path | None:
    """DS-P1.123: the private term list for an export of ``treeish``.

    The union of the list the exported tree tracks, the list committed at HEAD of the
    checkout this exporter runs from, and that checkout's working copy. A union only adds
    terms: a staged or unstaged edit of one copy cannot drop a term the others carry.
    A checkout that tracks the dropped internal docs is the private repository, so no list
    anywhere raises PrivateTermsMissing (the gate fails closed). With no list and no
    internal docs (a public clone) None, and the gate has no private terms.
    """
    copies: list[bytes] = []
    for repo, rev in ((root, treeish), (ROOT, "HEAD")):
        blob = subprocess.run(
            ["git", "-C", str(repo), "show", f"{rev}:{PRIVATE_TERMS_REL}"],
            capture_output=True, check=False,
        )
        if blob.returncode == 0:
            copies.append(blob.stdout)
    own = ROOT / PRIVATE_TERMS_REL
    if own.is_file():
        copies.append(own.read_bytes())
    if copies:
        path = scratch / "private_export_terms.txt"
        path.write_bytes(b"\n".join(copies) + b"\n")
        return path
    internal = PRIVATE_TERMS_REL.rsplit("/", 1)[0]
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", internal],
        capture_output=True, text=True, check=False,
    )
    if tracked.stdout.strip():
        raise PrivateTermsMissing(
            f"{ROOT} tracks {internal}/ but neither it nor the exported tree has {PRIVATE_TERMS_REL} "
            "(DS-P1.123): the private term check cannot run")
    return None


def run_hygiene_gate(
    dest_dir: Path,
    restricted_names_file: Path | None = None,
    root: Path = ROOT,
    private_terms_file: Path | None = None,
) -> tuple[int, str]:
    """Execute tools/public_artifact_hygiene.py with --export-gate."""
    cmd = [
        sys.executable,
        str(HYGIENE_TOOL),
        "--export-gate",
        "--no-index",
        str(dest_dir),
    ]
    if restricted_names_file:
        cmd.extend(["--restricted-names", str(restricted_names_file)])
    if private_terms_file:
        cmd.extend(["--private-terms", str(private_terms_file)])

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=root)
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode, output


def commit_export(
    dest_dir: Path,
    full_sha: str,
    short_sha: str,
    source_date: str,
    author_name: str | None = None,
    author_email: str | None = None,
) -> str | None:
    """Initialize or update git repo in dest_dir, staging and committing with provenance."""
    git_dir = dest_dir / ".git"
    if not git_dir.is_dir():
        subprocess.run(["git", "init", "-b", "main", str(dest_dir)], check=True, capture_output=True)

    # Set local user identity if unconfigured
    proc_name = subprocess.run(["git", "-C", str(dest_dir), "config", "user.name"], capture_output=True, text=True)
    if not proc_name.stdout.strip():
        name = author_name or "Deepskopion Exporter"
        subprocess.run(["git", "-C", str(dest_dir), "config", "user.name", name], check=True, capture_output=True)

    proc_email = subprocess.run(["git", "-C", str(dest_dir), "config", "user.email"], capture_output=True, text=True)
    if not proc_email.stdout.strip():
        email = author_email or "exporter@deepskopion.org"
        subprocess.run(["git", "-C", str(dest_dir), "config", "user.email", email], check=True, capture_output=True)

    # Stage all changes
    subprocess.run(["git", "-C", str(dest_dir), "add", "-A"], check=True, capture_output=True)

    # Check status
    status_proc = subprocess.run(
        ["git", "-C", str(dest_dir), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    if not status_proc.stdout.strip():
        return None

    # Stamped provenance message
    commit_msg = (
        f"Bitbucket export: {short_sha} ({source_date})\n\n"
        f"Exported from Bitbucket repository.\n\n"
        f"Provenance:\n"
        f"  Source-Commit: {full_sha}\n"
        f"  Source-Date: {source_date}\n"
    )

    subprocess.run(
        ["git", "-C", str(dest_dir), "commit", "-m", commit_msg],
        check=True, capture_output=True,
    )

    new_sha_proc = subprocess.run(
        ["git", "-C", str(dest_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return new_sha_proc.stdout.strip()


def export_github(
    dest_dir: Path | str,
    source_sha: str = "HEAD",
    root: Path | None = None,
    restricted_names_file: Path | None = None,
    do_commit: bool = True,
    author_name: str | None = None,
    author_email: str | None = None,
    paths: list[str] | None = None,
) -> tuple[int, str]:
    """Execute complete export pipeline with hygiene exit condition."""
    repo_root = (root or ROOT).resolve()
    target_dest = Path(dest_dir).resolve()

    try:
        full_sha, short_sha, source_date = _resolve_source_commit(source_sha, repo_root)
    except ValueError as exc:
        return 1, str(exc)

    stale = gate_checkout_refusal(full_sha) or gate_file_refusal(full_sha, repo_root)
    if stale:
        return 1, f"PUBLIC EXPORT GATE REFUSAL (DS-P2.396): {stale}\nExport aborted before any file was written."

    refused = managed_package_refusals(full_sha, repo_root)
    if refused:
        return 1, ("PUBLIC EXPORT GATE REFUSAL: managed package content is tracked (DS-P2.360):\n"
                   + "\n".join(refused) + "\nExport aborted before any file was written.")
    missing = target_input_refusals(full_sha, repo_root)
    if missing:
        return 1, ("PUBLIC EXPORT GATE REFUSAL: a shipped target names a file the export drops (DS-P1.113):\n"
                   + "\n".join(missing) + "\nExport aborted before any file was written.")

    terms_scratch = tempfile.TemporaryDirectory(prefix="ds-export-terms-")
    try:
        terms_file = private_terms_for_tree(full_sha, repo_root, Path(terms_scratch.name))
    except PrivateTermsMissing as exc:
        terms_scratch.cleanup()
        return 1, f"PUBLIC EXPORT GATE REFUSAL: {exc}\nExport aborted before any file was written."

    target_dest.mkdir(parents=True, exist_ok=True)

    # Clear prior working tree (keep .git for re-runnable sync)
    for child in list(target_dest.iterdir()):
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    # 1. Export tracked files excluding drop list
    _export_tree_from_sha(target_dest, full_sha, repo_root, paths=paths)

    # 2. Regenerate files embedding host paths
    regenerate_host_paths(target_dest, full_sha, repo_root)

    # 3. Public artifact hygiene gate as EXIT CONDITION
    rc, hygiene_out = run_hygiene_gate(
        target_dest,
        restricted_names_file=restricted_names_file,
        root=repo_root,
        private_terms_file=terms_file,
    )
    terms_scratch.cleanup()
    if rc != 0:
        return rc, f"PUBLIC EXPORT GATE REFUSAL:\n{hygiene_out}\nExport aborted before any commit was made."

    # 4. Commit with stamped provenance
    if not do_commit:
        return 0, f"{hygiene_out}\nPUBLIC EXPORT GATE PASS (skipping commit due to --no-commit)."

    new_commit = commit_export(
        target_dest,
        full_sha,
        short_sha,
        source_date,
        author_name=author_name,
        author_email=author_email,
    )
    if new_commit:
        msg = f"{hygiene_out}\nPUBLIC EXPORT GATE PASS: committed {new_commit} to {target_dest} (source {short_sha}, {source_date})."
    else:
        msg = f"{hygiene_out}\nPUBLIC EXPORT GATE PASS: destination working tree clean (already up to date with {short_sha})."

    return 0, msg


def gate_staged_tree(
    root: Path | None = None,
    restricted_names_file: Path | None = None,
) -> tuple[int, str]:
    """Run the export gate on what the next commit would publish.

    The same steps as an export, on the index tree, with findings named by
    repository path. The pre-commit hook calls this so a finding is refused at
    the commit that introduces it.
    """
    repo_root = (root or ROOT).resolve()
    tree = subprocess.run(
        ["git", "-C", str(repo_root), "write-tree"],
        capture_output=True, text=True, check=False,
    )
    if tree.returncode != 0:
        return 2, f"cannot read the index: {tree.stderr.strip()}"
    refused = managed_package_refusals(tree.stdout.strip(), repo_root)
    if refused:
        return 1, ("PUBLIC EXPORT GATE REFUSAL: managed package content is tracked (DS-P2.360):\n"
                   + "\n".join(refused))
    missing = target_input_refusals(tree.stdout.strip(), repo_root)
    if missing:
        return 1, ("PUBLIC EXPORT GATE REFUSAL: a shipped target names a file the export drops (DS-P1.113):\n"
                   + "\n".join(missing))
    with tempfile.TemporaryDirectory(prefix="ds-export-gate-") as scratch:
        try:
            terms_file = private_terms_for_tree(tree.stdout.strip(), repo_root, Path(scratch))
        except PrivateTermsMissing as exc:
            return 1, f"PUBLIC EXPORT GATE REFUSAL: {exc}"
        dest = Path(scratch).resolve() / "tree"
        dest.mkdir()
        _export_tree_from_sha(dest, tree.stdout.strip(), repo_root)
        regenerate_host_paths(dest, tree.stdout.strip(), repo_root)
        rc, output = run_hygiene_gate(
            dest, restricted_names_file=restricted_names_file, root=repo_root,
            private_terms_file=terms_file,
        )
    return rc, output.replace(f"{dest}/", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest", "-d", type=Path, default=Path("dist/github_export"),
        help="destination directory for export (default: dist/github_export)",
    )
    parser.add_argument(
        "--sha", "-s", default="HEAD",
        help="source Bitbucket commit sha or reference (default: HEAD)",
    )
    parser.add_argument(
        "--root", "-r", type=Path, default=ROOT,
        help="source repository root (default: repo root)",
    )
    parser.add_argument(
        "--restricted-names", type=Path, default=None,
        help="path to restricted_names.yml (default: auto-discover)",
    )
    parser.add_argument(
        "--no-commit", action="store_true",
        help="export and run hygiene gate, but do not commit",
    )
    parser.add_argument(
        "--author", default=None,
        help="author name for stamped export commit",
    )
    parser.add_argument(
        "--email", default=None,
        help="author email for stamped export commit",
    )
    parser.add_argument(
        "--staged", action="store_true",
        help="gate the git index as an export would publish it; writes nothing",
    )
    parser.add_argument(
        "paths", nargs="*",
        help="optional sub-paths to restrict export to",
    )
    args = parser.parse_args(argv)

    if args.staged:
        rc, message = gate_staged_tree(
            root=args.root, restricted_names_file=args.restricted_names,
        )
        print(message, file=sys.stderr if rc else sys.stdout)
        return rc

    rc, message = export_github(
        dest_dir=args.dest,
        source_sha=args.sha,
        root=args.root,
        restricted_names_file=args.restricted_names,
        do_commit=not args.no_commit,
        author_name=args.author,
        author_email=args.email,
        paths=args.paths or None,
    )
    if rc != 0:
        print(message, file=sys.stderr)
        return rc
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
