"""DS-X4: the checked-in inputs must describe a clean, queued Zybo rebuild.

The long Vivado job is acceptance evidence, not a useful commit-time test.  These
cheap assertions guard the exact defects which otherwise make that job fail late:
a stale lock containing vanished source paths, target drift, or loss of one of the
constraints/hooks which reconstructs the silicon design.
"""

import re
import shutil
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
TARGET_PATH = ROOT / "targets" / "zybo_datalogger.yml"  # DS-P1.103: the shipped Zybo target (canfd_demo is bench)
# DS-P2.287: one lock per target; the default target's lock is the file the root routertl.lock became.
_DEFAULT_TARGET = Path(str(yaml.safe_load((ROOT / "project.yml").read_text(encoding="utf-8"))["target"])).stem
LOCK_PATH = ROOT / "targets" / f"{_DEFAULT_TARGET}.routertl.lock"
IP_MANIFEST_PATH = ROOT / "ip.yml"


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


#: Where rr's pre-synth step leaves the regenerated HPS synthesis manifest
#: (hps_regenerate, DS-P2.196). The repository tracks no copy of it.
REGEN_HPS_QIP = "build/rr_ip_regen/ip/agilex_hps/agilex_hps/agilex_hps.qip"


def _stage_hps_fixture(tmp_path: Path) -> None:
    """Plant the real declaration and the regenerated HPS manifest a DE25 synth
    leaves in the build tree: the artifact that caused import_ip."""
    shutil.copy2(IP_MANIFEST_PATH, tmp_path / "ip.yml")
    qip = tmp_path / REGEN_HPS_QIP
    qip.parent.mkdir(parents=True)
    qip.write_text("# test fixture: regenerated HPS synthesis manifest\n", encoding="utf-8")


