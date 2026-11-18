"""DS-T1.17 — the Zybo build keeps the constraint evidence the timing audit needs.

The DS-P1.72 audit had no Zybo constraint compile order, no ignored-exception
count, no unconstrained-path report and no report_cdc, because the build never
reached Vivado. RouteRTL's Xilinx flow writes cdc.rpt itself; the other reports
come from the project's own Vivado hooks. This guard reads the hooks through
`rr config`, the same resolver the build uses, and checks that each hooked
script still issues the report commands. It needs no vendor tool.

The first such build's own compile-order report then showed the Zybo build
loading every hw/xdc/*.xdc, including other targets' constraint files, so the
XDC set the build resolves is pinned here too.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
TARGET = "targets/zybo_datalogger.yml"

_MARKER = "zybo build lost DS-T1.17 " + "constraint evidence"
_XDC_MARKER = "zybo build loads another " + "target's XDC"

#: tcl_hooks stage -> report commands (regex) one of its scripts must issue.
REQUIRED = {
    "post_synthesis": (r"report_compile_order -constraints",),
    "post_implementation": (
        r"report_exceptions -ignored(?!_)",
        r"report_exceptions -ignored_objects",
        r"check_timing -verbose",
        r"report_timing_summary -report_unconstrained",
    ),
}


def _hooks(stage: str) -> list[str]:
    env = dict(os.environ, RR_TARGET=TARGET)
    out = subprocess.run(
        ["rr", "config", "-f", TARGET, "get", f"tcl_hooks.{stage}"],
        cwd=ROOT, env=env, text=True, check=False,
        # stdout only: rr's notices go to stderr (RTL-P2.2163's Python 3.10
        # line made yaml.safe_load fail when the two were merged).
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert out.returncode == 0, (
        f"{_MARKER}: rr config resolves no tcl_hooks.{stage}:\n{out.stdout}{out.stderr}"
    )
    return list(yaml.safe_load(out.stdout) or [])


def _code(path: Path) -> str:
    return "\n".join(line.split("#", 1)[0] for line in path.read_text().splitlines())


def test_zybo_build_hooks_write_every_constraint_evidence_report() -> None:
    for stage, commands in REQUIRED.items():
        scripts = [ROOT / h for h in _hooks(stage)]
        missing_files = [str(s) for s in scripts if not s.is_file()]
        assert not missing_files, f"{_MARKER}: {stage} hook missing on disk: {missing_files}"
        code = "\n".join(_code(s) for s in scripts)
        for command in commands:
            assert re.search(command, code), (
                f"{_MARKER}: no tcl_hooks.{stage} script runs `{command}`"
            )


def test_zybo_build_loads_only_its_own_xdc(monkeypatch) -> None:
    """RouteRTL globs every *.xdc under a paths.constraints directory. With
    `hw/xdc` the Zybo build also loaded classic_eye.xdc, eye_loopback.xdc and
    pblock_ctu.xdc (two 12-1433 empty-pblock critical warnings, and a
    duplicate clk_fpga_0/1 max_delay pair that report_exceptions -ignored
    listed as totally overridden). Resolve the set the way the build does."""
    from routertl_core.constraints_resolver import resolve_constraint_files
    from sdk.cli.project_config import load_project_config

    monkeypatch.setenv("RR_TARGET", TARGET)
    monkeypatch.chdir(ROOT)
    config = load_project_config("project.yml")
    assert config["project"]["output_dir"] == "build/zybo_datalogger"
    xdc = resolve_constraint_files(config, ROOT, categories=("xdc",), vendor="xilinx")
    assert xdc == ["hw/xdc/timing.xdc", "hw/xdc/zybo_header_pullups.xdc"], f"{_XDC_MARKER}: {xdc}"


_TCK_MARKER = "zybo REA JTAG " + "clock unconstrained"


def _rea_scoped_xdc_applied(root: Path) -> str | None:
    """The package XDC the build applies to rr_rea_top, resolved through the
    same ip.lock reader the build uses (None when it is not wired in)."""
    from routertl_core.constraints_resolver import resolve_package_scoped_constraints

    for ref, path in resolve_package_scoped_constraints(root):
        if ref == "rr_rea_top" and path.endswith("/rr_rea_scoped.xdc"):
            return path
    return None


def _assert_rea_tck_clocked(root: Path) -> None:
    packages = yaml.safe_load((root / TARGET).read_text()).get("packages") or {}
    assert "routertl/rea" in packages, (
        f"{_TCK_MARKER}: {TARGET} does not pull routertl/rea, so no package XDC applies"
    )

    # The package file only counts if the build applies it: ip.lock must carry
    # it in scoped_constraints, which the build turns into `read_xdc -ref`.
    applied = _rea_scoped_xdc_applied(root)
    assert applied, (
        f"{_TCK_MARKER}: ip.lock has no scoped_constraints entry applying "
        "rr_rea_scoped.xdc to rr_rea_top; the build would never read it"
    )
    scoped_xdc = (root / applied).read_text()
    match = re.search(r"create_clock\s+-period\s+([\d.]+)\s+\[get_ports\s+tck_i\]", scoped_xdc)
    assert match, "rr_rea_scoped.xdc has no create_clock on [get_ports tck_i]"
    period = float(match.group(1))
    assert period <= 33.334, f"{_TCK_MARKER}: tck_i slower than 30 MHz"

    top = (root / "hw/rtl/deepskopion_top.vhd").read_text()
    top_inst = re.search(r"^\s*(\w+)\s*:\s*entity\s+rr_rea\.rr_rea_xilinx7\b", top, re.M)
    assert top_inst, f"{_TCK_MARKER}: deepskopion_top has no `: entity rr_rea.rr_rea_xilinx7`"

    wrapper = (root / "libs/routertl/rea/rtl/rr_rea_jtag_xilinx7.vhd").read_text()
    bscan_inst = re.search(r"^\s*(\w+)\s*:\s*BSCANE2\b", wrapper, re.M)
    assert bscan_inst, f"{_TCK_MARKER}: rr_rea_xilinx7 has no `: BSCANE2`"

    assert re.search(
        r"set_max_delay\s+-datapath_only\s+\$rr_rea_bound",
        scoped_xdc,
    ), "rr_rea_scoped.xdc has no set_max_delay -datapath_only $rr_rea_bound"


def test_zybo_xdc_clocks_the_rea_bscan_tck() -> None:
    """The first routed Zybo build reported 632 no_clock register pins rooted
    at u_rea/u_bscane2/TCK, and all 1269 unconstrained internal endpoints were
    inside u_rea. The routertl/rea package ships constraints/rr_rea_scoped.xdc
    (read_xdc -ref rr_rea_top), which creates the TCK clock on rr_rea_top/tck_i
    and bounds every REA tck<->sample crossing inside REA. This guard enforces
    that the package constraint is in the resolved set the build applies; the
    DS side of the crossing is test_zybo_xdc_bounds_rea_crossings_on_the_package_clock."""
    _assert_rea_tck_clocked(ROOT)


_DEAD_MARKER = "zybo REA crossing bound " + "names a clock no REA register launches on"
_PS_CLOCK = re.compile(r"clk_fpga_\d+")


def _rea_tck_pin(root: Path) -> str:
    """The pin the package puts the REA JTAG clock on, from the RTL and the
    package XDC (never from a Vivado-generated clock name)."""
    top = (root / "hw/rtl/deepskopion_top.vhd").read_text()
    rea = re.search(r"^\s*(\w+)\s*:\s*entity\s+rr_rea\.rr_rea_xilinx7\b", top, re.M)
    assert rea, "deepskopion_top has no `: entity rr_rea.rr_rea_xilinx7` instance"
    wrapper = (root / "libs/routertl/rea/rtl/rr_rea_jtag_xilinx7.vhd").read_text()
    core = re.search(r"^\s*(\w+)\s*:\s*entity\s+(?:\w+\.)?rr_rea_top\b", wrapper, re.M)
    assert core, "rr_rea_xilinx7 has no `: entity ... rr_rea_top` instance"
    scoped = (root / "libs/routertl/rea/constraints/rr_rea_scoped.xdc").read_text()
    port = re.search(r"create_clock\s+-period\s+[\d.]+\s+\[get_ports\s+(\w+)\]", scoped)
    assert port, "rr_rea_scoped.xdc has no create_clock on a rr_rea_top port"
    return f"{rea.group(1)}/{core.group(1)}/{port.group(1)}"


def _assert_rea_crossing_bounds_live(root: Path) -> None:
    """DS-P2.168. The REA registers launch on the package clock at
    rr_rea_top/tck_i. A timing.xdc clock elsewhere on the TCK net clocks none
    of them (the package clock overrides it below tck_i), so bounds written
    against it are dead: queue job 25c16f6356a1 listed them under
    report_exceptions -ignored as "Non-existent path", and REA's TDO register
    reached the JTAG datalogger's synchronizer as a 1 ps crossing (WNS -1.715
    ns). The bounds must name the clock by the package's pin."""
    xdc = _code(root / "hw/xdc/timing.xdc")
    rea_inst = _rea_tck_pin(root).split("/", 1)[0]
    own = re.findall(r"^\s*create_clock\b[^\n]*\[get_pins\s+\{?(\S+?)\}?\]", xdc, re.M)
    inside = [pin for pin in own if pin.startswith(rea_inst + "/")]
    assert not inside, (
        f"{_DEAD_MARKER}: timing.xdc creates a clock on {inside}; the package "
        "clock on rr_rea_top/tck_i overrides it for every REA register"
    )
    created = set(re.findall(r"^\s*create_clock\b[^\n]*-name\s+(\w+)", xdc, re.M))
    named = set(re.findall(r"\[get_clocks\s+\{?(\w+)\}?\]", xdc))
    unknown = sorted(n for n in named if n not in created and not _PS_CLOCK.fullmatch(n))
    assert not unknown, (
        f"{_DEAD_MARKER}: timing.xdc references clock(s) {unknown} that no "
        "constraint in the build creates"
    )
    pkg = rf"\[get_clocks\s+-of_objects\s+\[get_pins\s+{re.escape(_rea_tck_pin(root))}\]\]"
    sys_clk = r"\[get_clocks\s+clk_fpga_0\]"
    for frm, to, what in ((pkg, sys_clk, "REA TCK -> clk_fpga_0"),
                          (sys_clk, pkg, "clk_fpga_0 -> REA TCK")):
        assert re.search(
            rf"set_max_delay\s+-datapath_only\s+[\d.]+\s+-from\s+{frm}\s+-to\s+{to}", xdc), (
            f"{_DEAD_MARKER}: no {what} -datapath_only bound on the package clock "
            f"(get_clocks -of_objects [get_pins {_rea_tck_pin(root)}])"
        )


