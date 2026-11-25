# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P3.337: the telemetry exporter's documented copy blocks, executed.

docs/software/telemetry_exporter.md carries two shell blocks a reader pastes: the ssh route (Zybo
Z7-20, PolarFire SoC Discovery Kit) and the card route (DE25). The DS-P3.334 guard checks them as
text, which an edit can satisfy while breaking them. This guard runs each block with bash in a
temporary tree, with stub ssh and sudo first on PATH, and judges the outcome: the files that
arrive, the writes that must not happen, and the unmount that must.

Each block runs inside bubblewrap: a new network namespace (no network), /usr read-only, no home
directory, no new privileges (so a real sudo cannot run), and only the temporary tree writable. A
documentation edit that names a host tool by its absolute path therefore reaches no host credential,
device or network.

DS-P3.339 hardening: the blocks are taken from the rendered Markdown under "Per board" (a fence
hidden in an HTML comment is not a recipe); each run gets no stdin and a time bound, and a timeout
is a failure; a host-side read takes regular files only; the remote shell starts in a fake board
home without the sources; and the partition state compared before and after includes every
entry's mtime and the partition root itself.

DS-P3.345: the card is protected by the kernel, not by the fixture's own records. Every block runs
under a broker (tools/tests/copy_block_broker.py) that owns its user and mount namespace (unshare
-Urm). "sudo mount" makes the recipe's mount point a read-only bind of a broker-held copy of the
partition, so a write or chmod without sudo fails with EROFS at the write itself. "sudo <command>"
runs in a second sandbox where the copy is read-write; "sudo umount" writes the copy back only if it
changed, and is busy while the caller's working directory is inside the mount point. The broker
takes the caller's working directory from the kernel (SO_PEERCRED and /proc), never from a message,
and refuses any other operation. The partitions, the copies and the operation log live in state no
sandbox has bound. This replaces DS-P3.343's lock records, whose documented limit
(test_a_recipe_that_edits_the_lock_records_evades_the_fixture) is now
test_a_recipe_that_edits_the_lock_records_is_refused_at_the_write.

Threat model: a recipe can write to the card only through sudo, and every sudo call is logged where
the recipe cannot reach it.

Recipes are taken only from visible content: a fence under an element with the hidden attribute,
style display:none or visibility:hidden, or inside <template>, <script> or <style>, is not a recipe;
an unclosed hiding element hides everything after it, which refuses the doc.

