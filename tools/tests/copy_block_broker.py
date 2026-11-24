# SPDX-License-Identifier: MPL-2.0
"""DS-P3.345: the broker of the telemetry copy-block fixture.

The guard (test_telemetry_copy_blocks_execute_ds_p3_337.py) starts this script under
``unshare -Urm``: it is root of its own user and mount namespace, with no privilege on the host.
It makes the run tree a shared mount, signals ready, and starts the recipe's bubblewrap sandbox,
in which the recipe has no capabilities. The sandbox reaches the broker only through ``sudo``, a
thin client that sends ``{"op": "sudo", "argv": [...]}`` over a UNIX socket.

- ``sudo mount <partition> <mount point>`` (exactly two arguments): the partition is copied twice
  into broker-only state, a copy that sudo commands write and a view the recipe sees. A read-only
  bind is made of the view and bound onto the mount point, so the recipe sees a read-only card, and
  an unprivileged write or chmod there fails with EROFS at the write itself. (Binding a read-write
  mount and then remounting it read-only does NOT reach the sandbox read-only, measured on this
  bench; the bind has to be made from a read-only mount.) The mount point is pinned by an
  O_NOFOLLOW descriptor walk that keeps its parent directory open and checks the final component;
  after staging, the broker opens that name again through the parent, binds onto that descriptor,
  then reopens the name and requires the view's root (dev, inode). On any failure from the staging
  to the registration it detaches what it mounted (MNT_DETACH), removes the copy and the view on a
  best-effort basis, and refuses. If a detach itself fails, the error escapes the service loop with
  no reply and ends the run.
- ``sudo umount <mount point>`` (exactly one): refused, busy, while the caller's working directory
  is inside it. Otherwise the copy is written back to the partition only if it changed.
- ``sudo <any other command>``: run in a second sandbox with the same isolation that sees no
  recipe-writable tree: a snapshot of the caller's working directory, made by descriptors
  (directories, regular files, and the link text of symlinks listed as files; a symlink to a
  directory and every special file are left out; at most 64 MiB of regular-file bytes), and each
  mounted copy read-write at its mount point. When a worker invocation ends, the broker refreshes
  each view from its copy by value, in order and without special files, so the recipe's card view
  and the worker's writable card copy never share an inode. The refresh empties the view first and
  is held to the same 64 MiB limit; a larger copy raises there, which leaves that view partly
  rebuilt and the later views unrefreshed (TODO(DS-P3.351): the refresh limit and its failure mode
  — a card copy above 64 MiB ends the run with a half-built view).

Outside the threat model: a recipe can rename an ancestor of a mount point (TMPDIR itself
included: the run tree is the recipe's), so the path it asked to mount no longer names the mount.
Every broker operation after the mount addresses it through the descriptors pinned at mount time,
and a sudo command sees the card at the path inside its own namespace, so nothing is written
without sudo and no operation escapes the log; the recipe only confuses itself.

Input is hostile: any other op or argv shape is refused and logged. The caller's working directory
and environment come from SO_PEERCRED and /proc, never from the message. The operation log, the
partitions and the copies live in a state directory no sandbox has bound.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import selectors
import shutil
import socket
import stat
import struct
import subprocess
import sys
from pathlib import Path

#: The sandbox's sudo: a thin client of the broker, written into the run tree by the guard.
CLIENT = r'''import json, os, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
# the socket path is too long for AF_UNIX; reach it through a directory fd (cwd is left alone,
# because the broker reads it)
d, name = os.path.split(os.environ["COPY_BROKER"])
fd = os.open(d, os.O_PATH | os.O_DIRECTORY)
s.connect(f"/proc/self/fd/{fd}/{name}")
s.sendall(json.dumps({"op": "sudo", "argv": sys.argv[1:]}).encode())
s.shutdown(socket.SHUT_WR)
data = b""
while chunk := s.recv(65536):
    data += chunk
reply = json.loads(data)
sys.stdout.write(reply["stdout"])
sys.stderr.write(reply["stderr"])
sys.exit(reply["rc"])
'''
SUDO_STUB = '#!/bin/sh\nexec /usr/bin/python3 -I "$COPY_BROKER_CLIENT" "$@"\n'
#: The caller's environment keys a sudo command keeps (FAIL_CP is the fixture's failure switch).
SUDO_ENV = ("PATH", "TMPDIR", "HOME", "FAKEROOT", "BOARD_IP", "CARD_ROOT", "FAIL_CP", "COPY_BROKER",
            "COPY_BROKER_CLIENT")


def _sh(*argv: str) -> None:
    subprocess.run(list(argv), check=True, capture_output=True)


def _state(top: Path) -> str:
    """Type, mode, mtime, size and path of every entry, to tell whether a tree changed."""
    out = subprocess.run(["find", ".", "-printf", r"%y %m %T@ %s %p\n"], cwd=top, check=True,
                         capture_output=True, text=True).stdout
    return "\n".join(sorted(out.splitlines()))


_DIR = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
_LIBC = ctypes.CDLL(None, use_errno=True)
_UMOUNT_NOFOLLOW = 8
_MNT_DETACH = 2


def _detach(path: str) -> None:
    """Lazily unmount *path*: it leaves the mount table at once, even while referenced."""
    if _LIBC.umount2(path.encode(), _MNT_DETACH) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"umount2 {path}: {os.strerror(err)}")
SNAPSHOT_LIMIT = 64 << 20          # bytes a sudo command's working-directory snapshot may hold


def _pin(run: Path, target: str) -> tuple[int, str] | None:
    """The parent directory of *target* (which must lie under run/tmp, written as a plain absolute
    path) opened component by component without following any symlink, and the last component's
    name; None if any component is a symlink, missing, or not a directory. The run directory itself
    is created by the host and is not the recipe's to replace."""
    rel = os.path.relpath(os.path.normpath(target), run)
    parts = rel.split(os.sep)
    if rel.startswith("..") or len(parts) < 2 or parts[0] != "tmp" or any(p in ("", ".", "..") for p in parts):
        return None
    fd = os.open(run, os.O_PATH | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
        os.close(os.open(parts[-1], _DIR, dir_fd=fd))     # the target itself: a real directory
    except OSError:
        os.close(fd)
        return None
    return fd, parts[-1]


def _inside(cwd_fd: int, root_fd: int) -> bool:
    """Whether the directory open as *cwd_fd* is the mount root open as *root_fd*, or below it,
    by (device, inode) while walking up: no path is resolved."""
    target = os.fstat(root_fd)
    key = (target.st_dev, target.st_ino)
    fd = os.dup(cwd_fd)
    try:
        while True:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) == key:
                return True
            up = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            same = os.fstat(up)
            os.close(fd)
            fd = up
            if (same.st_dev, same.st_ino) == (st.st_dev, st.st_ino):
                return False
    finally:
        os.close(fd)


