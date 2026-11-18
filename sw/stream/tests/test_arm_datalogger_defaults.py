"""DS-P2.77 — boot-time datalogger arming.

Regression guard for the framing default. Nothing configures the UART datalogger at
power-on — config_control latches all-zeros out of reset, and the unarmed board
was MEASURED reading DATALOGGER_CONFIG = 0x00000000 (DS-P2.77, 2026-08-10, canfd_demo
Z7-20). So device 12 is OFF until software writes it, against lines that are 8N1.

0x6001 appears below only as a mis-framed STARTING STATE for the fixtures. It is
not a power-on default: it is what ``enable_datalogger(12)`` writes with extra=0, and
this file previously called it "the power-on default" on inherited authority. The
tests do not depend on which wrong value they start from — they assert where the
register ENDS UP.

What these tests lock down is that the correct framing is armed at boot and no
longer depends on a demo stimulus script that is not on every rootfs.

These tests assert the EFFECT (which words reach the register, in which order),
not merely that the encoder returns the right integer. A test that only checked
encode_datalogger_config() would have passed throughout the entire outage, because
the encoder was always correct — nothing ever CALLED it at boot.
"""

import builtins
import sys
from pathlib import Path

import pytest

# tests/ -> stream/ -> sw/ -> repo root
_REPO_ROOT_FOR_INIT = Path(__file__).resolve().parents[3]

from sw.stream.arm_datalogger_defaults import (
    DEFAULT_ARMING,
    DATALOGGER_CONFIG_OFFSET,
    UART_EXTRA_8N1,
    UART_ID,
    CONFIG_SCHEMA_MAGIC_V01,
    CONFIG_SCHEMA_MAGIC_V02,
    ConfigSchemaError,
    arm,
    build_default_arming,
    describe_config,
    discover_uart_ids,
    encode_datalogger_config,
    extract_uart_ids_from_helo,
    extract_uart_ids_from_registry,
    parse_config_schema,
    read_uart_ids_from_config_schema,
    read_uart_ids_from_regbank,
)


class RecordingRegbank:
    """Records every write so the ORDER of operations can be asserted."""

    def __init__(self, initial=0x6001):
        self.value = initial
        self.writes = []

    def read_u32(self, offset):
        return self.value

    def write_u32(self, offset, value):
        self.writes.append((offset, value))
        self.value = value


class RomRecordingRegbank(RecordingRegbank):
    """RecordingRegbank with BRAM streaming ROM emulation (CONFIG_NUM_WORDS / CONFIG_DATA)."""

    def __init__(self, rom_words, initial=0x0000):
        super().__init__(initial=initial)
        self.rom_words = list(rom_words)
        self.rom_ptr = 0

    def read_u32(self, offset):
        if offset == 0x0018:  # CONFIG_NUM_WORDS: resets streaming pointer
            self.rom_ptr = 0
            return len(self.rom_words)
        if offset == 0x001C:  # CONFIG_DATA: returns word and increments pointer
            if self.rom_ptr < len(self.rom_words):
                w = self.rom_words[self.rom_ptr]
                self.rom_ptr += 1
                return w
            return 0
        return super().read_u32(offset)


# config_schema.hex exactly as origin/main b70330a / d82ca6e shipped it (and as every
# bitstream built before DS-P2.162 carries it): magic 0xC0A1DE01, proto_id = index
# into the ALPHABETICAL protocol list (edge=0, i2c=1, jtag=2, spi=3, uart=4).
# I2C dataloggers are 4 and 5; the real UARTs are 12 and 13.
V01_DEPLOYED_ROM = [
    0xC0A1DE01, 0x0000000A,
    0x00010100, 0x00020100, 0x00030100, 0x00060100,   # edge 1,2,3,6
    0x01040200, 0x01050200,                            # i2c 4,5
    0x02100400,                                        # jtag 16
    0x03080400,                                        # spi 8
    0x040C0100, 0x040D0100,                            # uart 12,13
]

# The single-UART image the pre-DS-P2.162 tests were written for (UART 12 only).
V01_UART12_ROM = [0xC0A1DE01, 0x00000001, 0x040C0100]