def _render_zybo_ip_files(tmp_path: Path, monkeypatch) -> str:
    """Render the Make surface whose IP_FILES becomes Vivado import_ip argv."""
    from routertl_core.project_manager import generate_mk_file

    target = _yaml(TARGET_PATH)
    config = {
        "project": target["project"],
        "hardware": target["hardware"],
        # A Xilinx artifact is the positive control: IP_FILES is live even
        # when the foreign HPS qip is correctly absent.
        "sources": {"ip": ["ip/zybo_positive_control.xci"]},
    }
    (tmp_path / "ip").mkdir(exist_ok=True)
    (tmp_path / "ip/zybo_positive_control.xci").write_text(
        "positive control\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "build_env.mk"
    generate_mk_file(config, output)
    return output.read_text(encoding="utf-8")


def _render_zybo_libraries(tmp_path: Path, monkeypatch, *, explicit: bool) -> str:
    """Render the g_libraries dictionary consumed by gen_project.tcl."""
    from routertl_core.project_manager import generate_tcl_file

    target = _yaml(TARGET_PATH)
    shutil.copytree(ROOT / "hw/rtl", tmp_path / "hw/rtl")
    # All of hw/rtl, as the real flow lists it. Library attribution scans only
    # listed files, so listing just the core and the top hides the tich packages
    # the inference comes from, and the negative control stops reproducing it.
    sources = {
        "syn": [
            p.relative_to(tmp_path).as_posix()
            for p in sorted((tmp_path / "hw/rtl").rglob("*.vhd"))
        ],
        "libraries": dict(target["sources"]["libraries"]),
    }
    if not explicit:
        sources["libraries"].pop("xil_defaultlib")
    config = {
        "project": target["project"],
        "hardware": target["hardware"],
        "sources": sources,
    }
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "build_env.tcl"
    generate_tcl_file(config, output)
    return output.read_text(encoding="utf-8")


def test_zybo_rendered_ip_set_excludes_altera(tmp_path, monkeypatch, capsys):
    """The real frozen Agilex qip must not reach Vivado's IP_FILES."""
    _stage_hps_fixture(tmp_path)
    rendered = _render_zybo_ip_files(tmp_path, monkeypatch)
    notice = capsys.readouterr().out

    assert "IP_FILES := ip/zybo_positive_control.xci" in rendered
    assert "agilex_hps.qip" not in rendered
    assert "Skipped IP hps_subsys" in notice
    assert "is altera but target is xilinx" in notice


def test_zybo_ip_guard_fixture_exposes_the_forbidden_qip(tmp_path, monkeypatch):
    """Negative control: defeating the vendor mismatch must surface the qip."""
    _stage_hps_fixture(tmp_path)
    manifest_path = tmp_path / "ip.yml"
    manifest = _yaml(manifest_path)
    assert manifest["platform"]["hps"]["vendor"] == "altera"
    manifest["platform"]["hps"]["vendor"] = "xilinx"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    rendered = _render_zybo_ip_files(tmp_path, monkeypatch)
    assert REGEN_HPS_QIP in rendered


def _libraries_of(rendered: str, source: str) -> list[str]:
    """Each library the rendered g_libraries dictionary puts source in."""
    match = re.search(r"set g_libraries \[dict create (.*)\]\n", rendered)
    assert match, rendered
    return [
        library
        for library, files in re.findall(r'"(\w+)" \[list ([^\]]*)\]', match.group(1))
        if f'"{source}"' in files
    ]


def test_zybo_rendered_core_is_in_project_library(tmp_path, monkeypatch):
    """Vivado must read the core into the top's physical project library."""
    rendered = _render_zybo_libraries(tmp_path, monkeypatch, explicit=True)
    libraries = _libraries_of(rendered, "hw/rtl/deepskopion_core.vhd")

    assert libraries == ["xil_defaultlib"]
    assert "work" not in libraries
    assert "tich" not in libraries


def test_zybo_core_fixture_reproduces_the_bad_inference(tmp_path, monkeypatch):
    """Negative control: without the override the real core infers to tich."""
    rendered = _render_zybo_libraries(tmp_path, monkeypatch, explicit=False)
    assert _libraries_of(rendered, "hw/rtl/deepskopion_core.vhd") == ["tich"], rendered


def test_portable_fifo_sources_are_present_and_pinned_in_both_locks():
    """Catch an incomplete OLO closure before a 96-minute vendor-tool failure."""
    assert LOCK_PATH.is_file(), f"the default target's lock is missing: {LOCK_PATH}"
    lock = _yaml(LOCK_PATH)
    package_lock = _yaml(ROOT / "ip.lock")
    locked_sources = {
        entry["path"]: entry["sha256"]
        for entry in lock["sources"]
        if "excluded" not in entry
    }
    package_sources = {
        path: digest
        for name, package in package_lock.items()
        if name.startswith("open-logic/")
        for path, digest in package["file_hashes"].items()
    }

    required_units = {
        "libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_fifo_sync.vhd",
        "libs/open-logic/olo_base_fifo_async/src/base/vhdl/olo_base_fifo_async.vhd",
        "libs/open-logic/olo_base_wconv_n2xn/src/base/vhdl/olo_base_wconv_n2xn.vhd",
        "libs/open-logic/olo_base_wconv_xn2n/src/base/vhdl/olo_base_wconv_xn2n.vhd",
    }
    assert required_units <= locked_sources.keys()
    for path, digest in locked_sources.items():
        if path.startswith("libs/open-logic/"):
            assert package_sources.get(path) == digest, f"unpinned OLO source: {path}"

    olo_packages = [
        package for package in lock["dependencies"] if package["name"].startswith("open-logic/")
    ]
    assert {package["name"] for package in olo_packages} == {
        "open-logic/olo_base",
        "open-logic/olo_base_fifo_async",
        "open-logic/olo_base_fifo_sync",
        "open-logic/olo_base_wconv_n2xn",
        "open-logic/olo_base_wconv_xn2n",
    }
    assert {package["library"] for package in olo_packages} == {"olo"}


def test_zybo_build_identity_and_closure_inputs_are_pinned():
    target = _yaml(TARGET_PATH)
    hardware = target["hardware"]
    build = target["build_options"]

    assert hardware["part"] == "xc7z020clg400-1"
    assert hardware["platform"] == "zybo-z7-20"
    assert build["tool_version"] == "2024.1"
    assert target["project"]["output_dir"] == "build/zybo_datalogger"
    # DS-P2.168 / RTL-P2.1544: timing.xdc is parsed LATE, after the package
    # scoped XDC that creates the REA TCK clock its bounds name.
    assert target["sources"]["xdc"] == [
        {"path": "hw/xdc/timing.xdc", "processing_order": "late"},
        # DS-P2.309 interim (RTL-P2.2402, removed by DS-P3.269): Pmod input pull-ups
        {"path": "hw/xdc/zybo_header_pullups.xdc"},
    ]
    assert hardware["ps7_hooks"] == [
        "hw/bd/ds_hp2_off.tcl",
        "hw/bd/ds_irq_f2p_on.tcl",
        "hw/bd/ds_emio_uart_i2c_spi.tcl",
        "hw/bd/ds_emio_can.tcl",
    ]
    assert target["eda_memory"]["peaks"]["vivado"] == {
        "synth": 22000,
        "impl": 16000,
    }


def test_block_design_is_regenerated_and_queue_policy_stays_explicit():
    target = _yaml(TARGET_PATH)
    assert target["tcl_hooks"]["gen_project"] == [
        "hw/bd/deepskopion_bd.tcl"
    ]
    # DS-E0 deliberately keeps bench scheduling out of the public target.
    # DS-X4 invokes `rr queue submit ...` explicitly; restoring queue_builds
    # here would make an ordinary user's build hang without a dispatcher.
    assert "queue_builds" not in target["build_options"]
