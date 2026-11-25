# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.394: the copy-block broker fails closed, keeps what it mounted, and takes a rename barrier.

Before the fix an OSError raised while handling a request (umount2 EINVAL on a mount a racing rename
had moved) ended the broker process: the client read an empty reply (JSONDecodeError) and every later
sudo call failed, so one moved mount read as a cascade of unrelated EBUSY/EEXIST errors. Now the failing
request is refused with the error named, the broker is poisoned and refuses every later request naming
that first error, and a mount it could not undo stays registered for the final cleanup.
"""
from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("copy_block_broker", ROOT / "tools/tests/copy_block_broker.py")
broker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(broker)


def _ask(b, msg: dict) -> dict:
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    ours.sendall(json.dumps(msg).encode())
    ours.shutdown(socket.SHUT_WR)
    b.serve(theirs)
    reply = json.loads(ours.recv(1 << 20))
    ours.close()
    return reply


class _FailingUmount:
    """umount2 that fails with *err*, counting its calls."""

    def __init__(self, err: int) -> None:
        self.err, self.calls = err, 0

    def umount2(self, path, flags):
        self.calls += 1
        ctypes.set_errno(self.err)
        return -1


def _registered_mount(tmp_path: Path, b) -> str:
    """A registry entry like the one a successful mount leaves, on a plain directory."""
    (tmp_path / "tmp" / "m").mkdir(parents=True)
    parent = os.open(tmp_path / "tmp", os.O_PATH | os.O_DIRECTORY)
    root = os.open(tmp_path / "tmp" / "m", os.O_RDONLY | os.O_DIRECTORY)
    mnt = str(tmp_path / "tmp" / "m")
    b.mounts[mnt] = (tmp_path / "part", tmp_path / "copy", tmp_path / "ro", parent, root, "m", tmp_path / "view")
    return mnt


def test_a_failed_unmount_poisons_the_broker_and_the_next_request_is_refused_as_poisoned(tmp_path, monkeypatch):
    b = broker.Broker(tmp_path, tmp_path, {})
    mnt = _registered_mount(tmp_path, b)
    fake = _FailingUmount(22)
    monkeypatch.setattr(broker, "_LIBC", fake)
    first = _ask(b, {"op": "sudo", "argv": ["umount", mnt], "cwd": "/"})
    assert first["rc"] == broker.POISONED_RC == 70
    assert "failed in the broker" in first["stderr"] and "Invalid argument" in first["stderr"]
    assert b.poisoned is not None and "umount2" in b.poisoned
    second = _ask(b, {"op": "sudo", "argv": ["true"], "cwd": "/"})
    assert second["rc"] == 70 and f"broker poisoned by {b.poisoned}" in second["stderr"]
    assert fake.calls == 1                                   # the poisoned broker ran nothing more


def test_a_failed_unmount_keeps_the_mount_registered_for_the_cleanup(tmp_path, monkeypatch):
    b = broker.Broker(tmp_path, tmp_path, {})
    mnt = _registered_mount(tmp_path, b)
    fake = _FailingUmount(16)
    monkeypatch.setattr(broker, "_LIBC", fake)
    _ask(b, {"op": "sudo", "argv": ["umount", mnt], "cwd": "/"})
    assert mnt in b.mounts and b.mounts[mnt][4] == -1        # still ours; its root descriptor is closed
    b.cleanup()
    # the cleanup retried the recipe-side unmount (call 2), then detached the staging bind (call 3)
    assert fake.calls == 3 and b.mounts == {} and b.leftovers == []


def test_a_rollback_whose_detach_fails_poisons_and_keeps_the_staging_bind(tmp_path, monkeypatch):
    state = tmp_path / "state"
    (state / "parts" / "p").mkdir(parents=True)
    (tmp_path / "tmp" / "m").mkdir(parents=True)
    b = broker.Broker(tmp_path, state, {})
    detached = []

    def bind_fails(argv, **kwargs):
        raise subprocess.CalledProcessError(32, argv)

    def detach_fails(path):
        detached.append(path)
        raise OSError(22, f"umount2 {path}: Invalid argument")
    monkeypatch.setattr(broker, "_sh", lambda *argv: None)
    monkeypatch.setattr(broker.subprocess, "run", bind_fails)
    monkeypatch.setattr(broker, "_detach", detach_fails)
    reply = _ask(b, {"op": "sudo", "argv": ["mount", str(state / "parts" / "p"), str(tmp_path / "tmp" / "m")],
                     "cwd": "/"})
    assert reply["rc"] == 70 and "the broker is poisoned" in reply["stderr"] and b.poisoned is not None
    assert b.leftovers == [("ro", state / "ro1")] and detached == [str(state / "ro1")]
    b.cleanup()
    assert detached == [str(state / "ro1")] * 2 and b.leftovers == []


def _mount_request(tmp_path: Path) -> tuple[object, Path, dict]:
    state = tmp_path / "state"
    (state / "parts" / "p").mkdir(parents=True)
    (tmp_path / "tmp" / "m").mkdir(parents=True)
    b = broker.Broker(tmp_path, state, {})
    msg = {"op": "sudo", "argv": ["mount", str(state / "parts" / "p"), str(tmp_path / "tmp" / "m")], "cwd": "/"}
    return b, state, msg


def test_a_rollback_whose_recipe_side_detach_fails_keeps_that_bind_by_its_descriptor(tmp_path, monkeypatch):
    """The bind is placed, the name check then fails, and only the recipe-side detach fails: that bind
    is kept by the broker's own descriptor on the recipe's directory, the staging detach still runs,
    and the cleanup detaches the bind through that very descriptor, then closes it."""
    b, state, msg = _mount_request(tmp_path)
    recipe_dir = os.stat(tmp_path / "tmp" / "m")
    seen = []

    def detach(path):
        if path.startswith("/proc/self/fd/"):
            fd = int(path.rsplit("/", 1)[1])
            seen.append(("bind", os.path.samestat(os.fstat(fd), recipe_dir)))   # identity at call time
            raise OSError(22, f"umount2 {path}: Invalid argument")
        seen.append(("ro", path))
    monkeypatch.setattr(broker, "_sh", lambda *argv: None)
    monkeypatch.setattr(broker.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0))
    monkeypatch.setattr(broker, "_barrier", lambda parent, name: False)     # the name was taken away
    monkeypatch.setattr(broker, "_detach", detach)
    reply = _ask(b, msg)
    assert reply["rc"] == 70 and b.poisoned is not None
    assert seen == [("bind", True), ("ro", str(state / "ro1"))]              # the staging detach still ran
    assert len(b.leftovers) == 1 and b.leftovers[0][0] == "bind"
    fd = b.leftovers[0][1]
    b.cleanup()
    assert seen[-1] == ("bind", True) and b.leftovers == []                  # detached through that descriptor
    with pytest.raises(OSError):
        os.fstat(fd)                                                         # and closed afterwards


def test_an_unexpected_barrier_error_poisons_instead_of_an_ordinary_refusal(tmp_path, monkeypatch):
    b, state, msg = _mount_request(tmp_path)

    def barrier_eio(parent, name):
        raise broker.BarrierFault(5, "rename barrier: Input/output error")
    monkeypatch.setattr(broker, "_sh", lambda *argv: None)
    monkeypatch.setattr(broker.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0))
    monkeypatch.setattr(broker, "_barrier", barrier_eio)
    monkeypatch.setattr(broker, "_detach", lambda path: None)                 # both detaches succeed
    reply = _ask(b, msg)
    assert reply["rc"] == 70 and "Input/output error" in reply["stderr"] and "replaced" not in reply["stderr"]
    assert b.leftovers == []


def test_a_mkdir_error_other_than_eexist_is_a_barrier_fault(tmp_path, monkeypatch):
    d = tmp_path / "d"
    d.mkdir()
    parent = os.open(d, os.O_PATH | os.O_DIRECTORY)

    def eio(*args, **kwargs):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(broker.os, "mkdir", eio)
    try:
        with pytest.raises(broker.BarrierFault):
            broker._barrier(parent, "m")
    finally:
        os.close(parent)


def _placed_mount(monkeypatch):
    """Staging and both binds succeed (no real mounts); the name check then runs for real."""
    monkeypatch.setattr(broker, "_sh", lambda *argv: None)
    monkeypatch.setattr(broker.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0))
    monkeypatch.setattr(broker, "_detach", lambda path: None)


def test_a_broker_error_after_the_bind_poisons_rather_than_reporting_a_moved_name(tmp_path, monkeypatch):
    b, state, msg = _mount_request(tmp_path)
    _placed_mount(monkeypatch)
    real_stat, hits = os.stat, []

    def stat_eio_once(path, *args, **kwargs):           # only the identity check's stat fails
        if str(path) == str(state / "ro1") and not hits:
            hits.append(path)
            raise OSError(5, "Input/output error")
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(broker.os, "stat", stat_eio_once)
    reply = _ask(b, msg)
    assert hits and reply["rc"] == 70 and "Input/output error" in reply["stderr"] and "replaced" not in reply["stderr"]
    assert not (state / "ro1").exists() and not (state / "copy1").exists()   # the rollback completed


@pytest.mark.parametrize("replace", ["remove", "file"])
def test_a_name_removed_or_replaced_after_the_barrier_is_an_ordinary_refusal(tmp_path, monkeypatch, replace):
    """After the barrier saw the name, the recipe removes it, or puts a file there, before the broker
    reopens it: still the recipe's race, so rc 2 and not poisoned."""
    b, state, msg = _mount_request(tmp_path)
    _placed_mount(monkeypatch)
    real_barrier = broker._barrier

    def barrier_then_move(parent, name):
        seen = real_barrier(parent, name)
        os.rename(name, "moved", src_dir_fd=parent, dst_dir_fd=parent)
        if replace == "file":
            os.close(os.open(name, os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=parent))
        return seen
    monkeypatch.setattr(broker, "_barrier", barrier_then_move)
    reply = _ask(b, msg)
    assert reply["rc"] == 2 and "replaced while it was being mounted" in reply["stderr"] and b.poisoned is None