Prerequisites: bubblewrap with unprivileged user namespaces, unshare -Urm with shared mount
propagation, and markdown-it-py. On the bench
(RR_BENCH_CONFIG or RR_BENCH_FILE set, or the ignored runtimes/hw.local.yml present), in CI, or under the pre-commit
leg (DS_COPY_BLOCKS_REQUIRED) a missing prerequisite is a failure; elsewhere it skips by name.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# The hook leg points this at the STAGED copy of the doc; by default the checkout's copy is judged.
DOC = Path(os.environ.get("DS_TELEMETRY_DOC") or ROOT / "docs/software/telemetry_exporter.md")
DEST = Path("usr/local/share/deepskopion")
def _tracked_sources(root: Path = ROOT) -> dict[str, bytes]:
    """The exporter's tracked top-level modules, each a regular file reached through no symlink.
    The fixture and the expected bytes come from here only: an untracked or symlinked file in the
    checkout never enters the sandbox or a comparison."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "sw/telemetry/*.py"],
                         capture_output=True, check=True).stdout.decode().split("\0")
    sources = {}
    for rel in sorted(r for r in out if r and r.count("/") == 2):
        path = root / rel
        assert os.path.realpath(path) == os.path.abspath(path), f"{rel} goes through a symlink"
        assert stat.S_ISREG(path.lstat().st_mode), f"{rel} is not a regular file"
        sources[path.name] = path.read_bytes()
    assert "collectors.py" in sources and "__main__.py" in sources, sorted(sources)
    return sources


SOURCES_BYTES = _tracked_sources()
SOURCES = sorted(SOURCES_BYTES)
BWRAP = shutil.which("bwrap")
UNSHARE = shutil.which("unshare")
BROKER = Path(__file__).with_name("copy_block_broker.py")
_spec = importlib.util.spec_from_file_location("copy_block_broker", BROKER)
broker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(broker)
READY_SECONDS = 10          # the broker binds its tree and opens its socket well within this
RUN_SECONDS = 30            # a block runs in well under a second; one that waits is a failure


BENCH_FILE = ROOT / "runtimes/hw.local.yml"   # the documented default bench descriptor (ignored, local)


def _required_here(env=os.environ) -> bool:
    """In CI, on the bench (RR_BENCH_CONFIG or RR_BENCH_FILE, RouteRTL's descriptor overrides, or
    runtimes/hw.local.yml) or under the pre-commit leg (DS_COPY_BLOCKS_REQUIRED) a missing
    prerequisite fails; elsewhere it skips."""
    return bool(env.get("CI") or env.get("RR_BENCH_CONFIG") or env.get("RR_BENCH_FILE")
                or env.get("DS_COPY_BLOCKS_REQUIRED") or BENCH_FILE.exists())


def _prerequisite(problem: str | None) -> None:
    if problem is None:
        return
    if _required_here():
        pytest.fail(f"DS-P3.337 copy-block guard prerequisite missing on the bench or in CI: {problem}")
    pytest.skip(f"copy-block guard prerequisite missing (off the bench): {problem}")


def _bwrap_problem(bwrap: str | None = BWRAP) -> str | None:
    if not bwrap:
        return "bubblewrap (bwrap) is not installed"
    probe = subprocess.run([bwrap, "--unshare-all", "--ro-bind", "/", "/", "true"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=RUN_SECONDS)
    return None if probe.returncode == 0 else f"bwrap cannot create namespaces ({probe.stderr.strip()[-200:]})"


def _unshare_problem(unshare: str | None = UNSHARE) -> str | None:
    if not unshare:
        return "unshare (util-linux) is not installed"
    script = ('d=$(mktemp -d) && mount --bind "$d" "$d" && mount --make-shared "$d"; rc=$?; '
              'umount "$d" 2>/dev/null; rmdir "$d"; exit $rc')
    probe = subprocess.run([unshare, "-Urm", "sh", "-c", script], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=RUN_SECONDS)
    return None if probe.returncode == 0 else (
        f"unshare -Urm cannot make a shared mount ({probe.stderr.strip()[-200:]})")

# Remote commands run against $FAKEROOT; each call is logged; FAIL_CAT=<n> fails the n-th cat.
SSH_STUB = r"""#!/bin/sh
host="$1"; shift
printf '%s\t%s\n' "$host" "$*" >> "$FAKEROOT/ssh.log"
case "$*" in
  cat*) n=$(( $(cat "$FAKEROOT/cat.count" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKEROOT/cat.count"
        if [ "${FAIL_CAT:-0}" = "$n" ]; then cat > /dev/null; exit 1; fi ;;
esac
cd "$FAKEROOT/root"   # the remote shell starts in root's home on the board, not in the checkout
exec sh -c "$(printf '%s' "$*" | sed "s#/usr/local/share#$FAKEROOT/usr/local/share#g")"
"""
class _VisibleFences(HTMLParser):
    """Walks rendered HTML and keeps headings and sh code blocks a browser shows: nothing under an
    element with the hidden attribute, style display:none or visibility:hidden, or inside
    <template>, <script> or <style>. An element left unclosed keeps hiding to the end."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
    INERT = {"template", "script", "style"}
    # Block-level tags whose start closes an open <p>, as an HTML parser does (so a <p hidden>
    # before a fence does not hide it).
    P_CLOSERS = {"address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset", "figure",
                 "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p",
                 "pre", "section", "table", "ul"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.items: list[tuple] = []
        self._kind: tuple | None = None
        self._buf: list[str] = []
        self._tainted = False

    def _hidden(self) -> bool:
        return any(hides for _tag, hides in self.stack)

    @staticmethod
    def _style_hides(style: str) -> bool:
        """Whether a style attribute hides the element: a display:none or visibility:hidden|collapse
        declaration, comments ignored; a custom property such as --display is not display."""
        for decl in re.sub(r"/\*.*?\*/", " ", style, flags=re.S).split(";"):
            prop, _, value = decl.partition(":")
            value = re.sub(r"!\s*important\s*$", "", value.strip().lower())   # the priority is not the value
            prop, value = prop.strip().lower(), value.split()
            if (prop == "display" and value[:1] == ["none"]) or (
                    prop == "visibility" and value[:1] in (["hidden"], ["collapse"])):
                return True
        return False

    def handle_startendtag(self, tag, attrs):
        # HTML ignores the self-closing slash on a non-void element: <div hidden /> stays open.
        self.handle_starttag(tag, attrs)
        if tag in self.VOID:
            self.handle_endtag(tag)

    def handle_starttag(self, tag, attrs):
        if self._kind == ("sh",):           # markup inside a recipe (<br>, <span>…): not plain text
            self._tainted = True
        a = dict(attrs)
        style = a.get("style") or ""
        if tag in self.P_CLOSERS:
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == "p":
                    del self.stack[i:]
                    break
        hides = tag in self.INERT or "hidden" in a or self._style_hides(style)
        if tag not in self.VOID:
            self.stack.append((tag, hides))
        if self._kind is None and not self._hidden():
            if re.fullmatch(r"h[1-6]", tag):
                self._kind, self._buf = ("h", int(tag[1])), []
            elif tag == "code" and "language-sh" in (a.get("class") or "").split() and any(
                    t == "pre" for t, _ in self.stack):
                self._kind, self._buf = ("sh",), []

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                if self._kind and tag == ("code" if self._kind[0] == "sh" else f"h{self._kind[1]}"):
                    text = "".join(self._buf)
                    self.items.append(("sh", text, self._tainted) if self._kind == ("sh",) else (*self._kind, text))
                    self._kind, self._tainted = None, False
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self._kind is not None and not self._hidden():
            self._buf.append(data)


def _blocks(doc: str) -> dict[str, str]:
    """The sh fences a reader sees under the "Per board" heading, as CommonMark renders them: a
    fence inside an HTML comment, under a hidden element, or outside that section, is not a recipe."""
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        _prerequisite("markdown-it-py is not installed")
    page = re.sub(r"<!--.*?(-->|\Z)", "", MarkdownIt("commonmark").render(doc), flags=re.S)
    walk = _VisibleFences()
    walk.feed(page)
    walk.close()
    found, level = [], None
    for item in walk.items:
        if item[0] == "h":
            _kind, depth, text = item
            if level is not None and depth <= level:
                level = None
            if text.strip() == "Per board":
                level = depth
        elif level is not None:
            # markup inside a recipe: its rendered text is not its source (judged in this section only)
            assert not item[2], "a recipe under Per board holds HTML markup; not a recipe"
            found.append(item[1])
    both = [b for b in found if 'ssh "root@$BOARD_IP"' in b and 'mount "$CARD_ROOT"' in b]
    assert not both, f"a visible fence under Per board holds both routes, so neither is one recipe: {len(both)}"
    ssh = [b for b in found if 'ssh "root@$BOARD_IP"' in b]
    card = [b for b in found if 'mount "$CARD_ROOT"' in b]
    assert len(ssh) == 1 and len(card) == 1, f"rendered sh fences under Per board: {len(found)} ({len(ssh)} ssh, {len(card)} card)"
    return {"ssh": ssh[0], "card": card[0]}


def _state(tmp_path: Path) -> Path:
    """The broker's state (partitions, copies, the operation log), bound into no sandbox."""
    state = tmp_path.parent / f"{tmp_path.name}-broker"
    (state / "parts").mkdir(parents=True, exist_ok=True)
    return state


def _sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("ssh", SSH_STUB), ("sudo", broker.SUDO_STUB), ("broker_client.py", broker.CLIENT)):
        (bindir / name).write_text(body, encoding="utf-8")
        (bindir / name).chmod(0o755)
    work = tmp_path / "work"
    (work / "sw/telemetry").mkdir(parents=True)
    for name, data in SOURCES_BYTES.items():
        (work / "sw/telemetry" / name).write_bytes(data)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    _state(tmp_path)
    env = {"PATH": f"{bindir}:/usr/bin", "TMPDIR": str(tmpdir), "FAKEROOT": str(tmp_path / "board"),
           "BOARD_IP": "board-ip", "COPY_BROKER": str(tmp_path / ".broker.sock"),
           "COPY_BROKER_CLIENT": str(bindir / "broker_client.py")}
    (tmp_path / "board/root").mkdir(parents=True)     # root's home on the board: no sources
    return work, env


def _run(block: str, work: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run *block* in the recipe sandbox, under the broker that serves its sudo."""
    _prerequisite(_bwrap_problem())
    _prerequisite(_unshare_problem())
    tmp = Path(env["TMPDIR"]).parent
    state = _state(tmp)
    prefix = [BWRAP, "--unshare-all", "--die-with-parent", "--new-session",
              "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
              "--symlink", "usr/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
              "--bind", str(tmp), str(tmp), "--uid", "1000", "--gid", "1000", "--clearenv"]
    recipe = prefix + ["--chdir", str(work)]
    for key in ("PATH", "TMPDIR", "FAKEROOT", "BOARD_IP", "CARD_ROOT", "FAIL_CAT", "FAIL_CP", "COPY_BROKER",
                "COPY_BROKER_CLIENT"):
        if key in env:
            recipe += ["--setenv", key, env[key]]
    recipe += ["--setenv", "HOME", str(tmp / "home"), "/usr/bin/bash", "-c", block]
    spec = state / "sandbox.json"
    # the sudo worker: the same isolation, without the run tree (it gets a snapshot of its cwd)
    run_bind = prefix.index("--bind")
    worker = prefix[:run_bind] + prefix[run_bind + 3:]
    spec.write_text(json.dumps({"prefix": prefix, "worker": worker, "recipe": recipe, "tmpdir": env["TMPDIR"],
                                "socket": env["COPY_BROKER"], "seconds": RUN_SECONDS}), encoding="utf-8")
    ready_r, ready_w = os.pipe()
    proc = subprocess.Popen([UNSHARE, "-Urm", sys.executable, "-I", str(BROKER), "--run", str(tmp),
                             "--state", str(state), "--sandbox", str(spec), "--ready-fd", str(ready_w)],
                            pass_fds=(ready_w,), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    os.close(ready_w)
    try:
        ready, _, _ = select.select([ready_r], [], [], READY_SECONDS)
        if not ready or os.read(ready_r, 1) != b"R":
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            pytest.fail(f"the copy-block broker did not become ready within {READY_SECONDS} s: {err.strip()[-300:]}")
    finally:
        os.close(ready_r)
    try:
        out, err = proc.communicate(timeout=RUN_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        pytest.fail(f"the block did not finish within {RUN_SECONDS} s (it has no stdin): a recipe that waits fails")
    # A partition written back by the broker may carry what sudo made there (a symlink, a FIFO):
    # check the whole broker state before any host-side read can traverse it.
    _no_symlinks(state)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _partition(tmp_path: Path, *, root: bool) -> Path:
    """A fixture partition, held in the broker's state: no sandbox can reach it except through sudo."""
    part = _state(tmp_path) / "parts" / ("rootfs" if root else "bootfs")
    if root:
        (part / DEST / "sw/stream").mkdir(parents=True)
        (part / DEST / "sw/stream/__main__.py").write_text("# staged daemon\n", encoding="utf-8")
    else:
        part.mkdir()
        (part / "uImage").write_bytes(b"\0" * 16)
    part.chmod(0o755)
    return part


def _no_symlinked_component(path: Path) -> None:
    """No component of ``path`` is a symlink: its real path is its absolute path."""
    assert os.path.realpath(path) == os.path.abspath(path), f"{path} goes through a symlink (not followed)"


def _no_symlinks(top: Path) -> None:
    """The block ran with write access below ``top``; a symlink it left there, at any depth and
    including in place of a directory, must never be followed by a host-side read. Fails naming
    the path only, without reading anything. Runs before any snapshot or read."""
    _no_symlinked_component(top)
    links = [str(Path(d, n)) for d, dirs, files in os.walk(top, followlinks=False)
             for n in dirs + files if Path(d, n).is_symlink()]
    assert not links, f"the block left symlinks (not followed): {links}"
    special = [str(Path(d, n)) for d, _dirs, files in os.walk(top, followlinks=False) for n in files
               if not stat.S_ISREG(Path(d, n).lstat().st_mode)]
    assert not special, f"the block left special files (FIFO, socket or device; not read): {special}"


def _read(path: Path) -> bytes:
    _no_symlinked_component(path)
    assert stat.S_ISREG(path.lstat().st_mode), f"{path} is not a regular file (not read)"
    return path.read_bytes()


def _snapshot(part: Path) -> dict[str, tuple]:
    """The partition root and every entry: inode (so a delete-and-recopy shows), type, permission
    bits, mtime, and the contents of regular files."""
    _no_symlinks(part.parent)
    out = {}
    for p in [part, *sorted(part.rglob("*"))]:
        st = p.lstat()
        out[str(p.relative_to(part))] = (st.st_ino, stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_mtime_ns,
                                         _read(p) if stat.S_ISREG(st.st_mode) else None)
    return out


def _events(tmp_path: Path) -> list[dict]:
    log = _state(tmp_path) / "stub.log"
    return [json.loads(line) for line in _read(log).decode().splitlines()] if log.exists() or log.is_symlink() else []


def _log(tmp_path: Path) -> list[str]:
    """The broker's events, one string each: "sudo <argv>", "mount <path>", "umount <path>",
    "refused <why>". Each comes from one JSON record, so an argument cannot add a line."""
    out = []
    for e in _events(tmp_path):
        detail = " ".join(e["argv"]) if e["event"] == "sudo" else e.get("path", e.get("why", ""))
        out.append(f"{e['event']} {detail}")
    return out


def test_the_ssh_route_delivers_every_source_file_to_the_daemon_directory(tmp_path):
    work, env = _sandbox(tmp_path)
    proc = _run(_blocks(DOC.read_text(encoding="utf-8"))["ssh"], work, env)
    assert proc.returncode == 0, proc.stderr
    _no_symlinks(tmp_path)
    landed = tmp_path / "board" / DEST / "sw/telemetry"
    assert sorted(p.name for p in landed.iterdir()) == SOURCES
    for name in SOURCES:
        assert _read(landed / name) == SOURCES_BYTES[name], name
    assert all(line.startswith("root@board-ip\t")
               for line in _read(tmp_path / "board/ssh.log").decode().splitlines())


def test_the_ssh_route_runs_its_remote_commands_outside_the_checkout(tmp_path):
    work, env = _sandbox(tmp_path)
    proc = _run('ssh "root@$BOARD_IP" "pwd; test -e sw/telemetry && echo SOURCES"', work, env)
    assert proc.stdout.split() == [str(tmp_path / "board/root")], proc.stdout + proc.stderr


def test_a_write_to_the_mounted_card_needs_sudo(tmp_path):
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=True)
    block = ('MNT=$(mktemp -d); sudo mount "$CARD_ROOT" "$MNT"; '
             'mkdir "$MNT/plain" 2>/dev/null && echo PLAIN; sudo mkdir "$MNT/viasudo" && echo SUDO; '
             'sudo umount "$MNT"; rmdir "$MNT"')
    proc = _run(block, work, {**env, "CARD_ROOT": str(part)})
    assert proc.stdout.split() == ["SUDO"], proc.stdout + proc.stderr
    assert (part / "viasudo").is_dir() and not (part / "plain").exists()


def test_the_partition_state_includes_mtimes_and_the_root(tmp_path):
    part = _partition(tmp_path, root=False)
    before = _snapshot(part)
    st = (part / "uImage").stat()
    os.utime(part / "uImage", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))   # touched, same bytes
    assert _snapshot(part) != before, "an mtime-only change to the boot image"
    part.chmod(0o755)                        # a known starting mode, whatever the umask
    before = _snapshot(part)
    part.chmod(0o700)
    assert _snapshot(part) != before, "a mode change of the partition root"


def _card_steps(steps: list[str]) -> str:
    """Mount the card, prove a known file is visible, run each step on its own with its own stderr,
    then unmount. Prints MOUNTED, VISIBLE; per step DONE<i>, or REFUSED<i> followed by EROFS<i> when
    that step's own error is "Read-only file system"; and UNMOUNTED."""
    body = "".join(f'{{ {s}; }} 2> "$TMPDIR/err{i}" && echo DONE{i} || {{ echo REFUSED{i}; '
                   f'grep -q "Read-only file system" "$TMPDIR/err{i}" && echo EROFS{i}; }}; ' for i, s in enumerate(steps))
    return ('MNT=$(mktemp -d); sudo mount "$CARD_ROOT" "$MNT" && echo MOUNTED; test -f "$MNT/uImage" && echo VISIBLE; '
            f'{body}sudo umount "$MNT" && rmdir "$MNT" && echo UNMOUNTED')


@pytest.mark.parametrize("change", ['chmod 700 "$MNT"', 'touch "$MNT/uImage"', 'echo x > "$MNT/new"',
                                    'rm -f "$MNT/uImage"', 'chmod u+w "$MNT"'])
def test_a_change_to_the_mounted_card_without_sudo_fails_at_the_change(tmp_path, change):
    """DS-P3.345 (was ..._is_refused at the next sudo, DS-P3.343): the mounted card is a read-only
    bind, so the change itself fails with EROFS; the card is untouched and unmounts cleanly."""
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=False)
    before = _snapshot(part)
    proc = _run(_card_steps([change]), work, {**env, "CARD_ROOT": str(part)})
    assert proc.stdout.split() == ["MOUNTED", "VISIBLE", "REFUSED0", "EROFS0", "UNMOUNTED"], proc.stdout + proc.stderr
    assert _snapshot(part) == before


def test_a_relock_with_a_restored_mtime_fails_at_the_write(tmp_path):
    """DS-P3.345 (was test_a_relock_with_a_restored_mtime_is_refused, DS-P3.343): each step of the
    open, write, re-lock, restore-mtime sequence, run on its own, fails at its own write."""
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=False)
    before = _snapshot(part)
    steps = ['touch -r "$MNT" "$TMPDIR/ref"',                  # save the mtime (outside the card: succeeds)
             'chmod u+w "$MNT"', 'touch "$MNT/x"', 'rm -f "$MNT/uImage"', 'chmod a-w "$MNT"',
             'touch -r "$TMPDIR/ref" "$MNT"']                   # restore it
    proc = _run(_card_steps(steps), work, {**env, "CARD_ROOT": str(part)})
    expected = ["MOUNTED", "VISIBLE", "DONE0", *[w for i in range(1, 6) for w in (f"REFUSED{i}", f"EROFS{i}")], "UNMOUNTED"]
    assert proc.stdout.split() == expected, proc.stdout + proc.stderr
    assert _snapshot(part) == before


