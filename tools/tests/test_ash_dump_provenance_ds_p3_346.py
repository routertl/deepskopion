# SPDX-License-Identifier: MPL-2.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""DS-P3.346 (a): the parser's busybox is compared with the busybox the shipped DE25 image holds.

DS-P3.341 tied the parser's build to constants and a bench-only check of the Buildroot legal-info, not to the binary
in image 14d16638. Here the ash-option markers and the version string of the parser binary are compared with those
of the image's own /bin/busybox. A marker is a string compiled in only when an ash option is on, so a marker in one
binary and not the other means the two ashes were configured differently.

Limits, stated: it sees ash option strings and the version, not the 16 Buildroot patches (only 0008 touches ash.c,
one comment line), the toolchain, or an option whose value leaves no string.

The image comparison runs on the bench (the image is 100+ MB and not in public CI): without the image it skips with
a reason naming this ticket. The mismatch detection is exercised against the real parser binary everywhere the
parser can be built, so it is never proved on the image alone.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ash_dump  # noqa: E402

TOOLS = Path(__file__).resolve().parents[1]
IMAGE_SHA256 = "14d16638fa751228a9e1df229b1bd4d23a62f3c7caaa5f898c4d79220f1aff87"
DEFAULT_IMAGE = Path.home() / ".cache" / "de25-p2w" / "artifacts" / "p2img-p316-20261123" / "rootfs.ext4"


@pytest.fixture(scope="module")
def parser_bytes() -> bytes:
    github_proper = (os.environ.get("GITHUB_ACTIONS") == "true" and not os.environ.get("FORGEJO_ACTIONS")
                     and not os.environ.get("GITEA_ACTIONS"))
    if github_proper:
        pytest.skip("host-prerequisite: ash parser build (DS-P3.346) - GitHub Actions builds no parser; "
                    "the provenance tests run in the bench gate and the Forgejo CI")
    return ash_dump.build().read_bytes()


