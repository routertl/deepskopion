#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""DS-P3.341: the parse tree of a shell script, from the ash the DE25 image runs its init scripts with.

The image's /bin/busybox is BusyBox 1.36.1 for aarch64, which the desk cannot run. ash's parser does not depend on
the target architecture, only on the source and the options, so this builds BusyBox 1.36.1 natively (static, x86)
from the same source tarball with the same ash options, plus one patch (tools/ash_dump/ash-dump.patch) that makes
`sh -n SCRIPT` print each parsed command tree as one JSON line when DS_ASH_DUMP is set. Nothing is expanded or
executed: `sh -n` stops after the parse, and the process runs in a bubblewrap sandbox with no network, no
environment and no host file but the script.

What this proves, and what it does not: the same source (sha256 pinned below), the same ash options (the config of
Buildroot 2024.11.4's package/busybox/busybox.config, which built the image's busybox, sha256 pinned below), not the
same binary. The 16 patches Buildroot applies change awk, networking, seedrng, hwclock, the line editor and one
signal hunk of ash.c; none changes the parser.

A word is the text ash stored for it. Its control bytes keep which characters were quoted, escaped or expanded, so
the scan no longer guesses that from the script text (the source of the DS-P3.316 and DS-P3.342 review findings).

    python3 tools/ash_dump.py build            build (once) and print the binary's path
    python3 tools/ash_dump.py dump SCRIPT      print the decoded trees, one JSON line each

Without bwrap the tool refuses. The one opt-out is DS_ASH_PARSE_UNSANDBOXED=ci-no-userns, accepted only when a CI
marker (CI=true or GITHUB_ACTIONS=true) is also set, and every such run prints one line saying the parse was
unsandboxed and why. Public CI sets it in its workflow files because its container refuses user namespaces; the
planted-side-effect test, which runs there too, is what makes that parse acceptable.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
ASH_DIR = ROOT / "tools" / "ash_dump"
PATCH = ASH_DIR / "ash-dump.patch"
CONFIG = ASH_DIR / "busybox.config"

BUSYBOX_VERSION = "1.36.1"
TARBALL_SHA256 = "b8cc24c9574d809e7279c3be349795c5d5ceb6fdf19ca709f80cde50e47de314"
TARBALL_URL = f"https://busybox.net/downloads/busybox-{BUSYBOX_VERSION}.tar.bz2"
#: Buildroot 2024.11.4 package/busybox/busybox.config: the options the DE25 image's busybox was built with.
CONFIG_SHA256 = "cc69fa5a7519fbac9997466eedb444b7a459f1980171a4be4f4c8904797e4504"
UNSANDBOXED_ENV = "DS_ASH_PARSE_UNSANDBOXED"
UNSANDBOXED_VALUE = "ci-no-userns"          # the only accepted value; it also needs a CI marker in the environment
CACHE_ENV = "DS_ASH_DUMP_CACHE"
TARBALL_ENV = "DS_ASH_DUMP_TARBALL"
DUMP_TIMEOUT_S = 30
MAX_DUMP_BYTES = 64 * 1024 * 1024

# ash's control bytes (shell/ash.c, BusyBox 1.36.1): they stand in the stored text of a word.
CTLESC, CTLVAR, CTLENDVAR, CTLBACKQ = 0x81, 0x82, 0x83, 0x84
CTLARI, CTLENDARI, CTLQUOTEMARK = 0x86, 0x87, 0x88
CTLTOPROC, CTLFROMPROC = 0x89, 0x8A          # process substitution (bash compat), never literal
SENTINEL = ": ds-ash-dump-end-8d0b1f"
#: how the last dump() ran: "bwrap" or "unsandboxed"
LAST_MODE = ""
_ASSIGN_LIKE = __import__("re").compile(rb"^[A-Za-z_][A-Za-z0-9_]*=")


class AshDumpError(RuntimeError):
    """The parse could not be done: the sandbox or the built ash is missing, or the script is not valid shell."""


