# rr unknown-llm (AI-generated)
"""DS-P2.264: the DE25 P2 writer refuses unsafe writes and writes only P2.

p2write.sh runs on the host against stubs: a file stands in for /dev/mmcblk0p2
(P2W_DEV), plain files for /proc/mounts, /proc/partitions and /proc/cmdline,
and stub `tftp` / `nc` / `blockdev` on PATH. Each refusal is checked by its
effect (exit 1, the REFUSE line, and the device bytes), and each is paired with
the positive control so a script that always refuses cannot pass.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "overlay" / "usr" / "sbin" / "p2write.sh"
INIT = HERE / "overlay" / "init"

_TFTP = r"""#!/bin/sh
# tftp -g -r REMOTE -l LOCAL HOST ; LOCAL "-" = stdout. Serves $STUB_TFTP_ROOT/REMOTE.
while [ $# -gt 0 ]; do
  case "$1" in -g) ;; -r) shift; R=$1;; -l) shift; L=$1;; *) H=$1;; esac; shift
done
SRC="$STUB_TFTP_ROOT/$R"
[ -f "$SRC" ] || { echo "tftp: server error: (1) File not found" >&2; exit 1; }
if [ "$L" = "-" ]; then cat "$SRC"; exit 0; fi
# write pass: conv=notrunc keeps the rest of the "partition" as it was
dd if="$SRC" of="$L" bs=65536 conv=notrunc 2>/dev/null
if [ -n "$STUB_CORRUPT_WRITE" ]; then printf '\377' | dd of="$L" bs=1 seek=7 conv=notrunc 2>/dev/null; fi
exit 0
"""
_NC = '#!/bin/sh\ncat > "$STUB_NC_OUT"\n'
_BLOCKDEV = '#!/bin/sh\n[ "$1" = --getsize64 ] && stat -c %s "$2"\n'


def _setup(tmp: Path, image: bytes, part_size: int = 4096, mounted: bool = False,
           sha: str | None = None, nbytes: int | None = None, port: str = "9999"):
    bin_ = tmp / "bin"; bin_.mkdir()
    for name, body in (("tftp", _TFTP), ("nc", _NC), ("blockdev", _BLOCKDEV)):
        (bin_ / name).write_text(body); (bin_ / name).chmod(0o755)
    srv = tmp / "srv"; srv.mkdir()
    (srv / "img.ext4").write_bytes(image)
    dev = tmp / "mmcblk0p2"
    old = bytes([0xA5]) * part_size
    dev.write_bytes(old)
    (tmp / "mounts").write_text(
        (f"{dev} / ext4 rw 0 0\n" if mounted else "") + "proc /proc proc rw 0 0\n")
    (tmp / "partitions").write_text("major minor  #blocks  name\n 179 2 4 mmcblk0p2\n")
    s = sha or hashlib.sha256(image).hexdigest()
    n = len(image) if nbytes is None else nbytes
    (tmp / "cmdline").write_text(
        f"console=ttyS1 root=/dev/ram0 ds_ip=192.0.2.2/24 p2w_host=192.0.2.1 "
        f"p2w_img=img.ext4 p2w_sha={s} p2w_bytes={n} p2w_port={port}\n")
    (tmp / "t").mkdir()
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "P2W_DEV": str(dev), "P2W_MOUNTS": str(tmp / "mounts"),
           "P2W_PARTITIONS": str(tmp / "partitions"), "P2W_CMDLINE": str(tmp / "cmdline"),
           "TMPDIR": str(tmp / "t"), "STUB_TFTP_ROOT": str(srv), "STUB_NC_OUT": str(tmp / "rollback.bin")}
    return env, dev, old


def _run(env, extra=None):
    e = dict(env); e.update(extra or {})
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=e)


IMAGE = bytes(range(256)) * 8  # 2048 bytes


def test_positive_control_writes_p2_and_reads_back_equal(tmp_path):
    env, dev, old = _setup(tmp_path, IMAGE)
    r = _run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "P2W DONE" in r.stdout
    got = dev.read_bytes()
    assert got[:len(IMAGE)] == IMAGE and got[len(IMAGE):] == old[len(IMAGE):]
    assert (tmp_path / "rollback.bin").read_bytes() == old
    assert f"ROLLBACK bytes={len(old)} sha256={hashlib.sha256(old).hexdigest()}" in r.stdout


def test_R1_refuses_when_p2_is_mounted_and_writes_nothing(tmp_path):
    env, dev, old = _setup(tmp_path, IMAGE, mounted=True)
    r = _run(env)
    assert r.returncode == 1 and f"P2W REFUSE R1 {dev} is mounted" in r.stdout, r.stdout
    assert dev.read_bytes() == old and not (tmp_path / "rollback.bin").exists()


def test_R1_refuses_an_image_larger_than_p2(tmp_path):
    env, dev, old = _setup(tmp_path, IMAGE, part_size=1024)
    r = _run(env)
    assert r.returncode == 1 and "P2W REFUSE R1 image 2048 bytes exceeds" in r.stdout, r.stdout
    assert dev.read_bytes() == old and not (tmp_path / "rollback.bin").exists()


def test_R2_refuses_a_served_image_with_another_sha256_before_any_write(tmp_path):
    env, dev, old = _setup(tmp_path, IMAGE, sha="0" * 64)
    r = _run(env)
    assert r.returncode == 1 and "P2W REFUSE R2 sha256" in r.stdout and "nothing written" in r.stdout, r.stdout
    assert dev.read_bytes() == old


def test_R2_refuses_a_size_mismatch_before_any_write(tmp_path):
    env, dev, old = _setup(tmp_path, IMAGE, nbytes=len(IMAGE) - 1)
    r = _run(env)
    assert r.returncode == 1 and "P2W REFUSE R2 bytes" in r.stdout, r.stdout
    assert dev.read_bytes() == old


def test_R3_refuses_when_the_readback_differs(tmp_path):
    env, dev, old = _setup(tmp_path, IMAGE)
    r = _run(env, {"STUB_CORRUPT_WRITE": "1"})
    assert r.returncode == 1 and "P2W REFUSE R3 readback" in r.stdout, r.stdout
    assert "P2W DONE" not in r.stdout


def test_restore_leg_skips_the_rollback_copy_only(tmp_path):
    env, dev, old = _setup(tmp_path, IMAGE, port="none")
    r = _run(env)
    assert r.returncode == 0 and "ROLLBACK skipped (p2w_port=none)" in r.stdout, r.stdout
    assert not (tmp_path / "rollback.bin").exists()
    assert dev.read_bytes()[:len(IMAGE)] == IMAGE


def test_the_writer_and_init_name_no_block_device_but_p2():
    for f in (SCRIPT, INIT):
        text = f.read_text(encoding="utf-8")
        devs = set(re.findall(r"/dev/(?:mmcblk\w*|mtd\w*|sd[a-z]\w*|nvme\w*)", text))
        assert devs <= {"/dev/mmcblk0p2"}, f"{f.name} names {sorted(devs)}"
        for word in ("saveenv", "fw_setenv", "mmcblk0p1", "mtd", "flash_erase", "boot0"):
            assert word not in text, f"{f.name} contains {word!r}"


# ── the built rootfs carries every command the writer runs ─────────────────────

import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("check_applets", HERE / "check_applets.py")
CA = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(CA)


def _fake_rootfs(tmp: Path, skip: str = "") -> Path:
    root = tmp / "rootfs"
    for d in CA.DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "bin" / "busybox").write_bytes(b"\x7fELF")
    for name in CA.REQUIRED:
        if name != skip:
            (root / "bin" / name).symlink_to("busybox")
    return root


def test_applet_check_accepts_a_complete_rootfs(tmp_path):
    assert CA.main(["x", str(_fake_rootfs(tmp_path))]) == 0


def test_applet_check_refuses_a_rootfs_without_tftp(tmp_path, capsys):
    assert CA.main(["x", str(_fake_rootfs(tmp_path, skip="tftp"))]) == 1
    assert "missing: tftp" in capsys.readouterr().out


def test_the_applet_list_covers_every_command_the_scripts_run():
    """Each listed command is used, and the scripts use no external command
    that is not listed (builtins excluded)."""
    import re as _re
    builtins = {"echo", "exit", "eval", "set", "for", "do", "done", "if", "then", "fi",
                "else", "while", "case", "esac", "in", "say", "refuse", "arg", "wait",
                "return", "export", "read", "true", "false", "break", "local", "[", "test"}
    text = SCRIPT.read_text() + "\n" + INIT.read_text()
    code = "\n".join(l.split("#", 1)[0] for l in text.splitlines())
    # printed messages are not commands: blank a quoted string unless it holds a $( ... )
    code = _re.sub(r'"[^"]*"', lambda m: m.group(0) if "$(" in m.group(0) else '""', code)
    words = set(_re.findall(r"(?:^|[|;&(]|\$\(|\b(?:then|do|else)\s)\s*([a-z][a-z0-9]*)\b", code, _re.M))
    used = {w for w in words if w not in builtins and not w.startswith("p2w")}
    assert used <= set(CA.REQUIRED), f"scripts run commands not checked: {sorted(used - set(CA.REQUIRED))}"


_BUILT = Path(os.environ.get("P2W_ROOTFS", str(HERE.parents[3] / "sw/linux/de25/out/de25_p2writer/rootfs")))


@pytest.mark.skipif(not _BUILT.is_dir(), reason="no built de25_p2writer rootfs (rr linux rootfs, or set P2W_ROOTFS)")
def test_the_built_rootfs_carries_every_writer_command_and_the_overlay():
    assert CA.missing(_BUILT) == [], f"built rootfs lacks {CA.missing(_BUILT)}"
    for rel in ("init", "usr/sbin/p2write.sh"):
        assert (_BUILT / rel).read_bytes() == (HERE / "overlay" / rel).read_bytes(), rel
        assert os.access(_BUILT / rel, os.X_OK), rel
