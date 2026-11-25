# rr unknown-llm (AI-generated)
"""Regression guards for the DS-R0 portable FIFO boundary."""

from pathlib import Path

import sys
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
PORTABLE_RTL = (
    ROOT / "hw" / "rtl" / "deepskopion_core.vhd",
    ROOT / "hw" / "rtl" / "fifo_sync.vhd",
    *(ROOT / "hw" / "rtl" / "datalogger").rglob("*.vhd"),
    *(ROOT / "hw" / "rtl" / "responder").rglob("*.vhd"),
)
TARGETS = tuple(all_target_files(ROOT))


# Fabric-only witness targets that carry NO portable FIFO boundary and must not
# be forced to manifest ``open-logic/olo_base_fifo_sync``. These are excluded
# BY NAME (not by a yml-text substring): the DS-P2.132 PolarFire SoC known-answer
# images (``pfsoc_ka_a/b/rea``) are minimal known-answer bitstreams with no
# datalogger/responder core. Every OTHER ``targets/*.yml`` is a product target
# that compiles ``deepskopion_core``/``fifo_sync`` and so MUST carry olo — a
# by-name exclude (rather than a source-list grep) keeps the invariant on every
# product target, including ``auto_sources: true`` ones like ``de25_datalogger``
# whose source list is not spelled out in the yml text.
FIFO_WITNESS_EXCLUDES = ("pfsoc_ka_",)


def _is_portable_fifo_target(target_path: Path) -> bool:
    """True iff the target is a product target bound by the olo FIFO contract.

    Excludes the fabric-only ``pfsoc_ka_*`` witness targets by filename; every
    other target compiles the portable FIFO boundary and is asserted.
    """
    return not target_path.name.startswith(FIFO_WITNESS_EXCLUDES)


FIFO_TARGETS = tuple(t for t in TARGETS if _is_portable_fifo_target(t))
FIFO_CONSUMER_SOURCES = (
    "uart_playback.vhd",
    "uart_datalogger.vhd",
    "arbiter.vhd",
    "edge_datalogger.vhd",
    "i2c_injector.vhd",
    "spi_injector.vhd",
)


def test_portable_core_has_no_vendor_fifo_or_primitive_dependency() -> None:
    forbidden = ("fifo_xpm_", "library xpm", "library unisim")
    for path in PORTABLE_RTL:
        text = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"{path.relative_to(ROOT)} contains {token!r}"


def test_all_fifo_consumers_use_the_project_boundary() -> None:
    consumers = []
    for path in PORTABLE_RTL:
        text = path.read_text(encoding="utf-8").lower()
        consumers.extend((path, line) for line in text.splitlines() if "entity tich.fifo_sync" in line)

    assert len(consumers) == 7, "expected every datalogger/responder FIFO site to use fifo_sync"
    adapter = (ROOT / "hw" / "rtl" / "fifo_sync.vhd").read_text(encoding="utf-8").lower()
    assert "entity olo.olo_base_fifo_sync" in adapter


def _packages(target_path: Path, root: Path):
    """The target's ``packages`` as rr resolves them.

    A target without ``extends:`` is read as written, exactly as before. An
    ``extends:`` target (DS-P2.264: ``de25_p2writer`` extends ``de25_datalogger``)
    is an overlay on its base, resolved by rr's own target loader
    (``sdk.cli.project_config._load_target_file``: dicts deep-merged, lists
    replaced). Only if rr is not importable here does the test apply that rule
    itself: the child's own ``packages`` wins, else its base's.
    """
    raw = yaml.safe_load(target_path.read_text()) or {}
    if "extends" not in raw:
        return raw["packages"]
    try:
        from sdk.cli.project_config import _load_target_file
    except ImportError:
        if "packages" in raw:
            return raw["packages"]
        return _packages(root / "targets" / f"{raw['extends']}.yml", root)
    return _load_target_file(str(target_path.resolve()), source=__name__, base_dir=root)["packages"]


def _assert_portable_packages(packages) -> None:
    assert "open-logic/olo_base_fifo_sync" in packages
    assert not any(name.startswith("routertl/fifo_xpm_") for name in packages)
    assert not any(name.startswith("routertl/unisim_") for name in packages)