def test_zybo_xdc_bounds_rea_crossings_on_the_package_clock() -> None:
    _assert_rea_crossing_bounds_live(ROOT)


@pytest.mark.parametrize("mutant", ["interim_clock", "dead_name", "no_reverse_bound"])
def test_rea_crossing_guard_fails_on_the_dead_exception_shape(tmp_path, mutant) -> None:
    """Rebuild the 631e62a shape (interim create_clock rea_tck on the BSCANE2
    TCK pin, bounds written against it) and its partial variants on a copy of
    the tree; the guard must go red on each."""
    for rel in ("hw/xdc/timing.xdc", "hw/rtl/deepskopion_top.vhd",
                "libs/routertl/rea/rtl/rr_rea_jtag_xilinx7.vhd",
                "libs/routertl/rea/constraints/rr_rea_scoped.xdc"):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((ROOT / rel).read_text())
    xdc_path = tmp_path / "hw/xdc/timing.xdc"
    xdc = xdc_path.read_text()
    pkg = "[get_clocks -of_objects [get_pins u_rea/u_top/tck_i]]"
    assert xdc.count(pkg) == 2, "fixture drifted: expected both package-clock bounds"
    if mutant == "interim_clock":
        xdc = ("create_clock -name rea_tck -period 33.333 [get_pins u_rea/u_bscane2/TCK]\n"
               + xdc.replace(pkg, "[get_clocks rea_tck]"))
    elif mutant == "dead_name":
        xdc = xdc.replace(pkg, "[get_clocks rea_tck]")
    else:
        xdc = "\n".join(line for line in xdc.splitlines()
                        if not line.rstrip().endswith("-to " + pkg))
    xdc_path.write_text(xdc)
    with pytest.raises(AssertionError, match="no REA register launches on"):
        _assert_rea_crossing_bounds_live(tmp_path)


