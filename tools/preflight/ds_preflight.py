#!/usr/bin/env python3
"""DS-E0 (DS-P1.28) — the executable half of the supported-environment declaration.

`tools/preflight/supported_environment.yml` declares what Deepskopion supports.
This script CHECKS it, and it is wired into the canonical target's
`hooks.pre_build`, so a missing prerequisite fails the build **before synthesis
starts** and names the exact thing that is missing — instead of surfacing 90
minutes later as an OOM kill, a part mismatch, or a board that never appears.

Two stages, because they gate different things:

    --stage build   everything needed to get from `git clone` to a bitstream.
                    This is what the pre_build hook runs.
    --stage bench   everything needed to get that bitstream onto a board and
                    see traffic: a JTAG cable, the point-to-point link, a
                    console. Nothing here is needed to synthesise, so it must
                    never block a build on a machine with no board attached.

Severity comes from the declaration, not from this file:

    fail  — the golden path cannot work; exit 1.
    warn  — outside the declared envelope; reported, exit unaffected.

Design notes that are load-bearing:

* Every check reports **required vs found**. "Vivado check failed" is useless;
  "required 2024.1, found 2023.2" is a fix.
* The environment is probed ONCE into a `Probe` and every check is a pure
  function of (Probe, declaration, project files). That is what makes the
  regression guard possible: the tests remove one prerequisite from a synthetic
  Probe and assert this script FAILS naming it — for every declared
  prerequisite, not just the one someone remembered to test.
* No check "skips". A prerequisite that cannot be evaluated is a FAIL with
  "could not determine" as the found value — the GP-C0 no-third-state rule
  applies here too: a preflight that goes green because it could not look is
  the false green this repo has already been bitten by once
  (run_self_test.py, see docs/GOLDEN_PATH.md).

CLI:
    python3 tools/preflight/ds_preflight.py [--stage build|bench|all] [--json]
    exit 0 iff no `fail`-severity check failed.
"""
from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

try:
    from sdk.cli.project_config import (
        get_active_target,
        load_project_config,
        resolve_explicit_target_arg,
    )
except ImportError:
    # This file is also the executable prerequisite check that tells a fresh
    # clone to install RouteRTL.  Keep that entry point runnable before the
    # SDK exists; load_target() uses the project.yml pointer-only fallback in
    # that state, while the routertl check reports the missing prerequisite.
    get_active_target = None
    load_project_config = None
    resolve_explicit_target_arg = None

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
from active_target import load_active_target  # noqa: E402

DECLARATION = Path(__file__).resolve().parent / "supported_environment.yml"

STAGES = ("build", "bench", "all")

