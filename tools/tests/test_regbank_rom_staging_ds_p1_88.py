# rr unknown-llm (AI-generated)
"""DS-P1.88: every target that builds the regbank ROMs gets its hex files into the tool's run directory.

system_regbank_wrapper reads hw/rtl/regbank/regbank_schema.hex and
config_schema.hex with VHDL textio at elaboration (generics default to
rom_info_pkg's C_REGBANK_HEX / C_CONFIG_HEX, clone-relative paths). A vendor
tool resolves them against its own run directory. Measured 2026-11-05 on
PolarFire (queue job dbb840b8f23f): Synplify could not open them (CD695/CD677),
warned instead of failing, and both ROMs reduced to constants (CL189).

A target reaches the files in one of two ways: rr stages them because the target
declares them under ``sources.data`` (RTL-P1.297), or a pre-synthesis hook copies
them (the Zybo and DE25 stage_regbank_roms_pre.tcl). This test refuses a target
whose top builds the ROMs and that has neither, for product and bench targets
alike. Red-first fixture: the PolarFire target as it was before 81ab7770.

A target "builds the ROMs" when its top_module reaches regbank_rom through
instantiations under hw/, comments ignored. A one-level name match on the top's
file missed bench pfsoc_fic0_rea and held the PolarFire product only through a
comment once DS-P2.326 moved deepskopion_core into deepskopion_pfsoc_fabric.
"""
from __future__ import annotations

import functools
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files, has_bench, product_target_files, target_file  # noqa: E402

HEX_FILES = ("hw/rtl/regbank/regbank_schema.hex", "hw/rtl/regbank/config_schema.hex")
STAGING_HOOK = re.compile(r"stage_regbank_roms[^/]*\.tcl$")


def _target_files() -> list[Path]:
    """Products and, in a checkout that has them, the bench targets (the export has none)."""
    return sorted(all_target_files(ROOT))


_ENTITY_DECL = re.compile(r"(?im)^\s*entity\s+(\w+)\s+is\b")
# An instantiation is ``label : entity [library .] name``. VHDL allows any
# whitespace (newlines included) around the ``.`` of the selected name, any
# letter case, and a simple name with no library at all; the reach must not
# depend on how a file spells it. A label or a library may also be an extended
# identifier (``\u_regbank\``, a doubled backslash inside it), so both
# accept either form.
_IDENT = r"(?:\b\w+|\\(?:[^\\\n]|\\\\)+\\)"
_INSTANCES = (
    re.compile(rf"(?i){_IDENT}\s*:\s*entity\s+(?:{_IDENT}\s*\.\s*)?(\w+)"),
    re.compile(rf"(?im)^\s*{_IDENT}\s*:\s*(?:component\s+)?(\w+)\s+(?:generic|port)\s+map\b"),
)


# One left-to-right pass, as the VHDL lexer reads the text: whichever of a
# ``--`` line comment, a ``/* */`` block comment, a character literal or a
# string literal starts first owns the text up to its own end. So ``/*`` inside
# a line comment opens nothing (deepskopion_pfsoc_fabric.vhd has "*64/*8" in
# one), and ``--`` inside a string or a block comment starts no line comment.
# An unterminated block comment runs to the end of the file.
_VHDL_LEXEME = re.compile(r"""--[^\n]*|/\*.*?(?:\*/|\Z)|'.'|"(?:[^"\n]|"")*\"""", re.S)


def _strip_vhdl_comments(text: str) -> str:
    """VHDL text with its ``--`` and VHDL-2008 ``/* */`` comments blanked.

    A comment keeps its newlines, so the line-anchored instantiation pattern
    still sees each line where it was.
    """
    def blank(m: re.Match[str]) -> str:
        lexeme = m.group(0)
        if lexeme.startswith(("--", "/*")):
            return "\n" * lexeme.count("\n") or " "
        return lexeme
    return _VHDL_LEXEME.sub(blank, text)


