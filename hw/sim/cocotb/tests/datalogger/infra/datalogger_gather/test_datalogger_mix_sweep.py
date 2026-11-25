# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.65 — Arbitrary datalogger mix elaboration and per-protocol traffic smoke test.

Swept across build profiles via `rr sim sweep --tests test_datalogger_mix_sweep`
with `--profiles tools/tests/datalogger_mix/sim_profiles.yml`. Reads the active
profile from $RR_SIM_PROFILE (defaulting to "default"), generates the matching
datalogger_config_pkg.vhd and datalogger_gather.vhd (or takes them from a frozen
fixture when the profile says so), and elaborates one of three benches:

  core    tb_datalogger_mix_sweep: deepskopion_core plus a datalogger_gather;
          per-protocol traffic smoke and absent-protocol telemetry readback
  gather  tb_datalogger_gather_probe: the gather alone, EDGE ports mapped as
          the core maps them; per-protocol traffic smoke
  pkg     tb_datalogger_pkg_probe: the package alone; EDGE interface constants
  pinmap  tb_core_pin_map: deepskopion_core alone; toggles each board input and
          reads which logical capture pin it reaches (DS-P1.66)
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import yaml
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.paths import get_build_dir_for_simulator  # noqa: E402
from engine.simulation import run_simulation  # noqa: E402
from sdk.cocotb_helpers import requires       # noqa: E402

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
MIX_DIR = DEEPSKOPION_ROOT / "tools" / "tests" / "datalogger_mix"
PROFILES_PATH = MIX_DIR / "sim_profiles.yml"
PROJECT_CONFIG = DEEPSKOPION_ROOT / "tools" / "datalogger-builder" / "config" / "datalogger_config.yml"
sys.path.insert(0, str(DEEPSKOPION_ROOT / "tools" / "datalogger-builder"))
import datalogger_gather_gen as dgg          # noqa: E402
import configurator as cfg                   # noqa: E402

CLK_PERIOD_NS = 10

# Domain codes from system_ids.yml
DOMAIN_UART = 0x1
DOMAIN_I2C  = 0x2
DOMAIN_SPI  = 0x3
DOMAIN_EDGE = 0x6
DOMAIN_JTAG = 0x8

DOMAIN_BY_NAME = {
    "uart": DOMAIN_UART,
    "i2c": DOMAIN_I2C,
    "spi": DOMAIN_SPI,
    "edge": DOMAIN_EDGE,
    "jtag": DOMAIN_JTAG,
}


def _seed_regbank_rom_symlinks(root: Path) -> None:
    for build_dir in (root / "sim" / "work" / "nvc",
                      root / "hw" / "sim" / "work" / "nvc"):
        for hex_name in ("regbank_schema.hex", "config_schema.hex"):
            real = root / "hw" / "rtl" / "regbank" / hex_name
            link = build_dir / "hw" / "rtl" / "regbank" / hex_name
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                if link.resolve() == real.resolve():
                    continue
                link.unlink()
            link.symlink_to(real)


def load_profile(profile_name: str) -> dict:
    profiles = yaml.safe_load(PROFILES_PATH.read_text(encoding="utf-8"))
    if profile_name not in profiles:
        raise ValueError(f"Profile '{profile_name}' not defined in {PROFILES_PATH}")
    return profiles[profile_name]


def mix_config(counts: dict, allow_unwired: bool = False) -> dict:
    """The configurator's config for a {protocol: count} mix.

    DS-P1.66: these are the pin numbers a user gets (the configurator counts
    logical pins 0..N), not the core's legacy capture slots. The generator maps
    them onto core board pins; a bench on the legacy slots would hide a core
    that only wires those slots.
    """
    config = yaml.safe_load(cfg.generate_config_yaml(cfg.get_board("zybo-z7-20"), counts))
    if allow_unwired:
        config["Constraints"]["AllowUnwiredPins"] = True
    return config


def profile_config(profile: dict) -> dict:
    """The datalogger config a profile builds: the project's own file, or a mix."""
    if profile.get("config") == "project":
        return dgg.load_yaml(PROJECT_CONFIG)
    return mix_config(profile["dataloggers"], bool(profile.get("allow_unwired", False)))


