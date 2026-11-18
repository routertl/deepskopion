"""DS-P2.196 regression guard: rr regenerates the DE25 HPS and its EMIF.

The DE25 target ships no Intel or Altera generated file. ``ip.yml`` declares
the Agilex 5 HPS and its io96b EMIF by parameter (``hps_regenerate: true``,
RTL-P2.2101). ``rr synth run`` makes ``agilex_hps.ip`` and
``emif_io96b_hps.ip`` under the user's Quartus in ``build/rr_ip_regen/``, then
runs the same add_component-by-file pipeline the frozen files used, and the
full-system ``qsys-generate`` builds both synthesis trees in context.

This file supersedes ``test_de25_hps_frozen_completeness_p1_51.py``
(DS-P1.51/DS-P1.52/DS-P1.45). Each guard there pinned the frozen mechanism;
each has an equivalent here that pins the same failure on the regenerate path:

* ip.yml declares hps_frozen + hps_emif
  -> ``test_ip_yml_declares_hps_regenerate_and_emif``
* the hand-tcl hook stays retired -> unchanged
* the real generator renders add_component, not add_instance
  -> ``test_real_generator_renders_add_component_not_add_instance`` (driven
  through ``generate_hps_pre_synth`` with a stand-in Quartus, so the paths are
  the regenerated files)
* the frozen .ip files are vendored with real content
  -> ``test_regenerated_variants_are_made_from_the_declaration_in_the_build_tree``
* the HPS .ip carries the 48-pin IO48 mux
  -> ``test_declared_hps_parameters_carry_the_full_io48_pin_mux``
* hps_io is re-exported -> unchanged, on the regenerate render
* the frozen synthesis tree is tracked, not ignored
  -> ``test_no_vendor_generated_ip_file_is_tracked`` +
  ``test_the_regenerated_tree_is_ignored_by_git``
* every source the frozen qip names is tracked and inside the repo
  -> ``test_a_clean_tree_reaches_the_full_system_generate`` (a clean checkout
  is not stopped by a missing tree, and rr never generates the HPS standalone)
* the vendored tree passes the land guard's whitespace check, and the
  exemption is load-bearing and narrow
  -> ``test_no_whitespace_exemption_is_left_for_the_removed_tree``

The guards cannot run Quartus. The proof that the regenerated variants
elaborate and fit is the queued DE25 synth/impl recorded on DS-P2.196.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
IP_YML = ROOT / "ip.yml"
RETIRED_HOOK = ROOT / "hw/quartus/de25/de25_hps_subsys.tcl"
DE25_TARGET = ROOT / "targets" / "de25_datalogger.yml"
REGEN_DIR = "build/rr_ip_regen"
HPS_IP = f"{REGEN_DIR}/ip/agilex_hps/agilex_hps.ip"
EMIF_IP = f"{REGEN_DIR}/ip/emif_io96b_hps/emif_io96b_hps.ip"
REMOVED_TREES = ("ip/agilex_hps", "ip/emif_io96b_hps", "ip/de25_fabric_pll")


def _hps_entry() -> dict:
    config = yaml.safe_load(IP_YML.read_text(encoding="utf-8"))
    return next(ip for ip in config["ips"] if ip["name"] == "hps")


def _de25_part() -> str:
    return yaml.safe_load(DE25_TARGET.read_text(encoding="utf-8"))["hardware"]["part"]


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def _tracked_files() -> set[str]:
    out = _git("ls-files", "-z").stdout
    return {p for p in out.split("\0") if p}


# ── a stand-in Quartus: our own minimal IP-XACT, no vendor text ─────────────────

_IP = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <ipxact:component xmlns:altera="http://www.altera.com/XMLSchema/IPXact2014/extensions"
                      xmlns:ipxact="http://www.accellera.org/XMLSchema/IPXACT/1685-2014">
      <ipxact:library>{name}</ipxact:library>
      <ipxact:name>{name}</ipxact:name>
      <ipxact:model><ipxact:ports>{ports}</ipxact:ports></ipxact:model>
      <ipxact:vendorExtensions>
        <altera:altera_module_parameters>
          <ipxact:parameters>{top}</ipxact:parameters>
        </altera:altera_module_parameters>
        <altera:altera_packaged_subsystems altera:version="1.1">
          <altera:altera_internal_packaged_subsystem altera:name="{name}">
            <altera:sub_modules>{subs}</altera:sub_modules>
          </altera:altera_internal_packaged_subsystem>
        </altera:altera_packaged_subsystems>
      </ipxact:vendorExtensions>
    </ipxact:component>
    """
)