class AshSyntaxError(AshDumpError):
    """ash refused the script's syntax (`sh -n` exit non-zero)."""


@dataclass(frozen=True)
class Word:
    """One word as ash parsed it.

    literal   the word's text when it holds no expansion (quotes and escapes resolved), else None
    expands   the expansions in it, in order: "var", "cmd" (a substitution) or "arith"
    glob      an unquoted * ? or [ is in it
    tilde     it starts with an unquoted ~
    quoted    some of its characters are quoted or escaped
    subs      the parsed trees of its command substitutions, in order
    raw       the stored bytes
    template  the word as source-like text with each plain $NAME written ${NAME} and every other expansion
              written ${__ds_expansion__} (so ${PATH} in PATH=/a:$PATH can be recognised)
    var       NAME when the word is exactly one plain $NAME or ${NAME} (quotes allowed) and nothing else
    """
    raw: bytes
    literal: str | None
    expands: tuple[str, ...]
    glob: bool
    tilde: bool
    quoted: bool
    subs: tuple[dict, ...] = field(default=(), compare=False)
    template: str = field(default="", compare=False)
    var: str | None = field(default=None, compare=False)

    @property
    def is_plain(self) -> bool:
        """No expansion, no glob, no tilde: the shell runs exactly `literal`."""
        return self.literal is not None and not self.glob and not self.tilde


def decode_word(raw: bytes, subs: tuple[dict, ...] = (), assignment: bool = False) -> Word:
    out = bytearray()
    expands: list[str] = []
    template = ""
    parts: list[tuple[str, str]] = []
    glob = tilde = quoted = False
    open_bracket = False
    in_quote = False
    i, n = 0, len(raw)
    while i < n:
        b = raw[i]
        if b == CTLESC:
            quoted = True
            i += 1
            if i < n:
                out.append(raw[i])
                template += out[-1:].decode("utf-8", errors="replace")
                parts.append(("lit", ""))
        elif b == CTLQUOTEMARK:
            quoted = True
            in_quote = not in_quote
        elif b == CTLVAR:
            expands.append("var")
            subtype = raw[i + 1] if i + 1 < n else 0
            end = raw.find(b"=", i + 2)
            name = raw[i + 2:end].decode("utf-8", errors="replace") if end != -1 else ""
            i = end if end != -1 else n
            if subtype & 0x0F == 1:                  # VSNORMAL: $NAME, no operator, no body
                template += "${" + name + "}"
                parts.append(("var", name))
            elif subtype & 0x0F == 2 and raw[i + 1:i + 2] == bytes([CTLENDVAR]):   # ${NAME-} / ${NAME:-}: empty default
                template += "${" + name + ":-}"
                parts.append(("other", ""))
                i += 1
            else:                                    # ${NAME op word}: skip its body, which is not this word's text
                template += "${__ds_expansion__}"
                parts.append(("other", ""))
                depth = 1
                while i + 1 < n and depth:
                    i += 1
                    if raw[i] == CTLESC:
                        i += 1
                    elif raw[i] == CTLVAR:
                        depth += 1
                        i += 1
                    elif raw[i] == CTLENDVAR:
                        depth -= 1
        elif b == CTLBACKQ:
            expands.append("cmd")
            template += "${__ds_expansion__}"
            parts.append(("other", ""))
        elif b == CTLARI:
            start = i + 1
            i = start
            while i < n and raw[i] != CTLENDARI:
                i += 1
            body = raw[start:i].strip(b" \t")
            expands.append("arith")
            # A lone decimal constant evaluates to itself; the template keeps it findable so a judge may fold it.
            const = body.decode() if re.fullmatch(rb"0|[1-9][0-9]*", body) else None
            template += "${__ds_const_" + const + "}" if const else "${__ds_expansion__}"
            parts.append(("other", ""))
        elif b in (CTLTOPROC, CTLFROMPROC):
            expands.append("proc")
            template += "${__ds_expansion__}"
            parts.append(("other", ""))
        elif b in (CTLENDVAR, CTLENDARI):
            pass
        else:
            if not in_quote:
                if b in b"*?":
                    glob = True
                elif b == 0x2F:
                    open_bracket = False         # a bracket pair never spans path components
                elif b == 0x5B:                  # [ globs only when a closing ] follows
                    open_bracket = True
                elif b == 0x5D and open_bracket:
                    glob = True
                # ash expands an unquoted ~ at the start of a word; in an assignment also after the first = and
                # after each :
                if b == 0x7E and not expands:
                    prev = out[-1:]
                    if not out or (assignment and prev in (b"=", b":")):
                        tilde = True
            out.append(b)
            template += chr(b) if b < 0x80 else out[-1:].decode("utf-8", errors="replace")
            parts.append(("lit", ""))
        i += 1
    literal = None if expands else out.decode("utf-8", errors="surrogateescape")
    # a prefix assignment is not pathname-expanded: PATH=/opt/*/bin holds the characters it spells
    var = parts[0][1] if len(parts) == 1 and parts[0][0] == "var" else None
    return Word(raw, literal, tuple(expands), glob and not assignment, tilde, quoted, subs,
                literal if literal is not None else template, var)