def test_a_name_moved_during_the_bind_is_an_ordinary_refusal_and_the_barrier_directory_is_removed(tmp_path,
                                                                                                    monkeypatch):
    b, state, msg = _mount_request(tmp_path)
    _placed_mount(monkeypatch)
    real_barrier = broker._barrier

    def rename_then_barrier(parent, name):
        os.rename(name, "gone", src_dir_fd=parent, dst_dir_fd=parent)     # the race: the name is taken away
        return real_barrier(parent, name)                                  # which recreates it, empty
    monkeypatch.setattr(broker, "_barrier", rename_then_barrier)
    reply = _ask(b, msg)
    assert reply["rc"] == 2 and "replaced while it was being mounted" in reply["stderr"] and b.poisoned is None
    assert sorted(p.name for p in (tmp_path / "tmp").iterdir()) == ["gone"]   # the barrier's directory is gone


def test_cleanup_reports_a_mount_it_could_not_undo(tmp_path, monkeypatch):
    b = broker.Broker(tmp_path, tmp_path, {})
    _registered_mount(tmp_path, b)
    monkeypatch.setattr(broker, "_LIBC", _FailingUmount(16))
    assert b.cleanup() is False
    assert broker.Broker(tmp_path / "x", tmp_path / "x", {}).cleanup() is True