def test_a_recipe_that_edits_the_lock_records_is_refused_at_the_write(tmp_path):
    """DS-P3.345 inverts DS-P3.343's documented limit, test_a_recipe_that_edits_the_lock_records_
    evades_the_fixture: that sequence (unlock, write, re-lock, rewrite the lock record) evaded the
    same-uid lock completely. Now each card write fails at the write, and the broker's record (its
    operation log) is outside the sandbox: a write to it fails and the record stays intact."""
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=False)
    before = _snapshot(part)
    log = _state(tmp_path) / "stub.log"
    steps = ['chmod u+w "$MNT"', 'touch "$MNT/x"', 'chmod a-w "$MNT"', f'echo forged >> "{log}"']
    proc = _run(_card_steps(steps), work, {**env, "CARD_ROOT": str(part)})
    # the three card writes fail with EROFS; the record write fails because the record is not there
    expected = ["MOUNTED", "VISIBLE", *[w for i in range(3) for w in (f"REFUSED{i}", f"EROFS{i}")], "REFUSED3", "UNMOUNTED"]
    assert proc.stdout.split() == expected, proc.stdout + proc.stderr
    assert "forged" not in _read(log).decode()
    assert [e["event"] for e in _events(tmp_path)] == ["sudo", "mount", "sudo", "umount"]
    assert _snapshot(part) == before