def _snapshot(src_fd: int, dest: Path) -> None:
    """Copy the tree open as *src_fd* into a new *dest* (see _copy_tree)."""
    dest.mkdir()
    _copy_tree(src_fd, dest)


def _copy_tree(src_fd: int, dest: Path) -> None:
    """Copy the tree open as *src_fd* into the existing *dest* by descriptors only (no path is
    re-resolved, no symlink followed): directories, regular files and symlinks (as links), with their
    modes and mtimes; sockets, FIFOs and devices are left out, so nothing in the copy is the same
    inode as, or a channel back to, the source."""
    total = 0
    for root, _dirs, files, rfd in os.fwalk(".", dir_fd=src_fd, follow_symlinks=False):
        out = dest / root
        out.mkdir(exist_ok=True)
        dst = os.stat(".", dir_fd=rfd)
        os.chmod(out, stat.S_IMODE(dst.st_mode))
        for name in files:
            st = os.stat(name, dir_fd=rfd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                os.symlink(os.readlink(name, dir_fd=rfd), out / name)
            elif stat.S_ISREG(st.st_mode):
                # The name may have been replaced since the check: judge the descriptor actually
                # opened (non-blocking, so a FIFO swapped in cannot stall the broker), and count the
                # bytes actually copied, so a file that grows meanwhile is still held to the limit.
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=rfd)
                with os.fdopen(fd, "rb") as f:
                    st = os.fstat(f.fileno())
                    if not stat.S_ISREG(st.st_mode):
                        continue                  # a special file now: left out, like any other
                    with open(out / name, "wb") as g:
                        while chunk := f.read(1 << 16):
                            total += len(chunk)
                            if total > SNAPSHOT_LIMIT:
                                raise OSError("the working directory is larger than a sudo snapshot may be")
                            g.write(chunk)
                os.chmod(out / name, stat.S_IMODE(st.st_mode))
                os.utime(out / name, ns=(st.st_atime_ns, st.st_mtime_ns))