TELEMETRY_SLOTS = {
    "i2c": {"pkt": 208, "err": 240, "drop": 288},
    "spi": {"pkt": 212, "err": 244, "drop": 292},
    "uart": {"pkt": 216, "err": 248, "drop": 296},
    "edge": {"pkt": 224, "err": 256},
    "jtag": {"pkt": 236, "err": 264},
}


async def axil_write(dut, addr: int, data: int):
    dut.S_AXIL_AWADDR.value = addr
    dut.S_AXIL_AWVALID.value = 1
    dut.S_AXIL_WDATA.value = data
    dut.S_AXIL_WSTRB.value = 0xF
    dut.S_AXIL_WVALID.value = 1
    dut.S_AXIL_BREADY.value = 1

    aw_done = False
    w_done = False
    while not (aw_done and w_done):
        await RisingEdge(dut.CLK)
        if not aw_done and int(dut.S_AXIL_AWREADY.value) == 1:
            dut.S_AXIL_AWVALID.value = 0
            aw_done = True
        if not w_done and int(dut.S_AXIL_WREADY.value) == 1:
            dut.S_AXIL_WVALID.value = 0
            w_done = True

    while int(dut.S_AXIL_BVALID.value) == 0:
        await RisingEdge(dut.CLK)
    await RisingEdge(dut.CLK)


async def axil_read(dut, addr: int) -> int:
    dut.S_AXIL_ARADDR.value = addr
    dut.S_AXIL_ARVALID.value = 1
    dut.S_AXIL_RREADY.value = 1

    while True:
        await RisingEdge(dut.CLK)
        if int(dut.S_AXIL_ARREADY.value) == 1:
            dut.S_AXIL_ARVALID.value = 0
            break

    while int(dut.S_AXIL_RVALID.value) == 0:
        await RisingEdge(dut.CLK)
    val = int(dut.S_AXIL_RDATA.value)
    await RisingEdge(dut.CLK)
    return val


class DoutCollector:
    def __init__(self, dut):
        self.dut = dut
        self.frames = []  # (datalogger_id, domain, msg_type, payload)
        self._run = True

    async def run(self):
        self.dut.DOUT_REQ.value = 1
        while self._run:
            await RisingEdge(self.dut.CLK)
            if int(self.dut.DOUT_VALID.value) == 1:
                dev_id = int(self.dut.DOUT_DATALOGGER_ID.value)
                msg_type = int(self.dut.DOUT_MSG_TYPE.value)
                domain = (msg_type >> 8) & 0x0F
                payload = int(self.dut.DOUT_PAYLOAD.value)
                self.frames.append((dev_id, domain, msg_type, payload))

    def stop(self):
        self._run = False


async def _ramp_timestamp(dut):
    t = 0
    while True:
        await RisingEdge(dut.CLK)
        t = (t + 1) & 0xFFFFFFFF
        dut.TIMESTAMP.value = t


def _profile_checks(profile: dict) -> set[str]:
    return set(profile.get("checks", ["traffic", "absent_telemetry"]))


async def _drive_traffic(dut, name: str, inst_pins: dict, pins_val: int) -> int:
    """Drive a minimal transaction for one protocol instance; return the pin bus."""
    if name == "edge":
        line_pin = int(inst_pins["LINE"])
        # Pulse line high then low
        pins_val |= (1 << line_pin)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 20)
        pins_val &= ~(1 << line_pin)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 50)

    elif name == "i2c":
        scl = int(inst_pins["SCL"])
        sda = int(inst_pins["SDA"])
        # START: SDA falls while SCL high
        pins_val &= ~(1 << sda)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 10)
        # Toggle SCL low then high
        pins_val &= ~(1 << scl)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 10)
        pins_val |= (1 << scl)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 10)
        # STOP: SDA rises while SCL high
        pins_val |= (1 << sda)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 50)

    elif name == "spi":
        cs = int(inst_pins["CS"])
        sclk = int(inst_pins["SCLK"])
        mosi = int(inst_pins["MOSI"])
        # CS low
        pins_val &= ~(1 << cs)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 10)
        # Toggle SCLK
        pins_val |= (1 << mosi)
        pins_val |= (1 << sclk)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 10)
        pins_val &= ~(1 << sclk)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 10)
        # CS high
        pins_val |= (1 << cs)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 50)

    elif name == "uart":
        tx = int(inst_pins["TX"])
        # Drive start bit (0)
        pins_val &= ~(1 << tx)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 40)
        # Stop bit (1)
        pins_val |= (1 << tx)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 50)

    elif name == "jtag":
        tck = int(inst_pins["TCK"])
        tms = int(inst_pins["TMS"])
        # Toggle TCK
        pins_val |= (1 << tms)
        pins_val |= (1 << tck)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 10)
        pins_val &= ~(1 << tck)
        dut.CAPTURE_PINS.value = pins_val
        await ClockCycles(dut.CLK, 50)

    return pins_val