@functools.lru_cache(maxsize=None)
def _entity_graph(hw: Path) -> tuple[dict[str, Path], dict[Path, frozenset[str]]]:
    """Entity -> the file that declares it, and file -> the entities it instantiates, over ``hw``."""
    entity_file: dict[str, Path] = {}
    instances: dict[Path, frozenset[str]] = {}
    for path in sorted(hw.rglob("*.vhd")):
        try:
            text = _strip_vhdl_comments(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for m in _ENTITY_DECL.finditer(text):
            entity_file.setdefault(m.group(1).lower(), path)
        instances[path] = frozenset(m.group(1).lower() for rx in _INSTANCES for m in rx.finditer(text))
    return entity_file, instances


def _entity_file(entity: str) -> Path | None:
    """The file under hw/ that declares ``entity`` (comments ignored)."""
    return _entity_graph(ROOT / "hw")[0].get(entity.lower())


def _reaches_regbank_rom(top: str) -> bool:
    instances = _entity_graph(ROOT / "hw")[1]
    seen: set[str] = set()
    stack = [top.lower()]
    while stack:
        name = stack.pop()
        if name == "regbank_rom":
            return True
        path = _entity_file(name)
        if name in seen or path is None:
            continue
        seen.add(name)
        stack.extend(instances[path])
    return False


def builds_the_regbank_roms(doc: dict) -> bool:
    """The target synthesizes a top that reaches regbank_rom, the entity that
    reads the hex files, through its instantiations under hw/.

    The reach is transitive and reads code, not comments: deepskopion_pfsoc_top
    instantiates only deepskopion_pfsoc_fabric (DS-P2.326), which instantiates
    deepskopion_core, so a one-level name match counted the PolarFire product
    only through a comment and missed bench pfsoc_fic0_rea, whose top reaches
    the ROMs through the same fabric.
    """
    top = (doc.get("project") or {}).get("top_module")
    if not top:
        return False
    # A testbench top (bench/targets/de25_core_baseline.yml: tb_datalogger_to_ddr,
    # "this target never builds or programs anything") is simulated, never
    # synthesized, so no vendor tool resolves the hex paths for it.
    if top.startswith("tb_"):
        return False
    return _reaches_regbank_rom(top)


def staging_problems(doc: dict, name: str) -> list[str]:
    """Why ``doc`` would leave the ROM hex files out of the tool run directory."""
    data = ((doc.get("sources") or {}).get("data")) or []
    declared = {str(x).strip() for x in data}
    hooks = []
    tcl_hooks = doc.get("tcl_hooks") or {}
    for phase in ("gen_project", "synthesis"):
        entries = tcl_hooks.get(phase, [])
        for entry in entries if isinstance(entries, list) else [entries]:
            hooks.append(str(entry.get("script") if isinstance(entry, dict) else entry))
    if any(STAGING_HOOK.search(h.split()[0]) for h in hooks if h.strip()):
        return []
    missing = [h for h in HEX_FILES if h not in declared]
    if missing:
        return [f"{name}: builds the regbank ROMs but neither declares "
                f"{', '.join(missing)} under sources.data nor runs a "
                f"stage_regbank_roms hook before synthesis"]
    return []


def test_the_rom_paths_are_the_ones_the_wrapper_reads():
    wrapper = (ROOT / "hw/rtl/regbank/system_regbank_wrapper.vhd").read_text(encoding="utf-8")
    assert "G_REGBANK_HEX_FILE : string := C_REGBANK_HEX" in wrapper
    assert "G_CONFIG_HEX_FILE  : string := C_CONFIG_HEX" in wrapper
    for hex_file in HEX_FILES:
        assert (ROOT / hex_file).is_file(), hex_file


def test_every_product_target_builds_the_roms():
    products = product_target_files(ROOT)
    assert {p.stem for p in products} == {"zybo_datalogger", "de25_datalogger", "pfsoc_datalogger"}
    for path in products:
        assert builds_the_regbank_roms(_load(path)), path.stem


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _rom_building_target_files() -> list[Path]:
    """The targets whose top builds the regbank ROMs; the others have nothing to stage."""
    return [p for p in _target_files() if builds_the_regbank_roms(_load(p))]


@pytest.mark.parametrize("path", _rom_building_target_files(), ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_a_target_that_builds_the_roms_stages_their_hex_files(path):
    assert staging_problems(_load(path), path.stem) == []


def test_the_testbench_exemption_covers_only_simulation_tops():
    """The tb_ exemption must not hide a synthesized target: every target it
    skips names its testbench as top_sim too."""
    for path in _target_files():
        project = _load(path).get("project") or {}
        top = project.get("top_module") or ""
        if top.startswith("tb_"):
            assert project.get("top_sim") == top, path.stem


def test_the_pfsoc_target_before_81ab7770_is_refused():
    """Red-first on real history: the PolarFire target as it was when DS-P1.88
    was measured, before its hex files were declared."""
    old = subprocess.run(
        ["git", "show", "81ab7770^:targets/pfsoc_datalogger.yml"],
        cwd=ROOT, capture_output=True, text=True, errors="replace")
    if old.returncode != 0:
        pytest.skip("history before 81ab7770 not in this clone")
    doc = yaml.safe_load(old.stdout) or {}
    problems = staging_problems(doc, "pfsoc_datalogger@81ab7770^")
    assert problems and "regbank_schema.hex" in problems[0]


def test_removing_the_data_entries_is_refused_and_a_hook_is_accepted():
    doc = _load(ROOT / "targets/pfsoc_datalogger.yml")
    doc["sources"] = dict(doc.get("sources") or {})
    doc["sources"]["data"] = ["hw/rtl/regbank/config_schema.hex"]
    problems = staging_problems(doc, "pfsoc_datalogger")
    assert problems and "regbank_schema.hex" in problems[0]
    assert "config_schema.hex," not in problems[0]
    doc["tcl_hooks"] = {"synthesis": [{"script": "hw/libero/stage_regbank_roms_pre.tcl"}]}
    assert staging_problems(doc, "pfsoc_datalogger") == []


def _hw_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / "hw" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


_ROM = "entity regbank_rom is\nend entity;\narchitecture rtl of regbank_rom is\nbegin\nend architecture;\n"


def test_a_top_that_reaches_the_roms_through_a_wrapper_counts(tmp_path, monkeypatch):
    """The DS-P2.326 shape: the top only instantiates a fabric, the fabric
    holds the core, the core the wrapper, the wrapper the ROM."""
    _hw_tree(tmp_path, {
        "top.vhd": "entity thin_top is\nend entity;\narchitecture rtl of thin_top is\nbegin\n"
                   "  u_fabric : entity work.thin_fabric\n    port map (clk => clk);\nend architecture;\n",
        "fabric.vhd": "entity thin_fabric is\nend entity;\narchitecture rtl of thin_fabric is\nbegin\n"
                      "  u_core : component deepskopion_core\n    port map (clk => clk);\nend architecture;\n",
        "core.vhd": "entity deepskopion_core is\nend entity;\narchitecture rtl of deepskopion_core is\nbegin\n"
                    "  u_regbank : entity tich.system_regbank_wrapper port map (clk => clk);\nend architecture;\n",
        "wrapper.vhd": "entity system_regbank_wrapper is\nend entity;\n"
                       "architecture rtl of system_regbank_wrapper is\nbegin\n"
                       "  u_regbank_rom : entity tich.regbank_rom\n    generic map (G => 1)\n"
                       "    port map (clk => clk);\nend architecture;\n",
        "rom.vhd": _ROM,
    })
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert builds_the_regbank_roms({"project": {"top_module": "thin_top"}}), (
        "a top that reaches regbank_rom through a fabric wrapper is not counted")


_CHAIN_BELOW_THE_CORE = {
    "core.vhd": "entity deepskopion_core is\nend entity;\narchitecture rtl of deepskopion_core is\nbegin\n"
                "  u_regbank : entity tich.system_regbank_wrapper port map (clk => clk);\nend architecture;\n",
    "wrapper.vhd": "entity system_regbank_wrapper is\nend entity;\n"
                   "architecture rtl of system_regbank_wrapper is\nbegin\n"
                   "  u_regbank_rom : entity tich.regbank_rom\n    port map (clk => clk);\nend architecture;\n",
    "rom.vhd": _ROM,
}

# A top whose only path to the ROM is a commented-out instantiation of the
# core. Parsed as code, each would reach regbank_rom through core -> wrapper.
_COMMENTED_OUT_CORE = {
    "line": "  -- u_core : entity tich.deepskopion_core\n  --   port map (clk => clk);\n",
    "block": "  /* u_core : entity tich.deepskopion_core\n     port map (clk => clk); */\n",
}


@pytest.mark.parametrize("form", sorted(_COMMENTED_OUT_CORE))
def test_a_commented_out_instantiation_does_not_count(tmp_path, monkeypatch, form):
    """A top whose instantiation of the core is commented out builds no ROM,
    though the core below it still reaches regbank_rom."""
    _hw_tree(tmp_path, {
        "top.vhd": "entity talk_top is\nend entity;\narchitecture rtl of talk_top is\nbegin\n"
                   + _COMMENTED_OUT_CORE[form]
                   + "  u_leaf : entity work.leaf port map (clk => clk);\nend architecture;\n",
        "leaf.vhd": "entity leaf is\nend entity;\narchitecture rtl of leaf is\nbegin\nend architecture;\n",
        **_CHAIN_BELOW_THE_CORE,
    })
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert builds_the_regbank_roms({"project": {"top_module": "deepskopion_core"}}), (
        "fixture: the core below the commented-out instance does not reach regbank_rom")
    assert not builds_the_regbank_roms({"project": {"top_module": "talk_top"}}), (
        f"a {form}-commented-out instantiation of the core is counted as building the ROMs")


def test_a_slash_star_inside_a_line_comment_opens_no_block_comment(tmp_path, monkeypatch):
    """The deepskopion_pfsoc_fabric.vhd shape: a line comment holding ``/*``
    ("*64/*8 nets") above the real instantiation, and a later line comment
    holding ``*/``. Neither is a block-comment delimiter. The top reaches the
    core only through a fabric, as deepskopion_pfsoc_top does."""
    _hw_tree(tmp_path, {
        "top.vhd": "entity star_top is\nend entity;\narchitecture rtl of star_top is\n"
                   "  -- the *64/*8 nets carry the MSS shape\nbegin\n"
                   "  u_fabric : entity work.star_fabric\n    port map (clk => clk);\n"
                   "  -- */\nend architecture;\n",
        "fabric.vhd": "entity star_fabric is\nend entity;\narchitecture rtl of star_fabric is\nbegin\n"
                      "  u_core : entity tich.deepskopion_core port map (clk => clk);\nend architecture;\n",
        **_CHAIN_BELOW_THE_CORE,
    })
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert builds_the_regbank_roms({"project": {"top_module": "star_top"}}), (
        "a /* inside a line comment swallowed the instantiation after it")


def _copy_design(dst: Path) -> None:
    """Every hw/ VHDL file and every target file, at its repository path under ``dst``."""
    for src in [*(ROOT / "hw").rglob("*.vhd"), *_target_files()]:
        out = dst / src.relative_to(ROOT)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(src.read_bytes())


def _without_rom_staging(doc: dict) -> dict:
    """``doc`` with both hex files dropped from sources.data and every
    stage_regbank_roms hook dropped from tcl_hooks.

    The hooks are matched by the literal name, not by STAGING_HOOK: the removal
    is the control's input, so it must not move when the check under test does.
    """
    doc = dict(doc)
    sources = dict(doc.get("sources") or {})
    sources["data"] = [x for x in (sources.get("data") or []) if str(x).strip() not in HEX_FILES]
    doc["sources"] = sources
    hooks = {}
    for stage, entries in (doc.get("tcl_hooks") or {}).items():
        hooks[stage] = [e for e in (entries if isinstance(entries, list) else [entries])
                        if "stage_regbank_roms" not in str(e.get("script") if isinstance(e, dict) else e)]
    doc["tcl_hooks"] = hooks
    return doc


def test_a_post_synthesis_rom_staging_hook_is_refused():
    # The public export omits the classic_eye bench target.
    name = "classic_eye" if has_bench(ROOT) else "pfsoc_datalogger"
    target = _load(target_file(name, ROOT))
    assert builds_the_regbank_roms(target), name

    doc = _without_rom_staging(target)
    script = "hw/bd/stage_regbank_roms_pre.tcl"
    doc["tcl_hooks"]["synthesis"] = [script]
    assert staging_problems(doc, name) == []

    doc["tcl_hooks"]["post_synthesis"] = doc["tcl_hooks"].pop("synthesis")
    problems = staging_problems(doc, name)
    assert problems, f"{name}: accepted ROM staging registered only under post_synthesis"
    assert all(hex_file in problems[0] for hex_file in HEX_FILES), problems


def _reword_line_comments(text: str) -> str:
    """Every whole-line ``--`` comment reworded. A line that starts with ``--``
    is a comment whatever surrounds it (a VHDL string cannot span lines); a
    line holding ``*/`` is kept, so no block comment loses its end."""
    out = []
    for line in text.splitlines(keepends=True):
        body = line.lstrip()
        if body.startswith("--") and "*/" not in body:
            line = line[: len(line) - len(body)] + "-- reworded\n"
        out.append(line)
    return "".join(out)


def test_a_comment_only_edit_to_the_pfsoc_fabric_keeps_the_pfsoc_targets(tmp_path, monkeypatch):
    """A comment-only edit to deepskopion_pfsoc_fabric.vhd changes no verdict.

    The edit rewords every line comment of the fabric (so its own "*64/*8"
    comment is gone), then puts a ``/*`` in a line comment above the
    instantiations and a ``-- */`` below them, the landing reviewer's edit.
    The PolarFire targets must still be collected and still pass the staging
    check, and removing their real ROM staging must still be refused. The test
    reads no comment text of the live tree, so rewording a live comment cannot
    redden it.
    """
    _copy_design(tmp_path)
    fabric = tmp_path / "hw/rtl/pfsoc/deepskopion_pfsoc_fabric.vhd"
    edited = ("-- the *64/*8 nets carry the MSS shape\n"
              + _reword_line_comments(fabric.read_text(encoding="utf-8")) + "-- */\n")
    assert "/*" in edited
    fabric.write_text(edited, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    names = ["pfsoc_datalogger"] + (["pfsoc_fic0_rea"] if has_bench(tmp_path) else [])
    collected = {p.stem: p for p in _rom_building_target_files()}
    not_counted = [n for n in names if n not in collected]
    assert not_counted == [], f"a comment-only edit dropped {not_counted} from the ROM-building targets"
    for name in names:
        assert staging_problems(_load(collected[name]), name) == [], name
    accepted = [n for n in names
                if not staging_problems(_without_rom_staging(_load(collected[n])), n)]
    assert accepted == [], f"removing the ROM staging of {accepted} after a comment-only edit is not refused"


# The same instantiation of the wrapper, spelled as VHDL allows.
_WRAPPER_SPELLINGS = {
    "spaced-separator": "entity tich . system_regbank_wrapper",
    "split-separator": "entity tich\n      .system_regbank_wrapper",
    "upper-case": "ENTITY TICH.SYSTEM_REGBANK_WRAPPER",
    "simple-name": "entity system_regbank_wrapper",
}


@pytest.mark.parametrize("form", sorted(_WRAPPER_SPELLINGS))
def test_every_legal_spelling_of_the_selected_name_counts(tmp_path, monkeypatch, form):
    """A top that instantiates the wrapper reaches regbank_rom however the
    selected name is spaced or cased."""
    _hw_tree(tmp_path, {
        "top.vhd": "entity spell_top is\nend entity;\narchitecture rtl of spell_top is\nbegin\n"
                   f"  u_regbank : {_WRAPPER_SPELLINGS[form]}\n    port map (clk => clk);\n"
                   "end architecture;\n",
        "wrapper.vhd": _CHAIN_BELOW_THE_CORE["wrapper.vhd"],
        "rom.vhd": _ROM,
    })
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert builds_the_regbank_roms({"project": {"top_module": "spell_top"}}), (
        f"a top that instantiates the wrapper as {_WRAPPER_SPELLINGS[form]!r} is not counted")


# Extended identifiers (VHDL-93) as the instance label or the library name.
_EXTENDED_SPELLINGS = {
    "extended-label": "\\u_regbank\\ : entity tich . system_regbank_wrapper",
    "extended-label-doubled-backslash": "\\u\\\\regbank\\ : entity tich.system_regbank_wrapper",
    "extended-library": "u_regbank : entity \\tich\\ . system_regbank_wrapper",
    "extended-label-component": "\\u_regbank\\ : component system_regbank_wrapper",
}


@pytest.mark.parametrize("form", sorted(_EXTENDED_SPELLINGS))
def test_an_extended_identifier_label_or_library_still_counts(tmp_path, monkeypatch, form):
    """DS-P1.88 landing review (8ba5c886): a top whose wrapper instance is
    labelled ``\\u_regbank\\`` dropped out of the ROM-building targets, so its
    staging went unchecked. Every extended-identifier spelling must count."""
    _hw_tree(tmp_path, {
        "top.vhd": "entity ext_top is\nend entity;\narchitecture rtl of ext_top is\nbegin\n"
                   f"  {_EXTENDED_SPELLINGS[form]}\n    port map (clk => clk);\n"
                   "end architecture;\n",
        "wrapper.vhd": _CHAIN_BELOW_THE_CORE["wrapper.vhd"],
        "rom.vhd": _ROM,
    })
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert builds_the_regbank_roms({"project": {"top_module": "ext_top"}}), (
        f"a top that instantiates the wrapper as {_EXTENDED_SPELLINGS[form]!r} is not counted")


# Re-spellings of every ``entity <library>.<name>`` in the tree.
_SELECTED_NAME = re.compile(r"(?i)\b(entity\s+)(\w+)\.(\w+)")
_RESPELL = {
    "spaced-separator": lambda m: f"{m.group(1)}{m.group(2)} . {m.group(3)}",
    "split-separator": lambda m: f"{m.group(1)}{m.group(2)}\n      .{m.group(3)}",
    "upper-spaced": lambda m: f"ENTITY {m.group(2).upper()} . {m.group(3).upper()}",
}


@pytest.mark.parametrize("form", sorted(_RESPELL))
def test_a_respelled_selected_name_with_the_rom_staging_removed_is_refused(tmp_path, monkeypatch, form):
    """The landing reviewer's red control on the whole tree: every
    instantiation's selected name re-spelled (``entity tich .
    system_regbank_wrapper``) and every target's ROM staging removed. The
    re-spelling must drop no target from the ROM-building set, and every
    target in it must then fail the staging check."""
    _copy_design(tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    as_written = sorted(p.stem for p in _rom_building_target_files())
    assert {"zybo_datalogger", "de25_datalogger"} <= set(as_written), as_written
    for path in (tmp_path / "hw").rglob("*.vhd"):
        text = path.read_text(encoding="utf-8")
        path.write_text(_SELECTED_NAME.sub(_RESPELL[form], text), encoding="utf-8")
    for path in _target_files():
        path.write_text(yaml.safe_dump(_without_rom_staging(_load(path))), encoding="utf-8")
    _entity_graph.cache_clear()
    respelled = sorted(p.stem for p in _rom_building_target_files())
    dropped = sorted(set(as_written) - set(respelled))
    assert dropped == [], f"re-spelling the selected names dropped {dropped} from the ROM-building targets"
    accepted = [p.stem for p in _rom_building_target_files() if not staging_problems(_load(p), p.stem)]
    assert accepted == [], f"{accepted} build the regbank ROMs with their staging removed and pass"


def test_the_pfsoc_targets_that_reach_the_roms_through_the_fabric_are_checked():
    """deepskopion_pfsoc_top and the bench deepskopion_pfsoc_top_fic0_rea both
    reach regbank_rom only through deepskopion_pfsoc_fabric (DS-P2.326)."""
    names = ["pfsoc_datalogger"] + (["pfsoc_fic0_rea"] if has_bench(ROOT) else [])
    not_counted = [n for n in names if not builds_the_regbank_roms(_load(target_file(n, ROOT)))]
    assert not_counted == [], f"not counted as building the regbank ROMs: {not_counted}"
