# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""Mark AI-written source files with the RouteRTL provenance header (DS-P1.125).

The header is one comment line in the file's own syntax, ``<comment> rr <model> (AI-generated)``: ``--`` for VHDL,
``//`` for C, C headers, Verilog and SystemVerilog, ``#`` for Python and shell (``.sh``, any other file whose first
line is a ``sh``/``bash``/``dash`` shebang, every init and command script a rootfs overlay ships (by path, see
``overlay_shell_paths``), and the sourced shell files listed under ``[shell]``). It is the first line after any ``#!``
line and any Python encoding declaration. The text matches RouteRTL's V-019 rule, which reads the first 20 lines of a
VHDL file under a directory holding a ``.routertl-ai-generated`` sentinel.

Files that are not AI-written are listed in ``AI_PROVENANCE_EXCEPTIONS.txt`` at the repository root, by section:
``[vendored]``, ``[generated]``, ``[frozen-evidence]`` (files whose own sha256, or a hash over a set containing
them, a tracked record quotes; each line names that record in a ``# pinned-by: <path or glob>`` comment) and
``[human-written]`` (literal paths only); one path or glob per line, ``#`` starts a comment.
``[no-sentinel]`` lists the VHDL directories that cannot carry the sentinel because their subtree holds an excluded
VHDL file. Stamping works on bytes, so line endings and file modes are kept.

  ai_provenance_stamp.py --check                  tracked source files without the header (exit 1 if any)
  ai_provenance_stamp.py --write [--model M]      add the header and the sentinels (M, $RR_AI_MODEL, unknown-llm)
                                                 to the tracked files (the git index lists them); a new file
                                                 is stamped once it is added, and the stamped copy must be
                                                 staged again (git add) before --check and the guard see it
  ai_provenance_stamp.py --verify-diff BASE [--head REV]
        every source file that differs between BASE and REV (default: the index) gained exactly the header line at
        its position and nothing else, with its mode unchanged; renames, deletions and type changes are refused;
        added paths are listed (exit 1 on any refused change)
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
EXCEPTIONS = "AI_PROVENANCE_EXCEPTIONS.txt"
SENTINEL = ".routertl-ai-generated"
COMMENT = {".py": "#", ".sh": "#", ".bash": "#", ".vhd": "--", ".vhdl": "--", ".c": "//", ".h": "//", ".v": "//", ".sv": "//"}
SECTIONS = ("vendored", "generated", "frozen-evidence", "human-written", "no-sentinel", "shell")
MODEL_RE = r"[A-Za-z0-9_./\-:]+"   # ASCII only: the header must be valid in any source encoding
#: An extensionless file whose first line is one of these is a shell source (comment "#").
SHELL_SHEBANG = re.compile(
    rb"#!\s*(?:/usr/bin/env\s+(?:-\S+\s+)*)?(?:\S*/)?(?:(?:ba|da|a)?sh|busybox\s+a?sh)(?:\s|$)")
#: A tracked file directly inside one of these directories of a rootfs overlay is a command the image runs: shell by
#: path, whatever its first line and the [shell] list say. The overlay root is the path before the directory.
OVERLAY_SHELL_FILE = re.compile(r"^(?:(.*)/)?(?:etc/init\.d|usr/local/s?bin|usr/s?bin)/[^/]+$")
#: Files at an overlay root that the kernel runs as PID 1.
OVERLAY_PID1 = ("init", "linuxrc")
#: A PEP 263 encoding declaration; it must stay on line 1 or 2, so the header goes after it.
CODING_RE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")


def prefix_of(path: str, first_line: bytes, shell_paths: frozenset = frozenset()) -> str | None:
    """The comment prefix of a source file, or None when the file is not a source this tool marks. Shell: ``.sh``,
    any other file whose first line is a sh/bash/dash shebang (``env`` options such as ``-S`` allowed), and the
    sourced shell files listed in [shell]."""
    suffix = PurePosixPath(path).suffix
    if suffix in COMMENT:
        return COMMENT[suffix]
    if path in shell_paths or SHELL_SHEBANG.match(first_line):
        return "#"
    return None


def overlay_shell_paths(paths) -> frozenset:
    """The shipped init and command scripts, derived from the overlay layout alone: every path directly under
    etc/init.d/, usr/bin/, usr/sbin/, usr/local/bin/ or usr/local/sbin/, and the init/linuxrc at the root of each
    overlay those paths name. Removing a script's header, its #! line and its [shell] entry together leaves it here."""
    paths = set(paths)
    found, roots = set(), set()
    for p in paths:
        m = OVERLAY_SHELL_FILE.match(p)
        if m:
            found.add(p)
            roots.add(m.group(1))
    for r in roots:
        found.update(q for q in (f"{r}/{n}" if r else n for n in OVERLAY_PID1) if q in paths)
    return frozenset(found)


def _first_line(data: bytes) -> bytes:
    return data.split(b"\n", 1)[0]


def tag_re(prefix: str) -> re.Pattern:
    return re.compile(rf"{re.escape(prefix)} rr {MODEL_RE} \(AI-generated\)\r?")


def tag_line(prefix: str, model: str) -> str:
    if not re.fullmatch(MODEL_RE, model):
        raise ValueError(f"model id {model!r} does not match {MODEL_RE}")
    line = f"{prefix} rr {model} (AI-generated)"
    if CODING_RE.match(line.encode("utf-8")):
        raise ValueError(f"model id {model!r} would make the header a Python encoding declaration")
    return line


def _git(root: Path, *args: str, input_: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], input=input_, capture_output=True, check=True).stdout