def _edge_maxfreq_width() -> int:
    protocols = dgg.load_yaml(
        DEEPSKOPION_ROOT / "tools" / "datalogger-builder" / "protocols.yml")
    return int(protocols["protocols"]["EDGE"].get("maxfreq_width", 24))


async def _run_pkg_bench(dut, profile_name: str, profile: dict) -> None:
    """The generated package exports the EDGE interface constants for this mix."""
    expected_width = _edge_maxfreq_width()
    expected_edges = int(profile["dataloggers"].get("edge", 0))
    await Timer(1, unit="ns")
    width = int(dut.EDGE_MAXFREQ_WIDTH.value)
    edges = int(dut.EDGE_DATALOGGERS.value)
    assert edges == expected_edges, (
        f"Profile '{profile_name}': C_EDGE_DATALOGGERS={edges}, mix has {expected_edges}")
    assert width == expected_width, (
        f"Profile '{profile_name}': C_EDGE_MAXFREQ_WIDTH={width}, "
        f"protocols.yml maxfreq_width={expected_width}")
    dut._log.info(
        f"Profile '{profile_name}' pkg PASS: C_EDGE_DATALOGGERS={edges}, "
        f"C_EDGE_MAXFREQ_WIDTH={width}")


# deepskopion_core's capture inputs on tb_core_pin_map: (board pin, tb port,
# bit, idle level). The CAN bus is the wired-AND of the controller TX legs, so
# pulling CAN0_TX low is what moves it.
BOARD_INPUTS = (
    [(f"JA{i}", "JA", i, 0) for i in range(8)]
    + [(f"JB{i}", "JB", i, 0) for i in range(8)]
    + [(name, name, None, 0) for name in ("REA_TCK", "REA_TMS", "REA_TDI", "REA_TDO")]
    + [("CAN_BUS", "CAN0_TX", None, 1)]
)


def configured_pins(config: dict) -> dict[int, str]:
    """{logical pin: "PROTO id N SIGNAL"} for every signal the config captures."""
    used: dict[int, str] = {}
    for proto, instances in config.get("Dataloggers", {}).get("Burst", {}).items():
        for datalogger_id, pin_map in sorted(instances.items(), key=lambda x: int(x[0])):
            for signal, pin in pin_map.items():
                used.setdefault(int(pin), f"{proto.upper()} id {datalogger_id} {signal}")
    return used


def declared_capture_map(pkg_path: Path) -> dict[int, str]:
    """The C_CAPTURE_BOARD_PIN map a config package declares: {logical pin: board pin}."""
    text = pkg_path.read_text(encoding="utf-8")
    block = re.search(r"constant C_CAPTURE_BOARD_PIN\b.*?;", text, re.S)
    if not block:
        return {}
    return {int(pin): name for pin, name in
            re.findall(r"(\d+)\s*=>\s*C_BOARD_PIN_(\w+)", block.group(0))
            if name != "NONE"}


