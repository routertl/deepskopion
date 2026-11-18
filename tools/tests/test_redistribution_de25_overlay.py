"""docs/REDISTRIBUTION.md lists every ELF file the DE25 image's Python overlay ships.

tools/de25_linux_image.py stage writes the overlay from pinned downloads, so a
new upstream release can add a binary nobody listed. overlay_inventory_mismatch
compares the ELF files of a staged overlay with the doc's "| Image path |
Component |" table, in both directions, and finds any ELF file in the script
overlay. These tests run it on a fixture stage built from the doc's own list;
sw/linux/de25/test_de25_linux_image.py runs it on a real stage.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs/REDISTRIBUTION.md"
_spec = importlib.util.spec_from_file_location("de25_linux_image",
                                               ROOT / "tools/de25_linux_image.py")
de25_linux_image = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(de25_linux_image)

ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 57


def _fixture_stage(tmp_path: Path) -> tuple[Path, Path]:
    """A Python overlay holding exactly the listed ELF files, plus a symlink
    and a non-ELF file; an ELF-free script overlay."""
    python_overlay, script_overlay = tmp_path / "python_overlay", tmp_path / "rootfs_overlay"
    for image_path in de25_linux_image.documented_overlay_elfs(DOC):
        target = python_overlay / image_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(ELF)
    (python_overlay / "bin/python3").symlink_to("python3.13")
    (python_overlay / "lib/python3.13").mkdir(parents=True, exist_ok=True)
    (python_overlay / "lib/python3.13/os.py").write_text("# not an ELF file\n")
    (script_overlay / "etc").mkdir(parents=True)
    (script_overlay / "etc/deepskopion-inputs").write_text("msgpack_version=1.1.1\n")
    return python_overlay, script_overlay


def test_the_doc_lists_the_staged_runtime_binaries():
    listed = de25_linux_image.documented_overlay_elfs(DOC)
    assert "/lib/libc.so.6" in listed and "/bin/python3.13" in listed
    assert "/usr/local/lib/deepskopion/libds_edge_drain.so" in listed
    # DS-P2.227: iproute2's ip and three can-utils programs
    assert {"/usr/sbin/ip", "/usr/bin/cansend", "/usr/bin/candump",
            "/usr/bin/cangen"} <= listed
    assert len(listed) == 16, sorted(listed)


def test_a_fixture_stage_matching_the_doc_passes(tmp_path):
    python_overlay, script_overlay = _fixture_stage(tmp_path)
    assert de25_linux_image.overlay_inventory_mismatch(python_overlay, script_overlay, DOC) == []


def test_an_unlisted_elf_in_the_python_overlay_is_reported(tmp_path):
    python_overlay, script_overlay = _fixture_stage(tmp_path)
    (python_overlay / "lib/libgcc_s.so.1").write_bytes(ELF)
    problems = de25_linux_image.overlay_inventory_mismatch(python_overlay, script_overlay, DOC)
    assert problems == ["/lib/libgcc_s.so.1 is in the Python overlay but not listed "
                        "in REDISTRIBUTION.md"], problems


def test_a_listed_file_missing_from_the_stage_is_reported(tmp_path):
    python_overlay, script_overlay = _fixture_stage(tmp_path)
    (python_overlay / "lib/libutil.so.1").unlink()
    problems = de25_linux_image.overlay_inventory_mismatch(python_overlay, script_overlay, DOC)
    assert problems == ["/lib/libutil.so.1 is listed in REDISTRIBUTION.md but not in "
                        "the Python overlay"], problems


def test_an_elf_in_the_script_overlay_is_reported(tmp_path):
    python_overlay, script_overlay = _fixture_stage(tmp_path)
    (script_overlay / "usr/bin").mkdir(parents=True)
    (script_overlay / "usr/bin/tool").write_bytes(ELF)
    problems = de25_linux_image.overlay_inventory_mismatch(python_overlay, script_overlay, DOC)
    assert problems == ["/usr/bin/tool is an ELF file in the script overlay, which "
                        "holds none"], problems