_SEND = ("import json,os,socket,sys; s=socket.socket(socket.AF_UNIX); d,n=os.path.split(os.environ['COPY_BROKER']); "
         "s.connect('/proc/self/fd/%d/%s' % (os.open(d, os.O_PATH | os.O_DIRECTORY), n)); "
         "s.sendall(sys.argv[1].encode()); s.shutdown(socket.SHUT_WR); r=json.loads(s.recv(65536)); "
         "sys.stderr.write(r['stderr']); sys.exit(r['rc'])")


@pytest.mark.parametrize("message,why", [
    ('{"op": "rm", "argv": ["-rf", "/"]}', "refused op 'rm'"),   # repr: a hostile op cannot forge a log line
    ('{"op": "sudo", "argv": ["mount", "only-one"]}', "refused mount with 1 arguments"),
    ('{"op": "sudo", "argv": []}', "refused an argv that is not a non-empty list of strings"),
    ('not json', "refused a message that is not JSON"),
])
def test_the_broker_refuses_a_forged_operation(tmp_path, message, why):
    """A recipe can reach the broker's socket directly; anything but a well-formed sudo is refused
    and logged where the recipe cannot reach the log."""
    work, env = _sandbox(tmp_path)
    proc = _run(f"/usr/bin/python3 -I -c {json.dumps(_SEND)} '{message}'; echo RC=$?", work, env)
    assert "RC=2" in proc.stdout, proc.stdout + proc.stderr
    assert any(line.startswith(why) for line in _log(tmp_path)), _log(tmp_path)


@pytest.mark.parametrize("made", ['sudo ln -s /etc/hostname "$MNT/link"', 'sudo mkfifo "$MNT/pipe"'])
def test_what_sudo_leaves_on_the_card_is_checked_before_any_host_read(tmp_path, made):
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=False)
    with pytest.raises(AssertionError, match="the block left (symlinks|special files)"):
        _run(f'MNT=$(mktemp -d); sudo mount "$CARD_ROOT" "$MNT"; {made}; sudo umount "$MNT"; rmdir "$MNT"',
             work, {**env, "CARD_ROOT": str(part)})


def test_a_sudo_command_cannot_reach_a_socket_the_recipe_planted(tmp_path):
    """A sudo command sees a snapshot of its working directory (regular files, no special files),
    never the recipe's tree: it cannot connect to a socket the recipe listens on, so it cannot pass a
    writable card descriptor back to it."""
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=False)
    plant = ("import socket; s=socket.socket(socket.AF_UNIX); s.bind('plant.sock'); s.listen(1); "
             "open('plant.ready', 'w').close(); s.settimeout(5); s.accept()")
    block = (f'/usr/bin/python3 -I -c "{plant}" & for i in $(seq 50); do [ -e plant.ready ] && break; sleep 0.1; done; '
             'MNT=$(mktemp -d); sudo mount "$CARD_ROOT" "$MNT"; '
             'sudo test -S plant.sock && echo SEEN || echo UNSEEN; '
             'sudo test -f sw/telemetry/collectors.py && echo SOURCES; '      # positive control: the snapshot holds files
             'sudo umount "$MNT"; rmdir "$MNT"; kill %1 2>/dev/null; wait')
    proc = _run(block, work, {**env, "CARD_ROOT": str(part)})
    assert proc.stdout.split() == ["UNSEEN", "SOURCES"], proc.stdout + proc.stderr


# Both work from inside the mount point: its absolute path is longer than AF_UNIX allows.
_LISTEN = """import os, socket, sys
os.chdir(sys.argv[1])
s = socket.socket(socket.AF_UNIX)
s.bind("w.sock")
s.listen(1)
s.settimeout(4)
try:
    c, _ = s.accept()
    socket.send_fds(c, [b"x"], [os.open("uImage", os.O_WRONLY)])
finally:
    os.unlink("w.sock")
"""
_GRAB = """import os, socket, sys
os.chdir(sys.argv[1])
c = socket.socket(socket.AF_UNIX)
c.connect("w.sock")
_, fds, _, _ = socket.recv_fds(c, 1, 1)
os.write(fds[0], b"PWNED")
"""