# Where a cable serial would be a bench identity leak if it appeared.
_SERIAL_RE = re.compile(r"\b[0-9A-F]{12}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HOST_TFTP_ROOT = "/srv" + "/tftp"


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------

def load_declaration(path: Path | None = None) -> dict:
    """Load the supported-environment declaration (the SSOT)."""
    p = Path(path or DECLARATION)
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------
# Environment probe
# ---------------------------------------------------------------------------

@dataclass
class Probe:
    """One snapshot of the host, taken before any check runs.

    Every field is `None` when it could not be determined — never a guess, and
    never a value that makes a check pass by accident.
    """

    os_id: str | None = None
    os_version: str | None = None
    arch: str | None = None
    ram_gb: float | None = None
    #: True on a WSL2 kernel. There MemTotal is the VM's `.wslconfig` limit
    #: (default about half the host's RAM), not the machine's RAM (DS-P1.67).
    wsl2: bool | None = None
    free_disk_gb: float | None = None
    python_version: tuple[int, int, int] | None = None
    routertl_version: str | None = None
    vivado_version: str | None = None
    vivado_path: str | None = None
    jtag_cables: list[str] = field(default_factory=list)
    uart_devices: list[str] = field(default_factory=list)
    host_ipv4: list[str] = field(default_factory=list)


def _os_release() -> tuple[str | None, str | None]:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return None, None
    fields = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip().strip('"')
    return fields.get("ID"), fields.get("VERSION_ID")


def _ram_gb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None
    return None


#: The running kernel's release string. A WSL2 kernel names itself here
#: ("6.18.33.2-microsoft-standard-WSL2"; older ones "...-microsoft-standard").
_KERNEL_OSRELEASE = Path("/proc/sys/kernel/osrelease")


def _wsl2() -> bool | None:
    try:
        release = _KERNEL_OSRELEASE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    # WSL1 kernels report "...-Microsoft" and have no VM memory limit, so they
    # are not WSL2.
    return "microsoft-standard" in release or "wsl2" in release


def _free_disk_gb(root: Path) -> float | None:
    try:
        return shutil.disk_usage(root).free / (1024 ** 3)
    except OSError:
        return None


def _routertl_version() -> str | None:
    # Prefer the installed distribution metadata: no subprocess, no PATH games.
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("routertl")
        except PackageNotFoundError:
            pass
    except ImportError:
        pass
    rr = shutil.which("rr") or shutil.which("routertl")
    if not rr:
        return None
    try:
        out = subprocess.run([rr, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+\.\d+\.\d+)", out.stdout + out.stderr)
    return m.group(1) if m else None


_VIVADO_ROOTS = ("/tools/Xilinx/Vivado", "/opt/Xilinx/Vivado", "/tools/Xilinx/Vivado_Lab")


def _vivado() -> tuple[str | None, str | None]:
    """Return (version, path). Version is read from the install layout when it
    can be — .../Vivado/<version>/bin/vivado — and only then from the binary,
    because launching Vivado to learn its version costs seconds in a hook."""
    candidates: list[str] = []
    env_root = os.environ.get("XILINX_VIVADO")
    if env_root:
        candidates.append(str(Path(env_root) / "bin" / "vivado"))
    on_path = shutil.which("vivado")
    if on_path:
        candidates.append(on_path)
    for root in _VIVADO_ROOTS:
        candidates.extend(sorted(glob.glob(f"{root}/*/bin/vivado"), reverse=True))

    for cand in candidates:
        p = Path(cand)
        if not p.is_file():
            continue
        # .../Vivado/<version>/bin/vivado
        parent = p.parent.parent.name
        if re.fullmatch(r"\d{4}\.\d+", parent):
            return parent, str(p)
        try:
            out = subprocess.run([str(p), "-version"], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"v(\d{4}\.\d+)", out.stdout + out.stderr)
        if m:
            return m.group(1), str(p)
    return None, None


_BY_ID = Path("/dev/serial/by-id")
_ADEPT_RE = re.compile(r"Digilent_Adept_USB_Device_([0-9A-Za-z]+)-if\d+")


def _usb_cables_and_uarts() -> tuple[list[str], list[str]]:
    """Enumerate attached Digilent/FTDI boards from /dev/serial/by-id.

    by-id, never ttyUSBn: the numbering reshuffles between plug events, and a
    stable handle is the difference between "the console" and "some board's
    console" on a host with more than one board.
    """
    cables: list[str] = []
    uarts: list[str] = []
    try:
        entries = sorted(p.name for p in _BY_ID.iterdir())
    except OSError:
        return cables, uarts
    for name in entries:
        m = _ADEPT_RE.search(name)
        if m and m.group(1) not in cables:
            cables.append(m.group(1))
        if name.endswith("-port0"):
            uarts.append(str(_BY_ID / name))
    return cables, uarts


def _host_ipv4() -> list[str]:
    ip = shutil.which("ip")
    if not ip:
        return []
    try:
        out = subprocess.run([ip, "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out.stdout)


def probe_system(root: Path) -> Probe:
    """Take the one snapshot every check is judged against."""
    os_id, os_version = _os_release()
    cables, uarts = _usb_cables_and_uarts()
    vivado_version, vivado_path = _vivado()
    return Probe(
        os_id=os_id,
        os_version=os_version,
        arch=os.uname().machine,
        ram_gb=_ram_gb(),
        wsl2=_wsl2(),
        free_disk_gb=_free_disk_gb(root),
        python_version=sys.version_info[:3],
        routertl_version=_routertl_version(),
        vivado_version=vivado_version,
        vivado_path=vivado_path,
        jtag_cables=cables,
        uart_devices=uarts,
        host_ipv4=_host_ipv4(),
    )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Result:
    """One prerequisite, judged."""

    id: str
    stage: str
    ok: bool
    severity: str          # 'fail' | 'warn'
    requirement: str       # what the declaration demands
    found: str             # what this host actually has
    fix: str = ""

    @property
    def blocking(self) -> bool:
        return (not self.ok) and self.severity == "fail"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "stage": self.stage,
            "ok": self.ok,
            "severity": self.severity,
            "requirement": self.requirement,
            "found": self.found,
            "fix": self.fix,
        }


def _ver_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text))


# ---------------------------------------------------------------------------
# Project-side helpers (the target IS the file the user edits, so these read it)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _from_project_root(root: Path):
    """Give RouteRTL the same CWD-relative target semantics as ``rr``."""
    previous = Path.cwd()
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(previous)


def resolve_target_path(root: Path) -> Path | None:
    """Resolve the active file through RouteRTL's canonical target resolver."""
    path, _ = load_target(root)
    return path


def _load_pointer_target(root: Path) -> tuple[Path | None, dict]:
    """The active target for hosts without RouteRTL, in rr's order
    (tools/active_target.py: $RR_TARGET, then ``rr target set``'s local file,
    then project.yml). DS-P2.336: this fallback read only project.yml, so after
    ``rr target set`` it judged the project.yml default."""
    path, target = load_active_target(root)
    return (path.resolve(), target) if path is not None else (None, {})


def load_target(root: Path) -> tuple[Path | None, dict]:
    """Load exactly the config ``rr`` uses: $RR_TARGET, then the target
    ``rr target set`` stored locally, then project.yml."""
    root = Path(root).resolve()
    if load_project_config is None:
        return _load_pointer_target(root)
    try:
        with _from_project_root(root):
            pointer = get_active_target("project.yml")
            config = load_project_config("project.yml")
            path = (resolve_explicit_target_arg(pointer)
                    if pointer else root / "project.yml")
        return path, config
    except (OSError, ValueError, yaml.YAMLError):
        return None, {}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _declared_os_versions(spec: dict) -> list[tuple[str, str]]:
    """``(version, name)`` for the reference OS and each measured ``also_supported`` one."""
    out = [(str(spec["version"]), str(spec["name"]))]
    for extra in spec.get("also_supported") or ():
        out.append((str(extra["version"]), str(extra.get("name", extra["version"]))))
    return out


def _check_host_os(p: Probe, d: dict, root: Path) -> Result:
    spec = d["host"]["os"]
    declared = _declared_os_versions(spec)
    want = " or ".join(f"{spec['id']} {v} ({name})" for v, name in declared)
    got = f"{p.os_id or '?'} {p.os_version or '?'}"
    ok = p.os_id == spec["id"] and p.os_version in {v for v, _ in declared}
    return Result(
        "host.os", "build", ok, spec.get("severity", "warn"),
        f"a declared host OS: {want}", got,
        "This OS is not declared in tools/preflight/supported_environment.yml "
        "(host.os). It may work; it is outside the declared support envelope, "
        "so a failure there is not a defect report we accept.",
    )


def _check_host_arch(p: Probe, d: dict, root: Path) -> Result:
    spec = d["host"]["arch"]
    ok = p.arch == spec["value"]
    return Result(
        "host.arch", "build", ok, spec.get("severity", "fail"),
        f"CPU architecture {spec['value']}", p.arch or "could not determine",
        "Vivado ships x86_64 Linux binaries only — there is no arm64 build.",
    )


def _check_host_ram(p: Probe, d: dict, root: Path) -> Result:
    spec = d["host"]["ram_gb_min"]
    ok = p.ram_gb is not None and p.ram_gb >= float(spec["value"])
    found = f"{p.ram_gb:.1f} GB" if p.ram_gb is not None else "could not determine"
    fix = ("Synthesis of the canonical target peaked at 19.5 GB (DS-T2.62 was "
           "OOM-killed at a 14 GB cap). A smaller machine dies mid-build.")
    if p.wsl2 and p.ram_gb is not None:
        # DS-P1.67: the floor is RAM as the OS sees it, and under WSL2 that is
        # the VM limit. Without this a 32 GB machine is told it is too small.
        found += " (the WSL2 VM's memory limit, not the machine's RAM)"
        fix += (f" Under WSL2 the limit is `memory=` in the [wsl2] section of "
                f"%UserProfile%\\.wslconfig (default: about half the "
                f"machine's RAM). Set it to {spec['value']}GB or more, run "
                f"`wsl --shutdown` from Windows, reopen the shell and rerun "
                f"this check.")
    return Result(
        "host.ram", "build", ok, spec.get("severity", "fail"),
        f"at least {spec['value']} GB RAM", found, fix,
    )


#: DS-P3.210: where a queue job's first pre-build run records its disk check.
HOST_DISK_MARKER = Path(".routertl_cache") / "ds_preflight_host_disk.json"


def _check_host_disk(p: Probe, d: dict, root: Path) -> Result:
    """The free-disk floor is room for one build that has not started yet.

    DS-P3.210 (owner ruling 2026-11-06, "check once per job"): rr runs the
    pre-build hooks more than once in one job (before the project step and
    again before synthesis), and the second run would count the job's own new
    project against the floor (measured: 62 GB at the first run, 58.5 GB at the
    second, the build refused). Inside a queue job (RR_JOB_ID set) the first
    run that passes records the job id; a later run of the same job skips the
    disk check and says so. Every other check still runs. Without RR_JOB_ID the
    check runs every time."""
    spec = d["host"]["free_disk_gb_min"]
    requirement = f"at least {spec['value']} GB free on the checkout's filesystem"
    fix = "Checkpoints, the Vivado project and the Linux artifacts all land here."
    job = os.environ.get("RR_JOB_ID", "").strip()
    marker = root / HOST_DISK_MARKER
    if job:
        try:
            prev = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
        if isinstance(prev, dict) and prev.get("job_id") == job:
            return Result(
                "host.disk", "build", True, spec.get("severity", "fail"), requirement,
                f"skipped: checked at this job's first pre-build run "
                f"({prev.get('free_gb')} GB free, job {job})", fix,
            )
    ok = p.free_disk_gb is not None and p.free_disk_gb >= float(spec["value"])
    if job and ok:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"job_id": job, "free_gb": round(p.free_disk_gb, 1)}) + "\n",
                              encoding="utf-8")
        except OSError:
            pass  # without the marker the next run checks again: the old behaviour
    return Result(
        "host.disk", "build", ok, spec.get("severity", "fail"), requirement,
        f"{p.free_disk_gb:.1f} GB free" if p.free_disk_gb is not None else "could not determine",
        fix,
    )


