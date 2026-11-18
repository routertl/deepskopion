"""DS-P2.323: every cocotb file that names the regbank schema hex runs in both
nightly baseline tags, or is excluded by name with a reason.

DS-P1.104 (bb0a0f50) changed hw/rtl/regbank/config_schema.hex from 24 to 11
words. test_regbank_rom_stream_ds_p1_82 pinned the old words and went stale
without a red anywhere: it was in no simulation tag, so neither nightly
baseline (tools/sim/run_xilinx_baseline.py over targets/zybo_datalogger.yml
xilinx_baseline, bench/sim/run_de25_core_baseline.py over
bench/targets/de25_core_baseline.yml de25_baseline) ran it.

The rule is discovered, not listed: every file under hw/sim/cocotb/tests whose
text names regbank_schema.hex or config_schema.hex is a hit. A hit that is a
cocotb test module must be in BOTH tags; any other hit (a helper, a staging-only
bench) must be in EXCLUDED with its reason. A new content-reading test therefore
fails here until it is in the baselines.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import BENCH_DIR, PRODUCTS_DIR, has_bench, target_file  # noqa: E402  (DS-P1.103)

TESTS_DIR = Path("hw/sim/cocotb/tests")
#: tag -> the target that carries it. de25_core_baseline is a bench target (DS-P1.103): the public
#: export has no bench/, and there only the product tag is checked.
TAG_TARGETS = {
    "xilinx_baseline": "zybo_datalogger",
    "de25_baseline": "de25_core_baseline",
}
BENCH_TAGS = {"de25_baseline"}
HEX_NAME = re.compile(r"(?<![\w-])(regbank_schema|config_schema)\.hex(?![\w-])")
STAGES = "stages the hex for elaboration, asserts nothing on its content"

#: Files that name a schema hex but read nothing from it a schema change could
#: make stale. Keys are paths relative to the repository root.
EXCLUDED = {
    "hw/sim/cocotb/tests/datalogger/infra/datalogger_gather/test_datalogger_mix_sweep.py": STAGES,
    "hw/sim/cocotb/tests/infra/commit_addr_crossing/test_commit_addr_crossing.py": STAGES,
    "hw/sim/cocotb/tests/stimulus/core_seam/_core_seam_common.py": STAGES,
}

#: The content readers known at DS-P2.323. The scan must find at least these,
#: so a scan that silently finds nothing cannot pass.
KNOWN_READERS = {
    "test_regbank_rom_stream_ds_p1_82",
    "test_regbank_wrapper_rom_seam",
    "test_pfsoc_fic0_regbank_read_ds_p2_305",
    "test_regbank_reset_values_ds_p2_317",
}


def hex_naming_files(root: Path) -> list[str]:
    """Every file under the cocotb test tree whose text names either hex."""
    hits = []
    for path in sorted((root / TESTS_DIR).rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        if HEX_NAME.search(text):
            hits.append(path.relative_to(root).as_posix())
    return hits


def baseline_tags(root: Path) -> dict[str, set[str]]:
    tags = {}
    for tag, name in TAG_TARGETS.items():
        if tag in BENCH_TAGS and not has_bench(root):
            continue
        data = yaml.safe_load(target_file(name, root).read_text(encoding="utf-8")) or {}
        tags[tag] = set(((data.get("simulation") or {}).get("tags") or {}).get(tag) or [])
    return tags


def violations(root: Path, excluded: dict[str, str]) -> list[str]:
    tags = baseline_tags(root)
    out = []
    hits = hex_naming_files(root)
    for rel in hits:
        if rel in excluded:
            continue
        name = Path(rel).name
        module = Path(rel).stem
        if not (name.startswith("test_") and name.endswith(".py")):
            out.append(f"{rel}: names a schema hex, is not a test module and is not in EXCLUDED")
            continue
        missing = sorted(t for t, members in tags.items() if module not in members)
        if missing:
            out.append(f"{rel}: names a schema hex but {module} is not in {', '.join(missing)}")
    for rel in sorted(set(excluded) - set(hits)):
        out.append(f"{rel}: in EXCLUDED but no longer a file that names a schema hex")
    return out


def test_every_hex_reading_cocotb_file_is_in_both_baselines_or_excluded() -> None:
    found = {Path(p).stem for p in hex_naming_files(ROOT)}
    assert KNOWN_READERS <= found, f"scan lost known readers: {sorted(KNOWN_READERS - found)}"
    assert violations(ROOT, EXCLUDED) == []


def _tag_file(tmp: Path, tag: str) -> Path:
    return tmp / (BENCH_DIR if tag in BENCH_TAGS else PRODUCTS_DIR) / f"{TAG_TARGETS[tag]}.yml"


def _fixture(tmp: Path, tag_members: list[str], files: dict[str, str]) -> Path:
    for tag in TAG_TARGETS:
        path = _tag_file(tmp, tag)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"simulation": {"tags": {tag: tag_members}}}), encoding="utf-8")
    for rel, text in files.items():
        (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp / rel).write_text(text, encoding="utf-8")
    return tmp


def test_an_unlisted_hex_reading_test_is_refused(tmp_path: Path) -> None:
    rel = (TESTS_DIR / "x" / "test_new_rom_reader.py").as_posix()
    root = _fixture(tmp_path, ["test_other"], {rel: 'HEX = "config_schema.hex"\n'})
    assert violations(root, {}) == [
        f"{rel}: names a schema hex but test_new_rom_reader is not in de25_baseline, xilinx_baseline"
    ]


def test_a_reader_in_only_one_tag_is_refused(tmp_path: Path) -> None:
    rel = (TESTS_DIR / "x" / "test_new_rom_reader.py").as_posix()
    root = _fixture(tmp_path, [], {rel: 'HEX = "regbank_schema.hex"\n'})
    _tag_file(root, "xilinx_baseline").write_text(
        yaml.safe_dump({"simulation": {"tags": {"xilinx_baseline": ["test_new_rom_reader"]}}}),
        encoding="utf-8")
    assert violations(root, {}) == [
        f"{rel}: names a schema hex but test_new_rom_reader is not in de25_baseline"
    ]


def test_an_unlisted_helper_and_a_stale_exclusion_are_refused(tmp_path: Path) -> None:
    helper = (TESTS_DIR / "x" / "_helper.py").as_posix()
    gone = (TESTS_DIR / "x" / "test_gone.py").as_posix()
    root = _fixture(tmp_path, [], {helper: "# stages regbank_schema.hex\n"})
    assert violations(root, {gone: STAGES}) == [
        f"{helper}: names a schema hex, is not a test module and is not in EXCLUDED",
        f"{gone}: in EXCLUDED but no longer a file that names a schema hex",
    ]


def test_a_listed_reader_and_an_excluded_helper_pass(tmp_path: Path) -> None:
    rel = (TESTS_DIR / "x" / "test_new_rom_reader.py").as_posix()
    helper = (TESTS_DIR / "x" / "_helper.py").as_posix()
    root = _fixture(tmp_path, ["test_new_rom_reader"],
                    {rel: 'HEX = "config_schema.hex"\n', helper: "config_schema.hex\n"})
    assert violations(root, {helper: STAGES}) == []


def test_without_bench_only_the_product_tag_is_checked(tmp_path: Path) -> None:
    """The public export carries no bench/: the reader must still be in the product tag."""
    rel = (TESTS_DIR / "x" / "test_new_rom_reader.py").as_posix()
    root = _fixture(tmp_path, [], {rel: 'HEX = "config_schema.hex"\n'})
    bench_file = _tag_file(root, "de25_baseline")
    bench_file.unlink()
    bench_file.parent.rmdir()
    bench_file.parent.parent.rmdir()
    assert violations(root, {}) == [
        f"{rel}: names a schema hex but test_new_rom_reader is not in xilinx_baseline"
    ]