def tracked(root: Path = ROOT) -> dict[str, str]:
    """Tracked regular source files -> comment prefix (suffix map, or an extensionless file with a shell shebang)."""
    out = {}
    top = root.resolve()
    listed = [e for e in _git(root, "ls-files", "-s", "-z").split(b"\0") if e]
    shell = frozenset(load_exceptions(root)["shell"]) | overlay_shell_paths(
        e.partition(b"\t")[2].decode("utf-8", "surrogateescape") for e in listed)
    for e in listed:
        head, _, raw = e.partition(b"\t")
        p = raw.decode("utf-8", "surrogateescape")
        # Regular files only: a tracked symlink (mode 120000) or a path that resolves outside the checkout is never
        # read or written, so --write cannot modify a file outside the repository.
        if head.split()[0] not in (b"100644", b"100755"):
            continue
        f = root / p
        # The resolved path must be the literal path: no symlink at the file or at any directory above it.
        if not f.is_file() or f.resolve() != top.joinpath(*PurePosixPath(p).parts):
            continue
        if PurePosixPath(p).suffix in COMMENT:
            out[p] = COMMENT[PurePosixPath(p).suffix]
        else:
            with open(f, "rb") as fh:
                pre = prefix_of(p, fh.readline(200), shell)
            if pre:
                out[p] = pre
    return dict(sorted(out.items()))


def _exceptions_text(root: Path, from_index: bool | str) -> str:
    """The exceptions manifest from the working tree, the git index (True: the snapshot the verdict reads), or a
    revision (a string)."""
    if from_index:
        rev = "" if from_index is True else from_index
        return _git(root, "show", f"{rev}:{EXCEPTIONS}").decode("utf-8")
    return (root / EXCEPTIONS).read_text(encoding="utf-8")


def _exceptions_at(root: Path, rev: str) -> dict[str, list[str]]:
    """The policy at a revision; a revision before the manifest existed has every section empty."""
    if subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{rev}:{EXCEPTIONS}"],
                      capture_output=True).returncode != 0:
        return {s: [] for s in SECTIONS}
    return load_exceptions(root, from_index=rev)


def pinned_by(root: Path = ROOT, from_index: bool | str = False) -> dict[str, list[str]]:
    """[frozen-evidence] entry -> the record path(s) or glob(s) its ``# pinned-by:`` comment names."""
    out, cur = {}, None
    for raw in _exceptions_text(root, from_index).splitlines():
        line, _, comment = raw.partition("#")
        line = line.strip()
        if re.fullmatch(r"\[([\w-]+)\]", line):
            cur = line[1:-1]
        elif line and cur == "frozen-evidence":
            m = re.match(r"\s*pinned-by:\s*(.+)", comment)
            out[line] = [r.strip() for r in m.group(1).split(",")] if m else []
    return out


def load_exceptions(root: Path = ROOT, from_index: bool | str = False) -> dict[str, list[str]]:
    sec: dict[str, list[str]] = {s: [] for s in SECTIONS}
    cur = None
    for raw in _exceptions_text(root, from_index).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"\[([\w-]+)\]", line)
        if m:
            if m.group(1) not in sec:
                raise ValueError(f"{EXCEPTIONS}: unknown section [{m.group(1)}]")
            cur = m.group(1)
            continue
        if cur is None:
            raise ValueError(f"{EXCEPTIONS}: entry before any section: {line}")
        if cur in ("human-written", "shell") and re.search(r"[*?\[]", line):
            raise ValueError(f"{EXCEPTIONS}: [{cur}] takes literal paths only, not {line!r}")
        sec[cur].append(line)
    return sec


