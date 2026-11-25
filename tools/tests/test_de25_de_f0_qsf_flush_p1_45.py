# rr unknown-llm (AI-generated)
"""DE-F0 / DE-G0 (DS-P1.45) — the vendor-mismatch hook must not read a STALE qsf.

Measured off-bench on the DE-G0 rehearsal host, 2026-08-30 (Record E): project
generation for `de25_datalogger` aborted with

    Error (23035): Tcl error: DE-F0: parsed zero VHDL_FILE assignments out of
    .../build/de25_datalogger/project/deepskopion.qsf — regexp drifted from the
    generated qsf's actual syntax

on a clean clone, while the same file held **208** VHDL_FILE assignments once
the project closed and flushed. The regexp had not drifted at all. The hook
reads the qsf off DISK, and at the moment it runs, gen_project.tcl's
assignments live only in the OPEN PROJECT — so what it reads is whatever the
file happened to contain beforehand. On this bench that is a full qsf from an
earlier build, which is why the defect was invisible here for the hook's whole
life; on a machine that has never built this target it is an empty file.

This is the third defect of that shape DE-G0 has found (the gitignored frozen
HPS tree and the never-installed locked packages were the first two), and it
is the reason the guards below execute the hook instead of reading it:

* a test that greps the .tcl for `export_assignments` would pass against a
  hook that called it in the wrong place, or after the read;
* a test that asserted the regexp matches a sample qsf line was ALREADY
  effectively true — the regexp was never the problem — and would have stayed
  green through the entire off-bench failure.

So the hook is sourced under `tclsh` with the Quartus commands it uses
stubbed, in a project directory shaped like a clean clone's: assignments made
in memory, qsf on disk EMPTY. The fix is what makes that pass, and
`test_the_flush_is_load_bearing` re-runs the same scenario against a copy of
the hook with the flush stripped out — the in-repo encoding of the mutation,
so the guard cannot outlive what it guards.
"""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hw/quartus/de25/de25_exclude_vendor_mismatch.tcl"

#: A verbatim line from the qsf Quartus Pro 26.1.0 b110 generated for this
#: target off-bench, with the absolute clone path made relative to the fixture.
_QSF_LINE = ('set_global_assignment -name VHDL_FILE "{path}" '
             '-hdl_version VHDL_2008 -library tich')

_STUB_PREAMBLE = r"""
# --- Quartus command stubs -------------------------------------------------
# The hook may only use commands whose behaviour is modelled here; anything
# else is a NameError-equivalent in Tcl and fails the test loudly, which is
# what we want if the hook grows a new dependency on the tool.
set ::MEM {}

proc set_global_assignment {args} {
    # Quartus keeps assignments in the OPEN PROJECT. Nothing reaches the qsf
    # until export_assignments runs — that asymmetry IS the defect under test.
    lappend ::MEM $args
}

proc remove_all_global_assignments {args} {
    set kept {}
    foreach a $::MEM {
        if {[lindex $a 1] ne "VHDL_FILE"} { lappend kept $a }
    }
    set ::MEM $kept
}

proc export_assignments {} {
    # Mirrors Quartus Pro 26.1's own qsf syntax: paths without spaces are
    # emitted unquoted; paths with spaces are enclosed in double quotes.
    # The hook parses that text, so the stub must match Quartus's actual output format.
    set fh [open [file join [pwd] deepskopion.qsf] w]
    foreach a $::MEM {
        set out "set_global_assignment"
        set i 0
        foreach tok $a {
            if {$i == 2 && [string match "* *" $tok]} {
                append out " \"$tok\""
            } else {
                append out " $tok"
            }
            incr i
        }
        puts $fh $out
    }
    close $fh
}
"""


def _mutated_copy(tmp_path: Path, text: str) -> Path:
    """The hook with its flush removed, in a tree shaped like the repo's.

    The hook derives the project root from its own location and refuses if
    the DE-R0 vendor stubs are missing, so a copy dropped anywhere would fail
    for that unrelated reason and the mutation would prove nothing.
    """
    root = tmp_path / "fake_repo"
    (root / "hw/quartus/de25").mkdir(parents=True)
    stubs = root / "tools/sim/vendor_stubs/rr_ddr_stream_intel"
    stubs.mkdir(parents=True)
    for stub in ("rr_fifo_sync_xpm_body_stub.vhd",
                 "rr_fifo_async_mixed_xpm_body_stub.vhd"):
        (stubs / stub).write_text("-- placeholder\n", encoding="utf-8")
    hook = root / "hw/quartus/de25/de25_exclude_vendor_mismatch.tcl"
    hook.write_text(re.sub(r"^export_assignments$", "# flush removed",
                           text, count=1, flags=re.M), encoding="utf-8")
    return hook


def _run_hook(hook: Path, project_dir: Path, seeded: list) -> subprocess.CompletedProcess:
    """Source `hook` with `project_dir` as cwd, `seeded` already in memory."""
    harness = project_dir / "harness.tcl"
    lines = [_STUB_PREAMBLE]
    for path in seeded:
        lines.append(f'set_global_assignment -name VHDL_FILE "{path}" '
                     f'-hdl_version VHDL_2008 -library tich')
    lines.append(f'source {{{hook}}}')
    lines.append('puts "MEMCOUNT=[llength $::MEM]"')
    harness.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return subprocess.run(["tclsh", str(harness)], cwd=project_dir,
                          capture_output=True, text=True)