def _decode(node, assignment: bool = False):
    """The tree *node* (as ash printed it) with each {"w": hex, "bq": [...]} turned into a Word. Words under an
    "assign" key, and words shaped NAME=value (export, local and readonly take them as arguments), are decoded in
    assignment context."""
    if isinstance(node, dict):
        if "w" in node and set(node) <= {"w", "bq"}:
            raw = bytes.fromhex(node["w"])
            return decode_word(raw, tuple(_decode(t) for t in node.get("bq", [])),
                               assignment or bool(_ASSIGN_LIKE.match(raw)))
        return {k: _decode(v, k == "assign") for k, v in node.items()}
    if isinstance(node, list):
        return [_decode(v, assignment) for v in node]
    return node


def simple_commands(tree) -> Iterator[dict]:
    """Every simple command ("t": "cmd") in *tree*, depth first, in the order ash parsed them: those inside
    compound commands, function bodies, here-document bodies and command substitutions included."""
    if isinstance(tree, dict):
        if tree.get("t") == "cmd":
            yield tree
        for value in tree.values():
            yield from simple_commands(value)
    elif isinstance(tree, list):
        for value in tree:
            yield from simple_commands(value)
    elif isinstance(tree, Word):
        for sub in tree.subs:
            yield from simple_commands(sub)


# ---------------------------------------------------------------------------------------------------- the build
def cache_dir() -> Path:
    return Path(os.environ.get(CACHE_ENV) or Path.home() / ".cache" / "deepskopion" / "ash-dump")


def build_key() -> str:
    """Names the cache slot: any change to the source, the patch, the config or the recipe is a new build."""
    h = hashlib.sha256()
    for part in (TARBALL_SHA256.encode(), PATCH.read_bytes(), CONFIG.read_bytes(), b"recipe-1"):
        h.update(hashlib.sha256(part).digest())
    return h.hexdigest()[:16]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tarball() -> Path:
    """The pinned source tarball, from DS_ASH_DUMP_TARBALL, the cache, or (once) busybox.net, by sha256."""
    candidates = [Path(p) for p in (os.environ.get(TARBALL_ENV),) if p]
    candidates.append(cache_dir() / f"busybox-{BUSYBOX_VERSION}.tar.bz2")
    for path in candidates:
        if path.is_file():
            if _sha256(path) != TARBALL_SHA256:
                raise AshDumpError(f"{path} is not the pinned BusyBox {BUSYBOX_VERSION} source (sha256 {TARBALL_SHA256})")
            return path
    dest = candidates[-1]
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(TARBALL_URL, timeout=120) as resp, tempfile.NamedTemporaryFile(
                dir=dest.parent, delete=False) as tmp:
            shutil.copyfileobj(resp, tmp)
    except OSError as exc:
        raise AshDumpError(f"cannot fetch {TARBALL_URL}: {exc}; put the pinned tarball (sha256 {TARBALL_SHA256}) "
                           f"at {dest} or name it in {TARBALL_ENV}") from exc
    if _sha256(Path(tmp.name)) != TARBALL_SHA256:
        Path(tmp.name).unlink()
        raise AshDumpError(f"{TARBALL_URL} is not the pinned source (sha256 {TARBALL_SHA256})")
    os.replace(tmp.name, dest)
    return dest


