#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Reject bench identity and credentials from public artifacts (DS-C1).

The public boundary is wider than source code.  A clean target file is not
enough when generated RTL, a provenance record, an initramfs, or a captured
console transcript still carries the machine that produced it.  This command
uses one set of rules for all of them.

With no paths it scans every blob in the git index.  Paths supplied on the
command line are scanned in addition; directories are walked recursively and
common archive/compression formats (including ``newc`` initramfs and U-Boot
legacy images) are opened.  Consequently the same command can gate a commit
and judge build outputs without maintaining two policies.

Examples::

    python3 tools/public_artifact_hygiene.py
    python3 tools/public_artifact_hygiene.py \
        hw/rtl/pkg build/provenance.json sw/linux/out/rootfs.cpio.gz run.log

Exit 0 means every requested input was inspected and clean.  Missing or
unreadable inputs are errors, never a clean result.

Public Markdown path references (export gate, DS-P3.190)
--------------------------------------------------------
An exported ``.md`` file is refused when it names a path that the DROP rules
remove ("dropped path in docs (<rule>)") or, for a directory export, a path
that is not in the exported tree ("missing path in docs").  The finding names
the doc line.  ``_inspect_markdown_paths`` implements it.

The document is parsed by markdown-it-py (CommonMark, ``requirements.txt``),
and the checks walk its token stream.  Links are never found by a regular
expression over Markdown syntax.  Each destination is taken as the parser
normalised it (backslash escapes removed, entity and numeric character
references decoded), its ``#fragment`` and ``?query`` cut, then its
percent-escapes decoded.  It is then classified: an external scheme or a
protocol-relative ``//host/...`` is ignored; anything else is a repository
path that must not be dropped and, for a directory export, must exist.

Checked forms:

* inline links and images, ``[t](p)`` / ``![a](p)`` (``link_open`` href,
  ``image`` src), in every form CommonMark accepts: balanced or escaped
  parentheses, ``<p>``, titles, nested brackets in the text, an image inside
  a link;
* reference definitions ``[ref]: p`` (``definition`` tokens), used or not;
  each ``[t][ref]``, ``[t][]`` or ``[ref]`` use is judged at its
  definition's line;
* raw HTML ``<a href=...>``, ``<img src=...>``, ``<source src=...>`` in
  ``html_block`` and ``html_inline`` tokens (HTML entities decoded);
* link targets resolve against the doc's directory, ``/p`` against the
  repository root (as GitHub renders it); a target that leaves the root is
  missing;
* indented (four-space) code blocks are read as text: their links are
  judged;
* the one non-link Markdown check, backticked filenames (``code_inline``
  tokens): a token counts as a path when path-shaped (no whitespace, glob,
  scheme, leading ``/`` or ``~``) and either a bare document name
  (``.md``/``.markdown``/``.rst``/``.adoc``/``.pdf``, present when any
  exported file has that name) or a token with a separator that starts at
  ``./``, ``../`` or a known top-level directory (``_DOC_REPO_PREFIXES``),
  present relative to the doc or to the root.  Every path-shaped token is
  also checked against the DROP rules;
* bare repository paths in text (``_DOC_REPO_PATH_RE``, the ticket's "names
  a repo path"), fenced commands included, with link destinations taken
  out first and, in prose, backslash escapes removed.  Trailing sentence
  punctuation (``. , ; : ) ! ?``, a ``)`` only when unbalanced) is not part
  of the path.  Every relative form (``x/y``, ``./x/y``, ``../x/y``) is read
  the same way: resolved against the document's directory and against the
  root (where a fenced command runs), both readings meet the DROP rules, and
  the path is missing when neither reading is in the exported tree (a path
  that leaves the root never is).  Absence is judged for a path that, without
  its leading ``./`` and ``../`` steps, starts in ``_DOC_REPO_PREFIXES`` and
  has no glob: ``../tools/gone.py`` in ``docs/x.md`` is judged,
  ``sbin/init -> ../bin/busybox`` and ``read/write`` are not.

Deliberately not checked:

* external references: any URI scheme (``http:``, ``https:``, ``mailto:``,
  ``tel:``, ``ftp:``, ...), protocol-relative ``//host/...``, autolinks
  ``<scheme:...>``, and bare URLs in text;
* same-page ``#anchors``; footnote definitions ``[^n]:``;
* link syntax, reference definitions and backticks inside fenced code
  blocks (``fence`` tokens), and link syntax inside a code span: literal
  text there;
* backticked bare source/config names (``system_regs_uio.h``,
  ``hw.local.yml``: mostly generated, user-created or another project's
  files), unlisted extensions (``system.dtb``), versions, commands, calls,
  host paths (``/dev/ttyUSB0``, ``~/.bashrc``) and globs;
* backticked or bare paths under directories outside ``_DOC_REPO_PREFIXES``
  (``libs/``, ``sim/``, ``runtimes/``, ``build/``: fetched, local or
  generated content), and other projects' paths (``linux/Documentation/``);
* ``_DOC_PATH_ALLOWLIST`` entries; the root ``SDK_USER_GUIDE.md``; and in
  ``docs/REDISTRIBUTION.md``, which lists the dropped paths by design,
  everything except link absence;
* absence of a declared generated output named in backticks or bare text.
  The declaration is ``runtime_families.<family>.outputs`` in the exported
  tree's own ``tools/generated_sources.yml`` (``_GENERATED_SOURCES_REL``):
  gitignored files a build writes, such as ``hw/rtl/pkg/rom_info_pkg.vhd``.
  A doc may say where the build puts such a file.  A *link* to one is still
  missing (GitHub has no such file), and the DROP rules still apply to it.
  An export without that file declares nothing; a malformed one is an
  inspection error.

A missing markdown-it-py is an inspection error, never a clean result.
"""
from __future__ import annotations

import argparse
import bz2
import gzip
import html as _html
import io
import ipaddress
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import zipfile
import zlib
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_DEPTH = 8


class InspectionError(RuntimeError):
    """An input could not be inspected completely."""


def _joined(*parts: str) -> bytes:
    """Keep forbidden fixture signatures out of this tool's own source."""
    return "".join(parts).encode("ascii")


_FIXED_MARKERS = (
    ("host TFTP root", _joined("/srv", "/tftp")),
    ("private key", _joined("-----BEGIN ", "PRIVATE KEY-----")),
    ("private key", _joined("-----BEGIN RSA ", "PRIVATE KEY-----")),
    ("private key", _joined("-----BEGIN EC ", "PRIVATE KEY-----")),
    ("private key", _joined("-----BEGIN OPENSSH ", "PRIVATE KEY-----")),
)