async def _run_pinmap_bench(dut, profile_name: str, profile: dict) -> None:
    """DS-P1.66: every configured logical pin is wired to exactly one core input.

    Toggles each board input on its own and reads the core's capture_pin bus,
    so the map is measured on the elaborated core, not read from a generator.
    """
    used = configured_pins(profile_config(profile))
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    idle = {"JA": 0, "JB": 0, "REA_TCK": 0, "REA_TMS": 0, "REA_TDI": 0,
            "REA_TDO": 0, "CAN0_TX": 1}

    def apply(levels: dict) -> None:
        for port, value in levels.items():
            getattr(dut, port).value = value

    apply(idle)
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 10)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 5)
    rest = int(dut.CAPTURE_PIN_OBS.value)

    reached: dict[int, list[str]] = {}
    for name, port, bit, level in BOARD_INPUTS:
        levels = dict(idle)
        levels[port] = idle[port] ^ (1 << bit) if bit is not None else 1 - level
        apply(levels)
        await ClockCycles(dut.CLK, 4)
        moved = int(dut.CAPTURE_PIN_OBS.value) ^ rest
        apply(idle)
        await ClockCycles(dut.CLK, 4)
        for pin in range(len(dut.CAPTURE_PIN_OBS.value)):
            if (moved >> pin) & 1:
                reached.setdefault(pin, []).append(name)

    for pin, who in sorted(used.items()):
        sources = reached.get(pin, [])
        assert sources, (
            f"Profile '{profile_name}': logical capture pin {pin} ({who}) is driven by "
            f"no core input; it reads constant '0' (core wiring: {reached})")
        assert len(sources) == 1, (
            f"Profile '{profile_name}': logical capture pin {pin} ({who}) is driven by "
            f"several core inputs {sources}")
    wired = {pin: reached[pin][0] for pin in used}
    by_source: dict[str, list[int]] = {}
    for pin, source in wired.items():
        by_source.setdefault(source, []).append(pin)
    shared = {source: pins for source, pins in by_source.items() if len(pins) > 1}
    assert not shared, (
        f"Profile '{profile_name}': one core input reaches several configured pins: {shared}")

    expected = {int(pin): name for pin, name in (profile.get("expect_board_pins") or {}).items()}
    if expected:
        got = {pin: wired.get(pin) for pin in expected}
        assert got == expected, (
            f"Profile '{profile_name}': default mix wiring changed: core wires "
            f"{ {p: got[p] for p in expected if got[p] != expected[p]} }, legacy wiring "
            f"{ {p: expected[p] for p in expected if got[p] != expected[p]} }")

    declared = declared_capture_map(mix_pkg_path(profile_name, profile))
    declared_used = {pin: declared.get(pin) for pin in used}
    assert wired == declared_used, (
        f"Profile '{profile_name}': the core wires {wired} but the generated package "
        f"declares {declared_used}")
    dut._log.info(f"Profile '{profile_name}' pinmap PASS: {wired}")


