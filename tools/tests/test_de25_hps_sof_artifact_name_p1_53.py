"""DS-P1.53 revision regression guard: the HPS-bootable ``.sof`` basename
must be ONE NAME end-to-end — the staged ``linux.sof_path`` stem,
``run_socfpga_sof_hps``'s derived output name, ``tools/de25_linux_image.py``'s
``SOF_HPS_ARTIFACT``, and this ticket's close artifact
(``de25_datalogger_hps.sof``).

**Measured defect** (landing refusal on this ticket's first attempt):
``targets/de25_datalogger.yml`` pointed ``linux.sof_path`` straight at the
Quartus-named build output (``build/de25_datalogger/bitstream/deepskopion.sof``
— named after ``project.yml``'s ``platform.name``, not the target). rr's
``run_socfpga_sof_hps`` (sdk.cli.commands.linux_steps._socfpga) derives
its output name from ``sof.stem``, so that produced
``deepskopion_hps.sof`` — a name ``tools/de25_linux_image.py``'s
``SOF_HPS_ARTIFACT`` (hardcoded ``de25_datalogger_hps.sof``) and this ticket's
close artifact do not recognise, so ``build_lock()`` failed closed even
after a successful `rr linux sof`.

The fix stages a same-content copy of the built bitstream under the target
identity stem (``tools/de25_linux_image.py:_stage_target_stem_sof``) and
points ``linux.sof_path`` at THAT copy, so ``sof.stem`` is
``de25_datalogger`` end to end.
"""
import importlib.util
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET_YML = ROOT / "targets/de25_datalogger.yml"


def _load_de25_linux_image():
    """``tools/`` is not a package (no ``__init__.py``), so import the
    script by file path rather than assuming it is importable as a
    module."""
    spec = importlib.util.spec_from_file_location(
        "de25_linux_image", ROOT / "tools/de25_linux_image.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sof_path_value() -> str:
    """``targets/de25_datalogger.yml`` mixes YAML with inline trailing-comment
    blocks that ``yaml.safe_load`` handles fine (comments are stripped by
    the parser) — load it structurally rather than regexing the text."""
    data = yaml.safe_load(TARGET_YML.read_text())
    return data["linux"]["sof_path"]


def test_sof_hps_artifact_name_matches_target_stem():
    """SOF_HPS_ARTIFACT must be exactly ``<TARGET_STEM>_hps.sof`` — the name
    ``run_socfpga_sof_hps`` derives from a ``.sof`` whose stem is
    TARGET_STEM. A drift here (either constant edited alone) is exactly the
    class of defect that broke `build_lock()` on this ticket's first
    attempt."""
    image = _load_de25_linux_image()
    assert image.SOF_HPS_ARTIFACT == f"{image.TARGET_STEM}_hps.sof", (
        f"SOF_HPS_ARTIFACT ({image.SOF_HPS_ARTIFACT!r}) no longer matches "
        f"TARGET_STEM ({image.TARGET_STEM!r}) + '_hps.sof' — "
        "run_socfpga_sof_hps derives its output name from the staged "
        "sof's stem (TARGET_STEM), so these must move together."
    )


def test_sof_path_stem_matches_target_stem():
    """``targets/de25_datalogger.yml``'s ``linux.sof_path`` must point at a
    file whose STEM is TARGET_STEM — anything else (e.g. pointing straight
    at the Quartus-named ``deepskopion.sof``) reproduces this ticket's
    landing-refusal defect: `rr linux sof` succeeds but writes a basename
    nothing downstream recognises."""
    image = _load_de25_linux_image()
    sof_path = _sof_path_value()
    stem = Path(sof_path).stem
    assert stem == image.TARGET_STEM, (
        f"targets/de25_datalogger.yml linux.sof_path ({sof_path!r}) has stem "
        f"{stem!r}, not TARGET_STEM ({image.TARGET_STEM!r}) — "
        "run_socfpga_sof_hps would derive the wrong HPS .sof basename "
        f"({stem}_hps.sof instead of {image.SOF_HPS_ARTIFACT})."
    )


def test_stage_target_stem_sof_output_matches_sof_path():
    """The staging helper must write to exactly the path
    ``linux.sof_path`` declares — otherwise `rr linux sof` looks for a
    file the staging step never created."""
    image = _load_de25_linux_image()
    sof_path = _sof_path_value()
    bitstream_dir = ROOT / "build" / image.TARGET_STEM / "bitstream"
    expected = bitstream_dir / f"{image.TARGET_STEM}.sof"
    assert (ROOT / sof_path) == expected, (
        f"linux.sof_path ({sof_path!r}) resolves to {ROOT / sof_path}, but "
        f"_stage_target_stem_sof writes to {expected} — these must be the "
        "same file or the staged copy is never found."
    )