def _run(cmd, cwd: Path, log: Path, **kw) -> None:
    with log.open("ab") as fh:
        fh.write(("$ " + " ".join(map(str, cmd)) + "\n").encode())
        proc = subprocess.run(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              check=False, **kw)
    if proc.returncode:
        raise AshDumpError(f"{' '.join(map(str, cmd))} failed (exit {proc.returncode}); log: {log}")


def build() -> Path:
    """The path of the dump-ash binary, building it into the cache on first use."""
    slot = cache_dir() / build_key()
    binary = slot / "busybox"
    if binary.is_file():
        return _verified(slot)
    slot.parent.mkdir(parents=True, exist_ok=True)
    with (slot.parent / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)         # one builder at a time; the others find the finished slot
        if binary.is_file():
            return _verified(slot)
        return _build_locked(slot)


def _verified(slot: Path) -> Path:
    binary = slot / "busybox"
    recorded = slot / "busybox.sha256"
    if not recorded.is_file() or recorded.read_text(encoding="utf-8").strip() != _sha256(binary):
        raise AshDumpError(f"{binary} does not match the checksum recorded at build time; remove {slot} to rebuild")
    return binary


def _build_locked(slot: Path) -> Path:
    binary = slot / "busybox"
    if _sha256(CONFIG) != CONFIG_SHA256:
        raise AshDumpError(f"{CONFIG} is not the pinned Buildroot busybox.config (sha256 {CONFIG_SHA256})")
    for tool in ("make", "gcc", "patch", "tar"):
        if shutil.which(tool) is None:
            raise AshDumpError(f"building the dump ash needs {tool}, which is not on PATH")
    source = tarball()
    slot.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".build-", dir=slot.parent))
    log = work / "build.log"
    try:
        _run(["tar", "xjf", source, "-C", work], work, log)
        src = work / f"busybox-{BUSYBOX_VERSION}"
        _run(["patch", "-p1", "-i", PATCH], src, log)
        shutil.copy(CONFIG, src / ".config")
        text = (src / ".config").read_text(encoding="utf-8")
        text = text.replace("# CONFIG_STATIC is not set", "CONFIG_STATIC=y")
        text = "\n".join('CONFIG_CROSS_COMPILER_PREFIX=""' if ln.startswith("CONFIG_CROSS_COMPILER_PREFIX=") else ln
                         for ln in text.splitlines()) + "\n"
        (src / ".config").write_text(text, encoding="utf-8")
        # new options of the source that the config predates take their defaults (answer: the default)
        _run(["sh", "-c", "yes '' | make oldconfig"], src, log)
        _run(["make", f"-j{os.cpu_count() or 2}", "busybox"], src, log)
        out = work / "out"
        out.mkdir()
        shutil.copy(src / "busybox", out / "busybox")
        shutil.copy(src / ".config", out / "config")
        (out / "busybox.sha256").write_text(_sha256(out / "busybox") + "\n", encoding="utf-8")
        shutil.copy(log, out / "build.log")
        os.replace(out, slot)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return binary


# ---------------------------------------------------------------------------------------------------- the parse
def _bwrap_usable() -> str | None:
    exe = shutil.which("bwrap")
    if exe is None:
        return None
    probe = subprocess.run([exe, "--unshare-all", "--ro-bind", "/", "/", "true"], capture_output=True,
                           check=False, stdin=subprocess.DEVNULL, timeout=30)
    return exe if probe.returncode == 0 else None