def _check_python(p: Probe, d: dict, root: Path) -> Result:
    spec = d["python"]
    lo, hi = _ver_tuple(spec["min"]), _ver_tuple(spec["max_exclusive"])
    got = p.python_version
    ok = got is not None and lo <= got[: len(lo)] and got[: len(hi)] < hi
    return Result(
        "python", "build", ok, spec.get("severity", "fail"),
        f"Python >= {spec['min']}, < {spec['max_exclusive']}",
        ".".join(str(x) for x in got) if got else "could not determine",
        "Use the interpreter the supported host ships.",
    )


def _check_routertl(p: Probe, d: dict, root: Path) -> Result:
    spec = d["routertl"]
    ok = p.routertl_version is not None and _ver_tuple(p.routertl_version) >= _ver_tuple(spec["min"])
    return Result(
        "routertl", "build", ok, spec.get("severity", "fail"),
        f"routertl >= {spec['min']}", p.routertl_version or "not installed",
        spec.get("install", "pip install routertl"),
    )


def _check_vivado(p: Probe, d: dict, root: Path) -> Result:
    spec = d["vivado"]
    ok = p.vivado_version == spec["version"]
    found = f"{p.vivado_version} ({p.vivado_path})" if p.vivado_version else "not found"
    return Result(
        "vivado.version", "build", ok, spec.get("severity", "fail"),
        f"Vivado {spec['version']} — {spec['edition']}", found,
        "Install Vivado " + str(spec["version"]) + " and either put it on PATH "
        "or export XILINX_VIVADO=<install>/Vivado/" + str(spec["version"]) + ". "
        "The build is pinned to this release (build_options.tool_version); a "
        "different one re-times and re-fits the design.",
    )