def _param(pid: str, value: object) -> str:
    return (f'<ipxact:parameter parameterId="{pid}" type="string"><ipxact:name>{pid}</ipxact:name>'
            f"<ipxact:value>{value}</ipxact:value></ipxact:parameter>")


def _ip_text(name: str, top: dict, subs: dict, ports) -> str:
    return _IP.format(
        name=name,
        ports="".join(f"<ipxact:port><ipxact:name>{p}</ipxact:name></ipxact:port>" for p in ports),
        top="".join(_param(k, v) for k, v in top.items()),
        subs="".join(
            f'<altera:sub_module altera:name="{s}"><ipxact:parameters>'
            + "".join(_param(k, v) for k, v in vals.items())
            + "</ipxact:parameters></altera:sub_module>"
            for s, vals in subs.items()
        ),
    )


_SET = re.compile(r"^set_instance_parameter_value \$rr_i \{([^}]+)\} (.+)$")


class _StandInQuartus:
    """ip-deploy writes an empty variant; qsys-script applies the parameter lines
    the way Quartus keeps them; the EMIF reload derives the declared port set.
    The system qsys-script is recorded; qsys-generate builds nothing."""

    def __init__(self, emif_ports, emif_children):
        self.calls: list[list[str]] = []
        self.system_scripts: list[str] = []
        self.emif_ports = tuple(emif_ports)
        # ip-deploy emits every child parameter at a catalog default; rr refuses
        # to write a key the deployed file does not carry.
        self.emif_children = {s: {k: "0" for k in vals} for s, vals in emif_children.items()}

    def __call__(self, argv, cwd=None, **_kw):
        from sdk.engine.hps_regen import read_ip

        self.calls.append([str(a) for a in argv])
        tool = Path(argv[0]).name
        if tool == "ip-deploy":
            opts = dict(a[2:].split("=", 1) for a in argv[1:])
            out = Path(opts["output-directory"]) / f"{opts['output-name']}.ip"
            subs = (self.emif_children
                    if opts["component-name"] == "emif_io96b_hps" else {})
            out.write_text(_ip_text(opts["output-name"], {}, subs, ()), encoding="utf-8")
        elif tool == "qsys-script":
            text = Path(argv[1].split("=", 1)[1]).read_text(encoding="utf-8")
            m = re.search(r"^load_system \{(.+)\}$", text, re.M)
            if m is None:
                self.system_scripts.append(text)
                return subprocess.CompletedProcess(argv, 0, "", "")
            ip = Path(m.group(1))
            facts = read_ip(ip)
            top = dict(facts.top)
            for ln in text.splitlines():
                s = _SET.match(ln)
                if s:
                    v = s.group(2)
                    top[s.group(1)] = (",".join(re.findall(r"\{([^}]*)\}", v))
                                       if v.startswith("[list") else v.strip("{}"))
            ports = self.emif_ports if "emif_reload" in argv[1] else facts.ports
            ip.write_text(_ip_text(ip.stem, top, facts.sub_modules, ports), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _regenerate(tmp_path: Path) -> _StandInQuartus:
    """Run rr's real pre-synth HPS step on a copy of this project's ip.yml."""
    from sdk.engine.hps_gen import generate_hps_pre_synth

    shutil.copy2(IP_YML, tmp_path / "ip.yml")
    cfg = _hps_entry()["config"]
    fake = _StandInQuartus(cfg["emif_ports"], cfg["emif_child_parameters"])
    with patch("sdk.engine.hps_gen._resolve_qsys_bin",
               side_effect=lambda name, *_a, **_k: f"/q/quartus/sopc_builder/bin/{name}"), \
         patch("sdk.engine.hps_gen._hardware_part", return_value=_de25_part()), \
         patch("sdk.engine.hps_gen._hardware_platform", return_value="de25-standard"), \
         patch("sdk.engine.hps_regen.quartus_version", return_value="25.3.1.100"), \
         pytest.raises(RuntimeError, match="did not build the regenerated component tree"):
        # The stand-in system generate builds nothing, so rr must stop here
        # rather than generate the HPS on its own (RTL-P2.1159).
        generate_hps_pre_synth(tmp_path, runner=fake)
    return fake


@pytest.fixture(scope="module")
def regenerated(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ds_p2_196_regen")
    return tmp, _regenerate(tmp)


def _rendered_system_script(regenerated) -> str:
    _tmp, fake = regenerated
    assert len(fake.system_scripts) == 1, (
        f"rr ran {len(fake.system_scripts)} system qsys-script passes; expected 1")
    return fake.system_scripts[0]


# ── the declaration ─────────────────────────────────────────────────────────────

def test_ip_yml_declares_hps_regenerate_and_emif():
    """The rr-native lever: the HPS and EMIF are made from these keys."""
    hps_ip = _hps_entry()
    cfg = hps_ip["config"]
    assert hps_ip["type"] == "intel_agilex_5_soc"
    assert cfg.get("hps_regenerate") is True, (
        "ip.yml ips[].config.hps_regenerate must be true: the source tree holds "
        "no HPS .ip, so without it rr falls back to add_instance-by-catalog-type, "
        "the Generic-Component black box DS-P1.52 measured.")
    assert "hps_frozen" not in cfg, (
        "hps_frozen and hps_regenerate are exclusive; hps_frozen reads .ip files "
        "the source tree no longer holds.")
    assert cfg["hps_emif"] is True, (
        "hps_emif must stay true: without the io96b EMIF the HPS has no DRAM "
        "controller and the SPL never relocates.")
    assert cfg["emif_parameters"]["EMIF_PROTOCOL"] == "DDR4_COMP"
    assert cfg["emif_child_parameters"], "the DDR4 timings (EMIF child values) are not declared"
    assert {"mem_0_dbi_n", "ref_clk", "oct_rzqin_0"} <= set(cfg["emif_ports"])


def test_de25_hps_subsys_tcl_hook_stays_retired():
    """DS-P1.52: the hand-authored qsys-script this guard used to pin is
    retired outright — its presence (even unused) would risk a competing,
    tcl-driven HPS-generation path reappearing."""
    assert not RETIRED_HOOK.exists(), (
        f"{RETIRED_HOOK} must stay deleted (DS-P1.52) — this is the "
        "disproven mechanism: its add_component-by-file text guard (DS-"
        "P1.51) stayed green while a fresh queued bitstream still hit "
        "quartus_pfg Error 20809 (no real HPS partition in the .sof)."
    )


# ── the regenerate path, driven through rr's real pre-synth step ────────────────

def test_real_generator_renders_add_component_not_add_instance(regenerated):
    """The system script rr renders after regeneration names the two made files
    by path; add_instance-by-type (the black-box mechanism) never appears."""
    text = _rendered_system_script(regenerated)
    for name, rel in (("hps", "ip/agilex_hps/agilex_hps.ip"),
                      ("emif_hps", "ip/emif_io96b_hps/emif_io96b_hps.ip")):
        assert f"add_component {name} {{{rel}}}" in text, (
            f"the real rr HPS generator did not render 'add_component {name} "
            f"{{{rel}}}' for the regenerated variant")
        assert not re.search(rf"add_instance\s+{re.escape(name)}\s", text), (
            f"the real rr HPS generator rendered 'add_instance {name} ...': that "
            "resolves by IP-catalog type, the Generic-Component black box")


def test_regenerated_variants_are_made_from_the_declaration_in_the_build_tree(regenerated):
    """The two variants exist only in the build tree, and the EMIF variant carries
    the declared DDR4 values and the exact declared port set."""
    from sdk.engine.hps_regen import read_ip

    tmp, _fake = regenerated
    made = sorted(p.relative_to(tmp).as_posix() for p in tmp.rglob("*.ip"))
    assert made == [HPS_IP, EMIF_IP]
    cfg = _hps_entry()["config"]
    emif = read_ip(tmp / EMIF_IP)
    assert emif.top["EMIF_PROTOCOL"] == "DDR4_COMP"
    assert set(emif.ports) == set(cfg["emif_ports"])
    hps = read_ip(tmp / HPS_IP)
    assert hps.top["H2F_Width"] == str(cfg["h2f_width"])
    assert hps.top["f2s_data_width"] == str(cfg["f2s_width"])


def test_declared_hps_parameters_carry_the_full_io48_pin_mux(regenerated):
    """The measurable difference between a real HPS and a Generic Component is
    the IO48 pin mux. rr sets all 48 HPS_IO_Enable entries on the made variant."""
    from sdk.engine.hps_regen import read_ip

    tmp, _fake = regenerated
    io_enable = read_ip(tmp / HPS_IP).top["HPS_IO_Enable"].split(",")
    assert len(io_enable) == 48, (
        f"the regenerated agilex_hps.ip carries {len(io_enable)} HPS_IO_Enable "
        "entries; the DE25 IO48 pin mux has 48")
    for function in ("UART1:TX", "UART1:RX", "SDMMC:CCLK", "EMAC0:TX_CLK"):
        assert function in io_enable, f"{function} is not routed in the IO48 pin mux"


def test_hps_io_conduit_is_reexported_by_the_real_generator(regenerated):
    """The real HPS generator must actually re-export hps.hps_io — without
    this, quartus_pfg has no path from the HPS's physical pins to the board
    top, independent of whether the HPS component itself is real."""
    text = _rendered_system_script(regenerated)
    assert "add_interface hps_io conduit end" in text
    assert "set_interface_property hps_io EXPORT_OF hps.hps_io" in text, (
        "hps.hps_io must be exported as the generated hps_subsys top's "
        "hps_io interface — without this line the HPS's IO48 pin-mux never "
        "reaches board pins even if the component itself elaborates for real."
    )


def test_a_clean_tree_reaches_the_full_system_generate(regenerated):
    """DE-G0: a clean checkout with no generated tree is not stopped by a missing
    manifest. rr reaches the full-system qsys-generate, and it never runs
    qsys-generate on the HPS variant on its own."""
    _tmp, fake = regenerated
    generates = [c for c in fake.calls if Path(c[0]).name == "qsys-generate"]
    assert generates, "rr never reached the full-system qsys-generate"
    assert all("agilex_hps.ip" not in " ".join(c) for c in generates), (
        "rr generated the HPS variant standalone; that loses the full-system "
        "IO48 pin-mux context (RTL-P2.1159)")


# ── the source tree ─────────────────────────────────────────────────────────────

def test_no_vendor_generated_ip_file_is_tracked():
    """No Intel or Altera generated file is in the repository."""
    tracked = _tracked_files()
    leftovers = sorted(p for p in tracked if p.startswith(tuple(f"{t}/" for t in REMOVED_TREES)))
    assert leftovers == []
    vendor_ext = sorted(p for p in tracked if p.endswith((".ip", ".qip", ".qsys", ".sopcinfo")))
    assert vendor_ext == [], f"vendor-generated IP files are tracked: {vendor_ext[:6]}"


def test_the_regenerated_tree_is_ignored_by_git():
    """What rr makes must never be committed by accident."""
    for rel in (HPS_IP, EMIF_IP, f"{REGEN_DIR}/ip/agilex_hps/agilex_hps/agilex_hps.qip"):
        assert _git("check-ignore", "-q", rel).returncode == 0, f"{rel} is not ignored by git"


def test_no_whitespace_exemption_is_left_for_the_removed_tree():
    """The DS-P1.45 ``-whitespace`` exemption covered vendor output only. With no
    vendor output left it must be gone, so it cannot hide whitespace in a file
    that is later added at those paths."""
    for rel in ("ip/agilex_hps/agilex_hps/agilex_hps.qip", "ip/agilex_hps/agilex_hps.ip",
                "ip/emif_io96b_hps/emif_io96b_hps.ip"):
        attr = _git("check-attr", "whitespace", "--", rel).stdout.strip()
        assert attr.endswith(": unspecified"), f"whitespace exemption still applies: {attr!r}"