def test_a_socket_a_sudo_command_makes_on_the_card_never_reaches_the_recipe(tmp_path):
    """A sudo command listens on a socket on the card and offers a writable card descriptor. The
    recipe sees the card through a separate read-only view that is refreshed by value after each sudo
    command, without special files, so the socket never appears there and no descriptor is passed."""
    work, env = _sandbox(tmp_path)
    (work / "listen.py").write_text(_LISTEN, encoding="utf-8")
    (work / "grab.py").write_text(_GRAB, encoding="utf-8")
    part = _partition(tmp_path, root=False)
    block = ('MNT=$(mktemp -d); sudo mount "$CARD_ROOT" "$MNT" && echo MOUNTED; '
             'sudo /usr/bin/python3 -I listen.py "$MNT" & '
             'for i in $(seq 40); do [ -S "$MNT/w.sock" ] && break; sleep 0.1; done; '
             '/usr/bin/python3 -I grab.py "$MNT" && echo WROTE || echo NOWRITE; '
             'wait; sudo umount "$MNT"; rmdir "$MNT"')
    proc = _run(block, work, {**env, "CARD_ROOT": str(part)})
    assert proc.stdout.split() == ["MOUNTED", "NOWRITE"], proc.stdout + proc.stderr
    assert "timed out" in proc.stderr, "the listener was live and nobody reached it: " + proc.stderr
    # sudo itself made and removed the socket (a legitimate card change); the image is untouched
    assert _read(part / "uImage") == b"\0" * 16


# Runs Broker.mount in its own user and mount namespace, with the broker module's subprocess.run
# wrapped so that the recipe's move happens at an exact step: "swap" and "remove" between the broker
# opening the mount point and binding onto it, "vanish" while the broker stages the copies (before
# that open), and "held" is "swap" with a descriptor held open inside the read-only staging, which
# makes a plain unmount of it fail with EBUSY. Prints one JSON object: the reply, what is left
# mounted (per the broker's own mountinfo) or staged, and whether a later mount and unmount are served.
_MOUNT_RACE = r"""import importlib.util, json, os, subprocess, sys
spec = importlib.util.spec_from_file_location("copy_block_broker", sys.argv[1])
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)
base, move = sys.argv[2], sys.argv[3]
run, state = os.path.join(base, "run"), os.path.join(base, "state")
os.makedirs(os.path.join(run, "tmp", "m"))
os.makedirs(os.path.join(run, "tmp", "n"))
os.makedirs(os.path.join(state, "parts", "bootfs"))
open(os.path.join(state, "parts", "bootfs", "uImage"), "wb").write(bytes(16))
real, moved, held = subprocess.run, [], []
step = "remount,bind,ro" if move == "vanish" else "--no-canonicalize"
def run_with_move(argv, *args, **kwargs):
    if step in argv and not moved:
        moved.append(move)
        m = os.path.join(run, "tmp", "m")
        os.rename(m, os.path.join(run, "tmp", "away"))
        if move in ("swap", "held"):
            os.mkdir(m)
        if move == "held":
            held.append(os.open(os.path.join(state, "ro1", "uImage"), os.O_RDONLY))
    return real(argv, *args, **kwargs)
broker.subprocess.run = run_with_move
b = broker.Broker(broker.Path(run), broker.Path(state), {})
part = os.path.join(state, "parts", "bootfs")
first = b.mount([part, os.path.join(run, "tmp", "m")])
broker.subprocess.run = real
left_mounted = os.path.exists(os.path.join(run, "tmp", "away", "uImage"))
with open("/proc/self/mountinfo") as f:
    points = [line.split()[4] for line in f]
mounted_in_state = sorted(p for p in points if p.startswith(state + "/") or p == os.path.join(run, "tmp", "away"))
staged = sorted(e for e in os.listdir(state) if e not in ("parts", "stub.log"))
for fd in held:
    os.close(fd)
second = b.mount([part, os.path.join(run, "tmp", "n")])
cwd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
unmount = b.umount([os.path.join(run, "tmp", "n")], cwd)
print(json.dumps({"moved": moved, "held": len(held), "first": first, "left_mounted": left_mounted,
                  "mounted_in_state": mounted_in_state, "staged": staged,
                  "registered": sorted(b.mounts), "second": second["rc"], "unmount": unmount["rc"]}))
"""


@pytest.mark.parametrize("move", ["swap", "remove", "vanish", "held"])
def test_a_mount_point_moved_just_before_the_bind_is_refused_and_undone(tmp_path, move):
    """DS-P3.347, deterministic: the recipe renames the mount point away exactly before the bind,
    and either puts another directory at its name (swap) or leaves the name empty (remove); or it
    removes the name while the broker stages the copies (vanish); or it swaps while holding a
    reference into the read-only staging (held). In every case the broker refuses, leaves nothing
    mounted (its own mountinfo) or staged, registers nothing, and serves the next mount and unmount."""
    _prerequisite(_unshare_problem())
    script = tmp_path / "mount_race.py"
    script.write_text(_MOUNT_RACE, encoding="utf-8")
    proc = subprocess.run([UNSHARE, "-Urm", sys.executable, "-I", str(script), str(BROKER), str(tmp_path), move],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=RUN_SECONDS)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["moved"] == [move] and got["held"] == (move == "held"), got   # the move really happened
    assert got["first"]["rc"] == 2 and "replaced while it was being mounted" in got["first"]["stderr"], got
    assert (got["left_mounted"], got["mounted_in_state"], got["staged"], got["registered"]) == (False, [], [], []), got
    assert (got["second"], got["unmount"]) == (0, 0), got


# Swaps the names m and x (renameat2 RENAME_EXCHANGE) as fast as it can until "stop" appears,
# then prints how many swaps succeeded. A mount point cannot be swapped (EBUSY).
_EXCHANGE = """import ctypes, os
libc = ctypes.CDLL(None, use_errno=True)
n = 0
while not os.path.exists("stop"):
    if libc.renameat2(-100, b"m", -100, b"x", 2) == 0:
        n += 1
print("SWAPS", n)
"""


def test_a_mount_point_swapped_while_mounting_is_refused_or_mounted_where_named(tmp_path):
    """DS-P3.347, stress: while sudo mount runs, the recipe swaps the mount point with a sibling
    directory. Every trial either refuses (the name no longer holds the mount, which the broker
    undoes) or mounts the card at the name asked for, and both directories are removable afterwards;
    never a card mounted elsewhere. Whether a trial hits the race depends on scheduling: the
    deterministic case is test_a_mount_point_moved_just_before_the_bind_is_refused_and_undone."""
    work, env = _sandbox(tmp_path)
    (work / "exchange.py").write_text(_EXCHANGE, encoding="utf-8")
    part = _partition(tmp_path, root=False)
    block = ('cd "$TMPDIR"; for i in $(seq 20); do mkdir m x; '
             f'/usr/bin/python3 -I "{work}/exchange.py" & P=$!; sleep 0.05; '
             'sudo mount "$CARD_ROOT" "$TMPDIR/m" 2>/dev/null; RC=$?; touch stop; wait $P; rm -f stop; '
             'if [ -f m/uImage ]; then AT=m; elif [ -f x/uImage ]; then AT=x; else AT=none; fi; '
             'if [ $RC = 0 ]; then sudo umount "$TMPDIR/m"; U=$?; else U=-; fi; '
             'rmdir m x && C=ok || C=left; echo "T rc=$RC at=$AT u=$U c=$C"; done')
    proc = _run(block, work, {**env, "CARD_ROOT": str(part)})
    trials = [line for line in proc.stdout.splitlines() if line.startswith("T ")]
    swaps = [int(line.split()[1]) for line in proc.stdout.splitlines() if line.startswith("SWAPS")]
    assert len(trials) == 20 and len(swaps) == 20 and sum(swaps) > 0, proc.stdout + proc.stderr
    bad = [t for t in trials if t not in ("T rc=0 at=m u=0 c=ok", "T rc=2 at=none u=- c=ok")]
    assert not bad, proc.stdout + proc.stderr


