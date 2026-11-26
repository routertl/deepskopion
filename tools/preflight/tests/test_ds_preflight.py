# rr unknown-llm (AI-generated)
"""DS-E0 (DS-P1.28) — proof that the preflight can FAIL, prerequisite by prerequisite.

The defect this guards against is not "a check is wrong". It is the shape the
ticket describes: the repo declares an environment, the declaration drifts away
from what the build actually needs, and nothing notices — because the only
evidence anybody ever sees is a green run on the one machine that already had
everything installed.

So the load-bearing tests here REMOVE one prerequisite at a time and assert
that:

    * the run fails,
    * with a non-zero exit,
    * naming the exact prerequisite that is missing,
    * and reporting required-vs-found rather than "check failed".

The removal set is derived from `ds_preflight.CHECKS`, and a test asserts that
every check in that tuple is covered — so a check added later cannot quietly
join the suite without a failure case.

A second class of test judges the COMMITTED tree: the public target must stay
free of bench identity. That one is the regression guard for the actual defect
DS-E0 was filed on — a cable serial sitting in the file a stranger is told to
edit. Fixture values below are assembled so this test is not itself a leak.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
import yaml

_PREFLIGHT_DIR = pathlib.Path(__file__).resolve().parents[1]
_ROOT = _PREFLIGHT_DIR.parents[1]
sys.path.insert(0, str(_PREFLIGHT_DIR))
import ds_preflight as P                                   # noqa: E402


# ---------------------------------------------------------------------------
# A host that satisfies the committed declaration
# ---------------------------------------------------------------------------

def _ok_probe() -> P.Probe:
    d = P.load_declaration()
    return P.Probe(
        os_id=d["host"]["os"]["id"],
        os_version=d["host"]["os"]["version"],
        arch=d["host"]["arch"]["value"],
        ram_gb=float(d["host"]["ram_gb_min"]["value"]),
        free_disk_gb=float(d["host"]["free_disk_gb_min"]["value"]),
        python_version=(3, 11, 14),
        routertl_version=d["routertl"]["min"],
        vivado_version=d["vivado"]["version"],
        vivado_path="/opt/Xilinx/Vivado/" + d["vivado"]["version"] + "/bin/vivado",
        jtag_cables=["FAKESERIAL01"],
        uart_devices=["/dev/serial/by-id/usb-fake-if01-port0"],
        host_ipv4=["192.0.2.1"],
        host_programs={n: True for n in d["linux_host"]["buildroot_programs"]},
    )


@contextlib.contextmanager
def _select_xilinx_target(root: pathlib.Path, name: str = "zybo_datalogger"):
    """The DS-E0 declaration describes Zybo, independent of the repo default."""
    previous = os.environ.get("RR_TARGET")
    os.environ["RR_TARGET"] = str(root / "targets" / f"{name}.yml")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("RR_TARGET", None)
        else:
            os.environ["RR_TARGET"] = previous


def _run(probe=None, root=None, stage="all"):
    selected_root = pathlib.Path(root or _ROOT)
    with _select_xilinx_target(selected_root):
        return P.run_checks(stage, probe=probe, root=selected_root)


def _by_id(results):
    return {r.id: r for r in results}


# ---------------------------------------------------------------------------
# Baseline: the committed declaration + the committed tree agree
# ---------------------------------------------------------------------------

def test_a_conforming_host_and_the_committed_tree_pass_every_check():
    results = _run(_ok_probe(), _ROOT)
    bad = [r.id for r in results if not r.ok]
    assert bad == [], f"unexpected failures against the committed tree: {bad}"


def test_every_check_reports_required_and_found():
    for r in _run(_ok_probe(), _ROOT):
        assert r.requirement.strip(), f"{r.id} declares no requirement"
        assert r.found.strip(), f"{r.id} reports nothing found"
        assert r.severity in ("fail", "warn"), r.id


# ---------------------------------------------------------------------------
# Remove one host prerequisite at a time
# ---------------------------------------------------------------------------

#: (check id, attribute on Probe, value that removes the prerequisite)
HOST_REMOVALS = [
    ("host.arch", "arch", "aarch64"),
    ("host.arch", "arch", None),
    ("host.ram", "ram_gb", 8.0),
    ("host.ram", "ram_gb", None),
    ("host.disk", "free_disk_gb", 3.0),
    ("host.disk", "free_disk_gb", None),
    ("python", "python_version", (3, 9, 18)),
    ("python", "python_version", (3, 15, 0)),  # first release past the exclusive bound
    ("python", "python_version", None),
    ("routertl", "routertl_version", "4.0.0"),
    ("routertl", "routertl_version", None),
    ("vivado.version", "vivado_version", "2023.2"),
    ("vivado.version", "vivado_version", None),
    ("jtag.cable", "jtag_cables", []),
    ("network.host_ipv4", "host_ipv4", []),
    ("console.uart", "uart_devices", []),
    ("linux.buildroot_programs", "host_programs", None),
    ("linux.buildroot_programs", "host_programs", {}),
]


@pytest.mark.parametrize("check_id,attr,value", HOST_REMOVALS)
def test_removing_a_host_prerequisite_fails_and_names_it(check_id, attr, value):
    probe = _ok_probe()
    setattr(probe, attr, value)
    results = _run(probe, _ROOT)
    r = _by_id(results)[check_id]
    assert not r.ok, f"{check_id} passed with {attr}={value!r}"
    assert r.blocking, f"{check_id} failed but did not block the build"
    # The whole point: the report says what was wanted and what is there.
    assert r.requirement and r.found


@pytest.mark.parametrize("check_id,attr,value", HOST_REMOVALS)
def test_the_cli_exits_non_zero_and_prints_the_missing_prerequisite(
    check_id, attr, value, capsys
):
    probe = _ok_probe()
    setattr(probe, attr, value)
    stage = _by_id(_run(probe, _ROOT))[check_id].stage
    with _select_xilinx_target(_ROOT):
        rc = P.main([f"--stage={stage}"], probe=probe, root=_ROOT)
    err = capsys.readouterr().err
    assert rc == 1
    assert "MISSING PREREQUISITE" in err
    assert check_id in err
    # …and on stderr, because rr's hook runner swallows a failing hook's stdout.
    assert "required :" in err and "found    :" in err


def test_an_unsupported_host_os_warns_but_does_not_block_a_build():
    """Severity comes from the declaration. `host.os` is `warn`: a Debian box
    is outside the support envelope, which is a statement about what we will
    accept a bug report for — not a reason to refuse to synthesise."""
    probe = _ok_probe()
    probe.os_id, probe.os_version = "debian", "12"
    results = _run(probe, _ROOT)
    r = _by_id(results)["host.os"]
    assert not r.ok and not r.blocking
    with _select_xilinx_target(_ROOT):
        assert P.main(["--stage=build"], probe=probe, root=_ROOT) == 0


# ---------------------------------------------------------------------------
# DS-P1.67: under WSL2 the RAM the OS sees is the VM limit
# ---------------------------------------------------------------------------
#
# Measured on the off-bench rehearsal host (2026-11-07): a WSL2 VM still on its
# default limit reported 15.4 GB, and the refusal said only "a smaller machine
# dies mid-build", so the owner of a machine with enough RAM is told to buy RAM.
# The fix that works there is the `.wslconfig` limit plus `wsl --shutdown`.

_WSL2_RELEASE = "6.18.33.2-microsoft-standard-WSL2"


def _short_ram_cli_stderr(capsys, *, wsl2):
    probe = _ok_probe()
    probe.ram_gb = 15.4
    probe.wsl2 = wsl2
    with _select_xilinx_target(_ROOT):
        rc = P.main(["--stage=build"], probe=probe, root=_ROOT)
    assert rc == 1
    return capsys.readouterr().err


def test_a_wsl2_host_short_of_ram_is_told_to_raise_the_wslconfig_limit(capsys):
    err = _short_ram_cli_stderr(capsys, wsl2=True)
    assert "MISSING PREREQUISITE (1): host.ram" in err
    assert "15.4 GB (the WSL2 VM's memory limit, not the machine's RAM)" in err
    assert ".wslconfig" in err and "memory=" in err
    assert "wsl --shutdown" in err


def test_a_native_linux_host_short_of_ram_gets_no_wsl_advice(capsys):
    err = _short_ram_cli_stderr(capsys, wsl2=False)
    assert "MISSING PREREQUISITE (1): host.ram" in err
    assert "wslconfig" not in err and "WSL" not in err
    assert "found    : 15.4 GB\n" in err


@pytest.mark.parametrize("release,expected", [
    (_WSL2_RELEASE, True),
    ("5.15.167.4-microsoft-standard", True),     # WSL2 kernels before the -WSL2 suffix
    ("4.4.0-19041-Microsoft", False),            # WSL1: no VM, so no VM limit
    ("6.8.0-138-generic", False),
])
def test_the_host_probe_reads_wsl2_from_the_running_kernel(
        tmp_path, monkeypatch, release, expected):
    osrelease = tmp_path / "osrelease"
    osrelease.write_text(release + "\n", encoding="utf-8")
    monkeypatch.setattr(P, "_KERNEL_OSRELEASE", osrelease, raising=False)
    seen = getattr(P.probe_system(_ROOT), "wsl2", None)
    assert seen is expected, (
        f"probe_system reported wsl2={seen!r} for kernel {release!r}")


def test_a_second_board_with_no_named_cable_is_a_failure_not_a_guess(tmp_path):
    """Two boards attached and nothing naming which one is the DS-E0 JTAG rule:
    rr would otherwise take the first cable it enumerates, which on a
    multi-board host is somebody else's board."""
    probe = _ok_probe()
    probe.jtag_cables = ["FAKESERIAL01", "FAKESERIAL02"]
    # A tree whose bench descriptor names no cable — i.e. a plain clone.
    root = _clone_project(tmp_path)
    (root / "runtimes" / "hw.local.yml").unlink(missing_ok=True)
    r = _by_id(_run(probe, root))["jtag.unambiguous"]
    assert not r.ok and r.blocking