def excluded(path: str, sec: dict[str, list[str]]) -> bool:
    return any(fnmatch.fnmatchcase(path, pat) for s in SECTIONS[:4] for pat in sec[s])


_EOL = re.compile(rb"\r\n|\n|\r")


def physical_lines(data: bytes) -> list[tuple[bytes, bytes]]:
    """(content, line ending) per physical line; CRLF, LF and a lone CR each end a line, as Python reads source."""
    out, pos = [], 0
    while pos < len(data):
        m = _EOL.search(data, pos)
        if not m:
            out.append((data[pos:], b""))
            break
        out.append((data[pos:m.start()], m.group()))
        pos = m.end()
    return out


def is_python(path: str) -> bool:
    return PurePosixPath(path).suffix == ".py"


def tag_index(lines: list[bytes], python: bool = False) -> int:
    """The only line index the header may sit on: after a ``#!`` line, and, in a Python source only, after a
    PEP 263 encoding declaration on line 1 or 2 (Python reads it only there, after any comment or blank line 1)."""
    i = 1 if lines and lines[0].startswith(b"#!") else 0
    for k in ((0, 1) if python else ()):
        if k < len(lines) and CODING_RE.match(lines[k]) and (k == 0 or not lines[0].strip() or lines[0].lstrip().startswith(b"#")):
            i = max(i, k + 1)
    return i


BOM = b"\xef\xbb\xbf"