def _image_busybox(tmp_path: Path) -> bytes:
    explicit = os.environ.get("DS_DE25_IMAGE")
    image = Path(explicit or DEFAULT_IMAGE).expanduser()
    if not image.is_file():
        assert not explicit, f"DS_DE25_IMAGE names {image}, which does not exist"     # an explicit request never skips
        pytest.skip(f"the measured DE25 image (sha256 {IMAGE_SHA256[:8]}...) is not on this machine ({image}); "
                    "DS-P3.352 owns making it available off-bench; until then the comparison runs on the bench, a skip is not evidence")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    assert digest == IMAGE_SHA256, f"{image} is sha256 {digest}, not the measured image"
    spec = importlib.util.spec_from_file_location("de25_linux_image_prov", TOOLS / "de25_linux_image.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "busybox"
    subprocess.run([module.sbin_tool("debugfs"), "-R", f"dump /bin/busybox {out}", str(image)],
                   capture_output=True, check=False)
    assert out.is_file() and out.stat().st_size > 100_000, "could not extract /bin/busybox from the image"
    return out.read_bytes()


def test_the_markers_are_options_the_pinned_config_turns_on():
    config = (TOOLS / "ash_dump" / "busybox.config").read_text(encoding="utf-8")
    for option in ash_dump.ASH_MARKERS.values():
        assert f"{option}=y" in config, f"{option} is not =y in the pinned config: its marker cannot be compared"


def test_the_parser_binary_carries_every_marker_and_the_pinned_version(parser_bytes):
    props = ash_dump.ash_provenance(parser_bytes)
    assert props["versions"] == ["1.36.1"], props
    assert all(props["markers"].values()), props["markers"]       # the baseline every mutation below departs from


def test_the_parser_matches_the_busybox_in_the_shipped_image(parser_bytes, tmp_path):
    image = _image_busybox(tmp_path)
    assert ash_dump.ash_provenance(image)["versions"] == ["1.36.1"]
    assert ash_dump.provenance_mismatches(parser_bytes, image) == []


@pytest.mark.parametrize("marker", sorted(m.decode() for m in ash_dump.ASH_MARKERS))
def test_a_planted_option_mismatch_is_reported_naming_the_option(parser_bytes, marker):
    """The control that makes a clean comparison mean something: the same binary with one marker removed is refused."""
    planted = parser_bytes.replace(marker.encode(), b"\0" * len(marker))
    assert planted != parser_bytes
    problems = [p for p in ash_dump.provenance_mismatches(parser_bytes, planted) if "sh applet" not in p]
    assert len(problems) == 1 and ash_dump.ASH_MARKERS[marker.encode()] in problems[0], problems


def test_the_image_sh_applet_runs_ash_and_the_image_also_carries_a_hush_applet(tmp_path):
    """Measured on image 14d16638: `sh` and `ash` share one main, `hush` has another. The image's busybox config
    also builds hush (the pinned parser config does not): recorded here as a known difference. It does not reach
    the parse: shell/ash.c holds no HUSH conditional and `sh` runs ash."""
    image = _image_busybox(tmp_path)
    assert ash_dump.shell_applets(image) == {"sh_runs": "ash", "hush_applet": True}
    config = (TOOLS / "ash_dump" / "busybox.config").read_text(encoding="utf-8")
    assert "# CONFIG_HUSH is not set" in config and "CONFIG_SH_IS_ASH=y" in config


def test_an_image_whose_sh_applet_is_rewired_to_hush_is_refused(parser_bytes, tmp_path):
    """The control for the check above: the same image with the sh table entry pointing at hush's main."""
    import struct
    image = bytearray(_image_busybox(tmp_path))
    table, index = ash_dump._applet_table(bytes(image))
    hush_main = image[table + 8 * index["hush"]:table + 8 * index["hush"] + 8]
    image[table + 8 * index["sh"]:table + 8 * index["sh"] + 8] = hush_main
    assert ash_dump.shell_applets(bytes(image))["sh_runs"] == "hush"
    problems = ash_dump.provenance_mismatches(parser_bytes, bytes(image))
    assert len(problems) == 1 and "sh applet runs 'hush'" in problems[0], problems


def test_a_planted_version_mismatch_is_reported(parser_bytes):
    planted = parser_bytes.replace(b"BusyBox v1.36.1", b"BusyBox v1.36.2")
    problems = [p for p in ash_dump.provenance_mismatches(parser_bytes, planted) if "sh applet" not in p]
    assert len(problems) == 1 and "version differs" in problems[0], problems


@pytest.mark.parametrize("marker", sorted(m.decode() for m in ash_dump.ASH_MARKERS))
def test_a_marker_removed_from_the_real_image_binary_is_reported(parser_bytes, tmp_path, marker):
    """The same control on the genuine image bytes, so the image comparison itself is shown able to fail."""
    image = _image_busybox(tmp_path)
    planted = image.replace(marker.encode(), b"\0" * len(marker))
    assert planted != image and ash_dump.provenance_mismatches(parser_bytes, image) == []
    problems = ash_dump.provenance_mismatches(parser_bytes, planted)
    assert len(problems) == 1 and ash_dump.ASH_MARKERS[marker.encode()] in problems[0], problems


@pytest.mark.parametrize("blob", [b"", b"\x7fELF" + bytes(4096), b"BusyBox v1.36.1 but no ash markers",
                                  b"BusyBox v1.36.1 unalias TMOUT command_not_found_handle"])
def test_a_file_that_is_not_a_busybox_with_ash_is_refused_not_matched(parser_bytes, blob):
    """Two empty or foreign files agree on everything they lack; that must never read as a match."""
    assert ash_dump.provenance_mismatches(blob, blob) != []
    assert ash_dump.provenance_mismatches(parser_bytes, blob) != []


def test_every_bwrap_leg_skip_in_the_parser_tests_names_this_ticket_and_the_job_that_runs_it():
    """(b): the bwrap legs cannot run where user namespaces are refused. Each skip there is a literal that says which
    ticket records it and which job runs the leg (the land retest on the bench host; the commit hook does not), so
    it is never a bare skip, and land_content_guard can see the ticket. No leg may skip with any other reason."""
    import re
    source = (Path(__file__).with_name("test_ash_dump_ds_p3_341.py")).read_text(encoding="utf-8")
    skips = re.findall(r'pytest\.skip\("(DS-P3\.350: user namespaces[^"]*)"\s*"([^"]*)"\)', source)
    assert len(skips) == 3, skips
    assert all("land retest on the bench host" in tail for _head, tail in skips), skips
    assert "user namespaces are refused here (public CI)" not in source        # the old bare reason is gone
    assert 'SKIP_REASON = ("DS-P3.350: GitHub' in source                       # the GitHub skip too



def test_an_ambiguous_applet_table_is_no_answer_not_the_first_candidate(parser_bytes, tmp_path):
    """T1 round 2: the shipped binary has two table-like runs (offsets 0x19aba8, 0x1ed258); only one has every entry
    in code. A second fully valid copy must make the lookup refuse, not pick by file order."""
    image = _image_busybox(tmp_path)
    table, index = ash_dump._applet_table(image)
    decoy_at = 2019928                      # the second table-like run, inside a mapped segment (offset measured)
    planted = bytearray(image)
    planted[decoy_at:decoy_at + 8 * len(index)] = image[table:table + 8 * len(index)]
    assert ash_dump._applet_table(image) is not None and ash_dump._applet_table(bytes(planted)) is None
    problems = ash_dump.provenance_mismatches(parser_bytes, bytes(planted))
    assert any("sh applet runs None" in p for p in problems), problems


@pytest.mark.parametrize("marker", ["BASH_XTRACEFD", "arithmetic syntax error"])
def test_the_grammar_options_that_change_what_parses_have_markers(marker):
    assert marker.encode() in ash_dump.ASH_MARKERS     # BASH_COMPAT (<<<, &>, $'..', ${//}) and SH_MATH ($(( )))


def test_the_unmapped_bytes_of_a_binary_cannot_supply_the_sh_entry(parser_bytes, tmp_path):
    """T1 round 3: zero the genuine sh entry and append a good copy of the table past the mapped segments; the loader
    never maps those bytes, so the lookup must not use them."""
    image = bytearray(_image_busybox(tmp_path))
    table, index = ash_dump._applet_table(bytes(image))
    copy = bytes(image[table:table + 8 * len(index)])
    image[table + 8 * index["sh"]:table + 8 * index["sh"] + 8] = bytes(8)
    assert ash_dump.shell_applets(bytes(image) + copy)["sh_runs"] is None


def test_the_image_builds_ash_mail_support_the_parser_does_not_known_interactive_only_difference(parser_bytes, tmp_path):
    """T1 round 3 measured a second option difference beyond hush: the image carries the CONFIG_ASH_MAIL strings
    (MAILPATH, 'you have mail'), the parser does not. Pinned here on both sides. ASH_MAIL only checks mailboxes
    before an interactive prompt, so it does not reach `sh -n SCRIPT` parsing."""
    image = _image_busybox(tmp_path)
    for text in (b"you have mail", b"MAILPATH"):
        assert text in image and text not in parser_bytes
    assert "# CONFIG_ASH_MAIL is not set" in (TOOLS / "ash_dump" / "busybox.config").read_text(encoding="utf-8")