def _unsandboxed_requested() -> bool:
    """True only for the CI opt-out. A set-but-wrong value, or the opt-out outside CI, is an error."""
    value = os.environ.get(UNSANDBOXED_ENV)
    if value is None:
        return False
    in_ci = os.environ.get("CI") in ("true", "1") or os.environ.get("GITHUB_ACTIONS") == "true"
    if value != UNSANDBOXED_VALUE or not in_ci:
        raise AshDumpError(f"{UNSANDBOXED_ENV} is set to {value!r}: the only accepted value is "
                           f"{UNSANDBOXED_VALUE!r}, and only inside CI (CI=true)")
    return True


def _bwrap_prefix(bwrap: str, binary: Path, script: Path) -> list[str]:
    """The sandbox around the parse: no network, no environment, only the binary and the script."""
    return [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
            "--setenv", "DS_ASH_DUMP", "1", "--ro-bind", str(binary), "/busybox",
            "--ro-bind", str(script), "/script.sh", "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev"]


def _cap_output() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_DUMP_BYTES, MAX_DUMP_BYTES))


def dump(script: str | bytes) -> list:
    """The parse trees of *script_text*, one per top-level command, with each word decoded to a Word.
    Raises AshSyntaxError for text ash refuses, AshDumpError when the parse cannot be done safely.

    *script* may be bytes: they reach ash unchanged (no newline translation, no re-encoding). A sentinel command is
    appended and must come back last; a binary that is not the patched ash, or a script that swallows the sentinel
    (an open here-document or a trailing backslash), is refused instead of returning a short or empty tree."""
    global LAST_MODE
    data = script.encode("utf-8", errors="surrogateescape") if isinstance(script, str) else bytes(script)
    trailing = len(data) - len(data.rstrip(b"\\"))
    if data.endswith(b"\\") and trailing % 2 == 1:
        raise AshDumpError("the script ends in a backslash with no newline: ash keeps that backslash as text at the "
                           "end of the file, which the end marker cannot be appended after; refused, not guessed")
    data += b"\n" + SENTINEL.encode() + b"\n"
    binary = build()
    bwrap = _bwrap_usable()
    unsandboxed = _unsandboxed_requested()
    if bwrap is None and not unsandboxed:
        raise AshDumpError("the init-script parse runs in bubblewrap (bwrap), which is missing or refused user "
                           f"namespaces here; install it (CI alone may set {UNSANDBOXED_ENV}={UNSANDBOXED_VALUE})")
    with tempfile.TemporaryDirectory(prefix="ash-dump-") as tmp:
        script_path = Path(tmp) / "script.sh"
        script_path.write_bytes(data)
        script = script_path
        if bwrap and not unsandboxed:
            cmd = _bwrap_prefix(bwrap, binary, script) + ["/busybox", "sh", "-n", "/script.sh"]
            env = {}
            LAST_MODE = "bwrap"
        else:
            LAST_MODE = "unsandboxed"
            print(f"ash_dump: parse ran UNSANDBOXED ({UNSANDBOXED_ENV}={UNSANDBOXED_VALUE}: this CI container "
                  "refuses user namespaces)", file=sys.stderr)
            cmd = [str(binary), "sh", "-n", str(script)]
            env = {"DS_ASH_DUMP": "1", "PATH": "/nonexistent", "HOME": tmp}
        out_path, err_path = Path(tmp) / "out", Path(tmp) / "err"
        try:
            with out_path.open("wb") as out_fh, err_path.open("wb") as err_fh:
                proc = subprocess.run(cmd, stdout=out_fh, stderr=err_fh, check=False, stdin=subprocess.DEVNULL,
                                      env=env, timeout=DUMP_TIMEOUT_S, cwd=tmp, preexec_fn=_cap_output)
        except subprocess.TimeoutExpired as exc:
            raise AshDumpError(f"the parse did not finish in {DUMP_TIMEOUT_S} s") from exc
        if out_path.stat().st_size >= MAX_DUMP_BYTES:
            raise AshDumpError("the parse tree is larger than the limit")
        text = out_path.read_bytes().decode("utf-8", errors="replace")
        stderr_text = err_path.read_bytes()[-400:].decode("utf-8", errors="replace")
    if proc.returncode:
        raise AshSyntaxError((stderr_text + text[-400:]).strip()[-400:] or f"ash exited {proc.returncode}")
    try:
        trees = [_decode(json.loads(line)) for line in text.splitlines() if line.strip()]
    except RecursionError as exc:
        raise AshDumpError("the script nests or chains commands more deeply than the decoder allows") from exc
    last = trees[-1] if trees else None
    if (not isinstance(last, dict) or last.get("t") != "cmd" or last["assign"] or last["redir"]
            or [w.literal for w in last["args"]] != SENTINEL.split()):
        raise AshDumpError("the end-of-script marker did not come back last: the binary is not the patched ash, or "
                           "the script ends inside a here-document or after a trailing backslash")
    return trees[:-1]