def header_model(data: bytes | str, prefix: str, python: bool = False) -> str | None:
    """The model id of the header at its position (after a BOM, a ``#!`` line and an encoding declaration), or
    None when the file has no header there. Shared by the verdict and by --verify-diff."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if data.startswith(BOM):
        data = data[len(BOM):]
    lines = [c for c, _ in physical_lines(data)]
    i = tag_index(lines, python)
    if i >= len(lines):
        return None
    m = re.fullmatch(rf"{re.escape(prefix)} rr ({MODEL_RE}) \(AI-generated\)", lines[i].decode("utf-8", "replace"))
    return m.group(1) if m else None


def has_tag(data: bytes | str, prefix: str, python: bool = False) -> bool:
    return header_model(data, prefix, python) is not None


def indexed(root: Path = ROOT) -> dict[str, tuple[str, bytes]]:
    """Source files as the git index holds them -> (comment prefix, blob bytes); regular-file modes only. The
    header verdict reads these blobs, so an unstaged edit cannot hide a staged file without the header."""
    shell = frozenset(load_exceptions(root, from_index=True)["shell"])
    entries = []
    for e in _git(root, "ls-files", "-s", "-z").split(b"\0"):
        if e:
            head, _, raw = e.partition(b"\t")
            mode, sha = head.split()[0], head.split()[1].decode()
            if mode in (b"100644", b"100755"):
                entries.append((raw.decode("utf-8", "surrogateescape"), sha))
    shell |= overlay_shell_paths(p for p, _ in entries)
    blobs = _git(root, "cat-file", "--batch", input_=("".join(f"{s}\n" for _, s in entries)).encode())
    out, pos = {}, 0
    for path, _sha in entries:
        eol = blobs.index(b"\n", pos)
        size = int(blobs[pos:eol].split()[2])
        data = blobs[eol + 1:eol + 1 + size]
        pos = eol + 1 + size + 1
        pre = prefix_of(path, _first_line(data), shell)
        if pre:
            out[path] = (pre, data)
    return dict(sorted(out.items()))


def index_bytes(root: Path, path: str) -> bytes | None:
    """A file's bytes as the git index holds them, or None when it is not in the index."""
    r = subprocess.run(["git", "-C", str(root), "show", f":{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


CHECKSUM_ROW = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$")


def row_pins(record: str, text: str, path: str, digest: str) -> bool:
    """True when a sha256sum-style row of ``record`` gives ``digest`` for exactly ``path``; the row's path is taken
    relative to the record's own directory."""
    base = posixpath.dirname(record)
    for line in text.splitlines():
        m = CHECKSUM_ROW.match(line)
        if m and m.group(1).lower() == digest and posixpath.normpath(posixpath.join(base, m.group(2))) == path:
            return True
    return False


def frozen_problems(root: Path = ROOT, set_pinned: dict | None = None) -> list[str]:
    """[frozen-evidence] check on the git index snapshot: the policy, each file's bytes and each record's bytes all
    come from the index. Each entry names a record ("./NAME" = NAME in the file's own directory); whenever a matched
    file is staged, its record must be staged, and a row of it must give the file's sha256 for exactly that path.
    ``set_pinned`` maps a path to (record, check) for files pinned by a hash over a set (checked by the caller's
    function instead of a row)."""
    set_pinned = set_pinned or {}
    sec, pins = load_exceptions(root, from_index=True), pinned_by(root, from_index=True)
    staged = set(indexed(root))
    allfiles = set(_git(root, "ls-files", "-z").decode("utf-8", "surrogateescape").split("\0")) - {""}
    problems = []
    for pattern in sec["frozen-evidence"]:
        globs = pins.get(pattern, [])
        if not globs:
            problems.append(f"{pattern}: no '# pinned-by:' record named")
            continue
        for p in sorted(f for f in staged if fnmatch.fnmatchcase(f, pattern)):
            recs = set()
            for g in globs:
                if g.startswith("./"):
                    recs.add(posixpath.join(posixpath.dirname(p), g[2:]))
                else:
                    recs |= {r for r in allfiles if fnmatch.fnmatchcase(r, g)}
            recs = sorted(r for r in recs if r in allfiles and r != p)
            if not recs:
                problems.append(f"{p}: its pinned-by record {globs} is not staged")
                continue
            if p in set_pinned:
                rec, check = set_pinned[p]
                if rec not in recs or not check():
                    problems.append(f"{p}: the set hash is not in its declared record {globs}")
                continue
            h = hashlib.sha256(index_bytes(root, p) or b"").hexdigest()
            if not any(row_pins(r, (index_bytes(root, r) or b"").decode("utf-8", "ignore"), p, h) for r in recs):
                problems.append(f"{p}: no row of {recs} gives sha256 {h[:12]} for exactly this path")
    return problems


def missing(root: Path = ROOT) -> list[str]:
    sec = load_exceptions(root, from_index=True)
    return [p for p, (pre, data) in indexed(root).items() if not excluded(p, sec) and not has_tag(data, pre, is_python(p))]


def stamp_bytes(data: bytes, prefix: str, model: str, python: bool = False) -> bytes:
    """Insert the header at its position. The line ending of the file's first line is reused, so a CRLF file stays
    CRLF; nothing else changes. A file that already has the header at its position is returned unchanged."""
    if has_tag(data, prefix, python):
        return data
    if data.startswith(BOM):          # a UTF-8 byte-order mark stays at byte zero
        return BOM + stamp_bytes(data[len(BOM):], prefix, model, python)
    parts = physical_lines(data)
    eol = parts[0][1] if parts and parts[0][1] else b"\n"
    tag = tag_line(prefix, model).encode("utf-8") + eol
    i = tag_index([c for c, _ in parts], python)
    if i == 0:
        return tag + data
    head = [c + (e or eol) for c, e in parts[:i]]
    return b"".join(head) + tag + b"".join(c + e for c, e in parts[i:])


def stamp_text(text: str, prefix: str, model: str, python: bool = False) -> str:
    return stamp_bytes(text.encode("utf-8"), prefix, model, python).decode("utf-8")


def sentinel_dirs(root: Path = ROOT, from_index: bool = False) -> tuple[list[str], list[str]]:
    """(directories that get a sentinel, VHDL directories that cannot). A directory gets one only if every tracked
    VHDL file in its whole subtree is stamped, because V-019 applies a sentinel to all descendants. With
    ``from_index`` the VHDL set and the policy come from the git index (what a clean checkout holds); the guard
    uses that, so a staged file deleted only from the working tree still counts."""
    sec = load_exceptions(root, from_index=from_index)
    names = indexed(root) if from_index else tracked(root)
    vhdl = [p for p in names if PurePosixPath(p).suffix in (".vhd", ".vhdl")]
    dirs = sorted({str(PurePosixPath(p).parent) for p in vhdl})
    ok, blocked = [], []
    for d in dirs:
        sub = vhdl if d == "." else [p for p in vhdl if p.startswith(d + "/")]
        (blocked if any(excluded(p, sec) for p in sub) else ok).append(d)
    return ok, blocked


def write(root: Path, model: str) -> int:
    sec = load_exceptions(root)
    n = 0
    for p, pre in tracked(root).items():
        if excluded(p, sec):
            continue
        f = root / p
        data = f.read_bytes()
        new = stamp_bytes(data, pre, model, is_python(p))
        if new != data:
            f.write_bytes(new)
            n += 1
    ok, _ = sentinel_dirs(root)
    for d in ok:
        s = root / d / SENTINEL
        if s.is_symlink() or (s.exists() and not s.is_file()):
            raise SystemExit(f"refusing {s}: a sentinel must be a regular file, not a symlink or directory")
        if not s.exists():
            fd = os.open(s, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
            os.close(fd)
    print(f"stamped {n} file(s); sentinel in {len(ok)} director(ies)")
    return 0


def _tree(root: Path, rev: str | None) -> dict[str, tuple[str, str]]:
    """path -> (mode, blob sha) at rev, or in the index when rev is None."""
    out = {}
    if rev is None:
        for e in _git(root, "ls-files", "-s", "-z").split(b"\0"):
            if e:
                head, _, path = e.partition(b"\t")
                mode, sha, _stage = head.decode().split()
                out[path.decode("utf-8", "surrogateescape")] = (mode, sha)
    else:
        for e in _git(root, "ls-tree", "-r", "-z", rev).split(b"\0"):
            if e:
                head, _, path = e.partition(b"\t")
                mode, _kind, sha = head.decode().split()
                out[path.decode("utf-8", "surrogateescape")] = (mode, sha)
    return out


def _blob(root: Path, sha: str) -> bytes:
    return _git(root, "cat-file", "blob", sha)


def verify_diff(root: Path, base: str, head: str | None = None) -> int:
    old, new = _tree(root, base), _tree(root, head)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(p for p in set(old) & set(new) if old[p] != new[p])
    bad = [f"deleted or renamed away: {p}" for p in removed]
    stamped, tests, other = 0, 0, []
    sec = load_exceptions(root, from_index=head or True)
    shell = frozenset(sec["shell"]) | overlay_shell_paths(new)
    old_sec = _exceptions_at(root, base)
    old_shell = frozenset(old_sec["shell"]) | overlay_shell_paths(old)
    # A policy-only change that waives an existing source: once the base has a manifest, a source that existed and
    # was not excluded there may not become excluded in the same change (the exception needs its own review).
    if subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{base}:{EXCEPTIONS}"],
                      capture_output=True).returncode == 0:
        for p in sorted(set(old) & set(new)):
            if excluded(p, sec) and not excluded(p, old_sec) and prefix_of(
                    p, _first_line(_blob(root, old[p][1])), old_shell) is not None:
                bad.append(f"source newly excluded by the manifest: {p}")
    # A policy-only change: a fragment dropped from [shell] whose bytes are unchanged leaves the header check.
    for p in sorted(old_shell - shell):
        if p in new and p not in changed and prefix_of(p, _first_line(_blob(root, new[p][1])), shell) is None:
            bad.append(f"source scope changed by the [shell] list alone: {p}")
    for p in changed:
        (om, osha), (nm, nsha) = old[p], new[p]
        if om != nm:
            bad.append(f"mode {om} -> {nm}: {p}")
            continue
        if excluded(p, sec) or excluded(p, old_sec):   # excluded at either revision: byte-identical only
            bad.append(f"excluded file changed: {p}")
            continue
        a, b = _blob(root, osha), _blob(root, nsha)
        pre_old, pre = prefix_of(p, _first_line(a), old_shell), prefix_of(p, _first_line(b), shell)
        if pre_old is not None and pre != pre_old:      # a source leaving scope (or changing comment syntax)
            bad.append(f"source scope changed: {p}")
            continue
        if pre_old is None and pre is not None and not (p in shell and p not in old_shell):
            bad.append(f"source scope changed: {p}")       # entering scope other than by a new [shell] entry
            continue
        if pre is None:
            other.append(p)
            continue
        model = header_model(b, pre, is_python(p))
        if model is None or stamp_bytes(a, pre, model, is_python(p)) != b:
            bad.append(f"not exactly one header line: {p}")
            continue
        stamped += 1
        tests += ("/tests/" in p or PurePosixPath(p).name.startswith("test_"))
    print(f"verify-diff {base}..{head or 'INDEX'}: {stamped} source file(s) stamped ({tests} test files), "
          f"{len(added)} added path(s), {len(other)} other changed non-source path(s) (not judged), "
          f"{len(bad)} refused change(s)")
    for p in other:
        print(f"  other changed: {p}")
    for p in added:
        print(f"  added: {p}")
    for b in bad:
        print(f"  REFUSED: {b}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--write", action="store_true")
    g.add_argument("--verify-diff", metavar="BASE")
    ap.add_argument("--head", default=None, help="revision for --verify-diff (default: the index)")
    ap.add_argument("--model", default=None)
    a = ap.parse_args(argv)
    if a.check:
        m = missing()
        for p in m:
            print(f"missing header: {p}")
        print(f"{len(m)} file(s) without the header")
        return 1 if m else 0
    if a.write:
        return write(ROOT, a.model or os.environ.get("RR_AI_MODEL") or "unknown-llm")
    return verify_diff(ROOT, a.verify_diff, a.head)


if __name__ == "__main__":
    sys.exit(main())