@cocotb.test()
@requires("SGA-REQ-3")
async def test_datalogger_mix_traffic_smoke(dut):
    """Verify elaboration and per-protocol traffic routing for active mix."""
    profile_name = os.environ.get("RR_SIM_PROFILE", "default")
    profile = load_profile(profile_name)
    bench = profile.get("bench", "core")
    if bench == "pkg":
        await _run_pkg_bench(dut, profile_name, profile)
        return

    if bench == "pinmap":
        await _run_pinmap_bench(dut, profile_name, profile)
        return

    checks = _profile_checks(profile)
    has_core = bench == "core"
    config = profile_config(profile)
    burst_protocols = config.get("Dataloggers", {}).get("Burst", {})

    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())

    # Initial idle pin bus
    # High-Z buses idle high (I2C, SPI, UART), Edge/JTAG idle low
    pins_val = 0
    for proto_name, instances in burst_protocols.items():
        name_lower = proto_name.lower()
        if name_lower in ("i2c", "spi", "uart"):
            for inst in instances.values():
                for p in inst.values():
                    pins_val |= (1 << int(p))

    dut.CAPTURE_PINS.value = pins_val
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.DOUT_REQ.value = 0
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 10)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 5)

    if bench == "gather":
        expected_bits = int(profile["dataloggers"].get("edge", 0)) * (32 + _edge_maxfreq_width() + 1)
        port_bits = int(dut.EDGE_PORT_BITS.value)
        assert port_bits == expected_bits, (
            f"Profile '{profile_name}': gather EDGE port interface carries {port_bits} "
            f"bits, expected {expected_bits}")

    cocotb.start_soon(_ramp_timestamp(dut))
    coll = DoutCollector(dut)
    cocotb.start_soon(coll.run())

    present_protocols = {p.lower() for p in burst_protocols.keys() if p.lower() in TELEMETRY_SLOTS}
    absent_protocols = set(TELEMETRY_SLOTS.keys()) - present_protocols
    absent_checked = set(profile.get("absent_checks", sorted(absent_protocols)))
    assert absent_checked <= absent_protocols, (
        f"Profile '{profile_name}': absent_checks {sorted(absent_checked)} name a "
        f"protocol present in the mix {sorted(present_protocols)}")

    # For each active protocol in this mix, drive a minimal protocol traffic smoke
    active_domains_tested = set()
    if "traffic" in checks or "absent_telemetry" in checks:
        for proto_name, instances in burst_protocols.items():
            name_lower = proto_name.lower()
            exp_domain = DOMAIN_BY_NAME.get(name_lower)
            if exp_domain is None:
                continue

            first_id = sorted(int(k) for k in instances.keys())[0]
            inst_pins = instances[str(first_id)] if str(first_id) in instances else instances[first_id]

            before_pkts = {}
            if has_core:
                # Read present protocol PKT counters before driving traffic
                before_pkts = {p: await axil_read(dut, TELEMETRY_SLOTS[p]["pkt"]) for p in present_protocols}
                # Enable datalogger instance in core (AXI-Lite reg 32)
                await axil_write(dut, 32, (first_id << 11) | 0x01)
            # Enable datalogger instance in the gather (CONFIG)
            dut.CONFIG.value = (first_id << 11) | 0x01
            await ClockCycles(dut.CLK, 10)

            initial_count = len(coll.frames)
            pins_val = await _drive_traffic(dut, name_lower, inst_pins, pins_val)
            await ClockCycles(dut.CLK, 30)

            # Disable channel
            if has_core:
                await axil_write(dut, 32, (first_id << 11) | 0x00)
            dut.CONFIG.value = (first_id << 11) | 0x00
            await ClockCycles(dut.CLK, 30)

            if "traffic" in checks:
                new_frames = coll.frames[initial_count:]
                matching = [f for f in new_frames if f[0] == first_id and f[1] == exp_domain]
                assert len(matching) > 0, (
                    f"Profile '{profile_name}': Protocol {proto_name} (id={first_id}, domain={exp_domain}) "
                    f"emitted NO matching frames on DOUT (got: {new_frames})"
                )
            active_domains_tested.add(name_lower)

            if has_core and "traffic" in checks:
                # Present protocol PKT counter incremented for traffic on that protocol only
                after_pkts = {p: await axil_read(dut, TELEMETRY_SLOTS[p]["pkt"]) for p in present_protocols}
                assert after_pkts[name_lower] > before_pkts[name_lower], (
                    f"Profile '{profile_name}': present protocol '{name_lower}' PKT counter did not increment "
                    f"(before={before_pkts[name_lower]}, after={after_pkts[name_lower]})"
                )
                for other_p in present_protocols:
                    if other_p != name_lower:
                        assert after_pkts[other_p] == before_pkts[other_p], (
                            f"Profile '{profile_name}': protocol '{other_p}' PKT counter aliased to "
                            f"'{name_lower}' traffic "
                            f"(before={before_pkts[other_p]}, after={after_pkts[other_p]})"
                        )

    if has_core and "absent_telemetry" in checks:
        # Every checked ABSENT protocol's counters read exactly 0 after traffic
        for absent_p in sorted(absent_checked):
            for cnt_name, reg_addr in TELEMETRY_SLOTS[absent_p].items():
                cnt_val = await axil_read(dut, reg_addr)
                assert cnt_val == 0, (
                    f"Profile '{profile_name}': absent protocol '{absent_p}' {cnt_name} counter "
                    f"read non-zero value {cnt_val} at address {reg_addr}"
                )

    coll.stop()
    dut._log.info(
        f"Profile '{profile_name}' {bench} smoke PASS: tested protocols {sorted(active_domains_tested)}, "
        f"absent protocols {sorted(absent_protocols)}, total frames={len(coll.frames)}"
    )


def mix_out_dir(profile_name: str) -> Path:
    return DEEPSKOPION_ROOT / "build" / "datalogger_mix" / profile_name


def mix_pkg_path(profile_name: str, profile: dict) -> Path:
    """The config package a profile elaborates: frozen fixture or live output."""
    if "pkg" in set(profile.get("frozen", [])):
        return MIX_DIR / "fixtures" / str(profile.get("fixture", "")) / "datalogger_config_pkg.vhd"
    return mix_out_dir(profile_name) / "datalogger_config_pkg.vhd"