# DS-P3.346: provenance against the SHIPPED image. The parser is built from the image's recorded inputs
# (build_record.json); this compares the parser binary with the busybox the image really holds, on properties both
# carry. Each marker is a string that appears ONLY in shell/ash.c in the BusyBox 1.36.1 source tree (checked over every
# .c/.h file; hush shares RANDOM, pipefail and getopts, so those are not used), compiled in only when the named ash
# option is on. A marker present in one binary and absent in the other means the ashes were configured differently.
# It does not see the patches (only 0008 touches ash.c, one comment), a toolchain, or an option that leaves no
# unique string: ASH_INTERNAL_GLOB, FEATURE_SH_MATH_64 and ASH_EXPAND_PRMT are unchecked here. Closing that fully needs
# the build's real .config archived with the image (DS-P3.349); until then the parser's grammar options are
# verified for ASH_ALIAS, ASH_JOB_CONTROL, ASH_IDLE_TIMEOUT, ASH_BASH_NOT_FOUND_HOOK, ASH_BASH_COMPAT, FEATURE_SH_MATH.
ASH_MARKERS = {
    b"command_not_found_handle": "CONFIG_ASH_BASH_NOT_FOUND_HOOK",
    b"TMOUT": "CONFIG_ASH_IDLE_TIMEOUT",
    b"unalias": "CONFIG_ASH_ALIAS",
    b"can't access tty; job control turned off": "CONFIG_ASH_JOB_CONTROL",
    b"BASH_XTRACEFD": "CONFIG_ASH_BASH_COMPAT",          # ash.c only; guarded by BASH_XTRACEFD = ASH_BASH_COMPAT
    b"usage: getopts optstring var [arg]": "CONFIG_ASH_GETOPTS",   # ash.c only
    b"arithmetic syntax error": "CONFIG_FEATURE_SH_MATH",  # shell/math.c, built only with the $(( )) evaluator
}
_VERSION = re.compile(rb"BusyBox v(\d+\.\d+\.\d+)")


def ash_provenance(binary: bytes) -> dict:
    """The comparable properties of a busybox binary: its version(s) and which ash-only markers it carries."""
    return {"versions": sorted({m.decode() for m in _VERSION.findall(binary)}),
            "markers": {m.decode(): m in binary for m in ASH_MARKERS}}