# ---------------------------------------------------------------------------
# Remove one project-side prerequisite at a time
# ---------------------------------------------------------------------------

_COPY = (
    "project.yml",
    # DS-P1.103: the Xilinx subject is the shipped zybo_datalogger (canfd_demo,
    # the CTU CAN FD bench target, moved to bench/targets/).
    "targets/zybo_datalogger.yml",
    "tools/datalogger-builder/config/datalogger_config.yml",
)


def _clone_project(tmp: pathlib.Path) -> pathlib.Path:
    """A minimal copy of the files preflight reads, so a test can break one."""
    root = tmp / "clone"
    for rel in _COPY:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_ROOT / rel, dst)
    return root


def _target_of(root: pathlib.Path) -> pathlib.Path:
    with _select_xilinx_target(root):
        return P.resolve_target_path(root)


def test_rr_target_override_reaches_the_preflight_cli_report(monkeypatch, capsys):
    """DS-P3.151: judge the target rr will build, not project.yml's default."""
    monkeypatch.setenv("RR_TARGET", "targets/zybo_datalogger.yml")
    monkeypatch.chdir(_ROOT)

    rc = P.main(["--stage=build", "--json"], probe=_ok_probe(), root=_ROOT)
    payload = json.loads(capsys.readouterr().out)
    board = next(r for r in payload["results"] if r["id"] == "target.board")

    assert rc == 0, payload
    assert payload["verdict"] == "PASS"
    assert board["found"] == "xc7z020clg400-1 on zybo-z7-20"