def _check_target_part(p: Probe, d: dict, root: Path) -> Result:
    spec = d["board"]
    path, target = load_target(root)
    hw = target.get("hardware") or {}
    got = f"{hw.get('part')} on {hw.get('platform')}"
    ok = hw.get("part") == spec["part"] and hw.get("platform") == spec["platform"]
    return Result(
        "target.board", "build", ok, spec.get("severity", "fail"),
        f"the target builds for {spec['name']}: part {spec['part']}, "
        f"platform {spec['platform']}",
        got if path else "no target resolved from project.yml",
        f"Edit {path or 'the target yml'} — it is the one file you edit.",
    )


def _check_target_tool_version(p: Probe, d: dict, root: Path) -> Result:
    want = d["vivado"]["version"]
    path, target = load_target(root)
    got = ((target.get("build_options") or {}).get("tool_version"))
    ok = str(got) == str(want)
    return Result(
        "target.tool_version", "build", ok, d["vivado"].get("severity", "fail"),
        f"the target pins build_options.tool_version to {want}",
        str(got) if got else "unset",
        "The declared Vivado release and the target's pin must agree, or the "
        "declaration is describing a build nobody runs.",
    )


def _check_target_no_bench_identity(p: Probe, d: dict, root: Path) -> Result:
    """The public target must contain none of this bench.

    A cable serial identifies ONE physical cable; a home directory identifies
    ONE developer; any literal IPv4 identifies somebody else's network. All
    three break a stranger, and all three were in the tree when DS-E0 was
    filed. DS-C1 extends this same policy to every distributable artifact.
    """
    path, _ = load_target(root)
    leaks: list[str] = []
    if path is None:
        leaks.append("no target resolved from project.yml")
    else:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if not code.strip():
                continue
            for serial in _SERIAL_RE.findall(code):
                leaks.append(f"{path.name}:{n}: JTAG-cable-shaped serial {serial}")
            for ip in _IPV4_RE.findall(code):
                leaks.append(f"{path.name}:{n}: fixed IPv4 {ip}")
            if re.search(r"/home/[A-Za-z0-9._-]+", code) or _HOST_TFTP_ROOT in code:
                leaks.append(f"{path.name}:{n}: host-specific path")
    return Result(
        "target.no_bench_identity", "build", not leaks, "fail",
        "the public target carries no cable serial, fixed IPv4 or "
        "host-specific path",
        "; ".join(leaks) if leaks else "clean",
        "Bench identity belongs in the per-deployment descriptor "
        "(runtimes/hw.local.yml) or on the command line (rr program run --cable), "
        "never in the file a stranger edits.",
    )


