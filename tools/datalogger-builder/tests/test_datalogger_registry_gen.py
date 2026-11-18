"""Drift-guard for the single-sourced STATS protocol registry (LT-P3.250 /
DS-P3.120).

sw/stream/datalogger_registry.py must stay generated-in-sync with
tools/datalogger-builder/config/datalogger_config.yml (the same source
datalogger_gather_gen.py uses), and the active set must match what
datalogger_gather_gen derives. A drift here is exactly the bug the ticket was filed
for — the daemon emitting a stale/wrong per-protocol set.
"""
import sys
from pathlib import Path

_SB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SB))
import datalogger_registry_gen as srg  # noqa: E402

_ROOT = srg._repo_root(_SB)


def test_generated_file_in_sync_with_yaml():
    # The committed sw/stream/datalogger_registry.py must equal a fresh render.
    assert srg.check(_ROOT) == 0


def test_render_matches_committed():
    rendered = srg._render(_ROOT)
    committed = (_ROOT / srg._GEN_PATH).read_text(encoding="utf-8")
    assert rendered == committed


def test_active_set_matches_datalogger_gather_gen():
    """The registry's active set is byte-for-byte the set datalogger_gather_gen.py
    derives — both must come from the SAME sanitize_type + sorted(keys) of the
    same datalogger_config.yml. If datalogger_gather_gen ever changes its name map,
    this catches the registry going out of step with the RTL gather."""
    import yaml

    # datalogger_gather_gen lives next to us; import it for the canonical derivation.
    import datalogger_gather_gen as sgg  # noqa: E402

    cfg = yaml.safe_load((_ROOT / srg._CONFIG).read_text(encoding="utf-8"))
    parsed = sgg.parse_datalogger_config(cfg)
    all_protocols = {}
    all_protocols.update(parsed["stream_protocols"])
    all_protocols.update(parsed["burst_protocols"])
    gather_set = sorted(all_protocols.keys())  # UPPER, as sgg uses
    registry_set = [p.upper() for p in srg.active_protocols(cfg)]
    assert registry_set == gather_set


def test_known_active_set_today():
    """Anchor the CURRENT canonical active set (a hard-coded expectation — a
    value re-derived from the same yaml would tautologically pass)."""
    cfg_protos = [p.lower() for p in srg.active_protocols(
        __import__("yaml").safe_load((_ROOT / srg._CONFIG).read_text(encoding="utf-8")))]
    assert cfg_protos == ["can", "edge", "i2c", "spi", "uart"]