def test_shipped_entry_point_reports_routertl_missing_when_sdk_is_unimportable(
    tmp_path,
):
    """A fresh clone must get a prerequisite report, never an SDK traceback."""
    (tmp_path / "sitecustomize.py").write_text(
        """\
import importlib.abc
import importlib.metadata
import shutil
import sys

class _BlockSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "sdk" or fullname.startswith("sdk."):
            raise ModuleNotFoundError("sdk blocked by preflight regression test")
        return None

sys.meta_path.insert(0, _BlockSdk())
_real_version = importlib.metadata.version
_real_which = shutil.which

def _version(name):
    if name == "routertl":
        raise importlib.metadata.PackageNotFoundError(name)
    return _real_version(name)

def _which(name, *args, **kwargs):
    if name in ("rr", "routertl"):
        return None
    return _real_which(name, *args, **kwargs)

importlib.metadata.version = _version
shutil.which = _which
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)
    env.pop("RR_TARGET", None)
    completed = subprocess.run(
        [sys.executable, "tools/preflight/ds_preflight.py", "--stage=build"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 1
    assert "MISSING PREREQUISITE" in completed.stderr
    assert "[FAIL] routertl" in completed.stderr
    assert "found    : not installed" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_unset_rr_target_explicitly_judges_the_committed_de25_default(monkeypatch, tmp_path):
    """The default clone judges the committed DE25 default against the DE25 declaration (DS-P2.363; before, against
    the Zybo one, which failed it by construction)."""
    monkeypatch.delenv("RR_TARGET", raising=False)
    # DS-P2.342: the committed files only, so no `rr target set` choice in this checkout applies.
    root = tmp_path / "committed"
    for rel in ("project.yml", "targets/de25_datalogger.yml"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_ROOT / rel, root / rel)

    path = P.resolve_target_path(root)
    board = _by_id(P.run_checks("build", probe=_ok_probe(), root=root))[
        "target.board"
    ]

    assert path == (root / "targets/de25_datalogger.yml").resolve()
    assert board.ok and not board.blocking
    assert board.found == "A5ED013BB32AE4SCS on de25-standard"


def _mutate_target(root: pathlib.Path, fn):
    path = _target_of(root)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_a_target_for_another_board_fails_naming_the_board(tmp_path):
    root = _clone_project(tmp_path)
    _mutate_target(root, lambda d: d["hardware"].update(part="xc7z010clg400-1"))
    r = _by_id(_run(_ok_probe(), root))["target.board"]
    assert not r.ok and r.blocking
    assert "xc7z010clg400-1" in r.found


def test_a_target_pinned_to_another_vivado_fails(tmp_path):
    root = _clone_project(tmp_path)
    _mutate_target(root, lambda d: d["build_options"].update(tool_version="2022.2"))
    r = _by_id(_run(_ok_probe(), root))["target.tool_version"]
    assert not r.ok and r.blocking
    assert "2022.2" in r.found


def test_a_missing_project_yml_target_pointer_is_a_failure_not_a_pass(tmp_path):
    """The no-third-state rule: a prerequisite that cannot be evaluated is a
    FAIL. A preflight that goes green because it could not look is the false
    green this repo has already been bitten by once."""
    root = _clone_project(tmp_path)
    _target_of(root).unlink()
    results = _by_id(_run(_ok_probe(), root))
    assert results["target.board"].blocking
    assert results["target.no_bench_identity"].blocking


# ── the actual DS-E0 defect, in each of its three shapes ───────────────────

def test_a_cable_serial_in_the_public_target_fails(tmp_path):
    """This is the defect DS-E0 was filed on, verbatim: a bench cable serial in
    the one file a stranger is told to edit."""
    root = _clone_project(tmp_path)
    serial = "21" + "F" * 10
    _mutate_target(root, lambda d: d["build_options"].update(jtag_serial=serial))
    r = _by_id(_run(_ok_probe(), root))["target.no_bench_identity"]
    assert not r.ok and r.blocking
    assert serial in r.found


def test_a_fixed_address_in_the_public_target_fails(tmp_path):
    root = _clone_project(tmp_path)
    address = ".".join(str(n) for n in (192, 168, 45, 7))
    _mutate_target(
        root,
        lambda d: d["linux"].update(
            bootargs=d["linux"]["bootargs"] + f" ds_ip={address}/24"
        ),
    )
    results = _by_id(_run(_ok_probe(), root))
    assert results["target.no_bench_identity"].blocking
    assert address in results["target.no_bench_identity"].found
    assert results["target.network"].blocking


def test_a_host_specific_path_in_the_public_target_fails(tmp_path):
    root = _clone_project(tmp_path)
    home_path = "/ho" + "me/somedev/custom_build_overlay"
    _mutate_target(root, lambda d: d["linux"]["rootfs"].update(
        python_overlay=home_path))
    r = _by_id(_run(_ok_probe(), root))["target.no_bench_identity"]
    assert not r.ok and "host-specific path" in r.found


def test_the_build_preflight_only_judges_configuration_not_comments(tmp_path):
    """DS-C1 separately scans all bytes; this build check parses configuration."""
    root = _clone_project(tmp_path)
    path = _target_of(root)
    serial = "21" + "F" * 10
    address = ".".join(str(n) for n in (192, 168, 45, 9))
    path.write_text(
        path.read_text(encoding="utf-8")
        + f"\n# a historical bench board {serial} lived at {address}\n",
        encoding="utf-8",
    )
    assert _by_id(_run(_ok_probe(), root))["target.no_bench_identity"].ok


# ── the traffic source is a promise the design has to keep ────────────────

_DATALOGGER_CONFIG = "tools/datalogger-builder/config/datalogger_config.yml"


def _mutate_datalogger_config(root: pathlib.Path, fn):
    """Mutate the mix the selected target builds (its system_config), which is
    the file the preflight reads (DS-P2.218: a target may name its own mix)."""
    target = yaml.safe_load(_target_of(root).read_text(encoding="utf-8"))
    path = root / target.get("system_config", _DATALOGGER_CONFIG)
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(cfg)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def _drop_dt_nodes(*paths):
    return lambda d: d["linux"].__setitem__(
        "dt_nodes", [n for n in d["linux"]["dt_nodes"] if n.get("path") not in paths],
    )


def test_the_zybo_datalogger_build_preflight_passes_with_the_default_can_logger(
    capsys,
):
    """DS-T1.17: the committed zybo_datalogger build stopped in this hook,
    before Vivado, with `missing ids [20]`. DS-P2.121 (DS-D.43) made the CAN
    data logger opt-in and removed id 20 from datalogger_config.yml while the
    declaration still required it, so no Zybo constraint, unconstrained-path
    or report_cdc evidence could exist. Judge it the way the pre_build hook
    does: the CLI exit code, on the target the hook runs for.

    DS-P1.104: the owner's 2026-11-13 ruling puts one CAN FD logger (id 20)
    back in the default mix, on an external header pin, so the preflight
    reports CAN as enabled and must still pass."""
    with _select_xilinx_target(_ROOT, "zybo_datalogger"):
        rc = P.main(["--stage", "build", "--json"], probe=_ok_probe(), root=_ROOT)
    payload = json.loads(capsys.readouterr().out)
    loggers = next(r for r in payload["results"]
                   if r["id"] == "traffic_source.dataloggers")
    assert rc == 0 and payload["verdict"] == "PASS", (
        "zybo_datalogger build preflight blocks the build: "
        f"{loggers['found']}"
    )
    assert loggers["found"].endswith("opt-in buses enabled: CAN")


def test_dropping_a_golden_path_datalogger_kills_the_traffic_source(tmp_path):
    root = _clone_project(tmp_path)
    _mutate_datalogger_config(root, lambda c: c["Dataloggers"]["Burst"]["Spi"].pop(8))
    r = _by_id(_run(_ok_probe(), root))["traffic_source.dataloggers"]
    assert not r.ok and r.blocking
    assert "missing ids [8]" in r.found


def test_dropping_the_spidev_node_kills_the_traffic_source(tmp_path):
    """No spidev child, no /dev/spidev: SPI #8 has nothing driving it."""
    root = _clone_project(tmp_path)
    _mutate_target(root, _drop_dt_nodes("/axi/spi@e0006000/spidev@0"))
    r = _by_id(_run(_ok_probe(), root))["traffic_source.dt_nodes"]
    assert not r.ok and r.blocking, "spidev node removed but traffic source passed"
    assert "/axi/spi@e0006000/spidev@0" in r.found


def test_can_nodes_are_required_exactly_when_the_can_logger_is_configured(tmp_path):
    """DS-D.43 opt-in, both directions.

    CAN logger off: the PS CAN nodes are not part of the golden-path traffic
    source, so a target without them still builds.

    CAN logger on (id 20 in the target's mix, set by this test; DS-P1.104
    puts it in the product's default mix): one CAN node cannot complete a
    frame because nothing ACKs it. Removing the second node leaves a target
    that builds and captures nothing, which is exactly the failure a first-run
    user cannot diagnose."""
    root = _clone_project(tmp_path)
    _mutate_datalogger_config(root, lambda c: c["Dataloggers"]["Burst"].pop("Can", None))
    _mutate_target(root, _drop_dt_nodes("/axi/can@e0008000", "/axi/can@e0009000"))
    off = _by_id(_run(_ok_probe(), root))["traffic_source.dt_nodes"]
    assert off.ok, f"CAN-free build blocked on the PS CAN nodes: {off.found}"

    root = _clone_project(tmp_path / "can_on")
    _mutate_datalogger_config(
        root, lambda c: c["Dataloggers"]["Burst"].__setitem__("Can", {20: {"RX": 28}}),
    )
    _mutate_target(root, _drop_dt_nodes("/axi/can@e0009000"))
    results = _by_id(_run(_ok_probe(), root))
    on = results["traffic_source.dt_nodes"]
    assert not on.ok and on.blocking, "CAN logger enabled with one CAN node passed"
    assert "/axi/can@e0009000" in on.found
    assert results["traffic_source.dataloggers"].found.endswith(
        "opt-in buses enabled: CAN")


# ---------------------------------------------------------------------------
# No check may join the suite without a way to fail
# ---------------------------------------------------------------------------

def test_every_declared_check_has_a_removal_case():
    covered = {cid for cid, _, _ in HOST_REMOVALS} | {
        "host.os",                      # the warn-severity case, tested above
        "target.board",
        "target.tool_version",
        "target.no_bench_identity",
        "target.network",
        "traffic_source.dt_nodes",
        "traffic_source.dataloggers",
        "jtag.unambiguous",
    }
    declared = {r.id for r in _run(_ok_probe(), _ROOT)}
    assert declared - covered == set(), (
        "a check exists with no test that removes its prerequisite: "
        f"{sorted(declared - covered)}"
    )


@pytest.mark.parametrize("version,ok", [
    ("4.9.3", False), ("5.0.0", False), ("5.1.0", False), ("5.2.0", False),
    ("5.2.9", False), ("5.3.0", False), ("5.3.2", False), ("5.4.0", False),
    ("5.4.9", False), ("5.5.0", False), ("5.5.1", False), ("5.6.0", False),
    ("5.6.1", False), ("5.7.0", True), ("5.7.1", True), ("6.0.0", True),
])
def test_the_routertl_floor_is_the_first_release_that_reads_per_target_locks(version, ok):
    """RTL-P1.321: 5.7.0 is the published release DeepSkopion 0.1.0 is verified
    against (RTL-D.419's principle, its version superseded). It contains the
    earlier floors: RTL-D.419's 5.6.0, DS-P1.101's 5.5.0, the first
    published release that reads the per-target locks (targets/<t>.routertl.lock,
    DS-P2.287; 5.3.0 and 5.4.0 report a root-lock stub), and 5.3.0 (hps_regen,
    DS-P1.85). Anything older is refused, and the refusal says what valid
    input looks like."""
    d = P.load_declaration()
    assert d["routertl"]["min"] == "5.7.0"
    result = P._check_routertl(P.Probe(routertl_version=version), d, _ROOT)
    assert result.ok is ok
    assert result.requirement == "routertl >= 5.7.0"


# ---------------------------------------------------------------------------
# DS-P3.210: host.disk is checked once per queue job
# ---------------------------------------------------------------------------

def _floor() -> float:
    return float(P.load_declaration()["host"]["free_disk_gb_min"]["value"])


def _disk_run(root: pathlib.Path, free_gb: float):
    probe = _ok_probe()
    probe.free_disk_gb = free_gb
    return _by_id(_run(probe, root, stage="build"))


def _plant_build_tree(root: pathlib.Path) -> None:
    """Stand-in for the project the job's first stage creates (~1.5 GB measured)."""
    out = root / "build" / "zybo_datalogger" / "project"
    out.mkdir(parents=True)
    (out / "deepskopion.xpr").write_text("<Project/>\n", encoding="utf-8")


def test_host_disk_is_checked_once_per_queue_job(tmp_path, monkeypatch):
    """Owner ruling 2026-11-06, "check once per job": measured, the first
    pre-build run of a canfd_demo job saw 62 GB, the second 58.5 GB, and the
    job was refused by its own project. The second run of the same job skips
    the disk check and says so; every other check still runs."""
    root = _clone_project(tmp_path)
    monkeypatch.setenv("RR_JOB_ID", "job-a")
    first = _disk_run(root, _floor() + 2.0)
    assert first["host.disk"].ok and "skipped" not in first["host.disk"].found

    _plant_build_tree(root)
    second = _disk_run(root, _floor() - 1.5)
    assert second["host.disk"].ok, second["host.disk"].found
    assert second["host.disk"].found.startswith("skipped: checked at this job's first pre-build run")
    assert set(second) == set(first), "the second run dropped checks other than host.disk"

    monkeypatch.setenv("RR_JOB_ID", "job-b")
    other = _disk_run(root, _floor() - 1.5)
    assert not other["host.disk"].ok and other["host.disk"].blocking, "a new job skipped its own disk check"


def test_without_a_job_id_every_run_checks_the_disk(tmp_path, monkeypatch):
    root = _clone_project(tmp_path)
    monkeypatch.delenv("RR_JOB_ID", raising=False)
    assert _disk_run(root, _floor() + 2.0)["host.disk"].ok
    _plant_build_tree(root)
    again = _disk_run(root, _floor() - 1.5)["host.disk"]
    assert not again.ok and again.blocking
    assert not (root / P.HOST_DISK_MARKER).exists(), "a run outside a queue job left a marker"


def test_a_failed_first_check_is_not_recorded(tmp_path, monkeypatch):
    root = _clone_project(tmp_path)
    monkeypatch.setenv("RR_JOB_ID", "job-c")
    assert not _disk_run(root, _floor() - 1.5)["host.disk"].ok
    assert not _disk_run(root, _floor() - 1.5)["host.disk"].ok, "a refused job's later run passed"