def mix_sources(profile_name: str, profile: dict, out_dir: Path) -> tuple[Path, Path]:
    """Return (config pkg, gather) for a profile: live-generated or frozen fixture."""
    protocols_path = DEEPSKOPION_ROOT / "tools" / "datalogger-builder" / "protocols.yml"
    template_dir = DEEPSKOPION_ROOT / "tools" / "datalogger-builder" / "templates"
    protocols = dgg.load_yaml(protocols_path)
    config = profile_config(profile)

    frozen = set(profile.get("frozen", []))
    fixture_dir = MIX_DIR / "fixtures" / str(profile.get("fixture", ""))
    out_dir.mkdir(parents=True, exist_ok=True)

    pkg_path = mix_pkg_path(profile_name, profile)
    if "pkg" not in frozen:
        pkg_path.write_text(dgg.generate_datalogger_config_pkg(config, protocols), encoding="utf-8")
    if "gather" in frozen:
        gather_path = fixture_dir / "datalogger_gather.vhd"
    else:
        gather_path = out_dir / "datalogger_gather.vhd"
        gather_path.write_text(
            dgg.generate_datalogger_gather(config, protocols, template_dir), encoding="utf-8")
    for path in (pkg_path, gather_path):
        if not path.is_file():
            raise FileNotFoundError(f"Profile '{profile_name}': missing mix source {path}")
    return pkg_path, gather_path


def fixture_expectations() -> dict[Path, str]:
    """{fixture file: current generator output} for every frozen profile source.

    A fixture is the generator's output for its profile's mix, frozen so a bench
    can isolate one mechanism from the generator mechanisms upstream of it.
    `python test_datalogger_mix_sweep.py --refresh-fixtures` rewrites them.
    """
    profiles = yaml.safe_load(PROFILES_PATH.read_text(encoding="utf-8"))
    protocols = dgg.load_yaml(DEEPSKOPION_ROOT / "tools" / "datalogger-builder" / "protocols.yml")
    template_dir = DEEPSKOPION_ROOT / "tools" / "datalogger-builder" / "templates"
    expected: dict[Path, str] = {}
    for profile in profiles.values():
        fixture_dir = MIX_DIR / "fixtures" / str(profile.get("fixture", ""))
        config = profile_config(profile)
        for kind in profile.get("frozen", []):
            if kind == "pkg":
                path = fixture_dir / "datalogger_config_pkg.vhd"
                text = dgg.generate_datalogger_config_pkg(config, protocols)
            else:
                path = fixture_dir / "datalogger_gather.vhd"
                text = dgg.generate_datalogger_gather(config, protocols, template_dir)
            if expected.setdefault(path, text) != text:
                raise ValueError(f"profiles sharing fixture {path} disagree on the mix")
    return expected