class Broker:
    def __init__(self, run: Path, state: Path, sandbox: dict) -> None:
        self.run, self.state, self.sandbox = run, state, sandbox
        self.log = state / "stub.log"
        # mount point -> (partition, copy, ro bind, pinned parent fd, mount-root fd, name in parent, view)
        # The worker writes "copy"; the recipe sees a read-only bind of "view", a separate tree the
        # broker refreshes from the copy after each sudo command. No inode a sudo command creates
        # (a socket above all) is ever reachable from the recipe.
        self.mounts: dict[str, tuple[Path, Path, Path, int, int, str, Path]] = {}
        self.count = 0

    def note(self, event: str, **fields) -> None:
        """One JSON record per event: an argument holding a newline cannot forge a second event."""
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, **fields}) + "\n")

    def refuse(self, why: str) -> dict:
        self.note("refused", why=why)
        return {"rc": 2, "stdout": "", "stderr": f"broker: refused: {why}\n"}

    def serve(self, conn: socket.socket) -> None:
        cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid = struct.unpack("3i", cred)[0]
        data = b""
        while chunk := conn.recv(65536):
            data += chunk
            if len(data) > 1 << 20:
                break
        try:
            msg = json.loads(data)
        except ValueError:
            reply = self.refuse("a message that is not JSON")
        else:
            reply = self.handle(msg, pid)
        conn.sendall(json.dumps(reply).encode())
        conn.close()

    def handle(self, msg, pid: int) -> dict:
        if not isinstance(msg, dict) or set(msg) - {"op", "argv", "cwd"} or msg.get("op") != "sudo":
            return self.refuse(f"op {msg.get('op') if isinstance(msg, dict) else msg!r}")
        argv = msg.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            return self.refuse("an argv that is not a non-empty list of strings")
        # "cwd" in the message is ignored: the caller's own working directory is the kernel's.
        try:
            cwd_fd = os.open(f"/proc/{pid}/cwd", os.O_RDONLY | os.O_DIRECTORY)
            environ = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
        except OSError as exc:
            return self.refuse(f"a caller whose /proc entry cannot be read ({exc})")
        env = dict(e.decode(errors="replace").split("=", 1) for e in environ if b"=" in e)
        self.note("sudo", argv=argv)
        try:
            if argv[0] == "mount":
                return self.mount(argv[1:])
            if argv[0] == "umount":
                return self.umount(argv[1:], cwd_fd)
            return self.command(argv, cwd_fd, env)
        finally:
            os.close(cwd_fd)

    def mount(self, args: list[str]) -> dict:
        if len(args) != 2:
            return self.refuse(f"mount with {len(args)} arguments (the recipe passes <partition> <mount point>)")
        part, mnt = Path(os.path.realpath(args[0])), os.path.normpath(args[1])
        if not part.is_dir() or self.state / "parts" not in part.parents:   # broker-only state: no recipe access
            return self.refuse(f"mount of {args[0]}, which is not a fixture partition")
        pinned = None if mnt in self.mounts else _pin(self.run, mnt)
        if pinned is None:
            return self.refuse(f"mount onto {args[1]}, which is not a free directory under TMPDIR reached without symlinks")
        parent, name = pinned
        self.count += 1
        copy, ro = self.state / f"copy{self.count}", self.state / f"ro{self.count}"
        view = self.state / f"view{self.count}"
        # Everything from here to the registration is undone on any failure: the recipe may move or
        # remove the mount point at any moment (a mount point itself cannot be renamed, EBUSY).
        target = root = None
        ro_mounted = opening = placed = False
        try:
            _sh("cp", "-a", str(part), str(copy))
            _sh("cp", "-a", str(part), str(view))
            ro.mkdir()
            _sh("mount", "--bind", str(view), str(ro))
            ro_mounted = True
            _sh("mount", "-o", "remount,bind,ro", str(ro))
            opening = True
            target = os.open(name, _DIR, dir_fd=parent)      # the pinned directory, not a path
            subprocess.run(["mount", "--no-canonicalize", "--bind", str(ro), f"/proc/self/fd/{target}"],
                           check=True, capture_output=True, pass_fds=(target,))
            placed = True
            # The name holds the mount now exactly when it names the read-only view's root (measured).
            root = os.open(name, _DIR, dir_fd=parent)
            got, want = os.fstat(root), os.stat(ro)
            if (got.st_dev, got.st_ino) != (want.st_dev, want.st_ino):
                raise FileNotFoundError(f"{args[1]} no longer names the mount")
        except (OSError, subprocess.CalledProcessError) as exc:
            # MNT_DETACH: a reference into the mount (a recipe's descriptor or working directory)
            # must not stop the rollback; the mount leaves the mount table at once either way.
            if root is not None:
                os.close(root)
            if placed:
                _detach(f"/proc/self/fd/{target}")
            if ro_mounted:
                _detach(str(ro))
            if ro.is_dir():
                ro.rmdir()
            shutil.rmtree(copy, ignore_errors=True)
            shutil.rmtree(view, ignore_errors=True)
            os.close(parent)
            if placed or (opening and target is None):   # the name moved: before the open or after the bind
                return self.refuse(f"mount onto {args[1]}, which was replaced while it was being mounted")
            return self.refuse(f"mount onto {args[1]} failed ({exc})")
        finally:
            if target is not None:
                os.close(target)
        self.mounts[mnt] = (part, copy, ro, parent, root, name, view)
        self.note("mount", path=mnt)
        return {"rc": 0, "stdout": "", "stderr": ""}

    def umount(self, args: list[str], cwd_fd: int) -> dict:
        if len(args) != 1:
            return self.refuse(f"umount with {len(args)} arguments")
        mnt = os.path.normpath(args[0])
        if mnt not in self.mounts:
            return {"rc": 32, "stdout": "", "stderr": f"umount: {args[0]}: not mounted.\n"}
        if _inside(cwd_fd, self.mounts[mnt][4]):
            return {"rc": 32, "stdout": "", "stderr": f"umount: {args[0]}: target is busy.\n"}
        part, copy, ro, parent, root, name, view = self.mounts.pop(mnt)
        self._unmount(ro, parent, root, name)
        shutil.rmtree(view)
        changed = _state(copy) != _state(part) or subprocess.run(
            ["diff", "-r", "-q", "--no-dereference", str(copy), str(part)], capture_output=True).returncode != 0
        if changed:                       # only a tree that changed while mounted is written back
            for entry in part.iterdir():
                shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
            _sh("cp", "-a", f"{copy}/.", f"{part}/")
        shutil.rmtree(copy)
        ro.rmdir()
        self.note("umount", path=mnt)
        return {"rc": 0, "stdout": "", "stderr": ""}

    def command(self, argv: list[str], cwd_fd: int, env: dict[str, str]) -> dict:
        if argv[0] == "cp" and env.get("FAIL_CP") == "1":
            return {"rc": 1, "stdout": "", "stderr": "cp: failed (FAIL_CP)\n"}
        # The worker sees no recipe-writable tree: a snapshot of the caller's working directory
        # (by descriptors, special files left out) at the same path, and the card copies. A socket
        # the recipe planted is not there, so no descriptor can be passed back to it.
        cwd = os.readlink(f"/proc/self/fd/{cwd_fd}")
        self.count += 1
        snap = self.state / f"cwd{self.count}"
        try:
            _snapshot(cwd_fd, snap)
        except OSError as exc:
            shutil.rmtree(snap, ignore_errors=True)
            return self.refuse(f"a sudo command whose working directory cannot be copied ({exc})")
        cmd = list(self.sandbox["worker"]) + ["--ro-bind", str(snap), cwd]
        for mnt, (_part, copy, *_rest) in self.mounts.items():
            cmd += ["--bind", str(copy), mnt]           # sudo writes the card: read-write here only
        cmd += ["--chdir", cwd]
        for key in SUDO_ENV:
            if key in env:
                cmd += ["--setenv", key, "/tmp" if key == "TMPDIR" else env[key]]
        # "--": the request's argv is the command, never more bwrap options (--bind of broker state…)
        try:
            proc = subprocess.run(cmd + ["--"] + argv, capture_output=True, text=True, errors="replace",
                                  stdin=subprocess.DEVNULL, timeout=self.sandbox["seconds"])
        finally:
            shutil.rmtree(snap, ignore_errors=True)
            for _part, copy, *_m, view in self.mounts.values():
                self._refresh(copy, view)
        return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}

    @staticmethod
    def _refresh(copy: Path, view: Path) -> None:
        """Make the recipe's view match the worker's copy, by value: the view's root keeps its inode
        (it is the bind source), everything under it is replaced; special files are not carried."""
        for entry in view.iterdir():
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
        src = os.open(copy, os.O_RDONLY | os.O_DIRECTORY)
        try:
            _copy_tree(src, view)
        finally:
            os.close(src)

    @staticmethod
    def _unmount(ro: Path, parent: int, root: int, name: str) -> None:
        """Unmount by the pinned parent directory and the name, never by a path the recipe could
        swap: a directory cannot be renamed or replaced while something is mounted on it, and
        UMOUNT_NOFOLLOW refuses a symlink. The broker's own descriptor on the mount root is closed
        first, so only the recipe's use makes the unmount busy."""
        os.close(root)
        try:
            if _LIBC.umount2(f"/proc/self/fd/{parent}/{name}".encode(), _UMOUNT_NOFOLLOW) != 0:
                err = ctypes.get_errno()
                raise OSError(err, f"umount2: {os.strerror(err)}")
        finally:
            os.close(parent)
        _sh("umount", str(ro))

    def cleanup(self) -> None:
        for _mnt, (_part, copy, ro, parent, root, name, view) in list(self.mounts.items()):
            try:
                self._unmount(ro, parent, root, name)
            except (OSError, subprocess.CalledProcessError):
                pass
            shutil.rmtree(copy, ignore_errors=True)
            shutil.rmtree(view, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--sandbox", type=Path, required=True, help="JSON: prefix, recipe, tmpdir, seconds")
    ap.add_argument("--ready-fd", type=int, required=True)
    args = ap.parse_args()
    sandbox = json.loads(args.sandbox.read_text(encoding="utf-8"))
    broker = Broker(args.run, args.state, sandbox)
    _sh("mount", "--bind", str(args.run), str(args.run))
    _sh("mount", "--make-shared", str(args.run))
    path = sandbox["socket"]
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dir_fd = os.open(os.path.dirname(path), os.O_PATH | os.O_DIRECTORY)   # AF_UNIX paths are short
    srv.bind(f"/proc/self/fd/{dir_fd}/{os.path.basename(path)}")
    srv.listen(8)
    os.write(args.ready_fd, b"R")                    # the bind and the socket are up
    os.close(args.ready_fd)
    recipe = subprocess.Popen(sandbox["recipe"], stdin=subprocess.DEVNULL)
    sel = selectors.DefaultSelector()
    sel.register(srv, selectors.EVENT_READ)
    try:
        while recipe.poll() is None:
            for _key, _ev in sel.select(timeout=0.05):
                conn, _ = srv.accept()
                broker.serve(conn)
        return recipe.returncode
    finally:
        srv.close()
        os.unlink(path)
        broker.cleanup()


if __name__ == "__main__":
    sys.exit(main())