class _AfterStat:
    """The broker's os, except that the first stat of the entry "f" runs *then* once, right after
    the real stat: the window between checking an entry and opening it."""

    def __init__(self, then):
        self.then, self.done = then, False

    def __getattr__(self, name):
        return getattr(os, name)

    def stat(self, path, *args, **kwargs):
        st = os.stat(path, *args, **kwargs)
        if path == "f" and not self.done:
            self.done = True
            self.then()
        return st


def _copy_tree_of(src: Path, dest: Path) -> None:
    fd = os.open(src, os.O_RDONLY | os.O_DIRECTORY)
    try:
        broker._snapshot(fd, dest)
    finally:
        os.close(fd)


def test_an_entry_swapped_for_a_fifo_after_its_check_does_not_block_the_snapshot(tmp_path, monkeypatch):
    """DS-P3.347: the broker checks the descriptor it opened (non-blocking, no links), not the name
    it checked; a FIFO put in place of a regular file is left out and never blocks the broker."""
    import threading
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_bytes(b"data")

    def swap():
        (src / "f").unlink()
        os.mkfifo(src / "f")
    proxy = _AfterStat(swap)
    monkeypatch.setattr(broker, "os", proxy)
    result = []

    def copy():
        try:
            _copy_tree_of(src, tmp_path / "dest")
            result.append("ok")
        except Exception as exc:             # reported below as the test's own failure
            result.append(repr(exc))
    done = threading.Thread(target=copy, daemon=True)
    done.start()
    done.join(3)
    if done.is_alive():                     # release the blocked open, then report the block
        os.close(os.open(src / "f", os.O_WRONLY | os.O_NONBLOCK))
        done.join(3)
        pytest.fail("the snapshot blocked opening a FIFO swapped in after its check")
    assert proxy.done and stat.S_ISFIFO(os.lstat(src / "f").st_mode), "the swap to a FIFO ran"
    assert result == ["ok"], result
    assert (tmp_path / "dest").is_dir() and not (tmp_path / "dest/f").exists(), "a FIFO is not carried into the snapshot"


def test_a_file_that_grows_during_the_snapshot_is_held_to_the_limit(tmp_path, monkeypatch):
    """DS-P3.347: the limit counts the bytes actually copied, not the size seen at the check or at
    the open: the file grows only after its first bytes have been written to the snapshot."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_bytes(b"x" * 10)
    monkeypatch.setattr(broker, "SNAPSHOT_LIMIT", 64)
    grown = []

    class _GrowOnFirstWrite:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def write(self, data):
            if not grown:
                grown.append(len(data))
                with open(src / "f", "ab") as g:
                    g.write(b"y" * 1000)
            return self.f.write(data)

    def broker_open(path, mode="r", *args, **kwargs):
        f = open(path, mode, *args, **kwargs)
        return _GrowOnFirstWrite(f) if "w" in mode else f
    monkeypatch.setattr(broker, "open", broker_open, raising=False)
    with pytest.raises(OSError, match="larger than a sudo snapshot may be"):
        _copy_tree_of(src, tmp_path / "dest")
    assert grown, "the file grew only after copying began"


def test_no_record_of_the_card_is_reachable_from_the_recipe(tmp_path):
    """DS-P3.347, the broker half of the parent control. On the DS-P3.343 fixture the live record
    was "$MNT.locked" beside the mount point, and a recipe that rewrote it evaded the lock (measured
    on that fixture: with the rewrite the unmount succeeded, without it the unmount refused with 3).
    Under the broker the recipe finds no record beside the mount point, and the broker's state,
    which holds the partitions, the copies and the operation log, does not exist in its view."""
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=False)
    state = _state(tmp_path)
    block = ('MNT=$(mktemp -d); sudo mount "$CARD_ROOT" "$MNT" && echo MOUNTED; sudo mkdir "$MNT/next" && echo MADE; '
             'L=$(ls -A "$TMPDIR") && { [ "$L" = "${MNT##*/}" ] && echo NOTHING-BESIDE || echo BESIDE; } || echo LS-FAILED; '
             f'test -e "{state}" && echo STATE || echo NO-STATE; test -e "{state}/stub.log" && echo LOG || echo NO-LOG; '
             'sudo umount "$MNT" && echo UNMOUNTED; rmdir "$MNT"')
    proc = _run(block, work, {**env, "CARD_ROOT": str(part)})
    expected = ["MOUNTED", "MADE", "NOTHING-BESIDE", "NO-STATE", "NO-LOG", "UNMOUNTED"]
    assert proc.returncode == 0 and proc.stdout.split() == expected, proc.stdout + proc.stderr
    assert [e["event"] for e in _events(tmp_path)] == ["sudo", "mount", "sudo", "sudo", "umount"]
    assert (part / "next").is_dir(), "the sudo write reached the partition"


def test_a_mount_target_reached_through_a_symlink_is_refused(tmp_path):
    """The broker pins the mount point by descriptors without following links, so a checked path
    cannot be swapped for one that leads elsewhere."""
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=False)
    block = ('mkdir "$TMPDIR/real" && ln -s "$TMPDIR/real" "$TMPDIR/link"; '
             'sudo mount "$CARD_ROOT" "$TMPDIR/link"; echo RC=$?; test -e "$TMPDIR/real/uImage" && echo MOUNTED')
    proc = _run(block, work, {**env, "CARD_ROOT": str(part)})
    assert proc.stdout.split() == ["RC=2"], proc.stdout + proc.stderr
    assert any(line.startswith("refused mount onto") for line in _log(tmp_path)), _log(tmp_path)


def test_a_sudo_argv_is_never_read_as_bubblewrap_options(tmp_path):
    """A request that starts with --bind cannot give the sudo sandbox the broker's state."""
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=False)
    state = _state(tmp_path)
    proc = _run(f'MNT=$(mktemp -d); sudo mount "$CARD_ROOT" "$MNT"; sudo --bind "{state}" /x /usr/bin/cat /x/stub.log; '
                'echo RC=$?; sudo umount "$MNT"; rmdir "$MNT"', work, {**env, "CARD_ROOT": str(part)})
    assert "RC=" in proc.stdout and "RC=0" not in proc.stdout and '"event"' not in proc.stdout, proc.stdout


def test_an_argument_cannot_forge_a_log_event(tmp_path):
    work, env = _sandbox(tmp_path)
    _run("sudo true \"$(printf 'x\\nmount /forged')\"", work, env)
    assert [e["event"] for e in _events(tmp_path)] == ["sudo"]
    assert len(_log(tmp_path)) == 1 and "\n" in _log(tmp_path)[0]