def core_sources(pkg_path: Path, gather_path: Path) -> dict:
    root = str(DEEPSKOPION_ROOT)
    custom_libs = {
        "ctu_can_fd_rtl": [
            f"{root}/tools/sim/vendor_stubs/ctu_can_fd/can_top_apb_stub.vhd",
        ],
        "rr": [
            f"{root}/libs/routertl/rr_ddr_stream/rtl/ddr_stream_pkg.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/support/functions_pkg.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/support/xilinx/rr_fifo_async_mixed_xpm_body.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/support/rr_fifo_async_mixed.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/support/xilinx/rr_fifo_sync_xpm_body.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/support/rr_fifo_sync.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/support/ddr_sync_cdc.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/ddr_fifo_intf.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/axi4r.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/ptr_mgr.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/burstr_setter.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/downloader.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/support/skid_buffer.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/axi4w.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/burstw_setter.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/uploader.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/ddr_stream_axi.vhd",
            f"{root}/libs/routertl/rr_ddr_stream/rtl/ddr_stream_fic.vhd",
        ],
        "olo": [
            f"{root}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_pkg_array.vhd",
            f"{root}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_pkg_attribute.vhd",
            f"{root}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_pkg_math.vhd",
            f"{root}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_pkg_string.vhd",
            f"{root}/libs/open-logic/olo_base_wconv_xn2n/src/base/vhdl/olo_base_pkg_logic.vhd",
            f"{root}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_ram_sdp.vhd",
            f"{root}/libs/open-logic/olo_base_fifo_async/src/base/vhdl/olo_base_cc_bits.vhd",
            f"{root}/libs/open-logic/olo_base_fifo_async/src/base/vhdl/olo_base_cc_reset.vhd",
            f"{root}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_fifo_sync.vhd",
            f"{root}/libs/open-logic/olo_base_fifo_async/src/base/vhdl/olo_base_fifo_async.vhd",
            f"{root}/libs/open-logic/olo_base_wconv_n2xn/src/base/vhdl/olo_base_wconv_n2xn.vhd",
            f"{root}/libs/open-logic/olo_base_wconv_xn2n/src/base/vhdl/olo_base_wconv_xn2n.vhd",
        ],
        "rr_rea": [
            f"{root}/generated/rr_rea_build_id_pkg.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_pkg.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_dpram.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_capture_fsm.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_regbank.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_cdc.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_jtag_iface.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_fill_fsm.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_crc_sweep.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_trust_core.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_axi4lite.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_axis_window.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_top.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_trig_xbar.vhd",
            f"{root}/libs/routertl/rea/rtl/rr_rea_jtag_intel.vhd",
        ],
        "tich": [
            f"{root}/hw/rtl/pkg/rom_info_pkg.vhd",
            str(pkg_path),  # generated (or frozen) for the active mix
            f"{root}/hw/rtl/pkg/datalogger_pkg.vhd",
            f"{root}/hw/rtl/pkg/datalogger_records_pkg.vhd",
            f"{root}/hw/rtl/pkg/system_regbank_pkg.vhd",
            f"{root}/hw/rtl/pkg/zs_msg_pkg.vhd",
            f"{root}/hw/rtl/emio/ds_emio_mux.vhd",
            f"{root}/hw/rtl/stimulus/ds_stimulus_gen.vhd",
            f"{root}/hw/rtl/datalogger/infra/edge_batcher/edge_batcher.vhd",
            f"{root}/hw/rtl/datalogger/infra/edge_batcher_aggregator/edge_batcher_aggregator.vhd",
            f"{root}/hw/rtl/datalogger/infra/error_event_emitter/error_event_emitter.vhd",
            f"{root}/hw/rtl/datalogger/infra/error_event_bank/error_event_bank.vhd",
            f"{root}/hw/rtl/fifo_sync.vhd",
            f"{root}/libs/routertl/edge_detector/edge_detector.vhd",
            f"{root}/hw/rtl/datalogger/i2c/i2c_slave.vhd",
            f"{root}/hw/rtl/responder/i2c_injector.vhd",
            f"{root}/hw/rtl/datalogger/infra/maxfreq_event_emitter/maxfreq_event_emitter.vhd",
            f"{root}/hw/rtl/datalogger/infra/maxfreq_event_bank/maxfreq_event_bank.vhd",
            f"{root}/hw/rtl/pkg/arbiter_pkg.vhd",
            f"{root}/hw/rtl/pkg/system_config_pkg.vhd",
            f"{root}/hw/rtl/pkg/axi_pkg.vhd",
            f"{root}/hw/rtl/datalogger/infra/arbiter/arbiter.vhd",
            f"{root}/hw/rtl/datalogger/can/can_bit_timing.vhd",
            f"{root}/hw/rtl/datalogger/can/can_crc15.vhd",
            f"{root}/hw/rtl/datalogger/can/can_crc17.vhd",
            f"{root}/hw/rtl/datalogger/can/can_crc21.vhd",
            f"{root}/hw/rtl/datalogger/can/can_destuff.vhd",
            f"{root}/hw/rtl/datalogger/can/can_frame_fsm.vhd",
            f"{root}/hw/rtl/datalogger/infra/config_control/config_control.vhd",
            f"{root}/hw/rtl/datalogger/can/can_datalogger.vhd",
            f"{root}/hw/rtl/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
            f"{root}/hw/rtl/datalogger/nonstd/edge_datalogger/edge_datalogger.vhd",
            f"{root}/hw/rtl/emio/glitch_filter.vhd",
            f"{root}/hw/rtl/datalogger/i2c/i2c_datalogger.vhd",
            f"{root}/hw/rtl/datalogger/jtag/jtag_datalogger.vhd",
            f"{root}/libs/routertl/infilter/infilter.vhd",
            f"{root}/hw/rtl/datalogger/qspi/qspi_datalogger.vhd",
            f"{root}/hw/rtl/pkg/uart_pkg.vhd",
            f"{root}/libs/routertl/ram_dual_port/ram_dual_port.vhd",
            f"{root}/hw/rtl/datalogger/uart/uart_baudrate_gen.vhd",
            f"{root}/hw/rtl/datalogger/uart/uart_frame_gen.vhd",
            f"{root}/hw/rtl/datalogger/uart/uart_playback.vhd",
            f"{root}/hw/rtl/datalogger/uart/uart_stop_finder.vhd",
            f"{root}/hw/rtl/datalogger/uart/uart_datalogger.vhd",
            str(gather_path),  # generated (or frozen) for the active mix
            f"{root}/hw/rtl/responder/spi_slave.vhd",
            f"{root}/hw/rtl/responder/spi_injector.vhd",
            f"{root}/hw/rtl/regbank/regbank_rom.vhd",
            f"{root}/hw/rtl/regbank/system_regbank.vhd",
            f"{root}/hw/rtl/regbank/system_regbank_wrapper.vhd",
            f"{root}/hw/rtl/datalogger/infra/zs_msg_framer/zs_msg_framer.vhd",
            f"{root}/hw/rtl/ring_watermark.vhd",
            f"{root}/hw/rtl/deepskopion_core.vhd",
        ],
    }
    return custom_libs