def test_a_caller_that_hangs_up_does_not_end_the_service(tmp_path, monkeypatch):
    b = broker.Broker(tmp_path, tmp_path, {})
    sizes = iter([1 << 22, 1])               # a reply larger than the socket buffer, then a small one

    def handle(msg, pid):
        return {"rc": 0, "stdout": "x" * next(sizes), "stderr": ""}
    monkeypatch.setattr(b, "handle", handle)
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    ours.sendall(json.dumps({"op": "sudo", "argv": ["true"], "cwd": "/"}).encode())
    ours.shutdown(socket.SHUT_WR)
    ours.close()                                                           # gone before the reply
    b.serve(theirs)                                                        # returns, does not raise
    assert _ask(b, {"op": "sudo", "argv": ["true"], "cwd": "/"})["rc"] == 0


def test_a_poisoned_broker_refuses_even_a_message_that_is_not_json(tmp_path):
    b = broker.Broker(tmp_path, tmp_path, {})
    b.poisoned = "['umount', '/x']: [Errno 22] umount2: Invalid argument"
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    ours.sendall(b"not json")
    ours.shutdown(socket.SHUT_WR)
    b.serve(theirs)
    reply = json.loads(ours.recv(1 << 20))
    ours.close()
    assert reply["rc"] == 70 and f"broker poisoned by {b.poisoned}" in reply["stderr"]