def test_the_broker_takes_the_callers_cwd_from_the_kernel_not_the_message(tmp_path):
    """A forged "cwd" outside the mount point does not let a caller inside it unmount the card."""
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=False)
    forged = '{"op": "sudo", "argv": ["umount", "MNT_PATH"], "cwd": "/"}'
    block = ('MNT=$(mktemp -d); sudo mount "$CARD_ROOT" "$MNT"; cd "$MNT"; '
             f'/usr/bin/python3 -I -c {json.dumps(_SEND)} "$(printf %s \'{forged}\' | sed "s#MNT_PATH#$MNT#")"; '
             'echo RC=$?; cd /; sudo umount "$MNT"; echo CLEAN=$?')
    proc = _run(block, work, {**env, "CARD_ROOT": str(part)})
    assert proc.stdout.split() == ["RC=32", "CLEAN=0"], proc.stdout + proc.stderr
    assert "target is busy" in proc.stderr


# A controlled document for the parser tests, so that an edit of the real doc cannot break them.
FIXTURE_SSH = '( set -e\n  ssh "root@$BOARD_IP" "mkdir -p /x" )\n'
FIXTURE_CARD = '( set -e\n  sudo mount "$CARD_ROOT" "$MNT"\n  sudo mkdir -p "$MNT/x" )\n'


def _fixture(card_before: str = "", card_after: str = "", card: str = f"```sh\n{FIXTURE_CARD}```") -> str:
    return (f"# Doc\n\n### Per board\n\n```sh\n{FIXTURE_SSH}```\n\n{card_before}{card}{card_after}\n\n"
            "## Items to confirm\n")


@pytest.mark.parametrize("before,after", [
    ("<div hidden>\n\n", "\n\n</div>"), ('<div style="display: none">\n\n', "\n\n</div>"),
    ('<div style="display:&#9;none">\n\n', "\n\n</div>"), ('<div style="visibility:hidden">\n\n', "\n\n</div>"),
    ('<div style="display: /* collapsed */ none">\n\n', "\n\n</div>"), ('<div style="color: red; DISPLAY:None">\n\n', "\n\n</div>"),
    ("<div hidden />\n\n", "\n\n</div>"),                       # the slash is ignored: the div stays open
    ('<div style="display: none !important">\n\n', "\n\n</div>"), ('<div style="display:none!important">\n\n', "\n\n</div>"),
    ("<template>\n\n", "\n\n</template>"),
    ("<div hidden>\n\n", ""),                                    # unclosed: hides to the end, so refused
])
def test_a_recipe_under_a_hidden_element_is_not_a_recipe(before, after):
    with pytest.raises(AssertionError, match="rendered sh fences under Per board"):
        _blocks(_fixture(before, after))


@pytest.mark.parametrize("before,after", [("", ""), ('<div class="note">\n\n', "\n\n</div>"),
                                          ("<p hidden>\n\n", "\n\n</p>"),     # HTML closes the p at <pre>
                                          ('<div style="--display: none">\n\n', "\n\n</div>")])   # a custom property
def test_a_recipe_under_a_visible_element_is_still_a_recipe(before, after):
    assert _blocks(_fixture(before, after)) == {"ssh": FIXTURE_SSH, "card": FIXTURE_CARD}


def test_a_recipe_holding_markup_is_refused():
    raw = '<pre><code class="language-sh">( set -e\n  sudo mount "$CARD_ROOT" "$MNT"\n  sudo mkdir<br>-p "$MNT/x" )\n</code></pre>'
    with pytest.raises(AssertionError, match="holds HTML markup"):
        _blocks(_fixture(card=raw))
    # outside Per board, markup in a code block is not a recipe and is not judged
    other = _fixture() + '\n<pre><code class="language-sh">example<br>outside</code></pre>\n'
    assert _blocks(other) == {"ssh": FIXTURE_SSH, "card": FIXTURE_CARD}


def test_an_untouched_card_is_not_written_back(tmp_path):
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=False)
    before = _snapshot(part)
    fd = os.open(part / "uImage", os.O_RDONLY)       # the original file, whatever replaces its name
    try:
        proc = _run('MNT=$(mktemp -d); sudo mount "$CARD_ROOT" "$MNT"; sudo umount "$MNT"; rmdir "$MNT"',
                    work, {**env, "CARD_ROOT": str(part)})
        assert proc.returncode == 0, proc.stderr
        assert os.fstat(fd).st_nlink == 1, "the original boot image was unlinked (a delete-and-recopy)"
    finally:
        os.close(fd)
    assert _snapshot(part) == before


def test_a_block_that_waits_for_input_or_time_fails(tmp_path, monkeypatch):
    work, env = _sandbox(tmp_path)
    monkeypatch.setattr(__import__(__name__), "RUN_SECONDS", 2)
    read_end, write_end = os.pipe()                 # a readable stdin the block must not inherit
    os.write(write_end, b"INHERITED\n")
    os.close(write_end)
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        out = _run('read x && echo "GOT:$x" || echo NO-STDIN', work, env).stdout.split()
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read_end)
    assert out == ["NO-STDIN"], out
    with pytest.raises(pytest.fail.Exception, match="did not finish within 2 s"):
        _run("sleep 30", work, env)


def test_a_special_file_left_by_a_block_is_refused_without_being_read(tmp_path):
    work, env = _sandbox(tmp_path)
    _run('mkfifo "$FAKEROOT/pipe"', work, env)
    with pytest.raises(AssertionError, match="special files"):
        _no_symlinks(tmp_path)
    with pytest.raises(AssertionError, match="not a regular file"):
        _read(tmp_path / "board/pipe")


@pytest.mark.parametrize("env,required", [({}, False), ({"CI": "true"}, True), ({"RR_BENCH_CONFIG": "/x"}, True),
                                          ({"RR_BENCH_FILE": "/elsewhere/hw.yml"}, True),
                                          ({"DS_COPY_BLOCKS_REQUIRED": "1"}, True)])
def test_a_missing_prerequisite_fails_on_the_bench_and_in_ci_and_skips_elsewhere(monkeypatch, tmp_path, env, required):
    monkeypatch.setattr(__import__(__name__), "BENCH_FILE", tmp_path / "runtimes/hw.local.yml")   # absent
    for key in ("CI", "RR_BENCH_CONFIG", "RR_BENCH_FILE", "DS_COPY_BLOCKS_REQUIRED"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert _bwrap_problem(None) == "bubblewrap (bwrap) is not installed"
    expected = pytest.fail.Exception if required else pytest.skip.Exception
    # catch both outcomes, so the wrong one is a red test rather than a skipped one
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception), match="bubblewrap") as caught:
        _prerequisite(_bwrap_problem(None))
    assert caught.type is expected, caught.type


def test_a_missing_unshare_is_a_named_prerequisite(monkeypatch):
    """DS-P3.345: the broker needs unshare -Urm with shared propagation; without it the guard fails
    where it is required (here: the pre-commit leg's marker) instead of judging without the broker."""
    assert _unshare_problem(None) == "unshare (util-linux) is not installed"
    monkeypatch.setenv("DS_COPY_BLOCKS_REQUIRED", "1")
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception), match="unshare") as caught:
        _prerequisite(_unshare_problem(None))
    assert caught.type is pytest.fail.Exception, caught.type