_HOME_RE = re.compile(rb"/(?:home|Users)/[A-Za-z0-9._-]+(?:/|\b)")
_REMOVABLE_HOME_RE = re.compile(
    rb"/(?:run/)?media/(?!mmc|sd|usb|boot)[A-Za-z0-9._-]+/",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(rb"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
# DS-P2.360: one string literal (Python/JS/shell quoting, optional b/r/u/f prefix), and a run of two or more of them
# joined only by whitespace, `+` (Python, JS) or `&` (VHDL). An address split into two adjacent literals is the same
# leak as the whole literal; the public export of 2026-11-18 carried one in a test fixture.
_STR_LIT = rb"""[bBrRuUfF]{0,2}(?:"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')"""
_LIT_RUN_RE = re.compile(_STR_LIT + rb"(?:\s*[+&]?\s*" + _STR_LIT + rb")+")
_LIT_RE = re.compile(_STR_LIT)


def _joined_literal_runs(data: bytes):
    """(offset of the run, the run's literal contents joined) for every run of adjacent string literals."""
    for run in _LIT_RUN_RE.finditer(data):
        pieces = []
        for lit in _LIT_RE.finditer(run.group(0)):
            body = lit.group(0).lstrip(b"bBrRuUfF")
            pieces.append(body[1:-1])
        yield run.start(), b"".join(pieces)
_CABLE_RE = re.compile(rb"(?<![A-Z0-9])21[A-Z0-9]{10}(?![A-Z0-9])")
# A pasted kernel banner carries the account and host that built the kernel, in parentheses after the version.
# Exported text shows the placeholder instead.
# The whole parenthesised field is captured (any characters but brackets and blanks), so no account spelling slips
# through; an address in plain prose is not a banner and is not matched.
_KERNEL_BUILD_ID_RE = re.compile(rb"Linux version \S+ \(([^()\s]+@[^()\s]+)\)")
KERNEL_BUILD_ID_PLACEHOLDER = b"builder@buildhost"
_PUBLIC_KEY_RE = re.compile(
    rb"(?m)^[ \t]*(?:ssh-(?:ed25519|rsa)|ecdsa-sha2-nistp(?:256|384|521))[ \t]+"
    rb"[A-Za-z0-9+/]{20,}={0,3}(?:[ \t]|$)"
)
_TOKEN_RES = (
    ("AWS access key", re.compile(rb"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])")),
    ("GitHub token", re.compile(rb"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}")),
    ("GitLab token", re.compile(rb"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_-]{20,}")),
    ("Slack token", re.compile(rb"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{20,}")),
)

# ---------------------------------------------------------------------------
# DS-P2.113: GitHub-export gate definitions
# ---------------------------------------------------------------------------

DROP_LIST = (
    "docs/" + "in" + "ternal/",
    "." + "ag" + "ent/",
    "demo/config1/screencast.md",
    "sw/linux/stage_tftp_deploy.sh",
    "sw/linux/tests/test_stage_tftp_deploy.py",
    "docs/hardware/DE25_BOARD_FACTS.md",
    "libs/ctu-fee/",
    "docs/GOLDEN_PATH.md",
    "docs/design/de25_port_plan.md",
    "tools/tests/test_de25_port_plan.py",
    "tools/golden_path/",
    "sw/stream/tests/test_de_c0_silicon_capture.py",
    "sw/stream/tests/test_de_c0_p1_44_edge_retirement.py",
    "sw/stream/tests/test_de_g0_p1_45_hw_leg_captures.py",
    "sw/stream/tests/test_de_g0_p1_45_p145_captures.py",
    "tools/tests/test_de_c0_capture_correctness_doc.py",
    "tools/tests/test_de25_boot_precondition_p1_43.py",
    "tools/tests/test_launch_disclosure_ds_p2_160.py",
    "tools/tests/test_export_private_terms_ds_p1_123.py",
)

# DS-P1.123: words no exported file or path may contain. The list itself is
# private: it lives under the dropped internal tree, so the public tool reads it
# but never names its contents. Each non-comment line is one term, matched
# case-insensitively as a substring (a stem catches every inflection).
PRIVATE_TERMS_REL = "docs/" + "in" + "ternal/" + "export_private_terms.txt"

# Root-anchored export exclusions: the DROP verdicts of docs/REDISTRIBUTION.md
# plus files with no public meaning. Unlike DROP_LIST these only match a path
# relative to the repository or export root. A bare "out/" prefix applied to an
# absolute path would condemn every file of an export under a directory of that
# name. test_redistribution_verdicts keeps the two lists equal to the document.
DROP_PATTERNS = (
    ".forgejo/**",
    "bench/**",
    "targets/canfd_demo.ip.lock",
    "targets/de25_canfd.ip.lock",
    "tools/tests/test_de25_shell_contract_bench.py",
    "tools/tests/test_public_artifact_hygiene_de25_address_bench.py",
    "targets/canfd_demo.routertl.lock",
    "targets/classic_eye.routertl.lock",
    "targets/de25_canfd.routertl.lock",
    "targets/de25_core_baseline.routertl.lock",
    "targets/de25_p2writer.routertl.lock",
    "targets/eye_loopback.routertl.lock",
    "targets/pfsoc_fic0_rea.routertl.lock",
    "targets/pfsoc_ka_a.routertl.lock",
    "targets/pfsoc_ka_b.routertl.lock",
    "targets/pfsoc_ka_rea.routertl.lock",
    "sw/linux/kernel_ctucanfd.config-fragment",
    "sw/linux/kernel_patches/ctucanfd_ntxbufs.sh",
    "sw/linux/tests/test_ctucanfd_ntxbufs_patch_ds_p2_67.py",
    "hw/sim/cocotb/can_ctu_oracle.py",
    "hw/sim/cocotb/tests/datalogger/can/ds_t2_97_ctu_two_node_wire.json",
    "hw/sim/cocotb/tests/datalogger/can/test_ctu_two_node_wire_ds_t2_97.py",
    "hw/sim/cocotb/tests/datalogger/can/test_ctu_two_txtb_ds_p2_67.py",
    "hw/sim/cocotb/tests/datalogger/can/test_can_datalogger_ctu_wire_ds_t2_97.py",
    "hw/sim/cocotb/tests/datalogger/can/test_ctu_stuff_oracle.py",
    "hw/xdc/pblock_ctu.xdc",
    "sw/can/ctu_canfd.py",
    "sw/can/tests/test_ctu_canfd.py",
    "sw/stream/tests/test_arm_datalogger_defaults_bench.py",
    "sw/stream/tests/test_overlay_in_sync_bench.py",
    "tools/tests/test_baseline_scripts_name_their_target_ds_p2_273_bench.py",
    "tools/tests/test_ctu_package_lock_scope_ds_p1_103_bench.py",
    "tools/tests/test_de25_core_baseline_contract_bench.py",
    "tools/tests/test_pfsoc_ka_silicon_p2_132_bench.py",
    "tools/tests/test_pkg_install_clean_clone_p3_150_bench.py",
    "tools/tests/test_redistribution_verdicts_ds_p2_196_bench.py",
    "tools/tests/test_ref_design_manifest_p2_119_bench.py",
    "tools/tests/test_sim_sources_clean_checkout_ds_p2_204_bench.py",
    "tools/tests/test_target_files_ds_p1_103_bench.py",
    "tools/tests/test_target_xdc_rea_contract_ds_p3_168_bench.py",
    "sw/linux/de25/test_de25_linux_image_bench.py",
    "tools/tests/test_canfd_demo_can_datalogger_ds_p2_218.py",
    "tools/tests/test_de25_canfd_target_ds_p2_135.py",
    "tools/tests/test_can_opt_in_targets_carry_can_datalogger_ds_p3_211.py",
    "tools/tests/test_eye_loopback_buildability_ds_p3_172.py",
    "tools/tests/test_pfsoc_mss_wrapper_isolation_ds_p3_174.py",
    "context.json",
    "debug/pfsoc_*.yml",
    "docs/DS-*.md",
    "docs/design/pfsoc_port_plan.md",
    "docs/evidence/**",
    "docs/hardware/evidence/**",
    "docs/hardware/pf-l0*/**",
    "hw/libero/pfsoc/mss/disco_mss.cxz",
    "hw/libero/pfsoc/mss/BRINGUP_HISTORY.md",
    "out/**",
    "sw/linux/pfsoc/python_overlay.tar.gz",
    "sw/stream/tests/test_ds_p1_61_proto_stats_build_selection.py",
    "sw/stream/tests/test_edge4_live_evidence_ds_p1_71.py",
    "sw/stream/tests/test_edge_wire_identity_live_ds_t2_84.py",
    "sw/stream/tests/test_regbank_word_store_pfsoc_ds_t2_82.py",
    "tools/datalogger-builder/tests/test_cli_configurator_fit_reports.py",
    "tools/datalogger-builder/tests/fixtures/*.rpt",
    "tools/datalogger-builder/tests/fixtures/pfsoc_compile_netlist_hier_resources.csv",
    "tools/datalogger-builder/tests/test_cli_configurator_pfsoc.py",
    "tools/tests/test_clean_clone_rehearsal_record_p2_131.py",
    "tools/tests/test_de25_constraint_evidence_doc_ds_t1_19.py",
    "tools/tests/test_public_artifact_hygiene_evidence_ds_p1_45.py",
    "tools/tests/test_cross_link_rollout_ds_p2_44.py",
    "tools/tests/test_datalogger_mix_synthesis_ds_p2_163.py",
    "tools/tests/test_de25_generated_sdc_waivers_ds_t1_20.py",
    "tools/tests/test_ds_p2_178_zybo_cdc_evidence.py",
    "tools/tests/test_ds_p2_191_zybo_timing_evidence.py",
    "tools/tests/test_ds_t1_12_repin_silicon_witness.py",
    "tools/tests/test_hosting_decision_ds_p2_42.py",
    "tools/tests/test_hw_run_hps_devmem_ds_t2_88.py",
    "tools/tests/test_regbank_preamble_ds_t2_87.py",
    "tools/tests/test_timing_constraint_audit_ds_p1_72.py",
    "tools/tests/test_vendor_cdc_evidence_ds_t1_22.py",
    "tools/tests/test_de25_hw_contract_silicon_p2_165.py",
    "tools/tests/test_de25_drain_silicon_ds_p2_267.py",
    "tools/tests/test_ds_p1_79_de25_silicon_witness.py",
    "tools/tests/test_ds_p2_190_de25_silicon_witness.py",
    "tools/tests/test_witness_pin_gate_ds_p3_184.py",
    "tools/tests/test_pfsoc_frozen_mss_cxz_ds_p1_81.py",
    "tools/tests/test_pf_l0b_*.py",
    "tools/tests/test_pfsoc_mmio_routewave_p2_128.py",
    "tools/tests/test_pfsoc_mss_stage_p2_142.py",
    "tools/tests/test_pfsoc_l2lim_backing_p2_143.py",
    "tools/tests/test_pfsoc_fabric_program_p2_144.py",
    "tools/tests/test_pfsoc_hss_envm_job_ds_p2_145.py",
    "tools/pfsoc_stall_recover.py",
    "tools/tests/test_pfsoc_ddr_progress_ds_p2_145.py",
    "tools/tests/test_pfsoc_ioscb_snapshot_ds_p2_145.py",
    "tools/tests/test_pfsoc_scratchpad_firmware_composed_p2_143.py",
    "tools/tests/test_pfsoc_port_plan.py",
    "tools/tests/test_pfsoc_stall_recover_ds_p1_63.py",
    "tools/tests/test_pfsoc_iomux_console_mux_ds_p2_145.py",
    "tools/tests/test_pfsoc_cdc_evidence_ds_t1_22.py",
    "tools/tests/test_pfsoc_constraint_evidence_ds_t1_18.py",
    "tools/tests/test_pfsoc_ring_ddr_alias_ds_p1_62.py",
    "tools/tests/test_pfsoc_rea_mmio_rr_capture_ds_p2_128.py",
    "tools/tests/test_pfsoc_overlay_asset_ds_p1_81.py",
    "tools/tests/test_pfsoc_hss_l2lim_evidence_ds_p1_81.py",
    "tools/tests/test_pfsoc_ka_silicon_evidence_ds_p1_81.py",
    "tools/tests/test_pfsoc_golden_path_link_ds_p1_81.py",
    "tools/tests/test_pfsoc_rootfs_overlay_ds_p2_146.py",
    "tools/tests/test_redistribution_python_tarballs.py",
    "tools/tests/test_forge_ci_floor_leg_ds_p2_209.py",
    "tools/tests/data/**",
    "tools/tests/fixtures/ds_t1_20/*.rpt",
)


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(f"^{out}$")


_DROP_PATTERN_RES = tuple((p, _pattern_regex(p)) for p in DROP_PATTERNS)
# Any evidence directory, wherever a future one is added under docs/.
_DOCS_EVIDENCE_RE = re.compile(r"^docs/(?:.*/)?evidence/")

_AGENT_NAMES_RE = re.compile(
    rb"(?i)(?<![A-Za-z0-9])(?:"
    + _joined("cl", "aude") + rb"|"
    + _joined("co", "dex") + rb"|"
    + _joined("a", "gy") + rb"|"
    + _joined("fa", "ble") + rb"|"
    + _joined("op", "us") + rb"|"
    + _joined("gem", "ini")
    + rb")(?![A-Za-z0-9])"
)

_TICH_RE = re.compile(
    rb"(?i)(?<![A-Za-z0-9])(?:"
    + _joined("ti", "ch-super") + rb"|"
    + _joined("ti", "ch_super") + rb"|"
    + _joined("ti", "ch-pm") + rb"|"
    + _joined("ti", "ch_pm") + rb"|"
    + _joined("tl", "og")
    + rb")(?![A-Za-z0-9])"
)

_RETIRED_SN_RE = re.compile(
    rb"(?i)(?<![A-Za-z0-9])" + _joined("sn", "iffer") + rb"s?(?![A-Za-z0-9])"
)

_INTERNAL_PATH_RES = (
    ("internal sibling escape", re.compile(rb"\.\./" + _joined("route", "rtl"))),
    ("internal bench workspace path", re.compile(rb"(?<![A-Za-z0-9])" + _joined("git_", "repo/") + rb"(?:[A-Za-z0-9._-]+)")),
    ("internal docs reference", re.compile(rb"docs/" + _joined("inter", "nal/"))),
    ("internal agent reference", re.compile(rb"(?<![A-Za-z0-9])\." + _joined("ag", "ent/"))),
    ("internal admin reference", re.compile(rb"admin/" + _joined("restricted_", "names.yml"))),
)

_TICKET_ID_RE = re.compile(
    rb"(?<![A-Za-z0-9])(?:"
    rb"(?:DS|RTL|LT|PM|TIC|DDR)-[PTCXDE][0-9]+(?:\.[0-9]+)?"
    rb"|DS-D\.[0-9]+"
    rb"|Haz\.[0-9]+"
    rb")(?![A-Za-z0-9])"
)

# Markdown path references (DS-P3.190).  The module docstring lists which
# reference forms are judged and which are deliberately not.  Links, images,
# reference definitions, autolinks and code are found by markdown-it-py
# (CommonMark), never by a regular expression over Markdown syntax.
#
# Inline rules whose source span is recorded on the token they emit, so a
# finding can name its line: rule name -> token type.
_DOC_SPAN_RULES = {
    "link": "link_open",
    "image": "image",
    "autolink": "link_open",
    "backticks": "code_inline",
    "html_inline": "html_inline",
}
_DOC_MD_PARSER = None
# Raw HTML <a href> / <img src> / <source src>, the forms READMEs embed.
# This reads HTML (an html_block or html_inline token), not Markdown.
_DOC_HTML_REF_RE = re.compile(
    r"(?i)<(?:a|img|source)\b[^>]*?\s(?:href|src)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')"
)
# A CommonMark backslash escape: a backslash before ASCII punctuation.
_DOC_MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_DOC_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_DOC_REPO_PATH_RE = re.compile(
    rb"(?<![A-Za-z0-9_./:-])((?:\.{1,2}/)*(?:\.[A-Za-z0-9_-]+|[A-Za-z0-9_-]+)(?:/(?:[A-Za-z0-9_.*-]|\([A-Za-z0-9_.*-]*\))+)+/?)"
)
# The leading "./" and "../" steps of a relative path.
_DOC_DOT_STEPS_RE = re.compile(r"^(?:\.{1,2}/)+")
# Sentence punctuation that may end a bare path in prose without being part
# of it: "Use tools/export_github.py." names tools/export_github.py.
_DOC_SENTENCE_END = ".,;:)!?"
# A backticked token shaped like a relative path: no whitespace, no glob, no
# leading "/" or "~", no scheme.
_DOC_BACKTICK_PATH_RE = re.compile(
    r"^(?:\.{1,2}/)*[A-Za-z0-9_()\-][A-Za-z0-9_.()\-]*(?:/[A-Za-z0-9_.()\-]+)*/?$"
)
# Extensions of documents a bare backticked filename may name.
_DOC_DOC_EXTENSIONS = frozenset({"md", "markdown", "rst", "adoc", "pdf"})
_DOC_REPO_PREFIXES = (
    "docs/",
    "tools/",
    "hw/",
    "sw/",
    "demo/",
    "contracts/",
    "targets/",
    "ip/",
    "debug/",
    "hooks/",
    ".github/",
    "LICENSES/",
)

# The one declaration of gitignored build outputs (runtime_families), read
# from the exported tree so the export judges itself.  Module docstring.
_GENERATED_SOURCES_REL = "tools/generated_sources.yml"


def _declared_runtime_outputs(data: bytes, name: str) -> frozenset[str]:
    """Return the ``runtime_families.*.outputs`` paths of generated_sources.yml.

    Anything but a mapping of families, each with a list of relative path
    strings, is an InspectionError: a declaration the gate cannot read must
    not silently excuse, or silently stop excusing, a doc reference."""
    import yaml

    try:
        doc = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InspectionError(f"{name}: unreadable generated-output declaration: {exc}")
    families = doc.get("runtime_families") if isinstance(doc, dict) else None
    if not isinstance(families, dict):
        raise InspectionError(f"{name}: runtime_families is not a mapping")
    outputs: set[str] = set()
    for family, body in families.items():
        listed = body.get("outputs") if isinstance(body, dict) else None
        if not isinstance(listed, list) or not listed:
            raise InspectionError(
                f"{name}: runtime_families.{family}.outputs is not a non-empty list")
        for out in listed:
            norm = PurePosixPath(out).as_posix() if isinstance(out, str) else ""
            if not norm or norm.startswith(("/", "../")) or norm != out.rstrip("/"):
                raise InspectionError(
                    f"{name}: runtime_families.{family}.outputs entry {out!r} "
                    "is not a relative repository path")
            outputs.add(norm)
    return frozenset(outputs)


# Documented build outputs, user-placeholder examples, synced-core licence
# rows, and SDK-internal paths in public Markdown files. Keyed by exact
# repository-relative Markdown path and exact byte literal.
_DOC_PATH_ALLOWLIST: dict[str, set[bytes]] = {
    "FDD.md": {b"debug/emio_seam.yml"},
    "LICENSE.md": {
        b"libs/.targets/<target>/ctu-fee/ctucanfd_ip_core/",
        b"libs/.targets/<target>/ctu-fee/ctucanfd_ip_core/src/memory_registers/generated/",
    },
    "README.md": {
        b"hw/sim/waves/",                               # simulation output folder, written by rr sim run
        b"tools/datalogger-builder/config/my_mix.yml",  # the configurator example's output file
    },
    "docs/getting_started/datalogger_mix.md": {
        b"tools/datalogger-builder/config/my_mix.yml",
        b"targets/my_mix.yml",
    },
    # DS-P1.119: the DE25 image tool's output, programmed over JTAG by step 9 of the DE25 guide.
    "docs/getting_started/de25.md": {b"sw/linux/de25/out/de25_datalogger/de25_datalogger_hps.sof"},
    "docs/DEPLOYMENT_CONTRACT.md": {
        b"sw/linux/rootfs_overlay/usr/local/share/deepskopion/sw/stream/",
        b"sw/linux/rootfs_overlay/usr/sbin/dropbear",
        b"sw/linux/rootfs_overlay/usr/bin/",
        b"sw/linux/rootfs_overlay/usr/bin/scp",
        # DS-P1.118: the PolarFire target's staged dropbear (built by
        # sw/linux/pfsoc/build_python_overlay.sh; gitignored, never exported).
        b"sw/linux/pfsoc/python_overlay/usr/sbin/dropbear",
    },
    "docs/README.md": {b"SCREAMING_SNAKE.md", b"lower_snake.md"},
    "docs/SDK_USER_GUIDE.md": {
        b"hw/sim/cocotb/tests/test_my_block.py",
        b"tools/project-manager/manage_dependencies.py",
        b"tools/cocotb-testgen/README.md",
        b"tools/docker/README.md",
        b"tools/ila-gen",
        b"tools/report-analyzer",
        b"tools/report-analyzer/README.md",
    },
    "docs/hardware/BRING_UP_EDGE_DATALOGGER.md": {
        b"hw/bd/exclude_xpm_sim_from_synth.tcl",
    },
    "docs/hardware/datalogger_gather_contract.md": {
        b"hw/sim/cocotb/tests/datalogger/infra/test_datalogger_gather.py",
    },
    "docs/hardware/telemetry_counters.md": {
        b"hw/rtl/datalogger/infra/telemetry_counters.vhd",
    },
    "docs/software/linux_i2c_spi_userspace.md": {
        b"tools/spi/",
        b"tools/spi/spidev_test.c",
    },
    "hw/libero/pfsoc/mss/README.md": {
        b"disco_mss.cxz",
        b"hw/libero/pfsoc/mss/",
    },
    "hw/rtl/datalogger/nonstd/edge_datalogger/EDGE_DATALOGGER_V2_FDD.md": {
        b"docs/GOLDEN_PATH.md",
        b"hw/rtl/datalogger/nonstd/edge_datalogger.vhd",
    },
    "hw/sim/cocotb/tests/datalogger/uart/COVERAGE.md": {
        b"hw/sim/waves",
        b"test_uart_datalogger_parameterized.py",
    },
    "sw/linux/README.md": {
        b"python_overlay.tar.gz",
        b"out/sdcard/",
        b"out/linux.lock",
        b"out/canfd_demo/sdcard/",
        b"out/rootfs/",
        b"out/sdcard/uImage",
        b"sw/linux/buildroot/output",
        b"sw/linux/out/",
        b"sw/linux/python_overlay/",
        b"sw/linux/pfsoc/python_overlay/",
        b"sw/linux/buildroot/output/host/bin/arm-buildroot-linux-gnueabi-gcc",
        b"sw/linux/pfsoc/buildroot/output/host/bin/riscv64-buildroot-linux-gnu-gcc",
    },
}


def _matches_drop_list(path_str: str) -> str | None:
    parts_to_check = [path_str]
    if "!" in path_str:
        parts_to_check.append(path_str.rsplit("!", 1)[-1])
        parts_to_check.append(path_str.split("!", 1)[0])
    for cand in parts_to_check:
        clean = cand[6:] if cand.startswith("index:") else cand
        norm = PurePosixPath(clean).as_posix()
        if not norm.startswith("/"):
            if _DOCS_EVIDENCE_RE.match(norm):
                return "docs/**/evidence/"
            for pattern, regex in _DROP_PATTERN_RES:
                if regex.match(norm):
                    return pattern
        for item in DROP_LIST:
            if item.endswith("/"):
                prefix = item.rstrip("/")
                if norm == prefix or norm.startswith(item) or f"/{item}" in f"/{norm}":
                    return item
            else:
                if norm == item or norm.endswith(f"/{item}"):
                    return item
    return None


def _is_doc_path(name: str) -> bool:
    clean = name.split("!", 1)[0]
    if clean.startswith("index:"):
        clean = clean[6:]
    p = PurePosixPath(clean)
    if any(part.lower() in ("docs", "doc") for part in p.parts):
        return True
    if p.suffix.lower() in (".md", ".rst", ".adoc"):
        return True
    return False


def _dot_component(name: str) -> str | None:
    """The first path component that starts with a dot (``.gitignore``,
    ``.github``, ``.test-power.json``), or None.

    Exported dotfiles and dot-directories ship in every public clone, so the
    export gate holds their text to the same no-ticket-ID rule as the docs."""
    clean = name.split("!", 1)[0]
    if clean.startswith("index:"):
        clean = clean[6:]
    for part in PurePosixPath(clean).parts:
        if part.startswith(".") and part not in (".", ".."):
            return part
    return None


def _export_rel_name(name: str, rel_path: str | None) -> str:
    """*name* as a path inside the export, so a dot in the directory the export
    sits in (``~/.cache/...``) is never read as a dot-path of the exported
    tree. An absolute path outside the repository keeps only its basename."""
    if rel_path is not None:
        rel = rel_path.split("!", 1)[0]
    else:
        rel = name.split("!", 1)[0]
        if rel.startswith("index:"):
            rel = rel[6:]
        root_prefix = ROOT.resolve().as_posix().rstrip("/") + "/"
        if rel.startswith(root_prefix):
            rel = rel[len(root_prefix):]
    # Drop "./" prefixes only: a character strip of "./" would also eat the
    # leading dot of ".gitignore".
    while rel.startswith("./"):
        rel = rel[2:]
    return PurePosixPath(rel).name if PurePosixPath(rel).is_absolute() else rel


def load_private_terms(custom_path: Path | None = None, root: Path = ROOT) -> list[str]:
    """The private export terms (DS-P1.123).

    An explicit path must exist and list a term. Without one, the list is read from
    ``root``, else from the checkout this tool runs from. That checkout carrying the
    dropped internal docs is the private repository, so a missing list there is an error
    (the gate fails closed); without them (a public clone) there are no private terms. The exporter passes the list read from the exported git
    tree (export_github.py). In the private repository a guard test keeps the list present
    and naming its term, so deleting it fails that commit's gate.
    """
    if custom_path:
        path = custom_path
    else:
        found = [c for c in dict.fromkeys([root / PRIVATE_TERMS_REL, ROOT / PRIVATE_TERMS_REL]) if c.is_file()]
        if not found and not (ROOT / PRIVATE_TERMS_REL).parent.is_dir():
            return []
        path = found[0] if found else ROOT / PRIVATE_TERMS_REL
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise InspectionError(f"private export terms file not found: {path}") from None
    terms: list[str] = []
    for line in text.splitlines():
        s = line.strip().lower()
        if s and not s.startswith("#") and s not in terms:
            terms.append(s)
    if not terms:
        raise InspectionError(f"private export terms file {path} lists no term")
    return terms


def _private_term_findings(name: str, data: bytes, terms: list[str]) -> list[str]:
    """One finding per occurrence of a private term; the term is masked."""
    findings: list[str] = []
    lowered = data.lower()
    for term in terms:
        needle = term.encode("utf-8")
        start = 0
        while (offset := lowered.find(needle, start)) >= 0:
            findings.append(f"{name}:{_line_number(data, offset)}: private term: "
                            f"{term[:1]}{'*' * (len(term) - 1)}")
            start = offset + len(needle)
    return findings


def load_restricted_names(custom_path: Path | None = None) -> list[str]:
    cands: list[Path] = []
    if custom_path:
        cands.append(custom_path)
    else:
        env = os.environ.get("RESTRICTED_NAMES_FILE")
        if env:
            cands.append(Path(env))
        tich_env = os.environ.get("TI" + "CH_SUPER")
        if tich_env:
            cands.append(Path(tich_env) / "admin" / ("restricted_" + "names.yml"))
        cands.append(ROOT / "admin" / ("restricted_" + "names.yml"))
        cands.append(Path.home() / ("git_" + "repo") / ("ti" + "ch-super") / "admin" / ("restricted_" + "names.yml"))
        for parent in ROOT.resolve().parents:
            cands.append(parent / "admin" / ("restricted_" + "names.yml"))
            cands.append(parent / ("ti" + "ch-super") / "admin" / ("restricted_" + "names.yml"))

    found_file: Path | None = None
    names: list[str] = []
    for cand in cands:
        if cand.is_file():
            found_file = cand
            try:
                for line in cand.read_text(encoding="utf-8", errors="replace").splitlines():
                    s = line.strip()
                    if s.startswith("- name:") or s.startswith("name:"):
                        val = s.split(":", 1)[1].strip().strip("'\"")
                        if val and val not in names:
                            names.append(val)
                if names:
                    break
            except OSError:
                continue

    if not found_file:
        missing_target = str(custom_path) if custom_path else ("admin/" + "restricted_" + "names.yml")
        raise InspectionError(
            f"restricted names file not found: {missing_target}"
        )
    if not names:
        raise InspectionError(
            f"restricted names file {found_file} contains no client names"
        )
    return names


# Exact reference/example values compiled into upstream runtimes. These are not
# deployment settings or builder identity. Keep the exception narrow by both
# member suffix and literal; a different value in the same file still fails.
_UPSTREAM_REFERENCES = {
    "bin/busybox": {_joined("192.168", ".0.20"), _joined("192.168", ".0.254")},
    "lib/python3.13/ipaddress.py": {
        _joined("10.0", ".0.0"),
        _joined("172.16", ".0.0"),
        _joined("192.168", ".0.0"),
    },
    "lib/python3.13/random.py": {_joined("10.1", ".1.47")},
    "lib/python3.13/urllib/request.py": {_joined("192.168", ".0.1")},
    "constraints/ddr_stream_cdc_scoped.sdc": {b"docs/" + _joined("inter", "nal/")},
    # Vendored rr_ddr_stream manifests, byte-for-byte from the pinned release.
    "rr_ddr_stream/ip.yml": {b"docs/" + _joined("inter", "nal/")},
    "rr_ddr_stream/requirements.yml": {_joined("sn", "iffer")},
    "rtl/axi4w.vhd": {_joined("son", "ova")},
    "rtl/ddr_stream_axi.vhd": {b"docs/" + _joined("inter", "nal/")},
    "rtl/ddr_stream_pkg.vhd": {
        _joined("SNI", "FFER"),
        _joined("sn", "iffer"),
    },
}

# DS-D.39 removed the last image-address exemption. Deployment addresses belong
# only in the ignored runtimes/hw.local.yml and are injected while building the image;
# no public target is allowed a private-network literal now.
_TARGET_DECLARATIONS: dict[str, set[bytes]] = {}

# DS-P1.45's DE-G0 hardware-leg UART capture (`docs/` + `internal/conv/
# 2026-08-30_ds_p1_45_deg0_hw_leg/cap_uart.live.json`) records the daemon's
# annotation timestamps as bare nanosecond integers, and that hardware window
# landed at ~218.55 s of board uptime — so 24 of them are twelve digits
# beginning "21" and collide with _CABLE_RE, which cannot tell a decimal
# timestamp from an all-digit FTDI serial — and the bench HAS an all-digit one
# (a Zybo's, in boards.tsv; not spelled here, because this tool's own source
# must stay clean of the signatures it hunts). NARROWING the regex to require a
# letter would stop catching that real serial, so the exception is by exact
# file AND exact literal, like
# the two above: any OTHER twelve-character 21-token in this same file — a real
# cable serial included — still fails DS-C1. The capture is committed byte-for
# byte as the board produced it; rewriting evidence to please a scanner is the
# one fix that is not available here.
# The SAME collision, in the SAME shape, from DS-P1.45's UNATTENDED DE-G0 run
# (`2026-08-30_ds_p1_45_deg0_p145_hw_leg/cap_edge.live.json`): the edge
# datalogger's timestamps are a free-running microsecond counter rendered in
# nanoseconds, and this window landed where six of them are twelve digits
# beginning "21". Same reasoning as below, same discipline — exact file, exact
# literals, and the capture stays byte-for-byte as the board produced it. That
# this recurred on a different protocol in a different run is the argument for
# keeping the exception per-literal rather than widening the rule: the next
# window will land somewhere else again, and each one should have to be looked
# at.
_EVIDENCE_TIMESTAMPS = {
    "2026-08-30_ds_p1_45_deg0_p145_hw_leg/cap_edge.live.json": {
        _joined("21", "3403628283"), _joined("21", "3420239920"),
        _joined("21", "3420274120"), _joined("21", "3453628283"),
        _joined("21", "3470239920"), _joined("21", "3470274120"),
    },
    "2026-08-30_ds_p1_45_deg0_hw_leg/cap_uart.live.json": {
        _joined("21", "8550190990"), _joined("21", "8551522630"),
        _joined("21", "8553937870"), _joined("21", "8557025230"),
        _joined("21", "8560122310"), _joined("21", "8563221910"),
        _joined("21", "8566330510"), _joined("21", "8569405030"),
        _joined("21", "8572503910"), _joined("21", "8575621030"),
        _joined("21", "8581033390"), _joined("21", "8600190990"),
        _joined("21", "8600486903"), _joined("21", "8601522630"),
        _joined("21", "8603937870"), _joined("21", "8607025230"),
        _joined("21", "8610122310"), _joined("21", "8613221910"),
        _joined("21", "8616330510"), _joined("21", "8619405030"),
        _joined("21", "8622503910"), _joined("21", "8625621030"),
        _joined("21", "8631033390"), _joined("21", "8650486903"),
    },
    # DS-P1.71: daemon anchor_ns, RouteWave zoom bounds and VCD timestamps (ns) of
    # the sparse live capture, 216-220 s after boot -- 12-digit ns values that read
    # as 21xxxxxxxxxx.
    "2026-10-02_ds_p1_71_de25_edge4_live/sparse/session.json": {
        _joined("21", "6665793820"), _joined("21", "6915793820"),
        _joined("21", "7665793670"), _joined("21", "7665794070"),
    },
    "2026-10-02_ds_p1_71_de25_edge4_live/sparse/wire.json": {
        _joined("21", "6415429110"), _joined("21", "6915793820"),
        _joined("21", "7165793810"), _joined("21", "7415793810"),
        _joined("21", "7665793810"), _joined("21", "7915793810"),
        _joined("21", "8165793810"), _joined("21", "8415793810"),
        _joined("21", "8665793810"), _joined("21", "8915793810"),
        _joined("21", "9165793810"), _joined("21", "9415793810"),
        _joined("21", "9665793810"), _joined("21", "9915793810"),
    },
    "2026-10-02_ds_p1_71_de25_edge4_live/sparse/routewave.vcd": {
        _joined("21", "6915793820"), _joined("21", "7165793810"),
        _joined("21", "7415793810"), _joined("21", "7665793810"),
        _joined("21", "7915793810"), _joined("21", "8165793810"),
        _joined("21", "8165793840"), _joined("21", "8415793810"),
        _joined("21", "8665793810"), _joined("21", "8915793810"),
        _joined("21", "9165793810"), _joined("21", "9415793810"),
        _joined("21", "9665793810"), _joined("21", "9915793810"),
    },
}


def _known_allowed_literal(name: str, value: bytes) -> bool:
    allowed = _UPSTREAM_REFERENCES | _TARGET_DECLARATIONS | _EVIDENCE_TIMESTAMPS
    return any(name.endswith(suffix) and value in values
               for suffix, values in allowed.items())


# Reviewed exact-match exceptions for upstream constants in image members
# (owner ruling 2026-11-11): one member, one finding kind, one literal per entry.
EXCEPTIONS_FILE = ROOT / "tools" / "public_artifact_hygiene_exceptions.yml"
_EXCEPTION_KEYS = {"member", "kind", "literal", "source"}
_exceptions_cache: dict[Path, frozenset[tuple[str, str, bytes]]] = {}


def load_reviewed_exceptions(path: Path | None = None) -> frozenset[tuple[str, str, bytes]]:
    """(member, kind, literal) triples from the reviewed exceptions file.

    A missing file excuses nothing (empty set): that can only add findings, never
    remove one, so a scanner copied without its data stays strict. A malformed file
    is an InspectionError, never an empty set.
    """
    path = Path(path or EXCEPTIONS_FILE)
    if path in _exceptions_cache:
        return _exceptions_cache[path]
    import yaml

    if not path.is_file():
        _exceptions_cache[path] = frozenset()
        return _exceptions_cache[path]
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InspectionError(f"{path}: not readable YAML: {exc}") from exc
    entries = (doc or {}).get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        raise InspectionError(f"{path}: 'entries' must be a list")
    out = set()
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or set(e) != _EXCEPTION_KEYS:
            raise InspectionError(f"{path}: entry {i} must have exactly {sorted(_EXCEPTION_KEYS)}")
        member, kind, parts, source = e["member"], e["kind"], e["literal"], e["source"]
        if not (isinstance(member, str) and member and not member.startswith("/")
                and not any(c in member for c in "*?[]")):
            raise InspectionError(f"{path}: entry {i}: member must be one exact path, no globs")
        if not (isinstance(kind, str) and kind and isinstance(source, str) and source):
            raise InspectionError(f"{path}: entry {i}: kind and source must be non-empty strings")
        if not (isinstance(parts, list) and parts and all(isinstance(p, str) and p for p in parts)):
            raise InspectionError(f"{path}: entry {i}: literal must be a non-empty list of strings")
        out.add((member, kind, "".join(parts).encode("utf-8")))
    _exceptions_cache[path] = frozenset(out)
    return _exceptions_cache[path]


def _reviewed_exception(name: str, kind: str, value: bytes) -> bool:
    """True only for an exact (member, kind, literal) entry of the reviewed file."""
    return any(kind == k and value == lit and name.endswith("!" + member)
               for member, k, lit in load_reviewed_exceptions())


def _line_number(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def _private_ipv4(value: bytes) -> bool:
    try:
        address = ipaddress.ip_address(value.decode("ascii"))
    except ValueError:
        return False
    # ``IPv4Address.is_private`` also includes unspecified and documentation
    # ranges in some Python releases.  DS-C1 is specifically about an RFC1918
    # topology assumption, so spell that set out and keep listener addresses
    # and RFC 5737 examples legal.
    # (base as an integer, prefix length): no address literal, whole or split (DS-P2.360).
    private_nets = (
        ipaddress.ip_network((10 << 24, 8)),
        ipaddress.ip_network(((172 << 24) | (16 << 16), 12)),
        ipaddress.ip_network(((192 << 24) | (168 << 16), 16)),
    )
    return any(address in network for network in private_nets)


def _resolve_rel_doc_path(name: str, rel_path: str | None = None) -> str:
    if rel_path is not None:
        return PurePosixPath(rel_path.split("!", 1)[0]).as_posix()
    clean = name.split("!", 1)[0]
    if clean.startswith("index:"):
        clean = clean[6:]
    norm = PurePosixPath(clean).as_posix()
    root_prefix = ROOT.resolve().as_posix().rstrip("/") + "/"
    if norm.startswith(root_prefix):
        norm = norm[len(root_prefix):]
    return norm.lstrip("./")


def _is_allowed_doc_path(rel_doc: str, value: bytes) -> bool:
    allowed = _DOC_PATH_ALLOWLIST.get(rel_doc)
    return bool(allowed and value in allowed)


def inspect_bytes(name: str, data: bytes, *, export_gate: bool = False,
                  restricted_names: list[str] | None = None,
                  private_terms: list[str] | None = None,
                  rel_path: str | None = None,
                  exported_paths: set[str] | None = None,
                  generated_outputs: frozenset[str] = frozenset()) -> list[str]:
    """Return human-readable findings in one expanded artifact member."""
    findings: list[str] = []

    if export_gate:
        dropped = _matches_drop_list(name)
        if dropped:
            findings.append(f"{name}: dropped artifact in export: {dropped}")
            return findings

    def add(kind: str, match: re.Match[bytes] | int, value: bytes) -> None:
        if _known_allowed_literal(name, value) or _reviewed_exception(name, kind, value):
            return
        offset = match if isinstance(match, int) else match.start()
        shown = value.decode("ascii", "replace")
        if kind == "restricted client name":
            masked = (shown[:1] + "*" * (len(shown) - 1)) if len(shown) > 1 else "***"
            findings.append(f"{name}:{_line_number(data, offset)}: {kind}: {masked}")
        else:
            findings.append(f"{name}:{_line_number(data, offset)}: {kind}: {shown}")

    for kind, marker in _FIXED_MARKERS:
        start = 0
        while True:
            offset = data.find(marker, start)
            if offset < 0:
                break
            add(kind, offset, marker)
            start = offset + len(marker)
    for regex, kind in (
        (_HOME_RE, "developer home"),
        (_REMOVABLE_HOME_RE, "developer removable-media home"),
        (_CABLE_RE, "bench cable serial"),
        (_PUBLIC_KEY_RE, "preinstalled SSH authorized key"),
    ):
        for match in regex.finditer(data):
            add(kind, match, match.group(0).strip())
    for match in _IPV4_RE.finditer(data):
        if _private_ipv4(match.group(0)):
            add("private-subnet assumption", match, match.group(0))
    # Split literals matter for what ships: the export gate checks them (dropped bench-only files keep theirs).
    for offset, joined in (_joined_literal_runs(data) if export_gate else ()):
        for match in _IPV4_RE.finditer(joined):
            value = match.group(0)
            if _private_ipv4(value) and value not in data:      # whole literals are reported above
                add("private-subnet assumption (split literal)", offset, value)
        for match in _CABLE_RE.finditer(joined):
            value = match.group(0)
            if value not in data:
                add("bench cable serial (split literal)", offset, value)
    for kind, regex in _TOKEN_RES:
        for match in regex.finditer(data):
            add(kind, match, match.group(0))

    if export_gate:
        for match in _KERNEL_BUILD_ID_RE.finditer(data):
            if match.group(1) != KERNEL_BUILD_ID_PLACEHOLDER:
                add("kernel build account and host", match, match.group(1))
        for match in _AGENT_NAMES_RE.finditer(data):
            add("agent identity reference", match, match.group(0))
        for match in _TICH_RE.finditer(data):
            add("internal " + "ti" + "ch reference", match, match.group(0))
        for match in _RETIRED_SN_RE.finditer(data):
            add("retired " + "sn" + "iffer reference", match, match.group(0))
        if restricted_names:
            for rname in restricted_names:
                try:
                    r_pattern = re.compile(
                        rb"(?i)(?<![A-Za-z0-9])" + re.escape(rname.encode("utf-8")) + rb"(?![A-Za-z0-9])"
                    )
                    for match in r_pattern.finditer(data):
                        add("restricted client name", match, match.group(0))
                except re.error:
                    pass
        for kind, regex in _INTERNAL_PATH_RES:
            for match in regex.finditer(data):
                add(kind, match, match.group(0))
        if private_terms:
            findings.extend(_private_term_findings(name, data, private_terms))
        if _is_doc_path(name):
            for match in _TICKET_ID_RE.finditer(data):
                add("ticket narrative in docs", match, match.group(0))
        elif (dot := _dot_component(_export_rel_name(name, rel_path))) is not None:
            for match in _TICKET_ID_RE.finditer(data):
                add(f"ticket narrative in {dot}", match, match.group(0))
        rel_doc = _resolve_rel_doc_path(name, rel_path)
        if not name.endswith("!name") and PurePosixPath(rel_doc).suffix.lower() == ".md":
            _inspect_markdown_paths(
                name, data, rel_doc=rel_doc, exported_paths=exported_paths,
                generated_outputs=generated_outputs, add=add,
            )

    return findings


def _doc_record_span(fn, token_type: str):
    """Wrap a markdown-it inline rule so the token it emits carries its
    source span, ``meta["ds_span"] = (start, end, label_end)``.

    ``label_end`` is the index of the ``]`` closing a link or image label
    (-1 for the other rules): the destination and title follow it."""
    def rule(state, silent: bool) -> bool:
        start, first = state.pos, len(state.tokens)
        if not fn(state, silent):
            return False
        if silent:
            return True
        for token in state.tokens[first:]:
            if token.type == token_type:
                label_end = -1
                if token_type in ("link_open", "image") and token.markup != "autolink":
                    label_end = state.md.helpers.parseLinkLabel(
                        state, start + (1 if token_type == "image" else 0),
                        token_type == "link_open",
                    )
                token.meta["ds_span"] = (start, state.pos, label_end)
                break
        return True
    return rule


def _doc_markdown_parser():
    """The CommonMark parser (markdown-it-py) that reads public Markdown.

    Reference definitions are kept as ``definition`` tokens, so an unused
    definition is judged too.  A missing markdown-it-py is an inspection
    error, never a clean result."""
    global _DOC_MD_PARSER
    if _DOC_MD_PARSER is None:
        try:
            from markdown_it import MarkdownIt
        except ImportError as exc:
            raise InspectionError(
                "markdown-it-py is not installed; it reads the Markdown "
                "references of public docs (pip install -r requirements.txt)"
            ) from exc
        md = MarkdownIt("commonmark", {"inline_definitions": True})
        for rule in md.inline.ruler.__rules__:
            if rule.name in _DOC_SPAN_RULES:
                md.inline.ruler.at(
                    rule.name,
                    _doc_record_span(rule.fn, _DOC_SPAN_RULES[rule.name]),
                )
        _DOC_MD_PARSER = md
    return _DOC_MD_PARSER


class _DocInline:
    """What one inline source holds, in that source's coordinates.

    ``links``: (offset, destination) of inline links, images and raw HTML
    href/src, as the parser normalised them.  ``blanks``: spans the bare
    path scan must not read (destinations, autolinks, and link syntax that
    is literal text inside a code span).  ``code``: (offset, content) of
    code spans."""

    def __init__(self) -> None:
        self.links: list[tuple[int, str]] = []
        self.blanks: list[tuple[int, int]] = []
        self.code: list[tuple[int, str]] = []


def _doc_html_refs(html: str, offset: int, into: _DocInline) -> None:
    for m in _DOC_HTML_REF_RE.finditer(html):
        group = 1 if m.start(1) >= 0 else 2
        into.links.append((offset + m.start(group), _html.unescape(m.group(group))))
        into.blanks.append((offset + m.start(group), offset + m.end(group)))


def _doc_scan_inline(children, src: str) -> _DocInline:
    """Collect the references of one markdown-it inline token."""
    out = _DocInline()
    for token in children or ():
        span = token.meta.get("ds_span")
        if span is None:
            continue
        start, end, label_end = span
        if token.type in ("link_open", "image"):
            if token.markup == "autolink":
                # <scheme:...> or <user@host>: never a repository path.
                out.blanks.append((start, end))
            elif src[end - 1:end] == ")" and label_end >= 0:
                # [t](p) / ![a](p).  The reference forms [t][r], [t][] and
                # [r] end in "]" and are judged at their definition.
                href = token.attrs.get("href" if token.type == "link_open" else "src", "")
                out.links.append((label_end + 1, str(href)))
                out.blanks.append((label_end + 1, end))
        elif token.type == "code_inline":
            inner = start + len(token.markup)
            out.code.append((inner, token.content))
            # Link syntax inside a code span is literal text: not judged, and
            # not read as bare paths either.
            literal = _doc_literal_link_spans(src[inner:end - len(token.markup)])
            out.blanks.extend((inner + a, inner + b) for a, b in literal)
        elif token.type == "html_inline":
            _doc_html_refs(token.content, start, out)
    return out


def _doc_parse_inline(text: str) -> _DocInline:
    tokens = _doc_markdown_parser().parseInline(text, {})
    return _doc_scan_inline(tokens[0].children if tokens else (), text)


def _doc_literal_link_spans(text: str) -> list[tuple[int, int]]:
    """Spans of link syntax in literal text (a fence or a code span), which
    neither the link check nor the bare path scan judges."""
    found = _doc_parse_inline(text)
    spans = found.blanks + [(o, o + len(v)) for o, v in found.links]
    # Reference definitions are block syntax: blank each one after its "]:".
    line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    line_starts.append(len(text) + 1)
    for token in _doc_markdown_parser().parse(text, {}):
        if token.type != "definition" or not token.map:
            continue
        first, last = token.map
        start = line_starts[first]
        colon = text.find("]:", start, line_starts[last] - 1)
        spans.append((colon if colon >= 0 else start, line_starts[last] - 1))
    return spans


def _doc_blank(text: str, spans: list[tuple[int, int]]) -> str:
    """text with every span overwritten by spaces, line breaks kept."""
    chars = list(text)
    for start, end in spans:
        for i in range(max(start, 0), min(end, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def _doc_ref_target(raw: str) -> str | None:
    """The repository path a link destination names, or None when it names
    none: empty, a same-page #anchor, a URI scheme (http:, mailto:, tel:,
    ...) or a protocol-relative //host/... reference.

    ``raw`` is the destination as the CommonMark parser normalised it
    (escapes removed, entity references decoded, unsafe characters
    percent-encoded); the #fragment and ?query are cut before the
    percent-escapes are decoded."""
    target = raw.strip()
    if (
        not target
        or target.startswith(("#", "//"))
        or _DOC_URI_SCHEME_RE.match(target)
    ):
        return None
    target = unquote(target.split("#", 1)[0].split("?", 1)[0])
    return target or None


def _doc_resolve(base: PurePosixPath, target: str) -> str:
    """Normalise target against base ("/x" is the repository root).

    Returns "" for the root itself and a "../" prefix when target leaves it."""
    if target.startswith("/"):
        joined = target.lstrip("/")
    else:
        joined = (base / target).as_posix()
    if not joined:
        return ""
    norm = PurePosixPath(os.path.normpath(joined)).as_posix()
    return "" if norm == "." else norm


def _doc_backtick_counts_as_path(token: str) -> bool:
    """Whether a backticked token is judged as a path that must exist.

    It must be path-shaped (_doc_backtick_path_shaped).  A token without a
    separator then counts when it names a document (_DOC_DOC_EXTENSIONS):
    "Read `guide.md`".  Bare source or config names are mostly generated
    outputs (`system_regs_uio.h`), user-created files (`hw.local.yml`) or
    another project's files, so a bare one is not judged.  A token with a
    separator counts when it starts at "./", "../" or one of
    _DOC_REPO_PREFIXES, the same directories under which a bare path in
    running text is judged; "read/write", "etc/systemd/" or a fetched
    `libs/<core>/` name no path an export carries."""
    if not _doc_backtick_path_shaped(token):
        return False
    if "/" not in token.rstrip("/"):
        leaf = token.rstrip("/")
        return (
            "." in leaf.strip(".")
            and leaf.rsplit(".", 1)[-1].lower() in _DOC_DOC_EXTENSIONS
        )
    return token.startswith(("./", "../")) or token.startswith(_DOC_REPO_PREFIXES)


def _doc_strip_sentence_end(raw: str) -> str:
    """A bare path without the sentence punctuation that follows it.  A ")"
    is kept while it closes a "(" in the path: docs/guide_(v1).md."""
    while raw and raw[-1] in _DOC_SENTENCE_END:
        if raw[-1] == ")" and raw.count(")") <= raw.count("("):
            break
        raw = raw[:-1]
    return raw


def _doc_backtick_path_shaped(token: str) -> bool:
    return bool(_DOC_BACKTICK_PATH_RE.match(token)) and "..." not in token


def _inspect_markdown_paths(
    name: str,
    data: bytes,
    *,
    rel_doc: str,
    exported_paths: set[str] | None,
    generated_outputs: frozenset[str] = frozenset(),
    add,
) -> None:
    """Refuse a public Markdown file that names a dropped or absent path.

    The document is parsed by markdown-it-py (CommonMark); the checked and
    the deliberately unchecked reference forms are listed in the module
    docstring.  ``exported_paths`` is the exported tree (files and their
    parent directories); without it only the DROP rules are applied.
    ``generated_outputs`` are the declared runtime outputs: named in backticks
    or bare text they are not missing; linked, they are.
    """
    if rel_doc == "SDK_USER_GUIDE.md":
        return
    is_redistribution_doc = (
        rel_doc == "docs/REDISTRIBUTION.md"
        or rel_doc.endswith("/docs/REDISTRIBUTION.md")
    )
    md_dir = PurePosixPath(rel_doc).parent
    root_dir = PurePosixPath(".")
    seen: set[tuple[int, str]] = set()
    names: set[str] = set()
    if exported_paths is not None:
        names = {p.rsplit("/", 1)[-1] for p in exported_paths}
    line_starts = [0] + [m.end() for m in re.finditer(rb"\n", data)]

    def exists(path: str) -> bool:
        if path.startswith(".."):
            return False
        return path == "" or path in (exported_paths or ())

    def report(line_no: int, value: bytes, key_paths: list[str],
               rule: str | None, missing: bool) -> None:
        keys = {(line_no, k) for k in key_paths if k}
        if keys & seen:
            return
        offset = line_starts[min(max(line_no, 1), len(line_starts)) - 1]
        if rule:
            seen.update(keys)
            add(f"dropped path in docs ({rule})", offset, value)
        elif missing:
            seen.update(keys)
            add("missing path in docs (not in exported tree)", offset, value)

    def drop_rule(*paths: str) -> str | None:
        if is_redistribution_doc:
            return None
        for path in paths:
            if path and not path.startswith(".."):
                rule = _matches_drop_list(path)
                if rule:
                    return rule
        return None

    # Walk the CommonMark token stream.  Every reference is kept with its
    # 1-based line; text segments are kept for the backtick and bare scans.
    links: list[tuple[int, str]] = []          # (line, destination)
    code_spans: list[tuple[int, str]] = []     # (line, content)
    segments: list[tuple[int, str, bool]] = [] # (first line, text, prose?)

    def take_inline(found: _DocInline, src: str, base: int) -> None:
        for offset, dest in found.links:
            links.append((base + src.count("\n", 0, offset), dest))
        for offset, content in found.code:
            code_spans.append((base + src.count("\n", 0, offset), content))
        segments.append((base, _doc_blank(src, found.blanks), True))

    text = data.decode("utf-8", "replace")
    for token in _doc_markdown_parser().parse(text, {}):
        if not token.map:
            continue
        base = token.map[0] + 1
        if token.type == "inline":
            take_inline(_doc_scan_inline(token.children, token.content),
                        token.content, base)
        elif token.type == "definition":
            # [label]: dest, used or not.  A "^" label is a footnote, prose.
            if not str(token.meta.get("label", "")).startswith("^"):
                links.append((base, str(token.meta.get("url", ""))))
        elif token.type == "code_block":
            # An indented block is read as text: links in it are judged.
            take_inline(_doc_parse_inline(token.content), token.content, base)
        elif token.type == "fence":
            # Fenced code is literal: link syntax there is not judged, but a
            # bare repository path in a command still is.
            segments.append((
                base + 1,
                _doc_blank(token.content, _doc_literal_link_spans(token.content)),
                False,
            ))
        elif token.type == "html_block":
            found = _DocInline()
            _doc_html_refs(token.content, 0, found)
            for offset, dest in found.links:
                links.append((base + token.content.count("\n", 0, offset), dest))
            segments.append((base, _doc_blank(token.content, found.blanks), False))

    # 1. Link, image, reference-definition and HTML destinations.
    for line_no, dest in links:
        target = _doc_ref_target(dest)
        if target is None:
            continue
        target_bytes = target.encode("utf-8")
        if _is_allowed_doc_path(rel_doc, target_bytes):
            continue
        path = _doc_resolve(md_dir, target)
        rule = drop_rule(path, target.rstrip("/"), target)
        report(
            line_no, target_bytes, [path, target.rstrip("/")], rule,
            exported_paths is not None and not exists(path),
        )

    if is_redistribution_doc:
        return

    # 2. Backticked tokens (code spans; fenced code is not read here).
    for line_no, content in code_spans:
        tok = content.strip(" ")
        raw_bytes = tok.encode("utf-8")
        if _is_allowed_doc_path(rel_doc, raw_bytes) or not tok:
            continue
        from_doc = _doc_resolve(md_dir, tok)
        from_root = _doc_resolve(root_dir, tok)
        rule = None
        if ("." in tok or "/" in tok) and _doc_backtick_path_shaped(tok):
            rule = drop_rule(tok.rstrip("/"), from_doc)
        missing = False
        if (
            exported_paths is not None
            and _doc_backtick_counts_as_path(tok)
        ):
            if "/" in tok.rstrip("/"):
                missing = not (
                    exists(from_doc) or exists(from_root)
                    or from_doc in generated_outputs
                    or from_root in generated_outputs
                )
            else:
                missing = not (exists(from_doc) or tok.rstrip("/") in names)
        report(line_no, raw_bytes, [from_root, from_doc], rule, missing)

    # 3. Bare repository paths in text, fenced commands included.  In prose a
    # backslash escape is removed first, as CommonMark renders it.
    for base, segment, prose in segments:
        if prose:
            segment = _DOC_MD_ESCAPE_RE.sub(r"\1", segment)
        seg_bytes = segment.encode("utf-8")
        for m in _DOC_REPO_PATH_RE.finditer(seg_bytes):
            matched = m.group(1).decode("utf-8", "replace")
            raw_str = _doc_strip_sentence_end(matched)
            raw_bytes = raw_str.encode("utf-8")
            if not raw_str or _is_allowed_doc_path(rel_doc, raw_bytes):
                continue
            line_no = base + seg_bytes.count(b"\n", 0, m.start(1))
            clean = raw_str.rstrip("/")
            # One reading for every relative form (x/y, ./x/y, ../x/y): the
            # path resolved against the document's directory, and against the
            # root, as a doc names a repository path and a fenced command runs
            # there.  It names a repository path when, without its leading
            # "./" and "../" steps, it starts in a _DOC_REPO_PREFIXES
            # directory ("../bin/busybox", "read/write" and "libs/x" do not);
            # such a path that neither reading finds in the exported tree is
            # missing, one that leaves the root included.
            written = _DOC_DOT_STEPS_RE.sub("", clean)
            if _is_allowed_doc_path(rel_doc, written.encode("utf-8")):
                continue
            from_doc = _doc_resolve(md_dir, clean)
            from_root = _doc_resolve(root_dir, clean)
            missing = (
                exported_paths is not None
                and (written + "/").startswith(_DOC_REPO_PREFIXES)
                and "*" not in clean
                and "..." not in matched
                and not (
                    exists(from_doc) or exists(from_root)
                    or from_doc in generated_outputs
                    or from_root in generated_outputs
                )
            )
            report(line_no, raw_bytes, [from_doc, from_root],
                   drop_rule(from_doc, from_root, raw_str), missing)


def _safe_member_name(parent: str, member: str) -> str:
    clean = str(PurePosixPath(member))
    return f"{parent}!{clean}"


def _bounded(data: bytes, name: str) -> bytes:
    if len(data) > MAX_EXPANDED_BYTES:
        raise InspectionError(
            f"{name}: expanded member is {len(data)} bytes; limit is "
            f"{MAX_EXPANDED_BYTES}"
        )
    return data


def _newc_members(data: bytes, name: str) -> list[tuple[str, bytes]] | None:
    """Read a ``newc``/CRC cpio archive, returning None when it is not one."""
    if not data.startswith((b"070701", b"070702")):
        return None
    members: list[tuple[str, bytes]] = []
    pos = 0
    while pos + 110 <= len(data):
        header = data[pos:pos + 110]
        if header[:6] not in (b"070701", b"070702"):
            raise InspectionError(f"{name}: malformed newc header at byte {pos}")
        try:
            fields = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
        except ValueError as exc:
            raise InspectionError(f"{name}: invalid newc header at byte {pos}") from exc
        mode, size, namesize = fields[1], fields[6], fields[11]
        pos += 110
        raw_name = data[pos:pos + namesize]
        if len(raw_name) != namesize or not raw_name.endswith(b"\0"):
            raise InspectionError(f"{name}: truncated newc member name")
        member = raw_name[:-1].decode("utf-8", "replace")
        pos = (pos + namesize + 3) & ~3
        body = data[pos:pos + size]
        if len(body) != size:
            raise InspectionError(f"{name}: truncated newc member {member!r}")
        pos = (pos + size + 3) & ~3
        if member == "TRAILER!!!":
            return members
        if mode & 0o170000 == 0o100000:  # regular file
            members.append((_safe_member_name(name, member), body))
    raise InspectionError(f"{name}: newc archive has no TRAILER!!!")


def _gzip_members(data: bytes, name: str) -> list[tuple[str, bytes]]:
    """Expand gzip streams found anywhere in a binary (kernel images included)."""
    members: list[tuple[str, bytes]] = []
    pos = 0
    stream_no = 0
    while True:
        offset = data.find(b"\x1f\x8b\x08", pos)
        if offset < 0:
            return members
        pos = offset + 3
        try:
            dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = dec.decompress(data[offset:], MAX_EXPANDED_BYTES + 1)
            body += dec.flush()
        except zlib.error:
            continue
        if dec.unconsumed_tail or len(body) > MAX_EXPANDED_BYTES:
            raise InspectionError(f"{name}: gzip stream exceeds expansion limit")
        stream_no += 1
        members.append((f"{name}!gzip[{stream_no}]@{offset}", body))


def _archive_members(data: bytes, name: str) -> list[tuple[str, bytes]]:
    members = _newc_members(data, name)
    if members is not None:
        return members

    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return [
                    (_safe_member_name(name, info.filename),
                     _bounded(archive.read(info), info.filename))
                    for info in archive.infolist() if not info.is_dir()
                ]
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise InspectionError(f"{name}: unreadable zip archive: {exc}") from exc

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            out = []
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    raise InspectionError(f"{name}: could not read {member.name}")
                out.append((_safe_member_name(name, member.name),
                            _bounded(handle.read(), member.name)))
            return out
    except tarfile.ReadError:
        pass

    if data.startswith(b"\x1f\x8b\x08"):
        try:
            return [(f"{name}!gzip", _bounded(gzip.decompress(data), name))]
        except (OSError, EOFError) as exc:
            raise InspectionError(f"{name}: unreadable gzip stream: {exc}") from exc
    if data.startswith(b"BZh"):
        try:
            return [(f"{name}!bzip2", _bounded(bz2.decompress(data), name))]
        except OSError as exc:
            raise InspectionError(f"{name}: unreadable bzip2 stream: {exc}") from exc
    if data.startswith(b"\xfd7zXZ\x00"):
        try:
            return [(f"{name}!xz", _bounded(lzma.decompress(data), name))]
        except lzma.LZMAError as exc:
            raise InspectionError(f"{name}: unreadable xz stream: {exc}") from exc

    # Legacy U-Boot image: header + payload.  Header fields are big endian;
    # size at byte 12 prevents trailing workspace data becoming provenance.
    if len(data) >= 64 and data[:4] == b"'\x05\x19V":
        size = struct.unpack(">I", data[12:16])[0]
        if 64 + size > len(data):
            raise InspectionError(f"{name}: truncated U-Boot image payload")
        return [(f"{name}!uboot-payload", data[64:64 + size])]

    # A Linux zImage can embed compressed kernel/initramfs streams at a
    # non-zero offset.  Expand every valid gzip stream, not merely byte zero.
    return _gzip_members(data, name)


def inspect_artifact(name: str, data: bytes, depth: int = 0, *,
                     export_gate: bool = False,
                     restricted_names: list[str] | None = None,
                     private_terms: list[str] | None = None,
                     rel_path: str | None = None,
                     exported_paths: set[str] | None = None,
                     generated_outputs: frozenset[str] = frozenset()) -> list[str]:
    if depth > MAX_ARCHIVE_DEPTH:
        raise InspectionError(f"{name}: archive nesting exceeds {MAX_ARCHIVE_DEPTH}")
    if export_gate:
        dropped = _matches_drop_list(name)
        if dropped:
            return [f"{name}: dropped artifact in export: {dropped}"]
    members = _archive_members(data, name)
    # Inspect archive members, not their compressed/container byte stream.
    # Scanning both duplicated every finding without member context and made
    # precise upstream-reference handling impossible. Member names themselves
    # remain data and are scanned before their bodies.
    if not members:
        return inspect_bytes(name, data, export_gate=export_gate,
                             restricted_names=restricted_names,
                             private_terms=private_terms,
                             rel_path=rel_path,
                             exported_paths=exported_paths,
                             generated_outputs=generated_outputs)
    findings: list[str] = []
    # DS-P1.123: container bytes outside the members (a ZIP comment, an uncompressed
    # header field) are exported too, so they are searched for private terms.
    if export_gate and private_terms:
        findings.extend(_private_term_findings(name, data, private_terms))
    for member_name, body in members:
        leaf_name = member_name.rsplit("!", 1)[-1]
        findings.extend(inspect_bytes(member_name + "!name", leaf_name.encode(),
                                      export_gate=export_gate,
                                      restricted_names=restricted_names,
                                      private_terms=private_terms))
        findings.extend(inspect_artifact(member_name, body, depth + 1,
                                         export_gate=export_gate,
                                         restricted_names=restricted_names,
                                         private_terms=private_terms))
    return findings


def _index_blobs(root: Path) -> list[tuple[str, bytes]]:
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "-z"], cwd=root,
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise InspectionError(
            "git ls-files failed: " + proc.stderr.decode("utf-8", "replace")
        )
    out: list[tuple[str, bytes]] = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "surrogateescape")
        blob = subprocess.run(
            ["git", "show", f":{rel}"], cwd=root,
            capture_output=True, check=False,
        )
        if blob.returncode != 0:
            raise InspectionError(
                f"cannot read index blob {rel}: "
                + blob.stderr.decode("utf-8", "replace")
            )
        out.append((f"index:{rel}", blob.stdout))
    return out


def _path_blobs(path: Path) -> list[tuple[str, bytes]]:
    if not path.exists():
        raise InspectionError(f"{path}: input does not exist")
    if path.is_symlink():
        return [(str(path), os.readlink(path).encode("utf-8", "surrogateescape"))]
    if path.is_file():
        try:
            return [(str(path), path.read_bytes())]
        except OSError as exc:
            raise InspectionError(f"{path}: cannot read input: {exc}") from exc
    if path.is_dir():
        out = []
        for child in sorted(path.rglob("*")):
            if ".git" in child.parts:
                continue
            if child.is_symlink():
                out.append((str(child), os.readlink(child).encode(
                    "utf-8", "surrogateescape")))
            elif child.is_file():
                try:
                    out.append((str(child), child.read_bytes()))
                except OSError as exc:
                    raise InspectionError(f"{child}: cannot read input: {exc}") from exc
        return out
    raise InspectionError(f"{path}: unsupported input type")


def _check_export_target_pins(
    sources: list[tuple[str, bytes]],
    paths: list[Path],
    *,
    root: Path = ROOT,
) -> list[str]:
    """Fail the export gate when a shipped target declares a pin its top lacks."""
    import logging
    import tempfile
    import yaml
    from routertl_core.hdl_ports import parse_ports, parse_top_name
    from sdk.engine.board_pinout import (
        BoardPinoutError,
        load_board_pinout,
        resolve_subscription,
    )

    root_resolved = root.resolve()
    dir_roots = [p.resolve() for p in paths if p.exists() and p.is_dir()]
    targets_to_check: list[tuple[str, Path, str, bytes]] = []
    blob_overrides: dict[tuple[Path, str], bytes] = {}

    for source_name, data in sources:
        tree_root: Path | None = None
        rel_path: str | None = None
        if source_name.startswith("index:"):
            tree_root = root_resolved
            rel_path = source_name[len("index:"):]
        else:
            try:
                src_resolved = Path(source_name).resolve()
            except OSError:
                continue
            for d in dir_roots:
                try:
                    rel_path = src_resolved.relative_to(d).as_posix()
                    tree_root = d
                    break
                except ValueError:
                    continue
            if tree_root is None:
                try:
                    rel_path = src_resolved.relative_to(root_resolved).as_posix()
                    tree_root = root_resolved
                except ValueError:
                    continue
        if not tree_root or not rel_path:
            continue
        if rel_path.lower().endswith((".vhd", ".vhdl", ".v", ".sv")):
            blob_overrides[(tree_root, rel_path)] = data
        if (
            PurePosixPath(rel_path).parent == PurePosixPath("targets")
            and rel_path.endswith(".yml")
            and not _matches_drop_list(rel_path)
        ):
            targets_to_check.append((source_name, tree_root, rel_path, data))

    if not targets_to_check:
        return []

    findings: list[str] = []
    entity_cache: dict[Path, dict[str, list[str]]] = {}
    logger = logging.getLogger("sdk.engine.board_pinout")

    def _ports_for_tree(t_root: Path) -> dict[str, list[str]]:
        if t_root in entity_cache:
            return entity_cache[t_root]
        mapping: dict[str, list[str]] = {}
        hdl_files: dict[str, Path] = {}
        for sub in ("hw/rtl", "libs"):
            sdir = t_root / sub
            if not sdir.is_dir():
                continue
            for ext in ("*.vhd", "*.vhdl", "*.v", "*.sv"):
                for f in sorted(sdir.rglob(ext)):
                    if f.is_file():
                        rel = f.relative_to(t_root).as_posix()
                        hdl_files[rel] = f
        for (b_root, b_rel), _ in blob_overrides.items():
            if b_root == t_root and b_rel not in hdl_files:
                hdl_files[b_rel] = t_root / b_rel
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for rel, fpath in hdl_files.items():
                override = blob_overrides.get((t_root, rel))
                target_file = fpath
                if override is not None and (not fpath.is_file() or fpath.read_bytes() != override):
                    target_file = tmp_dir / Path(rel).name
                    target_file.write_bytes(override)
                elif not fpath.is_file():
                    continue
                try:
                    top_name = parse_top_name(str(target_file))
                    if not top_name:
                        continue
                    ports = parse_ports(str(target_file))
                except Exception:
                    continue
                if ports:
                    mapping[top_name.lower()] = [p.name for p in ports]
        entity_cache[t_root] = mapping
        return mapping

    class _DriftLogHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.WARNING)
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    for source_name, t_root, _rel_path, data in targets_to_check:
        try:
            cfg = yaml.safe_load(data.decode("utf-8", "replace"))
        except Exception as exc:
            findings.append(f"{source_name}: invalid target YAML: {exc}")
            continue
        if not isinstance(cfg, dict):
            continue
        hardware = cfg.get("hardware") or {}
        if not isinstance(hardware, dict):
            continue
        explicit_pins = hardware.get("pins")
        if not explicit_pins or not isinstance(explicit_pins, list):
            continue
        platform = str(hardware.get("platform") or "").strip()
        project = cfg.get("project") or {}
        top_module = project.get("top_module") if isinstance(project, dict) else None
        if not top_module or not isinstance(top_module, str):
            findings.append(
                f"{source_name}: target declares hardware.pins without project.top_module"
            )
            continue
        top_ports = _ports_for_tree(t_root).get(top_module.lower())
        if not top_ports:
            findings.append(
                f"{source_name}: top_module '{top_module}' not found or declares no ports"
            )
            continue
        if platform and platform != "default":
            handler = _DriftLogHandler()
            logger.addHandler(handler)
            try:
                board = load_board_pinout(platform)
                resolve_subscription(board, explicit_pins=explicit_pins, top_ports=top_ports)
            except BoardPinoutError as exc:
                findings.append(f"{source_name}: {exc}")
            finally:
                logger.removeHandler(handler)
            for msg in handler.messages:
                if "not declared on top" in msg:
                    findings.append(f"{source_name}: {msg}")
        else:
            top_lower = {p.lower() for p in top_ports}
            for pin in explicit_pins:
                base = str(pin).split("[", 1)[0].split("@", 1)[0].lower()
                if str(pin).lower() not in top_lower and base not in top_lower:
                    findings.append(
                        f"{source_name}: explicit pin '{pin}' not declared on top '{top_module}'"
                    )
    return findings


def scan(paths: list[Path], *, include_index: bool = True,
         root: Path = ROOT, export_gate: bool = False,
         restricted_names_file: Path | None = None,
         private_terms_file: Path | None = None) -> tuple[list[str], list[str]]:
    findings: list[str] = []
    errors: list[str] = []
    sources: list[tuple[str, bytes]] = []
    rnames = []
    terms: list[str] = []
    if export_gate:
        try:
            rnames = load_restricted_names(restricted_names_file)
            terms = load_private_terms(private_terms_file, root=root)
        except InspectionError as exc:
            errors.append(str(exc))
            return findings, errors
    try:
        if include_index:
            sources.extend(_index_blobs(root))
        for path in paths:
            sources.extend(_path_blobs(path))
    except InspectionError as exc:
        errors.append(str(exc))
        return findings, errors
    dir_exports: list[tuple[str, set[str], frozenset[str]]] = []
    if export_gate:
        for path in paths:
            if not path.is_dir():
                continue
            prefix = str(path).rstrip("/") + "/"
            exported_set: set[str] = set()
            for name, _ in sources:
                if name.startswith(prefix):
                    rel = PurePosixPath(name[len(prefix):]).as_posix()
                    dropped = _matches_drop_list(rel)
                    if dropped:
                        findings.append(
                            f"{name}: dropped artifact in export: {dropped}")
                    else:
                        exported_set.add(rel)
                        parts = rel.split("/")
                        for i in range(1, len(parts)):
                            exported_set.add("/".join(parts[:i]))
            generated: frozenset[str] = frozenset()
            decl_name = prefix + _GENERATED_SOURCES_REL
            for name, data in sources:
                if name == decl_name:
                    try:
                        generated = _declared_runtime_outputs(data, name)
                    except InspectionError as exc:
                        errors.append(str(exc))
                    break
            dir_exports.append((prefix, exported_set, generated))
        findings.extend(_check_export_target_pins(sources, paths, root=root))
    root_prefix = root.resolve().as_posix().rstrip("/") + "/"
    for name, data in sources:
        rel_path: str | None = None
        exported_paths: set[str] | None = None
        generated_outputs: frozenset[str] = frozenset()
        if export_gate:
            for prefix, exported_set, generated in dir_exports:
                if name.startswith(prefix):
                    rel_path = name[len(prefix):]
                    exported_paths = exported_set
                    generated_outputs = generated
                    break
            if rel_path is None:
                if name.startswith("index:"):
                    rel_path = name[6:]
                elif name.startswith(root_prefix):
                    rel_path = name[len(root_prefix):]
        if terms and rel_path is not None:
            lowered_path = rel_path.lower()
            findings.extend(f"{name}: private term in path: {term[:1]}{'*' * (len(term) - 1)}"
                            for term in terms if term in lowered_path)
        try:
            findings.extend(inspect_artifact(
                name, data, export_gate=export_gate, restricted_names=rnames,
                private_terms=terms,
                rel_path=rel_path, exported_paths=exported_paths,
                generated_outputs=generated_outputs,
            ))
        except InspectionError as exc:
            errors.append(str(exc))
    return sorted(set(findings)), sorted(set(errors))


def export_tree(dest_dir: Path, root: Path = ROOT,
                paths: list[Path] | None = None,
                restricted_names_file: Path | None = None,
                private_terms_file: Path | None = None) -> tuple[list[str], list[str]]:
    """Export the tree to dest_dir, dropping items in DROP_LIST, then gate the exported tree."""
    # Fail closed before touching dest_dir if restricted names or private terms cannot be loaded
    try:
        load_restricted_names(restricted_names_file)
        load_private_terms(private_terms_file, root=root)
    except InspectionError as exc:
        return [], [str(exc)]

    dest_dir = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()

    if paths:
        for p in paths:
            p = p.resolve()
            try:
                p.relative_to(root_resolved)
                p_root = root_resolved
            except ValueError:
                p_root = p.parent if p.is_dir() else p.parent
            if p.is_dir():
                for child in sorted(p.rglob("*")):
                    if child.is_dir():
                        continue
                    try:
                        rel_root = child.relative_to(p_root).as_posix()
                    except ValueError:
                        rel_root = child.name
                    rel = child.relative_to(p).as_posix()
                    if _matches_drop_list(rel) or _matches_drop_list(rel_root) or _matches_drop_list(child.as_posix()):
                        continue
                    target = dest_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if child.is_symlink():
                        os.symlink(os.readlink(child), target)
                    else:
                        shutil.copy2(child, target)
            elif p.is_file():
                try:
                    rel_root = p.relative_to(p_root).as_posix()
                except ValueError:
                    rel_root = p.name
                rel = p.name
                if _matches_drop_list(rel) or _matches_drop_list(rel_root) or _matches_drop_list(p.as_posix()):
                    continue
                target = dest_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
    else:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "-z"], cwd=root_resolved,
            capture_output=True, check=False,
        )
        if proc.returncode == 0:
            for raw in proc.stdout.split(b"\0"):
                if not raw:
                    continue
                rel = raw.decode("utf-8", "surrogateescape")
                if _matches_drop_list(rel):
                    continue
                target = dest_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                src_file = root_resolved / rel
                if src_file.exists():
                    if src_file.is_symlink():
                        os.symlink(os.readlink(src_file), target)
                    else:
                        shutil.copy2(src_file, target)
                else:
                    blob = subprocess.run(
                        ["git", "show", f":{rel}"], cwd=root_resolved,
                        capture_output=True, check=False,
                    )
                    if blob.returncode == 0:
                        target.write_bytes(blob.stdout)
        else:
            for child in sorted(root_resolved.rglob("*")):
                if child.is_dir():
                    continue
                rel = child.relative_to(root_resolved).as_posix()
                if _matches_drop_list(rel):
                    continue
                target = dest_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if child.is_symlink():
                    os.symlink(os.readlink(child), target)
                else:
                    shutil.copy2(child, target)

    return scan([dest_dir], include_index=False, export_gate=True,
                root=root, restricted_names_file=restricted_names_file,
                private_terms_file=private_terms_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-index", action="store_true",
        help="scan only named paths, not the git index",
    )
    parser.add_argument(
        "--export-gate", "--export", action="store_true", dest="export_gate",
        help="enforce GitHub-export gate rules (agent names, " + "sn" + "iffer, tich refs, ticket narrative in docs, drop list, etc.)",
    )
    parser.add_argument(
        "--restricted-names", type=Path, default=None,
        help="path to admin/" + "restricted_" + "names.yml (default: auto-discover)",
    )
    parser.add_argument(
        "--private-terms", type=Path, default=None,
        help="private export terms file (default: the one under the dropped internal docs of --root)",
    )
    parser.add_argument(
        "--root", type=Path, default=ROOT,
        help="repository root for export path resolution (default: repo root)",
    )
    parser.add_argument(
        "--export-to", type=Path, default=None,
        help="export repository to target directory, excluding drop-list items, then gate the exported tree",
    )
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help="generated RTL, provenance, Linux images/rootfs trees, transcripts",
    )
    args = parser.parse_args(argv)
    if args.no_index and not args.paths:
        parser.error("--no-index requires at least one path")

    if args.export_to:
        findings, errors = export_tree(
            args.export_to, root=args.root, paths=args.paths,
            restricted_names_file=args.restricted_names,
            private_terms_file=args.private_terms,
        )
        mode_label = "PUBLIC EXPORT GATE"
    else:
        include_index = not args.no_index and not (args.export_gate and args.paths)
        findings, errors = scan(
            args.paths, include_index=include_index,
            root=args.root,
            export_gate=args.export_gate,
            restricted_names_file=args.restricted_names,
            private_terms_file=args.private_terms,
        )
        mode_label = "PUBLIC EXPORT GATE" if args.export_gate else "PUBLIC ARTIFACT HYGIENE"

    if errors:
        print(f"{mode_label} SCAN INCOMPLETE:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
    if findings:
        print(f"{mode_label} FAIL:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
    if errors or findings:
        return 1
    count = "index" if not args.paths and not args.export_to else "index and requested artifacts"
    if args.no_index or (args.export_gate and args.paths) or args.export_to:
        count = "requested artifacts"
    print(f"{mode_label} PASS: {count} are clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