@pytest.mark.parametrize("launch", [subprocess.CalledProcessError(32, ["umount"]),
                                    FileNotFoundError(2, "No such file or directory: 'umount'"),
                                    OSError(24, "Too many open files")])
def test_an_unmount_whose_staging_side_fails_keeps_the_staging_bind(tmp_path, monkeypatch, launch):
    b = broker.Broker(tmp_path, tmp_path, {})
    mnt = _registered_mount(tmp_path, b)
    monkeypatch.setattr(broker, "_LIBC", _FailingUmount(0))
    broker._LIBC.umount2 = lambda path, flags: 0                     # the recipe-side unmount succeeds

    def staging_fails(*argv):
        raise launch
    detached = []
    monkeypatch.setattr(broker, "_sh", staging_fails)
    monkeypatch.setattr(broker, "_detach", detached.append)
    reply = _ask(b, {"op": "sudo", "argv": ["umount", mnt], "cwd": "/"})
    assert reply["rc"] == 70 and b.poisoned is not None
    assert mnt not in b.mounts and b.leftovers == [("ro", tmp_path / "ro")]
    b.cleanup()
    assert detached == [str(tmp_path / "ro")] and b.leftovers == []


def test_the_barrier_takes_the_parent_lock_by_a_mkdir_on_the_name_and_creates_nothing(tmp_path, monkeypatch):
    d = tmp_path / "d"
    (d / "m").mkdir(parents=True)
    (d / "m" / "keep").write_text("recipe's", encoding="utf-8")
    parent = os.open(d, os.O_PATH | os.O_DIRECTORY)
    calls, real_mkdir = [], os.mkdir

    def recording_mkdir(path, *args, dir_fd=None, **kwargs):
        calls.append((path, dir_fd))
        return real_mkdir(path, *args, dir_fd=dir_fd, **kwargs)
    monkeypatch.setattr(broker.os, "mkdir", recording_mkdir)
    try:
        assert broker._barrier(parent, "m") is True
    finally:
        os.close(parent)
    assert calls == [("m", parent)]                                   # a mkdir on that name, in that parent
    assert sorted(p.name for p in d.iterdir()) == ["m"]               # nothing created
    assert (d / "m" / "keep").read_text(encoding="utf-8") == "recipe's"


def test_the_barrier_reports_a_name_that_is_gone(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    parent = os.open(d, os.O_PATH | os.O_DIRECTORY)
    try:
        assert broker._barrier(parent, "m") is False
    finally:
        os.close(parent)


def test_mount_takes_the_rename_barrier_between_the_bind_and_its_name_check():
    """A rename that raced the bind finishes before the broker checks that the name holds the mount."""
    src = (ROOT / "tools/tests/copy_block_broker.py").read_text(encoding="utf-8")
    body = src[src.index("    def mount(self"):src.index("    def umount(self")]
    assert body.index("placed = True") < body.index("_barrier(parent, name)") < body.index("root = os.open(name")