def test_the_default_bench_descriptor_makes_the_prerequisites_required(monkeypatch, tmp_path):
    """A bench set up the documented way (runtimes/hw.local.yml, no RR_BENCH_CONFIG) fails, never skips."""
    for key in ("CI", "RR_BENCH_CONFIG", "RR_BENCH_FILE", "DS_COPY_BLOCKS_REQUIRED"):
        monkeypatch.delenv(key, raising=False)
    bench = tmp_path / "runtimes/hw.local.yml"
    monkeypatch.setattr(__import__(__name__), "BENCH_FILE", bench)
    assert not _required_here()
    bench.parent.mkdir()
    bench.write_text("bench: {}\n", encoding="utf-8")
    assert _required_here()
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception), match="bubblewrap") as caught:
        _prerequisite(_bwrap_problem(None))
    assert caught.type is pytest.fail.Exception, caught.type


def test_a_block_cannot_reach_the_host(tmp_path):
    """The sandbox itself: its own network namespace, no host file outside the temp tree, no new privileges."""
    work, env = _sandbox(tmp_path)
    host_only = tmp_path.parent / f"{tmp_path.name}-host-only"
    host_only.write_text("host\n", encoding="utf-8")
    try:
        probe = (f'readlink /proc/self/ns/net; test -e "{host_only}" && echo HOSTFILE; '
                 "sed -n 's/^NoNewPrivs:[[:space:]]*/nnp=/p' /proc/self/status")
        proc = _run(probe, work, env)
    finally:
        host_only.unlink()
    lines = proc.stdout.split()
    assert len(lines) == 2, proc.stdout + proc.stderr
    assert lines[0] != os.readlink("/proc/self/ns/net"), "the block shares the host network namespace"
    assert lines[1] == "nnp=1", lines
    assert "HOSTFILE" not in proc.stdout


def test_the_ssh_route_stops_at_the_first_failed_copy(tmp_path):
    work, env = _sandbox(tmp_path)
    proc = _run(_blocks(DOC.read_text(encoding="utf-8"))["ssh"], work, {**env, "FAIL_CAT": "1"})
    assert proc.returncode != 0
    _no_symlinks(tmp_path)
    cats = [ln for ln in _read(tmp_path / "board/ssh.log").decode().splitlines() if "\tcat " in ln]
    assert len(cats) == 1, cats


def test_the_card_route_delivers_every_source_file_and_unmounts(tmp_path):
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=True)
    proc = _run(_blocks(DOC.read_text(encoding="utf-8"))["card"], work, {**env, "CARD_ROOT": str(part)})
    assert proc.returncode == 0, proc.stderr
    _no_symlinks(tmp_path)
    landed = part / DEST / "sw/telemetry"
    assert sorted(p.name for p in landed.iterdir()) == SOURCES
    for name in SOURCES:
        assert _read(landed / name) == SOURCES_BYTES[name], name
    assert [ln.split()[0] for ln in _log(tmp_path) if not ln.startswith("sudo")] == ["mount", "umount"]
    assert list((tmp_path / "tmp").iterdir()) == [], "the mount point is removed"


def test_a_wrong_partition_refuses_with_no_write_and_still_unmounts(tmp_path):
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=False)
    before = _snapshot(part)
    proc = _run(_blocks(DOC.read_text(encoding="utf-8"))["card"], work, {**env, "CARD_ROOT": str(part)})
    assert proc.returncode != 0
    _no_symlinks(tmp_path)
    assert _snapshot(part) == before, "nothing may be written to a partition that is not the root"
    others = [ln for ln in _log(tmp_path)
              if ln.startswith("sudo ") and not ln.startswith(("sudo mount ", "sudo umount "))]
    assert others == [], f"only mount and umount may run before the partition is confirmed: {others}"
    assert [ln.split()[0] for ln in _log(tmp_path) if not ln.startswith("sudo")] == ["mount", "umount"]
    assert list((tmp_path / "tmp").iterdir()) == [], "the unmount and the mount point removal succeeded"


def test_a_failed_copy_still_unmounts(tmp_path):
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=True)
    proc = _run(_blocks(DOC.read_text(encoding="utf-8"))["card"], work,
                {**env, "CARD_ROOT": str(part), "FAIL_CP": "1"})
    assert proc.returncode != 0
    _no_symlinks(tmp_path)
    assert [ln.split()[0] for ln in _log(tmp_path) if not ln.startswith("sudo")] == ["mount", "umount"]
    assert list((tmp_path / "tmp").iterdir()) == [], "the unmount and the mount point removal succeeded"


def test_a_symlink_left_by_a_block_is_refused_without_being_followed(tmp_path):
    work, env = _sandbox(tmp_path)
    secret = tmp_path.parent / f"{tmp_path.name}-secret"
    secret.write_text("HOST-SECRET\n", encoding="utf-8")
    try:
        _run(f'ln -s "{secret}" "$FAKEROOT/ssh.log"', work, env)
        with pytest.raises(AssertionError) as caught:
            _no_symlinks(tmp_path)
    finally:
        secret.unlink()
    assert "symlinks" in str(caught.value) and "HOST-SECRET" not in str(caught.value)


def test_a_partition_replaced_by_a_symlink_is_refused_before_any_read(tmp_path):
    """DS-P3.345: a block cannot reach the partition, which lives in broker-only state. A link
    planted there host-side is still refused at the guard's first check, before a snapshot could read
    through it."""
    work, env = _sandbox(tmp_path)
    part = _partition(tmp_path, root=False)
    host_dir = tmp_path.parent / f"{tmp_path.name}-host-dir"
    host_dir.mkdir()
    (host_dir / "secret").write_text("HOST-SECRET\n", encoding="utf-8")
    try:
        proc = _run(f'rm -rf "{part}" && ln -s "{host_dir}" "{part}" && echo SWAPPED', work, env)
        assert "SWAPPED" not in proc.stdout and not part.is_symlink(), "a recipe reached the partition"
        shutil.rmtree(part)               # planted host-side: the guard's own reads must not follow it
        part.symlink_to(host_dir)
        for check in (lambda: _no_symlinks(part.parent), lambda: _snapshot(part), lambda: _read(part / "secret")):
            with pytest.raises(AssertionError) as caught:
                check()
            assert "HOST-SECRET" not in str(caught.value)
    finally:
        shutil.rmtree(host_dir)


def test_the_fixture_comes_from_tracked_regular_files_only(tmp_path):
    """A planted untracked symlink never enters the fixture; a tracked symlink is refused."""
    repo = tmp_path / "repo"
    (repo / "sw/telemetry").mkdir(parents=True)
    for name in ("collectors.py", "__main__.py"):
        (repo / "sw/telemetry" / name).write_text(f"# {name}\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE-BYTES\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    (repo / "sw/telemetry/planted.py").symlink_to(outside)                 # untracked
    assert sorted(_tracked_sources(repo)) == ["__main__.py", "collectors.py"]
    assert b"OUTSIDE-BYTES" not in b"".join(_tracked_sources(repo).values())
    subprocess.run(["git", "-C", str(repo), "add", "sw/telemetry/planted.py"], check=True)  # now tracked
    with pytest.raises(AssertionError, match="symlink"):
        _tracked_sources(repo)


def test_the_de25_row_offers_no_ssh_route():
    (row,) = [ln for ln in DOC.read_text(encoding="utf-8").splitlines() if ln.startswith("| Terasic DE25 |")]
    route = row.strip("|").split("|")[3]
    assert "no SSH server" in route
    rest = route.replace("no SSH server", "")
    assert not re.search(r"(?i)\bssh\b|\bscp\b|\bsftp\b", rest), route