class NoRomRegbank(RecordingRegbank):
    """A regbank whose config ROM aperture cannot be read at all."""

    def read_u32(self, offset):
        if offset in (0x0018, 0x001C):
            raise OSError("config ROM aperture not mapped")
        return super().read_u32(offset)

def test_arms_uart_to_8n1_not_the_mis_framed_5n1_word():
    rb = RomRecordingRegbank(V01_UART12_ROM, initial=0x6001)
    arm(rb, sleep=lambda _s: None)

    final = rb.writes[-1][1]
    assert (final >> 11) & 0x1F == UART_ID
    assert final & 1 == 1, "datalogger must end up ENABLED"
    assert (final >> 2) & 7 == 3, "dataBits must encode 8 bits (nbits-5 == 3)"
    assert (final >> 5) & 1 == 0, "no parity"
    # The precise word the silicon needs; 0x6001 is the mis-framed 5N1 word.
    assert final == 0x600D
    assert final != 0x6001


def test_disable_precedes_enable_so_calibration_actually_resets():
    """A config change alone does not reset the datalogger's calibration FSM
    (configEnable stays high), so writing the new framing without first
    disabling leaves the stale calibration in force and the fix silently
    does nothing. Order is load-bearing, so it is asserted."""
    rb = RomRecordingRegbank(V01_UART12_ROM)
    arm(rb, sleep=lambda _s: None)

    assert len(rb.writes) >= 2, "expected a disable write then an enable write"
    disable_word, enable_word = rb.writes[-2][1], rb.writes[-1][1]
    assert disable_word & 1 == 0, "first write must DISABLE"
    assert enable_word & 1 == 1, "second write must ENABLE"
    assert all(off == DATALOGGER_CONFIG_OFFSET for off, _ in rb.writes)


def test_every_default_arming_entry_is_actually_written():
    """Guards the class of bug where an entry is added to the table but the
    loop is not reached — the shape of the original defect."""
    rb = RomRecordingRegbank(V01_UART12_ROM)
    arm(rb, sleep=lambda _s: None)

    written_ids = {(w >> 11) & 0x1F for _off, w in rb.writes}
    for device_id, _extra, _why in DEFAULT_ARMING:
        assert device_id in written_ids


def test_a_failing_device_does_not_abort_the_boot():
    """A board that cannot arm one datalogger must still boot and stream."""

    class ExplodingRegbank(RomRecordingRegbank):
        def write_u32(self, offset, value):
            raise OSError("mmap gone")

    lines = arm(ExplodingRegbank(V01_UART12_ROM), sleep=lambda _s: None)
    assert any("FAILED" in line for line in lines), "failure must be LOUD, not swallowed"


def test_arming_is_reported_for_the_boot_log():
    rb = RomRecordingRegbank(V01_UART12_ROM)
    lines = arm(rb, sleep=lambda _s: None)
    assert lines and str(UART_ID) in lines[0]
    # The log must name the before AND after state — a bare "armed" line is
    # what makes a stale config invisible in the first place.
    assert "->" in lines[0]


@pytest.mark.parametrize(
    "word,expect",
    [
        (0x6001, "dataBits=5"),   # what enable_datalogger(12) writes with extra=0
        (0x600D, "dataBits=8"),   # what we arm
        (0x6003, "autoSync=1"),
    ],
)
def test_describe_config_decodes_the_words_an_operator_will_meet(word, expect):
    assert expect in describe_config(word)


def test_encode_rejects_an_out_of_range_device_id():
    with pytest.raises(ValueError):
        encode_datalogger_config(32, True, UART_EXTRA_8N1)