def _applet_table(binary: bytes):
    """(offset of the applet-main table, {applet name: index}) of a static, non-PIE 64-bit busybox ELF, or None.

    The names are one sorted NUL-separated run starting "[", "[[". The main table is found by aliasing: "[", "[[" and
    "test" share one main, so the table is the 8-aligned array whose entries 0, 1 and index("test") are equal and in
    the text segment. No code is run."""
    import struct
    start = binary.find(b"[\x00[[\x00")
    if start < 0 or binary[:4] != b"\x7fELF" or binary[4] != 2:
        return None
    names = binary[start:binary.find(b"\x00\x00", start)].split(b"\x00")
    index = {n.decode(errors="replace"): k for k, n in enumerate(names)}
    if "test" not in index or len(names) < 100:
        return None
    entry = index["test"]
    phoff = struct.unpack_from("<Q", binary, 0x20)[0]
    phentsize, phnum = struct.unpack_from("<HH", binary, 0x36)
    text, mapped = [], []
    for k in range(phnum):
        kind, flags, off, vaddr, _pa, filesz, _ms, _al = struct.unpack_from("<IIQQQQQQ", binary, phoff + k * phentsize)
        if kind == 1:
            mapped.append((off, off + filesz))          # file bytes the loader maps: the only bytes that can run
            if flags & 1:
                text.append((vaddr, vaddr + filesz))
    word = lambda o: struct.unpack_from("<Q", binary, o)[0]
    in_text = lambda a: any(lo <= a < hi for lo, hi in text)
    found = [o for o in range(0, len(binary) - 8 * len(names) + 1, 8)
             if any(lo <= o and o + 8 * len(names) <= hi for lo, hi in mapped)
             and word(o) == word(o + 8) == word(o + 8 * entry) and in_text(word(o))
             and all(in_text(word(o + 8 * k)) for k in range(len(names)))]          # every entry is code
    return (found[0], index) if len(found) == 1 else None                          # ambiguous = no answer


def applet_mains(binary: bytes) -> dict[str, int] | None:
    """{applet name: main-function address} read from that table, or None."""
    import struct
    located = _applet_table(binary)
    if located is None:
        return None
    table, index = located
    return {name: struct.unpack_from("<Q", binary, table + 8 * k)[0] for name, k in index.items()}


def shell_applets(binary: bytes) -> dict:
    """What `sh` runs in a busybox binary: {"sh_runs": "ash"|"hush"|"other"|None, "hush_applet": bool}."""
    mains = applet_mains(binary)
    if not mains or "sh" not in mains:
        return {"sh_runs": None, "hush_applet": None}
    runs = "ash" if mains.get("ash") == mains["sh"] else "hush" if mains.get("hush") == mains["sh"] else "other"
    return {"sh_runs": runs, "hush_applet": "hush" in mains}


def provenance_mismatches(parser: bytes, image: bytes) -> list[str]:
    """What differs between the parser's busybox and the image's. Empty means no difference was found; it also
    refuses a binary with no version or no marker at all (a non-busybox, a wrong file), never reading that as a match,
    and refuses an image whose `sh` applet does not run ash."""
    mine, theirs = ash_provenance(parser), ash_provenance(image)
    problems = []
    for name, props in (("parser", mine), ("image", theirs)):
        if not props["versions"]:
            problems.append(f"{name}: no BusyBox version string (not a busybox binary)")
        if not any(props["markers"].values()):
            problems.append(f"{name}: carries none of the ash markers (not a busybox with ash)")
    if mine["versions"] != theirs["versions"]:
        problems.append(f"version differs: parser {mine['versions']} image {theirs['versions']}")
    for marker, present in mine["markers"].items():
        if present != theirs["markers"][marker]:
            problems.append(f"{ASH_MARKERS[marker.encode()]} differs: marker {marker!r} parser={present} "
                            f"image={theirs['markers'][marker]}")
    sh = shell_applets(image)
    if sh["sh_runs"] != "ash":
        problems.append(f"image: the sh applet runs {sh['sh_runs']!r}, not ash (the parser is ash's)")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args[:1] == ["build"]:
            print(build())
            return 0
        if len(args) == 2 and args[0] == "dump":
            for tree in dump(Path(args[1]).read_bytes()):
                print(json.dumps(tree, default=lambda w: {"literal": w.literal, "expands": w.expands,
                                                          "glob": w.glob, "tilde": w.tilde, "subs": w.subs}))
            return 0
    except AshDumpError as exc:
        print(f"ash_dump: {exc}", file=sys.stderr)
        return 2
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