def test_zybo_xdc_bounds_tck_sampled_as_data() -> None:
    """The JTAG datalogger samples TCK as data on clk_sys. That path starts at
    the BSCANE2 TCK pin, which -from [get_clocks rea_tck] does not match, so
    the 1447d2a build timed it with a 1 ps requirement (WNS -1.403 ns). It
    stays bounded only while a -from that exact pin exists. The pin is named
    from the RTL, not from the create_clock, so this guard holds on its own
    when the rea_tck clock above is the thing that broke."""
    core = (ROOT / "hw/rtl/deepskopion_core.vhd").read_text()
    assert re.search(
        r"(?:capture_pin\(\d+\)|board_pin\(C_BOARD_PIN_REA_TCK\))\s*<=\s*rea_tap_tck\s*;",
        core,
    ), "deepskopion_core no longer samples rea_tap_tck: retire this bound"
    top = (ROOT / "hw/rtl/deepskopion_top.vhd").read_text()
    top_inst = re.search(r"^\s*(\w+)\s*:\s*entity\s+rr_rea\.rr_rea_xilinx7\b", top, re.M)
    assert top_inst, "deepskopion_top has no `: entity rr_rea.rr_rea_xilinx7` instance"
    wrapper = (ROOT / "libs/routertl/rea/rtl/rr_rea_jtag_xilinx7.vhd").read_text()
    bscan_inst = re.search(r"^\s*(\w+)\s*:\s*BSCANE2\b", wrapper, re.M)
    assert bscan_inst, "rr_rea_xilinx7 has no `: BSCANE2` instance"
    tck_pin = f"{top_inst.group(1)}/{bscan_inst.group(1)}/TCK"
    xdc = _code(ROOT / "hw/xdc/timing.xdc")
    assert re.search(
        rf"set_max_delay\s+-datapath_only\s+[\d.]+\s+-from\s+\[get_pins\s+"
        rf"{re.escape(tck_pin)}\]\s+-to\s+\[get_clocks\s+clk_fpga_0\]", xdc), (
        f"{_TCK_MARKER}: TCK sampled as data has no -datapath_only bound from {tck_pin}"
    )