def test_de25_arm_writes_uart_defaults_without_importing_testbench(monkeypatch):
    """DS-P2.126: DE25 rootfs excludes sw/stream/testbench.

    With DS_MSS_FABRIC unset, arm() must finish the UART disable→enable
    writes. Importing the contract first raises ModuleNotFoundError on that
    image and the datalogger stays at the power-on disabled word.
    """
    monkeypatch.delenv("DS_MSS_FABRIC", raising=False)
    real_import = builtins.__import__

    def _block_testbench(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sw.stream.testbench" or (
            isinstance(name, str) and name.startswith("sw.stream.testbench.")
        ):
            raise ModuleNotFoundError("No module named 'sw.stream.testbench'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _block_testbench)
    for key in list(sys.modules):
        if key == "sw.stream.testbench" or key.startswith("sw.stream.testbench."):
            monkeypatch.delitem(sys.modules, key, raising=False)

    rb = RomRecordingRegbank(V01_UART12_ROM, initial=0x0000)
    lines = arm(rb, sleep=lambda _s: None)

    assert [off for off, _ in rb.writes] == [
        DATALOGGER_CONFIG_OFFSET, DATALOGGER_CONFIG_OFFSET,
    ], (
        "DE25 arm with DS_MSS_FABRIC unset must write only the UART "
        f"datalogger defaults, wrote {rb.writes!r}"
    )
    disable_word, enable_word = rb.writes[0][1], rb.writes[1][1]
    assert disable_word & 1 == 0, "first default write must DISABLE"
    assert enable_word == 0x600D, (
        f"UART 8N1 enable word missing; wrote 0x{enable_word:04X}"
    )
    assert any("datalogger 12" in line for line in lines)


# ---------------------------------------------------------------------------
# The guard that was ACTUALLY missing (DS-P2.77, corrected 2026-08-10).
#
# arm() is invoked by the boot init script, NOT by the daemon — and the ORDER
# is load-bearing: the script's own comment says "arm datalogger CONFIG defaults
# BEFORE the daemon opens the ring". Nothing tested either fact, so a reader
# grepping only *.py/*.sh finds no caller at all (the init script has no
# extension) and can conclude the arming is dead code. That misreading cost a
# redundant second arming path, added after the ring was already open — the
# exact ordering the script warns against.
#
# These assert the real contract at its real call site.
# ---------------------------------------------------------------------------

_INIT_SCRIPT = (
    _REPO_ROOT_FOR_INIT / "sw" / "linux" / "rootfs_overlay"
    / "etc" / "init.d" / "S99deepskopion-stream"
)


def test_init_script_arms_the_datalogger_at_boot():
    """The ONLY thing that arms the framing on a shipped image."""
    assert _INIT_SCRIPT.is_file(), f"boot init script missing at {_INIT_SCRIPT}"
    body = _INIT_SCRIPT.read_text(encoding="utf-8", errors="replace")
    assert "sw.stream.arm_datalogger_defaults" in body, (
        "the boot init script must invoke sw.stream.arm_datalogger_defaults — "
        "without it nothing enables the UART datalogger on a booted board and "
        "DATALOGGER_CONFIG stays at its 0x0000 power-on value"
    )


def test_arming_precedes_the_daemon_launch():
    """Order is the whole point: arming after the daemon has opened the ring
    re-runs the disable->enable calibration reset underneath a live drain."""
    body = _INIT_SCRIPT.read_text(encoding="utf-8", errors="replace")
    arm_at = body.index("sw.stream.arm_datalogger_defaults")
    # the daemon launch is the OTHER `-m sw.stream` invocation
    launch_at = body.index("python3 -m sw.stream \\")
    assert arm_at < launch_at, (
        "the arming must run BEFORE the daemon is launched (it opens the ring); "
        "see the DS-P2.77 comment in the init script"
    )


def test_de25_daemon_launch_advertises_the_100mhz_fabric_clock():
    """DS-P2.155: both DE25 targets now advertise 100 MHz — clk_sys is the
    CLOCK2_50 x2 fabric-IOPLL product, so ds_clk_hz must match the RTL
    G_SYS_CLK_HZ (100 MHz) and the DTB deepskopion-clk node. A stale 50 MHz
    token made the daemon reconstruct every UART/EDGE timestamp at 20 ns/tick,
    2x wrong against the silicon."""
    body = _INIT_SCRIPT.read_text(encoding="utf-8", errors="replace")
    launch_at = body.index("python3 -m sw.stream \\")
    launch = body[launch_at:launch_at + 300]
    # DS-P1.103: the DE25 product always; the bench target (bench/targets/) only where bench/ exists.
    sys.path.insert(0, str(_REPO_ROOT_FOR_INIT / "tools"))
    from target_files import target_file, with_bench
    for tgt in sorted(target_file(n, _REPO_ROOT_FOR_INIT).relative_to(_REPO_ROOT_FOR_INIT).as_posix()
                      for n in with_bench({"de25_datalogger"}, {"de25_canfd"}, _REPO_ROOT_FOR_INIT)):
        de25_target = (
            _REPO_ROOT_FOR_INIT / tgt
        ).read_text(encoding="utf-8", errors="replace")
        assert "ds_clk_hz=100000000" in de25_target, (
            f"{tgt}: bootargs must advertise the 100 MHz fabric-IOPLL clk_sys"
        )
        assert "ds_clk_hz=50000000" not in de25_target, (
            f"{tgt}: the stale 50 MHz fabric clock must be gone (DS-P2.155)"
        )
    assert "ds_clk_hz=" in body and "--regbank-clk-hz ${REGBANK_CLK_HZ}" in body
    assert "${CLK_ARGS}" in launch
    # The clock must still ride the per-target kernel cmdline, NOT be hard-coded
    # into the rootfs overlay shared with the Zynq targets — neither the old
    # 50 MHz nor the new 100 MHz value may be baked in literally.
    assert "--regbank-clk-hz 50000000" not in body
    assert "--regbank-clk-hz 100000000" not in body, (
        "the fabric clock must not be baked into the shared rootfs overlay — it "
        "rides ds_clk_hz on the per-target kernel cmdline"
    )


def test_arming_failure_is_reported_not_swallowed():
    """A board that cannot arm must still boot — but must SAY so."""
    body = _INIT_SCRIPT.read_text(encoding="utf-8", errors="replace")
    arm_line_idx = body.index("sw.stream.arm_datalogger_defaults")
    tail = body[arm_line_idx:arm_line_idx + 300]
    assert "||" in tail and "failed" in tail, (
        "the init script must report an arming failure rather than pass in "
        "silence (no-silent-failures)"
    )


# ---------------------------------------------------------------------------
# Dynamic Datalogger ID Discovery (DS-P2.162)
#
# arm_datalogger_defaults.py previously hard-coded UART_ID = 12. But the CLI
# configurator assigns IDs sequentially from 1 (the README mix gives UART0 = 8),
# so any non-default mix armed the wrong datalogger or failed to arm dataloggers.
# Software must discover UART IDs dynamically from config_schema.hex /
# datalogger_registry / HELO declaration, arming every datalogger of the mix
# with NO hard-coded ID.
# ---------------------------------------------------------------------------

def _build_mix(counts: dict, board_name: str = "zybo-z7-20"):
    """Helper to generate a specific mix via configurator."""
    tools_builder = _REPO_ROOT_FOR_INIT / "tools" / "datalogger-builder"
    sys.path.insert(0, str(tools_builder))
    try:
        import configurator as cfg_tool
        import datalogger_gather_gen as dgg
        import yaml
    finally:
        sys.path.pop(0)

    board = cfg_tool.get_board(board_name)
    cfg_yaml = cfg_tool.generate_config_yaml(board, counts)
    cfg = yaml.safe_load(cfg_yaml)

    parsed = dgg.parse_datalogger_config(cfg)
    burst = parsed.get("burst_protocols", {})
    if "UART" in burst:
        expected_uart_ids = sorted(int(k) for k in burst["UART"].keys())
    else:
        expected_uart_ids = []
    active_protos = sorted(burst.keys())

    hex_text = dgg.generate_config_schema_hex(cfg, {})
    words = [int(line.strip(), 16) for line in hex_text.strip().splitlines() if line.strip()]

    return cfg, expected_uart_ids, active_protos, hex_text, words


def _build_readme_mix():
    """Helper to generate the README mix (--uart 2 --i2c 2 --spi 1 --edge 4) via configurator."""
    return _build_mix({"uart": 2, "i2c": 2, "spi": 1, "edge": 4})


def test_arms_every_datalogger_of_non_default_mix():
    """DS-P2.162: non-default mix arms every UART datalogger correctly with NO hard-coded ID.

    Configures the README mix (--uart 2 --i2c 2 --spi 1 --edge 4) through the configurator,
    loads the manifest into regbank streaming ROM, and proves arm() discovers and arms
    every datalogger in that mix. Production path arm(rb) uses NO active_protocols.
    """
    _cfg, expected_uart_ids, _active_protos, _hex_text, words = _build_readme_mix()
    assert len(expected_uart_ids) == 2, "README mix must configure 2 UART instances"
    assert 12 not in expected_uart_ids, "non-default mix must not have default UART_ID=12"

    rb = RomRecordingRegbank(rom_words=words, initial=0x0000)
    lines = arm(rb, sleep=lambda _s: None)

    written_ids = {(w >> 11) & 0x1F for off, w in rb.writes if off == DATALOGGER_CONFIG_OFFSET}
    assert set(expected_uart_ids).issubset(written_ids), (
        "non-default mix dataloggers must be armed, got only default device"
    )
    for dev_id in expected_uart_ids:
        enable_word = encode_datalogger_config(dev_id, True, UART_EXTRA_8N1)
        assert (DATALOGGER_CONFIG_OFFSET, enable_word) in rb.writes, (
            f"datalogger {dev_id} was not armed to 8N1"
        )
        assert any(f"datalogger {dev_id}" in line for line in lines), (
            f"datalogger {dev_id} arming was not reported in log"
        )


def test_arms_no_dataloggers_when_mix_has_no_uart():
    """DS-P2.162: a mix with NO UART ({"uart": 0, "i2c": 2, "spi": 1, "edge": 4} on zybo-z7-20)
    arms no UART extra on any datalogger and reports 0 dataloggers armed.
    """
    _cfg, expected_uart_ids, _active_protos, _hex_text, words = _build_mix(
        {"uart": 0, "i2c": 2, "spi": 1, "edge": 4}
    )
    assert expected_uart_ids == []

    rb = RomRecordingRegbank(rom_words=words, initial=0x0000)
    lines = arm(rb, sleep=lambda _s: None)
    writes = [w for off, w in rb.writes if off == DATALOGGER_CONFIG_OFFSET]
    assert writes == [], f"Expected 0 DATALOGGER_CONFIG writes for no-UART mix, got {writes}"
    assert any("0 dataloggers armed" in line for line in lines)
    assert read_uart_ids_from_regbank(rb) == []


def test_arms_only_uart_dataloggers_when_uart_not_highest_proto_id():
    """DS-P2.162: a mix where UART is not the highest protocol id (e.g. includes JTAG)
    arms exactly the UART dataloggers with no guessing.
    """
    _cfg, expected_uart_ids, _active_protos, _hex_text, words = _build_mix(
        {"uart": 2, "i2c": 2, "spi": 1, "edge": 4, "jtag": 1}
    )
    assert expected_uart_ids == [9, 10]

    rb = RomRecordingRegbank(rom_words=words, initial=0x0000)
    assert read_uart_ids_from_regbank(rb) == expected_uart_ids

    lines = arm(rb, sleep=lambda _s: None)
    written_ids = sorted({(w >> 11) & 0x1F for off, w in rb.writes if off == DATALOGGER_CONFIG_OFFSET})
    assert written_ids == expected_uart_ids
    for dev_id in expected_uart_ids:
        enable_word = encode_datalogger_config(dev_id, True, UART_EXTRA_8N1)
        assert (DATALOGGER_CONFIG_OFFSET, enable_word) in rb.writes
        assert any(f"datalogger {dev_id}" in line for line in lines)


def test_arms_every_datalogger_from_config_schema_hex():
    """DS-P2.162: arm() accepts config_hex directly and arms every datalogger with no active_protocols."""
    _cfg, expected_uart_ids, _active_protos, hex_text, _words = _build_readme_mix()

    rb = RecordingRegbank(initial=0x0000)
    lines = arm(rb, config_hex=hex_text, sleep=lambda _s: None)

    written_ids = {(w >> 11) & 0x1F for off, w in rb.writes if off == DATALOGGER_CONFIG_OFFSET}
    assert set(expected_uart_ids).issubset(written_ids)
    for dev_id in expected_uart_ids:
        enable_word = encode_datalogger_config(dev_id, True, UART_EXTRA_8N1)
        assert (DATALOGGER_CONFIG_OFFSET, enable_word) in rb.writes
        assert any(f"datalogger {dev_id}" in line for line in lines)


def test_arms_every_datalogger_from_datalogger_registry():
    """DS-P2.162: arm() discovers UART IDs from a datalogger_registry specification."""
    _cfg, expected_uart_ids, _active_protos, _hex_text, _words = _build_readme_mix()

    class MockRegistry:
        DATALOGGER_IDS = {"uart": expected_uart_ids, "edge": [1, 2, 3, 4]}

    rb = NoRomRegbank(initial=0x0000)
    lines = arm(rb, registry=MockRegistry, sleep=lambda _s: None)

    written_ids = {(w >> 11) & 0x1F for off, w in rb.writes if off == DATALOGGER_CONFIG_OFFSET}
    assert set(expected_uart_ids).issubset(written_ids)
    for dev_id in expected_uart_ids:
        enable_word = encode_datalogger_config(dev_id, True, UART_EXTRA_8N1)
        assert (DATALOGGER_CONFIG_OFFSET, enable_word) in rb.writes
        assert any(f"datalogger {dev_id}" in line for line in lines)


def test_explicit_registry_mismatch_raises_value_error():
    """DS-P2.162: explicit registry mismatch against live schema ROM raises ValueError."""
    _cfg, expected_uart_ids, _active_protos, _hex_text, words = _build_readme_mix()
    rb = RomRecordingRegbank(rom_words=words, initial=0x0000)

    class MismatchedRegistry:
        DATALOGGER_IDS = {"uart": [12, 13]}

    raised = None
    try:
        discover_uart_ids(regbank=rb, registry=MismatchedRegistry)
    except ValueError as exc:
        raised = exc
    assert raised is not None, "discover_uart_ids must raise ValueError on registry mismatch"
    assert "[12, 13]" in str(raised)

    raised = None
    try:
        arm(rb, registry=MismatchedRegistry, sleep=lambda _s: None)
    except ValueError as exc:
        raised = exc
    assert raised is not None, "registry mismatch must raise ValueError"
    assert "mismatch live configuration schema" in str(raised)
    assert "[12, 13]" in str(raised), "error must name the stale registry IDs"
    assert str(expected_uart_ids) in str(raised), "error must name the live schema IDs"


def test_arms_every_datalogger_from_helo_declaration():
    """DS-P2.162: arm() discovers UART IDs from a HELO channel declaration."""
    _cfg, expected_uart_ids, _active_protos, _hex_text, _words = _build_readme_mix()

    helo_declaration = {
        "version": 1,
        "sample_clk_hz": 100_000_000,
        "channels": [
            {"id": dev_id, "name": f"uart_tx_{dev_id}", "width": 1, "protocol": "uart"}
            for dev_id in expected_uart_ids
        ],
    }

    rb = NoRomRegbank(initial=0x0000)
    lines = arm(rb, helo=helo_declaration, sleep=lambda _s: None)

    written_ids = {(w >> 11) & 0x1F for off, w in rb.writes if off == DATALOGGER_CONFIG_OFFSET}
    assert set(expected_uart_ids).issubset(written_ids)
    for dev_id in expected_uart_ids:
        enable_word = encode_datalogger_config(dev_id, True, UART_EXTRA_8N1)
        assert (DATALOGGER_CONFIG_OFFSET, enable_word) in rb.writes
        assert any(f"datalogger {dev_id}" in line for line in lines)


def test_discover_uart_ids_helpers():
    """DS-P2.162: unit tests for discovery parsing helpers."""
    _cfg, expected_uart_ids, active_protos, hex_text, words = _build_readme_mix()

    # From words / hex without active_protocols
    ids_from_words = read_uart_ids_from_config_schema(words)
    assert ids_from_words == expected_uart_ids

    ids_from_hex = read_uart_ids_from_config_schema(hex_text)
    assert ids_from_hex == expected_uart_ids

    # With optional active_protocols passed
    assert read_uart_ids_from_config_schema(words, active_protocols=active_protos) == expected_uart_ids

    # From HELO channels
    channels = [{"id": 42, "name": "uart_byte", "datalogger_id": 15}]
    assert extract_uart_ids_from_helo(channels) == [15]

    # From registry dict
    reg_dict = {"DATALOGGER_IDS": {"uart": [3, 7]}}
    assert extract_uart_ids_from_registry(reg_dict) == [3, 7]

    # discover_uart_ids with matching registry succeeds
    discovered = discover_uart_ids(
        config_hex=words,
        registry={"UART_IDS": expected_uart_ids},
    )
    assert discovered == expected_uart_ids

    # discover_uart_ids with mismatching registry raises ValueError
    with pytest.raises(ValueError, match="mismatch live configuration schema"):
        discover_uart_ids(
            config_hex=words,
            registry={"UART_IDS": [99]},
        )

    # build_default_arming produces disabled->enabled pairs
    arming_list = build_default_arming(expected_uart_ids)
    assert len(arming_list) == len(expected_uart_ids)
    assert [d for d, _extra, _why in arming_list] == expected_uart_ids


# ---------------------------------------------------------------------------
# ROM format versioning (DS-P2.162 revision).
#
# The first DS-P2.162 delivery re-encoded proto_id (alphabetical index ->
# system_ids.yml code) but kept magic 0xC0A1DE01, so software could not tell an
# old ROM from a new one. Installed on a board whose bitstream predates the
# change, it armed the I2C dataloggers (4, 5) as UART and never armed UART 12.
# The new encoding therefore carries a new magic (0xC0A1DE02); v01 is still
# decoded with its own table; any other magic is refused.
# ---------------------------------------------------------------------------

def _armed_ids(rb):
    return sorted({(w >> 11) & 0x1F for off, w in rb.writes
                   if off == DATALOGGER_CONFIG_OFFSET})


def test_v01_rom_from_a_deployed_bitstream_arms_uart_12_not_the_i2c_loggers():
    """A bitstream built before DS-P2.162 (v01 ROM, alphabetical proto_id) must
    still arm its real UARTs 12 and 13, and must NOT write UART framing into the
    I2C dataloggers 4 and 5 (old proto_id 1, which the v02 table calls 'uart')."""
    rb = RomRecordingRegbank(V01_DEPLOYED_ROM, initial=0x0000)
    lines = arm(rb, sleep=lambda _s: None)

    assert _armed_ids(rb) == [12, 13], (
        f"v01 ROM must arm exactly its UARTs 12,13; armed {_armed_ids(rb)}"
    )
    for dev_id in (4, 5):
        assert (DATALOGGER_CONFIG_OFFSET, encode_datalogger_config(dev_id, True, UART_EXTRA_8N1)) \
            not in rb.writes, f"I2C datalogger {dev_id} was armed with UART 8N1"
    for dev_id in (12, 13):
        assert (DATALOGGER_CONFIG_OFFSET, encode_datalogger_config(dev_id, True, UART_EXTRA_8N1)) \
            in rb.writes
        assert any(f"datalogger {dev_id}" in line for line in lines)


def test_v01_rom_is_decoded_with_the_alphabetical_table_by_the_helpers_too():
    assert read_uart_ids_from_config_schema(V01_DEPLOYED_ROM) == [12, 13]
    parsed = parse_config_schema(V01_DEPLOYED_ROM)
    assert parsed["i2c"] == [4, 5]
    assert parsed["edge"] == [1, 2, 3, 6]
    assert parsed["jtag"] == [16]
    assert parsed["spi"] == [8]


def test_v02_rom_arms_exactly_its_uarts_through_arm():
    """The checked-in ROM (what bitstreams bake in) carries magic 0xC0A1DE02 and
    system_ids.yml codes; through arm() it arms its UARTs 12,13 and nothing else
    (not the I2C 4,5, JTAG 16, SPI 8 or edge loggers)."""
    hex_path = _REPO_ROOT_FOR_INIT / "hw" / "rtl" / "regbank" / "config_schema.hex"
    words = [int(l, 16) for l in hex_path.read_text().split()]
    assert words[0] == CONFIG_SCHEMA_MAGIC_V02, "checked-in config_schema.hex must be a v02 ROM"

    rb = RomRecordingRegbank(words, initial=0x0000)
    arm(rb, sleep=lambda _s: None)

    assert _armed_ids(rb) == [12, 13], (
        f"v02 ROM must arm exactly its UARTs 12,13; armed {_armed_ids(rb)}"
    )


def test_v02_generated_mix_arms_only_its_uarts():
    """A freshly generated non-default mix (with JTAG) is v02 and arms exactly its UARTs."""
    _cfg, expected_uart_ids, _protos, _hex, words = _build_mix(
        {"uart": 2, "i2c": 2, "spi": 1, "edge": 4, "jtag": 1}
    )
    assert words[0] == CONFIG_SCHEMA_MAGIC_V02 == 0xC0A1DE02
    assert expected_uart_ids and 12 not in expected_uart_ids
    rb = RomRecordingRegbank(words, initial=0x0000)
    arm(rb, sleep=lambda _s: None)
    assert _armed_ids(rb) == expected_uart_ids


def test_generator_stamps_v02_magic():
    _cfg, _ids, _protos, hex_text, _words = _build_readme_mix()
    assert hex_text.splitlines()[0] == f"{CONFIG_SCHEMA_MAGIC_V02:08x}"
    assert CONFIG_SCHEMA_MAGIC_V01 == 0xC0A1DE01


@pytest.mark.parametrize("magic", [0xC0A1DE00, 0xC0A1DE03, 0xDEADBEEF, 0xFFFFFFFF])
def test_unknown_rom_magic_is_refused_loudly_through_arm(magic):
    """No silent fall-back to a default UART id on a ROM nobody can decode."""
    rom = [magic, 1, 0x010C0100]
    rb = RomRecordingRegbank(rom, initial=0x0000)
    raised = None
    try:
        arm(rb, sleep=lambda _s: None)
    except ConfigSchemaError as exc:
        raised = exc
    assert raised is not None, "unknown ROM magic must be refused, not defaulted"
    assert f"0x{magic:08X}" in str(raised)
    assert rb.writes == [], "nothing may be armed from a ROM that cannot be decoded"
    assert issubclass(ConfigSchemaError, ValueError)
    with pytest.raises(ConfigSchemaError):
        parse_config_schema(rom)


def test_rom_magic_zero_means_no_rom_and_falls_back_to_default_arming():
    """DS-P2.173 / DS-D.47: a config_schema ROM that reads 0x00000000 (unpopulated or
    uninitialised BRAM, or an image built without the ROM) indicates no ROM is present,
    falling back to DEFAULT_ARMING rather than crashing the boot arm with ConfigSchemaError.
    Non-zero unknown magics remain strict refusals."""
    rom = [0x00000000, 1, 0x010C0100]
    rb = RomRecordingRegbank(rom, initial=0x0000)

    # read_uart_ids_from_regbank returns None for magic 0 (no ROM present)
    try:
        discovered = read_uart_ids_from_regbank(rb)
        err = None
    except Exception as exc:
        discovered = None
        err = str(exc)

    assert err is None, f"magic 0 must fall back to DEFAULT_ARMING without raising: {err}"
    assert discovered is None

    # arm() falls back to DEFAULT_ARMING (UART 12, 8N1)
    lines = arm(rb, sleep=lambda _s: None)
    assert _armed_ids(rb) == [UART_ID], (
        f"magic 0 must fall back to DEFAULT_ARMING [{UART_ID}]; armed {_armed_ids(rb)}"
    )
    enable_word = encode_datalogger_config(UART_ID, True, UART_EXTRA_8N1)
    assert (DATALOGGER_CONFIG_OFFSET, enable_word) in rb.writes
    assert any(f"datalogger {UART_ID}" in line for line in lines)

    # Also covers an all-zero regbank aperture (e.g. uninitialised aperture / stub regbank)
    zero_rb = RecordingRegbank(initial=0x0000)
    assert read_uart_ids_from_regbank(zero_rb) is None
    lines_zero = arm(zero_rb, sleep=lambda _s: None)
    assert _armed_ids(zero_rb) == [UART_ID]
    assert any(f"datalogger {UART_ID}" in line for line in lines_zero)
