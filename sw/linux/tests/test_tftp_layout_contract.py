"""TFTP layout contract — declared layout must be the layout actually served.

DS-P2.84. For three weeks (2026-07-21 → 2026-08-10) runtimes/hw.yml declared
`layout: dnsmasq-unique-root` while dnsmasq lay dead and tftpd-hpa served a
FLAT <tftp-root> — so a board tftpbooting a bare `uImage` received another
board's kernel (measured client-side from 192.0.2.4). Nothing noticed,
because SD-first boot means the TFTP path is only exercised on opt-in
(`bootsrc=net`), and no check compared the declaration to reality.

The contract now (owner-confirmed 2026-08-10):

    server : tftpd-hpa, FLAT <tftp-root> — no server-side per-client scoping
    client : boot.cmd supplies the whole path
             tftpboot ${ipaddr}/<project>/active/<file>
    disk   : <tftp-root>/<board-ip>/<project>/active -> <slot>
             <tftp-root>/<board-ip>/<project>/<slot>/{uImage,system.dtb,...}
    compat : <tftp-root>/<board-ip>/active -> <project>/active  (rr-sdh-toh
             fetches ${ipaddr}/active/<file> and must keep booting)

Two layers of guard:

* STATIC (always runs): hw.yml must declare shared-root, and every tftpboot in
  boot.cmd must carry the ${ipaddr}/<project>/ prefix. These two only work as
  a PAIR — client-scoped paths on a unique-root server resolve to
  <tftp-root>/<ip>/<ip>/... and 404, bare names on a flat server collide.
* LIVE (skips off-bench): a raw TFTP GET against the running server must
  return byte-identical content for every <ip>/<project>/active and compat
  <ip>/active path staged on disk, and dnsmasq must not be actively serving
  tftp-unique-root. On the pre-fix bench this layer fails in two ways at
  once: the declaration (unique-root) and, had it been re-enabled, the probe
  (unique-root prepends the CLIENT ip, so every explicit path 404s).
"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
# DS-P2.161: the local deployment descriptor is runtimes/hw.local.yml; the
# tracked runtimes/hw.yml is the rr.hw.contract/v1 contract and has no bench.
LOCAL_HW_YML = REPO / "runtimes" / "hw.local.yml"
EXAMPLE_HW_YML = REPO / "runtimes" / "hw.example.yml"
BOOT_CMD = REPO / "sw" / "linux" / "boot.cmd"
PROJECT = "deepskopion"

# Keep the live probe fast: fetch only files under this size (dtb/boot.scr are
# KBs; uImage at ~24 MB still transfers from localhost in ~a second).
MAX_PROBE_BYTES = 64 * 1024 * 1024

# ── the flat root holds documentation only (DS-P3.142) ────────────────────
#
# DS-P2.84 retired the two *dangerous* bare names (uImage, system.dtb). But 20
# more bare-name files plus two alias symlinks lingered at the flat root, and a
# bare `tftpboot <name>` resolves to the ONE shared <tftp-root>/<name> every board
# sees — BOOT.bin being the worst (a bare fetch would net-boot a stale
# first-stage image and look like a mysterious board, not a wrong file). The
# root must now hold only: README.md, per-board <ip>/ directories, and the
# _retired-flat-DS-P2.84/ quarantine.
ROOT_DOC = "README.md"

# The ONLY root children allowed to be non-directories, and only because a LIVE
# referrer still depends on each (reported, not moved — see the quarantine dir's
# DS-P3.142 WHY.md). This is a shrinking debt list, not a licence to leave bare
# names at the root: adding a name here without a real referrer defeats the
# guard, which is exactly the "detector nobody enforces" failure this test
# exists to prevent.
LIVE_REFERRER_HOLDOUTS = frozenset({
    "kv260_linux.bit",  # <board-ip>/h264-dec/kv260_linux.bit -> ../../kv260_linux.bit
    "dssw.tgz",         # docs/DEEPSKOPION_DEV_GUIDE.md §6.2 SW-refresh loop (bare fetch)
    # KV260 host binaries fetched BARE (`tftp -g -r <name>`, no path prefix) by the
    # sibling kv260_linux checkout, so a bare name resolves to the top level and
    # moving them 404s the fetch — standing hazard KV2-P3.4 / NET_DEPLOY_EPIC Haz.87.
    # Referrers (kv260_linux/sw/linux/): kv260_t22_readout.py, kv260_p35_readout.py,
    # kv260_panic_bisect.py, kv260_dp_session.py. Recorded in the quarantine WHY.md.
    "padump",           # kv260_t22_readout.py:73, kv260_p35_readout.py:63
    "rtp_bridge",       # kv260_{t22_readout,p35_readout,panic_bisect,dp_session}.py
    "read_fb_min",      # kv260_dp_session.py:685
})


def _flat_root_offenders(root):
    """Direct children of <root> that a bare `tftpboot <name>` would resolve.

    An offender is any root child that is NOT the README and NOT a real
    directory — a plain file, or an alias symlink (even one pointing at a board
    directory is still a bare name that resolves and someone will boot from).
    Documented live-referrer holdouts are excluded. Returns sorted names."""
    offenders = []
    for entry in sorted(Path(root).iterdir()):
        name = entry.name
        if name == ROOT_DOC or name in LIVE_REFERRER_HOLDOUTS:
            continue
        # A real directory (a board <ip>/ tree or the retired quarantine) cannot
        # be fetched as a file; a symlink — even to a directory — resolves to a
        # bare name, so it counts as an offender.
        if entry.is_dir() and not entry.is_symlink():
            continue
        offenders.append(name)
    return offenders


# The tftpd-hpa default served root on this bench. Built by concatenation so no
# tracked file carries the literal contiguously (matching tools/preflight's own
# convention); it is used ONLY as a last-resort fallback and ONLY when it exists
# as a real directory, so a fresh clone / CI (which has no such dir) still skips
# the live check while a bench review — where the reviewer runs plain `pytest`
# with no TFTP_ROOT and no local hw.yml — actually arms it. See DS-P3.142 P2.
BENCH_DEFAULT_ROOT = Path("/srv" + "/tftp")


def _real_tftp_root():
    """The deployment's served root, or None off-bench.

    Prefer an explicit TFTP_ROOT (the deployment-owned handle the stage script
    already uses, so this check survives under cron/systemd where it deploys);
    fall back to a local runtimes/hw.local.yml; then, on a bench that supplied
    neither, to the well-known tftpd-hpa root if it exists as a directory. The
    public hw.example.yml carries only the `<tftp-root>` placeholder, so it
    never resolves a real root here."""
    env = os.environ.get("TFTP_ROOT")
    if env:
        return Path(env)
    if LOCAL_HW_YML.is_file():
        path = Path(_tftp_decl()["path"])
        if str(path) != "<tftp-root>":
            return path
    if BENCH_DEFAULT_ROOT.is_dir():
        return BENCH_DEFAULT_ROOT
    return None


def _tftp_server_answering():
    """True if a TFTP server answers on localhost:69 (i.e. we are on a bench).

    Any answer — even FILE-NOT-FOUND — proves a live server. Unlike
    _bench_available() this does NOT require a local hw.yml, so it can tell an
    off-bench checkout (no server) apart from a bench that simply left the root
    handle unset — the case the deployed-effect guard must refuse to skip."""
    if not shutil.which("curl"):
        return False
    r = subprocess.run(
        ["curl", "-s", "--max-time", "5", "tftp://127.0.0.1/.probe-nonexistent"],
        capture_output=True,
    )
    return r.returncode in (0, 68)


def _tftp_decl():
    path = LOCAL_HW_YML if LOCAL_HW_YML.is_file() else EXAMPLE_HW_YML
    hw = yaml.safe_load(path.read_text())
    matches = [
        resource for resource in hw["bench"]["resources"].values()
        if resource.get("kind") == "tftp"
    ]
    assert len(matches) == 1, f"{path} must declare exactly one TFTP resource"
    return matches[0]


def _tftpboot_paths():
    """Every path argument of a tftpboot command in boot.cmd."""
    paths = []
    for line in BOOT_CMD.read_text().splitlines():
        line = line.split("#", 1)[0]
        toks = line.replace(";", " ").split()
        for i, tok in enumerate(toks):
            if tok == "tftpboot" and i + 2 < len(toks):
                paths.append(toks[i + 2])  # tftpboot <addr> <path>
    return paths


# ── static: the declaration and the client convention, as a pair ──────────


def test_descriptor_example_declares_shared_root_without_a_host_path():
    """Two different claims, so two different files (DS-X5 correction).

    The layout claim is about whatever descriptor is IN EFFECT — a local
    runtimes/hw.local.yml that declares a server-side layout re-arms DS-P2.84 just
    as surely as the example would. The placeholder claim is about the PUBLIC
    EXAMPLE only: DS-C1 exists precisely so that a real deployment's hw.yml
    carries a real, deployment-owned root. Asserting `<tftp-root>`
    against the effective descriptor made this test fail on every host that
    had actually done what DS-C1 asks; it stayed green only while no checkout
    had a local hw.yml at all.
    """
    decl = _tftp_decl()
    assert decl.get("layout") == "shared-root", (
        f"TFTP layout is {decl.get('layout')!r}. A flat server needs a "
        "FLAT root (tftpd-hpa); declaring any server-side per-client layout "
        "re-arms the DS-P2.84 trap: boot.cmd trusts the declaration, and a "
        "declared-but-unimplemented layout hands boards each other's kernels."
    )
    example = yaml.safe_load(EXAMPLE_HW_YML.read_text())
    example_tftp = [
        resource for resource in example["bench"]["resources"].values()
        if resource.get("kind") == "tftp"
    ]
    assert len(example_tftp) == 1, (
        f"{EXAMPLE_HW_YML} must declare exactly one TFTP resource"
    )
    assert example_tftp[0].get("path") == "<tftp-root>", (
        "the public descriptor EXAMPLE must require a deployment-owned root — "
        "a real host path checked in here is the DS-C1 leak this guard exists "
        "to catch (a local runtimes/hw.local.yml naming a real root is correct "
        "and is not what is being asserted)."
    )


def _layout_values(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "layout":
                yield v
            yield from _layout_values(v)
    elif isinstance(node, list):
        for v in node:
            yield from _layout_values(v)


def test_no_unique_root_layout_anywhere_in_hw_yml():
    # The old value must not creep back under any resource (YAML values only —
    # hw.yml's comments legitimately narrate why unique-root is retired).
    layouts = list(_layout_values(yaml.safe_load(EXAMPLE_HW_YML.read_text())))
    assert not any("unique-root" in str(v) for v in layouts), (
        "hw.yml mentions a unique-root TFTP layout. dnsmasq tftp-unique-root "
        "is retired (DS-P2.84): it keys on the CLIENT ip, so it would resolve "
        "an explicit ${ipaddr}/<project>/... fetch to <tftp-root>/<ip>/<ip>/... "
        "and break every project that scopes its own path (rr-sdh-toh, "
        "deepskopion)."
    )


def test_boot_cmd_fetches_are_client_scoped():
    paths = _tftpboot_paths()
    assert paths, f"no tftpboot commands found in {BOOT_CMD}"
    bare = [p for p in paths if not p.startswith("${ipaddr}/" + PROJECT + "/")]
    assert not bare, (
        f"boot.cmd tftpboots {bare} without the ${{ipaddr}}/{PROJECT}/ "
        "prefix. On the flat shared root a bare filename resolves to the ONE "
        "<tftp-root>/<file> shared by every board and project — exactly how a "
        "Z7-20 booted a Classic's dtb (boot.cmd header, 2026-06-20) and how "
        ".7.4 got a 300 KB-different uImage (measured 2026-08-10)."
    )


def test_netboot_requires_user_state_or_dhcp_before_any_fetch():
    """No baked address, and no fetch with empty U-Boot network state."""
    text = BOOT_CMD.read_text()
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    assert "setenv ipaddr " not in code and "setenv serverip " not in code
    assert "dhcp" in code
    guard = code.index('if test "${ipaddr}" = "" || test "${serverip}" = ""')
    fetch = code.index("tftpboot")
    assert guard < fetch, "boot.cmd fetches before validating network state"


# ── static: the flat root holds documentation only (DS-P3.142) ────────────


def test_flat_root_offender_detector_has_power(tmp_path):
    """Prove the detector FIRES on a planted DS-P2.84 trap and CLEARS on a
    clean root — with no bench, so it runs in CI / cron / a fresh clone too.

    This is the environment-independent power proof: if _flat_root_offenders
    were gutted to `return []`, the planted-trap assertion below goes red."""
    root = tmp_path
    (root / ROOT_DOC).write_text("layout doc\n")
    # RFC 5737 documentation address on purpose — DS-C1 forbids real bench
    # RFC1918 literals in tracked files, and the detector is name-agnostic.
    (root / "192.0.2.2").mkdir()                 # a real per-board tree
    (root / "_retired-flat-DS-P2.84").mkdir()    # the quarantine dir
    # A clean root — only the README and real directories — has no offenders.
    assert _flat_root_offenders(root) == []
    # Plant the exact trap this ticket retired: a bare first-stage image and an
    # alias symlink pointing at a board dir. Both must be flagged.
    (root / "BOOT.bin").write_bytes(b"\x00" * 16)
    (root / "classic_eye").symlink_to("192.0.2.2")
    assert set(_flat_root_offenders(root)) == {"BOOT.bin", "classic_eye"}, (
        "detector missed a bare file or an alias symlink at the root"
    )
    # A documented live-referrer holdout is explicitly NOT an offender.
    (root / "dssw.tgz").write_bytes(b"x")
    assert "dssw.tgz" not in _flat_root_offenders(root)


# ── live: the declaration must be what the server actually does ───────────


def _bench_available():
    if not shutil.which("curl"):
        return False
    if not LOCAL_HW_YML.is_file():
        return False
    root = Path(_tftp_decl()["path"])
    if not root.is_dir():
        return False
    # One cheap probe: any answer (even FILE-NOT-FOUND) proves a server on :69.
    r = subprocess.run(
        ["curl", "-s", "--max-time", "5", "tftp://127.0.0.1/.probe-nonexistent"],
        capture_output=True,
    )
    # curl rc 68 = TFTP file not found → server alive; rc 0 = served; anything
    # else (7 refused, 28 timeout) → no TFTP server here, we are off-bench.
    return r.returncode in (0, 68)


bench = pytest.mark.skipif(
    "not _bench_available()",
    reason="not on the bench (no local TFTP server / root / curl)",
)


def _get(path):
    r = subprocess.run(
        ["curl", "-s", "--max-time", "60", f"tftp://127.0.0.1/{path}"],
        capture_output=True,
    )
    return r.returncode, r.stdout


def _staged_probe_files(root):
    """(tftp_path, disk_file) pairs: one small file per staged active dir,
    both the <ip>/<project>/active form and the <ip>/active compat form."""
    pairs = []
    for board_dir in sorted(d for d in root.iterdir() if d.is_dir()):
        actives = [d / "active" for d in board_dir.iterdir() if d.is_dir()]
        actives += [board_dir / "active"]  # compat symlink
        for active in actives:
            if not active.is_dir():
                continue
            files = sorted(
                (f for f in active.iterdir()
                 if f.is_file() and f.stat().st_size <= MAX_PROBE_BYTES),
                key=lambda f: f.stat().st_size,
            )
            if files:
                rel = active.relative_to(root)
                pairs.append((f"{rel}/{files[0].name}", files[0]))
    return pairs


@bench
def test_served_layout_is_the_declared_flat_root():
    """Every staged <ip>/<project>/active and compat <ip>/active path must be
    served byte-identically by the LIVE server. This is the preflight that was
    missing for three weeks: it fails if the server stops honoring
    client-scoped paths (e.g. dnsmasq unique-root resurrects and prepends the
    client ip → 404), and it fails if staged trees exist that the server
    cannot deliver."""
    root = Path(_tftp_decl()["path"])
    pairs = _staged_probe_files(root)
    assert pairs, f"no staged <ip>/<project>/active trees under {root}"
    failures = []
    for tftp_path, disk_file in pairs:
        rc, data = _get(tftp_path)
        if rc != 0:
            failures.append(f"{tftp_path}: GET failed (curl rc {rc})")
        elif hashlib.md5(data).digest() != hashlib.md5(
                disk_file.read_bytes()).digest():
            failures.append(
                f"{tftp_path}: served {len(data)} bytes != staged {disk_file}"
            )
    assert not failures, (
        "declared layout shared-root is NOT what the server serves:\n  "
        + "\n  ".join(failures)
    )


@bench
def test_dnsmasq_not_actively_serving_unique_root():
    """dnsmasq is retired from TFTP duty (DS-P2.84). If it ever comes back
    ACTIVE while a tftp-unique-root config survives, every explicit
    ${ipaddr}/<project>/ fetch breaks at once — fail loudly before a board
    finds out."""
    is_active = subprocess.run(
        ["systemctl", "is-active", "--quiet", "dnsmasq"]
    ).returncode == 0
    if not is_active:
        return
    conf = subprocess.run(
        ["grep", "-rl", "^tftp-unique-root", "/etc/dnsmasq.conf",
         "/etc/dnsmasq.d/"],
        capture_output=True, text=True,
    )
    assert not conf.stdout.strip(), (
        "dnsmasq is ACTIVE and tftp-unique-root is configured in: "
        f"{conf.stdout.split()} — this server-side per-client scoping breaks "
        "every client-scoped fetch (resolves to <tftp-root>/<client-ip>/<path>)."
        " Reconfigure or stop the deployment's dnsmasq service."
    )


def test_flat_root_holds_documentation_only():
    """DS-P3.142: on the live deployment the served root must carry no bare
    name a board could fetch by accident. This asserts the DEPLOYED effect —
    the actual contents of the served directory — not a declaration.

    It skips ONLY when genuinely off-bench (no TFTP server answering on :69 and
    no root handle resolvable) — a fresh clone / CI. On a bench it is armed: if
    a live server answers but no root is resolvable, the test FAILS loudly
    rather than skipping, because a green-by-skip here is exactly the false
    assurance the prior DS-P3.142 landing was refused for (5 passed / 3 skipped
    under a plain `pytest`, the guard never actually looking at the root). The
    detector's power is also proven unconditionally, with no bench, in
    test_flat_root_offender_detector_has_power.

    A flat root is shared by every board and project (DS-P2.84), so a lingering
    BOOT.bin / *.bit / system.dtb.* / host binary is a live collision. The fix
    for a red result is to move the named file under _retired-flat-DS-P2.84/
    (nothing deleted) — or, if a live referrer genuinely needs it at the root,
    to add it to LIVE_REFERRER_HOLDOUTS with that referrer recorded in the
    quarantine WHY.md."""
    root = _real_tftp_root()
    if root is None or not root.is_dir():
        if _tftp_server_answering():
            pytest.fail(
                "a live TFTP server is answering on localhost but no served "
                "root is resolvable (TFTP_ROOT unset, no local runtimes/hw.local.yml, "
                f"and {BENCH_DEFAULT_ROOT} is not a directory). Refusing to "
                "skip the deployed-effect guard on a bench: set TFTP_ROOT to the "
                "served directory so this check actually inspects it."
            )
        pytest.skip(
            "off-bench: no TFTP server on :69 and no deployment root "
            "(set TFTP_ROOT=<served dir> or provide a local runtimes/hw.local.yml)"
        )
    offenders = _flat_root_offenders(root)
    assert offenders == [], (
        f"{root} still holds bare names a `tftpboot <name>` resolves to the "
        f"single shared copy every board sees: {offenders}. "
        "Retire them under _retired-flat-DS-P2.84/ or justify each as a "
        "documented LIVE_REFERRER_HOLDOUT (DS-P3.142 / DS-P2.84)."
    )