def _clean_clone_project(tmp_path: Path) -> tuple[Path, list]:
    """A project dir as a machine that has NEVER built this target has it.

    Quartus has created the .qsf and it is empty; every assignment made so far
    is in the open project only.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / "deepskopion.qsf").write_text("", encoding="utf-8")
    seeded = [str(ROOT / "hw/rtl/pkg/datalogger_pkg.vhd"),
              str(ROOT / "hw/rtl/pkg/rom_info_pkg.vhd")]
    return project, seeded


@pytest.fixture(autouse=True)
def _needs_tclsh():
    if shutil.which("tclsh") is None:
        pytest.skip("tclsh not installed — this guard executes the hook, and "
                    "asserting on its source text instead is exactly the "
                    "weaker check that let DE-G0 Record E through")


def test_the_hook_survives_a_qsf_that_has_never_been_written(tmp_path):
    """THE GUARD: the clean-clone shape must not abort project generation."""
    project, seeded = _clean_clone_project(tmp_path)
    run = _run_hook(HOOK, project, seeded)

    assert "parsed zero VHDL_FILE assignments" not in (run.stdout + run.stderr), (
        "the hook read the qsf before the open project was flushed to it, so "
        "on a clean clone it sees an EMPTY file and aborts the build — the "
        "off-bench failure of DE-G0 Record E, invisible on any machine that "
        f"has built this target before.\nstdout: {run.stdout}\n"
        f"stderr: {run.stderr}")
    assert run.returncode == 0, (
        f"hook failed: rc={run.returncode}\n{run.stdout}\n{run.stderr}")
    # Positive control: the run must have SEEN the seeded assignments, not
    # merely have failed to complain. A hook that parsed nothing and skipped
    # its work quietly would satisfy the assertion above.
    assert "already absent from the generated qsf" in run.stdout, (
        f"the hook did not report on the source set it inspected: {run.stdout}")
    assert int(re.search(r"MEMCOUNT=(\d+)", run.stdout).group(1)) >= len(seeded) + 2, (
        "the two DE-R0 GEN_XPM binder stubs must still be added on top of the "
        f"seeded sources: {run.stdout}")


def test_the_flush_is_load_bearing(tmp_path):
    """MUTATION, encoded in the repo: strip the flush, the guard goes RED.

    Without this, `test_the_hook_survives_...` would keep passing if someone
    moved `export_assignments` after the read, or deleted it and left a stale
    qsf lying around in the fixture.
    """
    text = HOOK.read_text(encoding="utf-8")
    assert re.search(r"^export_assignments$", text, re.M), (
        "the hook no longer flushes the open project before reading the qsf; "
        "if it has been replaced by another mechanism, re-point this mutation")
    mutated = _mutated_copy(tmp_path, text)

    project, seeded = _clean_clone_project(tmp_path)
    run = _run_hook(mutated, project, seeded)

    assert run.returncode != 0 and "parsed zero VHDL_FILE assignments" in (
        run.stdout + run.stderr), (
        "the hook still succeeded against a clean clone with the flush "
        "removed, so the guard above proves nothing about the flush: "
        f"rc={run.returncode}\n{run.stdout}\n{run.stderr}")


def test_a_stale_qsf_is_what_hid_the_defect(tmp_path):
    """The bench's shape, kept as documentation with teeth.

    With a populated qsf left behind by an earlier build, the UNFIXED hook
    passes — which is why this defect survived every green run on this bench
    and needed a second machine to find. If this ever goes red, the story in
    the docstring is wrong and the real cause is something else.
    """
    mutated = _mutated_copy(tmp_path, HOOK.read_text(encoding="utf-8"))

    project, seeded = _clean_clone_project(tmp_path)
    (project / "deepskopion.qsf").write_text(
        "\n".join(_QSF_LINE.format(path=p) for p in seeded) + "\n",
        encoding="utf-8")

    run = _run_hook(mutated, project, seeded)
    assert run.returncode == 0, (
        "even with a stale populated qsf the unfixed hook failed, so the "
        f"off-bench failure had another cause: {run.stdout}\n{run.stderr}")


def test_unquoted_and_quoted_assignments_exclude_vendor_mismatch(tmp_path):
    """DS-P2.117: Quartus Pro 26.1 emits unquoted paths for paths without spaces.

    The hook must parse both unquoted and quoted VHDL_FILE assignments and drop
    libs/routertl/rea/rtl/rr_rea_jtag_xilinx7.vhd from the synthesis source set.
    """
    project, seeded = _clean_clone_project(tmp_path)
    xilinx_jtag = str(ROOT / "libs/routertl/rea/rtl/rr_rea_jtag_xilinx7.vhd")
    quoted_file = "/fake quoted path/file_with_spaces.vhd"
    seeded.extend([xilinx_jtag, quoted_file])

    run = _run_hook(HOOK, project, seeded)

    assert run.returncode == 0, f"hook failed: rc={run.returncode}\n{run.stdout}\n{run.stderr}"
    assert f"dropped {xilinx_jtag} from the synthesis source set" in run.stdout, (
        f"unquoted vendor mismatch path was not excluded: {run.stdout}"
    )
    assert "3 of 4 VHDL_FILE assignments kept" in run.stdout
    assert "MEMCOUNT=5" in run.stdout