def test_rea_scoped_xdc_guard_fails_when_the_lock_entry_is_dropped(tmp_path) -> None:
    """The guard must go red when ip.lock stops applying the package XDC, not
    only when timing.xdc grows a second clock (the DS-P2.168 review mutant:
    drop the scoped_constraints entry, 632 no_clock pins return, text checks
    on the package file stay green). Runs the real guard on a copy of the tree
    whose lock lacks the rr_rea_top record."""
    assert _rea_scoped_xdc_applied(ROOT), "baseline: the real lock applies it"
    _assert_rea_tck_clocked(ROOT)

    for rel in (
        "hw/xdc/timing.xdc",
        TARGET,
        "hw/rtl/deepskopion_top.vhd",
        "libs/routertl/rea/rtl/rr_rea_jtag_xilinx7.vhd",
        "libs/routertl/rea/constraints/rr_rea_scoped.xdc",
    ):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((ROOT / rel).read_text())

    lock = yaml.safe_load((ROOT / "ip.lock").read_text())
    for entry in lock.values():
        if isinstance(entry, dict):
            entry["scoped_constraints"] = [
                rec for rec in entry.get("scoped_constraints") or []
                if not (rec.get("ref") == "rr_rea_top"
                        and str(rec.get("path", "")).endswith("/rr_rea_scoped.xdc"))
            ]
    (tmp_path / "ip.lock").write_text(yaml.safe_dump(lock))

    assert _rea_scoped_xdc_applied(tmp_path) is None
    with pytest.raises(AssertionError, match="no scoped_constraints entry"):
        _assert_rea_tck_clocked(tmp_path)
