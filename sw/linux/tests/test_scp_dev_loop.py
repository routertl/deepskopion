"""scp on the board rootfs — the dev-loop file-push path (DS-P3.79 / NET-5).

Dropbear (the running sshd) never ships scp when BR2_PACKAGE_DROPBEAR_SMALL=y
(the Buildroot default, unchanged here — dropbear stays small for size), so
host->board file pushes during dev fell back to `ssh root@board 'cat > /path'
< file`. The fix adds a real OpenSSH scp client via BR2_PACKAGE_OPENSSH(+
_CLIENT), staged into rootfs_overlay/ by stage_openssh_scp_overlay.sh
(extract_python_overlay.sh is deliberately python-only, so it never reaches
the deployed overlay on its own — the same gap stage_dropbear_overlay.sh /
stage_can_overlay.sh close for their binaries).

While wiring this up, `make deepskopion_defconfig` revealed a live defect:
BR2_PACKAGE_OPENSSH_SERVER defaults to y once BR2_PACKAGE_OPENSSH is enabled
(Buildroot's package/openssh/Config.in), so the original commit that added
BR2_PACKAGE_OPENSSH without an explicit `# BR2_PACKAGE_OPENSSH_SERVER is not
set` silently pulled sshd into the build too — exactly the "would clash [with
dropbear]" scenario its own comment warned against. `test_defconfig_...`
below is the regression guard: it fails against the pre-fix defconfig (proven
by running it there) and would catch a future edit that re-adds
BR2_PACKAGE_OPENSSH without the disable line.
"""

import os
import subprocess
from pathlib import Path

import pytest
REPO = Path(__file__).resolve().parents[3]
DEFCONFIG = REPO / "sw" / "linux" / "buildroot_deepskopion_defconfig"
BOARD_HOST = os.environ.get("DEEPSKOPION_BOARD_HOST")


def _defconfig_lines():
    return DEFCONFIG.read_text().splitlines()


# ── static: the defconfig must build a working, non-clashing scp ───────────


def test_defconfig_declares_scp_without_sshd_clash():
    lines = _defconfig_lines()
    assert "BR2_PACKAGE_OPENSSH=y" in lines, (
        f"{DEFCONFIG} does not enable BR2_PACKAGE_OPENSSH — dropbear alone "
        "(BR2_PACKAGE_DROPBEAR_SMALL defaults to y in Buildroot, which "
        "strips dropbear's own scp) leaves the board with no scp binary."
    )
    assert "BR2_PACKAGE_OPENSSH_CLIENT=y" in lines, (
        f"{DEFCONFIG} enables BR2_PACKAGE_OPENSSH but not "
        "BR2_PACKAGE_OPENSSH_CLIENT — the client programs (scp/ssh/sftp) "
        "would not be built."
    )
    assert "# BR2_PACKAGE_OPENSSH_SERVER is not set" in lines, (
        f"{DEFCONFIG} enables BR2_PACKAGE_OPENSSH without explicitly "
        "disabling BR2_PACKAGE_OPENSSH_SERVER. Buildroot defaults that "
        "option to y once OPENSSH is on (package/openssh/Config.in), so "
        "omitting the disable line silently builds sshd alongside dropbear "
        "-- the exact clash the defconfig's own comment warns against. "
        "Verify with: cd sw/linux/buildroot && make deepskopion_defconfig "
        "&& grep OPENSSH_SERVER .config"
    )


# ── static: the staged overlay build artifact, when present, must be real ──


def _staged_rootfs():
    for out in (REPO / "sw" / "linux" / "out").glob("*/rootfs"):
        if (out / "usr" / "bin" / "scp").exists():
            return out
    return None


@pytest.mark.skipif(
    _staged_rootfs() is None,
    reason="no built rootfs under sw/linux/out/*/rootfs (run rr linux rootfs first)",
)
def test_staged_rootfs_scp_is_a_real_arm_binary_with_its_libs():
    rootfs = _staged_rootfs()
    scp = rootfs / "usr" / "bin" / "scp"
    out = subprocess.run(["file", str(scp)], capture_output=True, text=True).stdout
    assert "ELF" in out and "ARM" in out, (
        f"{scp} is not an ARM ELF binary ({out!r}) — stage_openssh_scp_overlay.sh "
        "may have copied the wrong file, or the buildroot output tree is for "
        "the wrong target."
    )
    for lib in ("libssl.so.3", "libcrypto.so.3"):
        assert (rootfs / "usr" / "lib" / lib).exists(), (
            f"{rootfs}/usr/lib/{lib} missing — scp is dynamically linked "
            "against OpenSSL (dropbear-small never needed libssl/libcrypto, "
            "so python_overlay does not provide them); "
            "stage_openssh_scp_overlay.sh must stage it alongside scp or "
            "the binary fails to start on the board with a missing-library "
            "error."
        )
    assert not (rootfs / "usr" / "sbin" / "sshd").exists(), (
        f"{rootfs}/usr/sbin/sshd present — BR2_PACKAGE_OPENSSH_SERVER got "
        "built despite the defconfig disabling it; it would clash with "
        "dropbear on port 22."
    )


# ── live: the board actually has a working scp (skips off-bench) ──────────


def _on_bench():
    return bool(BOARD_HOST) and (
        subprocess.run(
            ["ping", "-c", "1", "-W", "1", BOARD_HOST], capture_output=True
        ).returncode
        == 0
    )


bench = pytest.mark.skipif(
    "not _on_bench()",
    reason="DEEPSKOPION_BOARD_HOST is unset or does not answer ping",
)


@bench
def test_board_has_working_scp_and_round_trips_a_file(tmp_path):
    ssh_opts = ["-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new"]
    which = subprocess.run(
        ["ssh", *ssh_opts, f"root@{BOARD_HOST}", "which scp"],
        capture_output=True, text=True, timeout=15,
    )
    assert which.returncode == 0 and which.stdout.strip(), (
        "`which scp` failed on the board — the running image predates this "
        "fix (redeploy: rr linux rootfs && rr linux kernel --refresh && "
        "rr linux sdcard, then flash/netboot the new uImage) or the live "
        "dev-loop push (rr linux push-sw, or the ssh 'cat >' fallback "
        "sw/linux/stage_openssh_scp_overlay.sh documents) was never applied."
    )

    payload = f"DS-P3.79 scp round-trip {which.stdout!r}\n"
    src = tmp_path / "scp_roundtrip.txt"
    src.write_text(payload)
    dst = "/tmp/ds_p3_79_scp_roundtrip_test.txt"
    try:
        push = subprocess.run(
            ["scp", *ssh_opts, str(src), f"root@{BOARD_HOST}:{dst}"],
            capture_output=True, text=True, timeout=20,
        )
        assert push.returncode == 0, f"host->board scp failed: {push.stderr}"

        readback = subprocess.run(
            ["ssh", *ssh_opts, f"root@{BOARD_HOST}", f"cat {dst}"],
            capture_output=True, text=True, timeout=15,
        )
        assert readback.stdout == payload, (
            f"round-tripped content mismatch: sent {payload!r}, board has "
            f"{readback.stdout!r}"
        )
    finally:
        subprocess.run(
            ["ssh", *ssh_opts, f"root@{BOARD_HOST}", f"rm -f {dst}"],
            capture_output=True, timeout=15,
        )