def _clean_nvc_build() -> None:
    """Start every bench from an empty nvc build dir (what `rr sim run --clean`
    does; `rr sim sweep` has no --clean).

    The dir is shared with the project's own sims, which compile the same
    library names (olo, rr) from other source sets. An incremental compile on
    top of that elaborates against mismatched unit checksums ("design unit
    depends on OLO... with checksum X but the current version has Y"), so a
    mix bench's verdict would depend on whatever ran before it.
    """
    build_dir = get_build_dir_for_simulator("nvc")
    print(f"  [mix bench] clean nvc build dir {build_dir}")
    shutil.rmtree(build_dir, ignore_errors=True)


def main():
    profile_name = os.environ.get("RR_SIM_PROFILE", "default")
    profile = load_profile(profile_name)
    bench = profile.get("bench", "core")

    out_dir = mix_out_dir(profile_name)
    pkg_path, gather_path = mix_sources(profile_name, profile, out_dir)

    _clean_nvc_build()
    _seed_regbank_rom_symlinks(DEEPSKOPION_ROOT)

    if bench == "pkg":
        top_level = "tb_datalogger_pkg_probe"
        custom_libs = {"tich": [str(pkg_path), str(MIX_DIR / f"{top_level}.vhd")]}
    elif bench == "gather":
        # No deepskopion_core: the gather's port contract is checked on its own.
        top_level = "tb_datalogger_gather_probe"
        custom_libs = core_sources(pkg_path, gather_path)
        core_vhd = str(DEEPSKOPION_ROOT / "hw" / "rtl" / "deepskopion_core.vhd")
        custom_libs["tich"] = [f for f in custom_libs["tich"] if f != core_vhd]
        custom_libs["tich"].append(str(MIX_DIR / f"{top_level}.vhd"))
    elif bench == "pinmap":
        # deepskopion_core alone: no bench-side gather, no bench-side pin map.
        top_level = "tb_core_pin_map"
        custom_libs = core_sources(pkg_path, gather_path)
        custom_libs["tich"].append(str(MIX_DIR / f"{top_level}.vhd"))
    elif bench == "core":
        top_level = "tb_datalogger_mix_sweep"
        custom_libs = core_sources(pkg_path, gather_path)
        custom_libs["tich"].append(str(MIX_DIR / f"{top_level}.vhd"))
    else:
        raise ValueError(f"Profile '{profile_name}': unknown bench {bench!r}")

    run_simulation(
        top_level=top_level,
        module=Path(__file__).stem,
        top_level_lang="vhdl",
        waves=False,
        verbose=False,
        work_lib="tich",
        custom_libraries=custom_libs,
        # DS-P1.75: custom_libs already names every source, the locked packages
        # included. Injecting the project packages on top runs rr's package
        # scoping walk over paths.sources, which sees hw/rtl/pkg's default
        # datalogger_config_pkg beside this mix's generated one and refuses the
        # pair (RTL-P2.1785) before authoritative= could name a winner. The
        # final compile-set gate still refuses any differing copy listed here.
        include_project_packages=False,
    )


if __name__ == "__main__":
    if "--refresh-fixtures" in sys.argv:
        for path, text in fixture_expectations().items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(DEEPSKOPION_ROOT)}")
    else:
        main()
