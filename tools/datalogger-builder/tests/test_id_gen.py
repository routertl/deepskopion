"""Regression guard for id_gen.py — the system_ids.yml drift-check (DS-P3.57).

Proves the canonical id-registry stays the single source of truth: the live
RTL/SW copies must match it (catches the 4-hand-synced-copies class of bug),
and the drift comparison must FAIL when a code disagrees or an unknown domain
appears.
"""
import sys
from pathlib import Path

_SB = Path(__file__).resolve().parents[1]          # tools/datalogger-builder
sys.path.insert(0, str(_SB))
import id_gen  # noqa: E402

_ROOT = id_gen._repo_root(_SB)


def test_registry_loads_and_has_core_domains():
    reg = id_gen.load_registry(_ROOT)
    # globally-stable codes must never move
    assert reg["sys"] == 0x0 and reg["uart"] == 0x1 and reg["i2c"] == 0x2
    assert reg["spi"] == 0x3 and reg["edge"] == 0x6 and reg["trig"] == 0x7
    assert reg["jtag"] == 0x8        # the new datalogger's home


def test_live_rtl_and_sw_match_registry():
    """The whole point: zs_msg.py + datalogger_gather_gen.py agree with the registry."""
    assert id_gen.check(_ROOT) == 0


def test_diff_clean_when_live_is_a_subset():
    # registry is allowed to be AHEAD (planned/reserved); subset live is fine
    reg = {"uart": 0x1, "i2c": 0x2, "jtag": 0x8}
    assert id_gen.diff_against_registry(reg, [("live", {"uart": 0x1, "i2c": 0x2})]) == []


def test_diff_catches_code_mismatch():
    reg = {"uart": 0x1}
    problems = id_gen.diff_against_registry(reg, [("live", {"uart": 0x9})])
    assert problems and "uart" in problems[0] and "!=" in problems[0]


def test_diff_catches_unknown_domain():
    reg = {"uart": 0x1}
    problems = id_gen.diff_against_registry(reg, [("live", {"mystery": 0x2})])
    assert problems and "NOT in system_ids.yml" in problems[0]


def test_emit_python_and_vhdl_run():
    # smoke: both emitters produce output without raising
    assert id_gen.emit_python(_ROOT) == 0
    assert id_gen.emit_vhdl(_ROOT) == 0


def test_gen_sw_is_idempotent():
    """gen-sw on the synced repo must be a no-op — the generated zs_msg.py block
    already matches the registry."""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert id_gen.gen_sw(_ROOT) == 0
    assert "already in sync" in buf.getvalue()


def test_zs_msg_pkg_rtl_matches_registry():
    """The hand-written RTL package C_ZS_DOMAIN_* is drift-guarded too."""
    reg = id_gen.load_registry(_ROOT)
    pkg = id_gen.parse_zs_msg_pkg(_ROOT)
    assert pkg and pkg["edge"] == 0x6 and pkg["sys"] == 0x0
    assert id_gen.diff_against_registry(reg, [("zs_msg_pkg", pkg)]) == []


# ── DS-P3.70: severity + error_events single-source ─────────────────────────
def test_severity_registry_loads_and_single_sources():
    sev = id_gen.load_section(_ROOT, "severity")
    assert sev["error"] == 0x2 and sev["info"] == 0x0 and len(sev) >= 5
    sw = (_ROOT / "sw" / "stream" / "zs_msg.py").read_text()
    rtl = (_ROOT / "hw" / "rtl" / "pkg" / "zs_msg_pkg.vhd").read_text()
    assert id_gen._parse_consts(sw, py_prefix="SEV_") == sev, "zs_msg.py SEV_* drifted"
    assert id_gen._parse_consts(rtl, vhdl_prefix="C_ZS_SEV_") == sev, "zs_msg_pkg.vhd C_ZS_SEV_* drifted"


def test_error_events_registry_reserved_range_and_single_sourced():
    err = id_gen.load_section(_ROOT, "error_events")
    assert err["internal_fifo_overflow"] == 0xF0
    for name, code in err.items():
        assert id_gen._ERR_EVT_MIN <= code <= id_gen._ERR_EVT_MAX, \
            f"{name}=0x{code:X} outside reserved error-event range"
    sw = (_ROOT / "sw" / "stream" / "zs_msg.py").read_text()
    rtl = (_ROOT / "hw" / "rtl" / "pkg" / "zs_msg_pkg.vhd").read_text()
    assert id_gen._parse_consts(sw, py_prefix="ERR_EVT_") == err, "zs_msg.py ERR_EVT_* drifted"
    assert id_gen._parse_consts(rtl, vhdl_prefix="C_ZS_ERR_EVT_") == err, "zs_msg_pkg.vhd C_ZS_ERR_EVT_* drifted"


def test_check_covers_severity_and_error_events():
    # check() returns 0 only if domains+severity+error_events all match live code.
    assert id_gen.check(_ROOT) == 0