def test_open_logic_is_manifested_and_vendor_wrappers_are_not_installed() -> None:
    fifo_manifest = yaml.safe_load((ROOT / "hw" / "rtl" / "fifo" / "ip.yml").read_text())
    # DS-P2.137: the registry package is a `dependencies:` entry. rr's manifest
    # resolver reads every `ips:` entry as a child ip.yml path, so the old
    # `ips: [open-logic/olo_base_fifo_sync]` made this manifest unloadable.
    assert fifo_manifest["dependencies"] == {"open-logic/olo_base_fifo_sync": "^4.4.1"}
    assert fifo_manifest["ips"] == []

    # Guard against the filter matching nothing (which would make the loop below
    # vacuously pass): the portable boundary is used by every product target.
    assert FIFO_TARGETS, "no target compiles the portable FIFO boundary — filter is broken"
    for target_path in FIFO_TARGETS:
        _assert_portable_packages(_packages(target_path, ROOT))


def test_lock_pins_open_logic_without_legacy_fifo_or_unisim_packages() -> None:
    lock = yaml.safe_load((ROOT / "ip.lock").read_text())
    olo = lock["open-logic/olo_base_fifo_sync"]
    assert olo["resolved_version"] == "4.4.1"
    assert olo["library"] == "olo"
    assert olo["commit"] == "d1407a7427d9201a7bcd8c37749ec083b13006dc"
    assert not any(name.startswith("routertl/fifo_xpm_") for name in lock)
    assert not any(name.startswith("routertl/unisim_") for name in lock)


def _hand_scoped_offenders(sim_root: Path) -> list[str]:
    offenders = []
    for path in sim_root.rglob("*.py"):
        # A consumer named only in a comment is not a compiled source
        # (test_datalogger_to_ddr.py cites edge_datalogger.vhd in prose since
        # DS-T2.84 and builds its set from the DUT closure), so match code only.
        text = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        if "custom_libraries" not in text or "scoped_libraries(" in text:
            continue
        if not any(name in text for name in FIFO_CONSUMER_SOURCES):
            continue
        has_boundary = (
            "fifo_sync.vhd" in text
            or "olo_fifo_sources" in text
            or "CUSTOM_LIBRARIES" in text
        )
        if not has_boundary:
            offenders.append(str(path.relative_to(sim_root)))
    return offenders


def test_hand_scoped_fifo_consumer_tests_compile_the_portable_boundary() -> None:
    offenders = _hand_scoped_offenders(ROOT / "hw" / "sim")
    assert not offenders, (
        "hand-scoped FIFO consumer tests must compile fifo_sync and open-logic: "
        + ", ".join(offenders)
    )


def test_hand_scoped_guard_still_flags_a_consumer_listed_in_code(tmp_path: Path) -> None:
    """DS-P1.75: ignoring comments must not hide a consumer the test compiles."""
    (tmp_path / "test_commented.py").write_text(
        "# compiles edge_datalogger.vhd via the DUT closure\n"
        "run_simulation(custom_libraries=resolved)\n", encoding="utf-8")
    (tmp_path / "test_listed.py").write_text(
        "run_simulation(custom_libraries={'tich': ['rtl/edge_datalogger.vhd']})"
        "  # no fifo boundary\n", encoding="utf-8")
    assert _hand_scoped_offenders(tmp_path) == ["test_listed.py"]


def test_an_extends_target_inherits_its_base_packages_into_the_check(tmp_path) -> None:
    """DS-P2.264 red-first: a child whose base carries routertl/fifo_xpm_* fails."""
    (tmp_path / "targets").mkdir()
    (tmp_path / "targets" / "base.yml").write_text(
        "packages:\n  - open-logic/olo_base_fifo_sync\n  - routertl/fifo_xpm_sync\n")
    child = tmp_path / "targets" / "child.yml"
    child.write_text("extends: base\n")
    packages = _packages(child, tmp_path)
    assert "routertl/fifo_xpm_sync" in packages
    with pytest.raises(AssertionError):
        _assert_portable_packages(packages)