def _check_target_network(p: Probe, d: dict, root: Path) -> Result:
    spec = d["network"]
    path, target = load_target(root)
    bootargs = str(((target.get("linux") or {}).get("bootargs")) or "")
    fixed = _IPV4_RE.findall(bootargs)
    legacy = "bench_ip=" in bootargs
    got = ", ".join(fixed) if fixed else ("legacy bench_ip" if legacy else "deployment-owned")
    ok = not fixed and not legacy
    return Result(
        "target.network", "build", ok, spec.get("severity", "fail"),
        "the target leaves addressing to DHCP or owner-supplied boot state "
        f"({spec['topology']})",
        got,
        "Remove literal addresses and bench_ip from linux.bootargs; S40network "
        f"accepts {spec['static_bootarg']} when DHCP is unavailable.",
    )


def _configured_datalogger_ids(root: Path, target: dict) -> tuple[set[int], str]:
    """The datalogger ids the target's system_config builds, and where from."""
    cfg_rel = target.get("system_config")
    ids: set[int] = set()
    if not cfg_rel:
        return ids, "no system_config in the target"
    try:
        cfg = yaml.safe_load((root / str(cfg_rel)).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    for _tier, protos in (cfg.get("Dataloggers") or {}).items():
        if not isinstance(protos, dict):
            continue
        for _proto, instances in protos.items():
            if isinstance(instances, dict):
                ids.update(k for k in instances if isinstance(k, int))
    return ids, str(cfg_rel)


def _enabled_opt_ins(spec: dict, ids: set[int]) -> list[dict]:
    """Opt-in buses (DS-D.43) whose datalogger this build actually carries."""
    return [o for o in spec.get("opt_in") or []
            if set(o.get("datalogger_ids") or []) & ids]


def _check_traffic_source_dt(p: Probe, d: dict, root: Path) -> Result:
    spec = d["traffic_source"]
    path, target = load_target(root)
    declared = [str(n.get("path")) for n in ((target.get("linux") or {}).get("dt_nodes") or [])
                if isinstance(n, dict)]
    ids, _where = _configured_datalogger_ids(root, target)
    required = list(spec["required_dt_nodes"])
    for opt in _enabled_opt_ins(spec, ids):
        required += [n for n in opt.get("required_dt_nodes") or [] if n not in required]
    missing = [n for n in required if n not in declared]
    return Result(
        "traffic_source.dt_nodes", "build", not missing, spec.get("severity", "fail"),
        "the deterministic on-chip traffic source is still wired: "
        + ", ".join(required),
        "missing " + ", ".join(missing) if missing else "present",
        "Without the SPI controller and its spidev child, Linux has no SPI device "
        "to drive; with the CAN logger enabled, a lone PS CAN node has nothing to "
        "ACK it, so no frame ever completes and the first run decodes nothing.",
    )


def _check_traffic_source_dataloggers(p: Probe, d: dict, root: Path) -> Result:
    spec = d["traffic_source"]
    path, target = load_target(root)
    ids, where = _configured_datalogger_ids(root, target)
    missing = sorted(set(spec["required_datalogger_ids"]) - ids)
    opt_ins = [o["name"] for o in _enabled_opt_ins(spec, ids)]
    found = f"missing ids {missing} in {where}" if missing else f"present in {where}"
    found += "; opt-in buses enabled: " + (", ".join(opt_ins) if opt_ins else "none")
    return Result(
        "traffic_source.dataloggers", "build", not missing, spec.get("severity", "fail"),
        "the dataloggers that observe the on-chip traffic are configured: ids "
        + ", ".join(str(i) for i in spec["required_datalogger_ids"]),
        found,
        "Traffic with no datalogger watching it is not a traffic source.",
    )


def _check_jtag_cable(p: Probe, d: dict, root: Path) -> Result:
    spec = d["jtag"]
    ok = bool(p.jtag_cables)
    return Result(
        "jtag.cable", "bench", ok, spec.get("severity", "fail"),
        f"a programming cable is attached — {spec['interface']}",
        ", ".join(p.jtag_cables) if p.jtag_cables else "no Digilent/FTDI board found",
        "Connect the board's PROG/UART micro-B port to the host and check the "
        "udev rules let you open it.",
    )


def _check_jtag_unambiguous(p: Probe, d: dict, root: Path) -> Result:
    """More than one board on the host is allowed — silently guessing is not."""
    spec = d["jtag"]
    n = len(p.jtag_cables)
    declared = _bench_declared_cables(root)
    ok = n <= 1 or bool(declared)
    return Result(
        "jtag.unambiguous", "bench", ok, spec.get("severity", "fail"),
        "with more than one board attached, the cable is named — in the "
        "per-deployment bench descriptor (runtimes/hw.local.yml) or per invocation "
        "(rr program run --cable <serial>) — never in the target",
        f"{n} cable(s) attached, bench descriptor names {sorted(declared) or 'none'}",
        "rr will otherwise take the first cable it enumerates, which on a "
        "multi-board host is somebody else's board.",
    )


def _bench_declared_cables(root: Path) -> set[str]:
    """Cable serials from the PER-DEPLOYMENT descriptor — never from a target.

    DS-P2.161: that descriptor is the gitignored ``runtimes/hw.local.yml``.
    The tracked ``runtimes/hw.yml`` is the hardware contract and names no
    cable, so reading it here would make every multi-board host look like a
    bench with no declared cable."""
    path = root / "runtimes" / "hw.local.yml"
    if not path.is_file():
        return set()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set()
    out = set()
    for spec in ((data.get("bench") or {}).get("resources") or {}).values():
        if isinstance(spec, dict) and spec.get("kind") == "jtag" and spec.get("cable"):
            out.add(str(spec["cable"]))
    return out


def _check_network_host(p: Probe, d: dict, root: Path) -> Result:
    spec = d["network"]
    ok = bool(p.host_ipv4)
    return Result(
        "network.host_ipv4", "bench", ok, spec.get("severity", "fail"),
        "the host has an IPv4 address on the owner-selected board link "
        f"({spec['addressing']})",
        ", ".join(p.host_ipv4) if p.host_ipv4 else "no IPv4 addresses found",
        "Enable DHCP on the link or assign a host address chosen for this "
        "deployment; then supply the board side with ds_ip when needed.",
    )


def _check_console(p: Probe, d: dict, root: Path) -> Result:
    spec = d["jtag"]
    ok = bool(p.uart_devices)
    return Result(
        "console.uart", "bench", ok, spec.get("severity", "fail"),
        "the board console is reachable: " + spec["interface"],
        f"{len(p.uart_devices)} serial device(s) under /dev/serial/by-id"
        if p.uart_devices else "no serial devices under /dev/serial/by-id",
        "Use the by-id path, not /dev/ttyUSBn — the numbering reshuffles "
        "between plug events.",
    )


#: Every declared prerequisite, in report order. The tests iterate this list,
#: so a check added here is automatically covered by the removal guard.
CHECKS = (
    _check_host_os,
    _check_host_arch,
    _check_host_ram,
    _check_host_disk,
    _check_python,
    _check_routertl,
    _check_vivado,
    _check_target_part,
    _check_target_tool_version,
    _check_target_no_bench_identity,
    _check_target_network,
    _check_traffic_source_dt,
    _check_traffic_source_dataloggers,
    _check_jtag_cable,
    _check_jtag_unambiguous,
    _check_network_host,
    _check_console,
)


def run_checks(stage: str = "build", probe: Probe | None = None,
               declaration: dict | None = None, root: Path | None = None) -> list[Result]:
    """Judge every prerequisite in *stage* against *probe*."""
    root = Path(root or ROOT)
    d = declaration or load_declaration()
    p = probe if probe is not None else probe_system(root)
    results = [fn(p, d, root) for fn in CHECKS]
    if stage != "all":
        results = [r for r in results if r.stage == stage]
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def render(results: list[Result], stage: str) -> str:
    lines = [
        f"deepskopion preflight (DS-E0) — stage '{stage}', "
        f"declaration: {DECLARATION.relative_to(ROOT) if DECLARATION.is_relative_to(ROOT) else DECLARATION}",
    ]
    for r in results:
        icon = "OK  " if r.ok else ("FAIL" if r.severity == "fail" else "WARN")
        lines.append(f"  [{icon}] {r.id}")
        if not r.ok:
            lines.append(f"           required : {r.requirement}")
            lines.append(f"           found    : {r.found}")
            if r.fix:
                lines.append(f"           fix      : {r.fix}")
    blocking = [r for r in results if r.blocking]
    warned = [r for r in results if not r.ok and not r.blocking]
    if blocking:
        lines.append("")
        lines.append(
            f"MISSING PREREQUISITE ({len(blocking)}): "
            + ", ".join(r.id for r in blocking)
        )
        lines.append(
            "This build cannot produce the golden path on this host. See "
            "docs/SUPPORTED_ENVIRONMENT.md."
        )
    else:
        lines.append("")
        lines.append(
            f"preflight PASS — {len(results) - len(warned)}/{len(results)} "
            f"prerequisites met"
            + (f", {len(warned)} advisory" if warned else "")
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None, probe: Probe | None = None,
         declaration: dict | None = None, root: Path | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check the DS-E0 supported environment")
    ap.add_argument("--stage", choices=STAGES, default="build",
                    help="build = clone->bitstream; bench = board, link, console")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    # --declaration exists so the guard tests can judge a synthetic host
    # against a real declaration (and vice versa). The pre_build hook never
    # passes it: the build is always judged against the committed SSOT, so
    # this is not a waiver route.
    ap.add_argument("--declaration", default=None,
                    help="path to a supported_environment.yml (default: the committed one)")
    args = ap.parse_args(argv)

    if declaration is None and args.declaration:
        declaration = load_declaration(Path(args.declaration))

    results = run_checks(args.stage, probe=probe, declaration=declaration, root=root)
    failed = any(r.blocking for r in results)
    if args.json:
        print(json.dumps({
            "stage": args.stage,
            "results": [r.as_dict() for r in results],
            "verdict": "FAIL" if failed else "PASS",
        }, indent=2))
    else:
        # A FAILING report goes to stderr: rr's hook runner reports a failing
        # pre_build hook's stderr and swallows its stdout, so a report written
        # only to stdout would abort the build with "exit code 1" and no reason
        # — which is the same as not naming the prerequisite at all.
        print(render(results, args.stage), file=sys.stderr if failed else sys.stdout)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
