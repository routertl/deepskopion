#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""id_gen.py — generate / drift-check the deepskopion id-space from the
canonical registry (system_ids.yml).  DS-P3.57.

Single source of truth = deepskopion/system_ids.yml. This tool emits the
domain map in the two consumer dialects (VHDL + Python) and — most importantly
— DRIFT-CHECKS the live copies against the registry so the four hand-synced
copies (datalogger_gather_gen.py:_domain_map, the generated RTL C_DOMAIN_*,
sw/stream/zs_msg.py:DOMAIN_*, and any hardcoded domain literal) can never
silently diverge. Mirrors the project_regbank.yml -> regen_regbank.sh discipline.

Usage:
  id_gen.py check          # verify live RTL/SW/generator match the registry (CI gate)
  id_gen.py emit-python    # print the canonical DOMAIN_* / DOMAIN_NAMES block
  id_gen.py emit-vhdl      # print the canonical C_DOMAIN_* constants

The registry is AHEAD of the live code (it declares planned/reserved domains
like jtag/mdio early to keep their codes STABLE). So `check` is directional:
every domain present in the live code must EXIST in the registry with the SAME
code; the registry may legitimately contain more.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

# The generator keys the SYS(0x0) domain as "TIMESTAMP" (timestamp messages
# route through DOMAIN_SYS). Map such consumer-side aliases to registry names.
_GEN_ALIASES = {"timestamp": "sys"}

# Marker fences for the GENERATED domain block inside sw/stream/zs_msg.py.
_SW_BEGIN = "# >>> GENERATED domains from system_ids.yml by id_gen.py (DS-P3.57) — DO NOT EDIT >>>"
_SW_END = "# <<< END GENERATED domains <<<"
# Marker fences for the GENERATED C_ZS_DOMAIN_* block inside hw/rtl/pkg/zs_msg_pkg.vhd.
_RTL_BEGIN = "  -- >>> GENERATED domains from system_ids.yml by id_gen.py (DS-P3.57) — DO NOT EDIT >>>"
_RTL_END = "  -- <<< END GENERATED domains <<<"

# DS-P3.70: severity + error-event registries follow the SAME single-source
# discipline as domains. Each is a fenced, generated block in both dialects.
_SW_SEV_BEGIN = "# >>> GENERATED severity from system_ids.yml by id_gen.py (DS-P3.70) — DO NOT EDIT >>>"
_SW_SEV_END = "# <<< END GENERATED severity <<<"
_RTL_SEV_BEGIN = "  -- >>> GENERATED severity from system_ids.yml by id_gen.py (DS-P3.70) — DO NOT EDIT >>>"
_RTL_SEV_END = "  -- <<< END GENERATED severity <<<"
_SW_ERR_BEGIN = "# >>> GENERATED error_events from system_ids.yml by id_gen.py (DS-P3.70) — DO NOT EDIT >>>"
_SW_ERR_END = "# <<< END GENERATED error_events <<<"
_RTL_ERR_BEGIN = "  -- >>> GENERATED error_events from system_ids.yml by id_gen.py (DS-P3.70) — DO NOT EDIT >>>"
_RTL_ERR_END = "  -- <<< END GENERATED error_events <<<"

# error_events are reserved to event_id[7:0] = 0xF0..0xFF (never collide with
# per-protocol codes 0x00..0xEF). Guarded by check().
_ERR_EVT_MIN, _ERR_EVT_MAX = 0xF0, 0xFF


def _repo_root(start: Path) -> Path:
    """Walk up until we find system_ids.yml (the registry marks the root)."""
    for p in [start, *start.parents]:
        if (p / "system_ids.yml").is_file():
            return p
    raise SystemExit("id_gen: could not locate system_ids.yml (repo root)")


def load_registry(root: Path) -> dict[str, int]:
    """Return {domain_name: code} from system_ids.yml."""
    doc = yaml.safe_load((root / "system_ids.yml").read_text())
    return {name: int(d["code"]) for name, d in doc["domains"].items()}


def load_section(root: Path, key: str) -> dict[str, int]:
    """Return {name: code} from a top-level mapping in system_ids.yml (e.g.
    'severity', 'error_events'). Empty dict if the section is absent."""
    doc = yaml.safe_load((root / "system_ids.yml").read_text())
    return {name: int(d["code"]) for name, d in (doc.get(key) or {}).items()}


def _py_const_block(reg: dict[str, int], begin: str, end: str,
                    prefix: str, comment: str) -> str:
    """A fenced Python constant block (prefix+NAME = 0xCODE), name-aligned."""
    w = max(len(n) for n in reg)
    out = [begin, comment]
    out += [f"{prefix}{name.upper():<{w}} = 0x{code:X}" for name, code in reg.items()]
    out.append(end)
    return "\n".join(out)


def _vhdl_const_block(reg: dict[str, int], begin: str, end: str,
                      prefix: str, nbits: int, comment: str) -> str:
    """A fenced VHDL constant block (constant prefix+NAME : slv := X\"CODE\")."""
    w = max(len(n) for n in reg)
    hexw = (nbits + 3) // 4
    out = [begin, comment]
    out += [f'  constant {prefix}{name.upper():<{w}} : '
            f'std_logic_vector({nbits - 1} downto 0) := X"{code:0{hexw}X}";'
            for name, code in reg.items()]
    out.append(end)
    return "\n".join(out)


def _replace_fenced(path: Path, begin: str, end: str, new_block: str) -> None:
    """Replace the begin..end fenced region of @p path with @p new_block (which
    itself includes the fences). Idempotent; raises if the fences are absent."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if begin not in text or end not in text:
        raise SystemExit(f"id_gen: fence not found in {path} — add it once:\n  {begin}\n  {end}")
    head, _, rest = text.partition(begin)
    _, _, tail = rest.partition(end)
    new = head + new_block + tail
    if new != text:
        path.write_text(new, encoding="utf-8")
        print(f"✅ regenerated a block in {path.name}")
    else:
        print(f"✅ {path.name} block already in sync")


def _parse_consts(text: str, py_prefix: str | None = None,
                  vhdl_prefix: str | None = None) -> dict[str, int]:
    """Parse {name: code} from `PREFIX_NAME = 0xN` (python) or
    `constant PREFIX_NAME : ... X\"NN\"` (vhdl)."""
    out: dict[str, int] = {}
    if py_prefix:
        for m in re.finditer(rf"^{re.escape(py_prefix)}(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)", text, re.M):
            out[m.group(1).lower()] = int(m.group(2), 0)
    if vhdl_prefix:
        for m in re.finditer(rf'constant\s+{re.escape(vhdl_prefix)}(\w+)\s*:.*?X"([0-9A-Fa-f]+)"', text):
            out[m.group(1).strip().lower()] = int(m.group(2), 16)
    return out


def parse_zs_msg(root: Path) -> dict[str, int]:
    """Extract {name: code} from sw/stream/zs_msg.py DOMAIN_* assignments."""
    text = (root / "sw" / "stream" / "zs_msg.py").read_text()
    out: dict[str, int] = {}
    for m in re.finditer(r"^DOMAIN_(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)", text, re.M):
        name = m.group(1).lower()
        if name == "names":  # skip DOMAIN_NAMES dict
            continue
        out[name] = int(m.group(2), 0)
    return out


def parse_zs_msg_pkg(root: Path) -> dict[str, int]:
    """Extract {name: code} from the hand-written hw/rtl/pkg/zs_msg_pkg.vhd
    C_ZS_DOMAIN_* constants (a separate RTL copy, drift-guarded here until it is
    generated too — DS-P3.57 follow-up)."""
    pkg = root / "hw" / "rtl" / "pkg" / "zs_msg_pkg.vhd"
    text = pkg.read_text(encoding="utf-8", errors="replace")
    out: dict[str, int] = {}
    for m in re.finditer(r'constant\s+C_ZS_DOMAIN_(\w+)\s*:.*?X"([0-9A-Fa-f]+)"', text):
        out[m.group(1).strip().lower()] = int(m.group(2), 16)
    return out


def parse_generated_rtl(root: Path) -> dict[str, int]:
    """Extract {name: code} from the GENERATED datalogger_gather.vhd C_DOMAIN_*
    constants. Since DS-P3.57 the generator sources its map from system_ids.yml,
    so this checks the registry -> RTL path END-TO-END (the generated OUTPUT,
    not the generator's source — robust to how the map is produced). Only the
    active build's protocols appear, which is a valid subset of the registry."""
    rtl = root / "hw" / "rtl" / "datalogger" / "infra" / "datalogger_gather" / "datalogger_gather.vhd"
    text = rtl.read_text(encoding="utf-8", errors="replace")
    out: dict[str, int] = {}
    for m in re.finditer(r'constant\s+C_DOMAIN_(\w+)\s*:.*?X"([0-9A-Fa-f]+)"', text):
        name = _GEN_ALIASES.get(m.group(1).strip().lower(), m.group(1).strip().lower())
        out[name] = int(m.group(2), 16)
    return out


def diff_against_registry(reg: dict[str, int],
                          live_maps: list[tuple[str, dict[str, int]]]) -> list[str]:
    """Pure drift comparison (no I/O). Returns a problem string per mismatch:
    a live domain absent from the registry, or a code that disagrees."""
    problems: list[str] = []
    for label, live in live_maps:
        for name, code in live.items():
            if name not in reg:
                problems.append(f"  {label}: '{name}' (0x{code:X}) is NOT in system_ids.yml")
            elif reg[name] != code:
                problems.append(f"  {label}: '{name}'=0x{code:X} != registry 0x{reg[name]:X}")
    return problems


def check(root: Path) -> int:
    reg = load_registry(root)
    problems = diff_against_registry(reg, [
        ("sw/stream/zs_msg.py", parse_zs_msg(root)),
        ("generated datalogger_gather.vhd C_DOMAIN_*", parse_generated_rtl(root)),
        ("hw/rtl/pkg/zs_msg_pkg.vhd C_ZS_DOMAIN_*", parse_zs_msg_pkg(root)),
    ])

    # DS-P3.70: severity + error_events follow the same single-source drift gate.
    sw_text = (root / "sw" / "stream" / "zs_msg.py").read_text(encoding="utf-8", errors="replace")
    rtl_text = (root / "hw" / "rtl" / "pkg" / "zs_msg_pkg.vhd").read_text(encoding="utf-8", errors="replace")
    sev = load_section(root, "severity")
    err = load_section(root, "error_events")
    if sev:
        problems += diff_against_registry(sev, [
            ("zs_msg.py SEV_*", _parse_consts(sw_text, py_prefix="SEV_")),
            ("zs_msg_pkg.vhd C_ZS_SEV_*", _parse_consts(rtl_text, vhdl_prefix="C_ZS_SEV_")),
        ])
    if err:
        problems += diff_against_registry(err, [
            ("zs_msg.py ERR_EVT_*", _parse_consts(sw_text, py_prefix="ERR_EVT_")),
            ("zs_msg_pkg.vhd C_ZS_ERR_EVT_*", _parse_consts(rtl_text, vhdl_prefix="C_ZS_ERR_EVT_")),
        ])
        for name, code in err.items():     # reserved-range guard
            if not (_ERR_EVT_MIN <= code <= _ERR_EVT_MAX):
                problems.append(f"  error_events: '{name}'=0x{code:X} outside the reserved "
                                f"0x{_ERR_EVT_MIN:X}..0x{_ERR_EVT_MAX:X} range")

    if problems:
        print("❌ id drift detected (system_ids.yml vs live code):")
        print("\n".join(problems))
        print("\nFix: update system_ids.yml (the single source of truth) or the drifting copy.")
        return 1
    print(f"✅ id-registry in sync: {len(reg)} domains / {len(sev)} severities / "
          f"{len(err)} error-events declared; all live constants match system_ids.yml.")
    return 0


def _domain_block(reg: dict[str, int]) -> str:
    """The canonical, fenced Python domain block (DOMAIN_* + DOMAIN_NAMES)."""
    w = max(len(n) for n in reg)                 # const-name alignment
    kw = max(len(n) for n in reg) + 1            # dict-key alignment (name + ':')
    out = [_SW_BEGIN, "# Domain enum (msg_type[11:8])"]
    for name, code in reg.items():
        out.append(f"DOMAIN_{name.upper():<{w}} = 0x{code:X}")
    out += ["", "# Human-readable domain names (for annotation text).", "DOMAIN_NAMES = {"]
    for name, code in reg.items():
        key = (name.upper() + ":")
        out.append(f"    DOMAIN_{key:<{kw}} \"{name}\",")
    out += ["}", _SW_END]
    return "\n".join(out)


def emit_python(root: Path) -> int:
    print(_domain_block(load_registry(root)))
    return 0


def gen_sw(root: Path) -> int:
    """Rewrite the fenced domain / severity / error_events blocks inside
    sw/stream/zs_msg.py from the registry (idempotent)."""
    p = root / "sw" / "stream" / "zs_msg.py"
    _replace_fenced(p, _SW_BEGIN, _SW_END, _domain_block(load_registry(root)))
    _replace_fenced(p, _SW_SEV_BEGIN, _SW_SEV_END, _py_const_block(
        load_section(root, "severity"), _SW_SEV_BEGIN, _SW_SEV_END,
        "SEV_", "# Severity enum (msg_type[15:12])."))
    _replace_fenced(p, _SW_ERR_BEGIN, _SW_ERR_END, _py_const_block(
        load_section(root, "error_events"), _SW_ERR_BEGIN, _SW_ERR_END,
        "ERR_EVT_", "# Cross-domain error-provenance codes (event_id[7:0], reserved 0xF0-0xFF)."))
    return 0


def emit_vhdl(root: Path) -> int:
    reg = load_registry(root)
    print("  -- Domain codes — GENERATED from system_ids.yml (DS-P3.57). DO NOT EDIT.")
    for name, code in reg.items():
        print(f'  constant C_DOMAIN_{name.upper():<6}: std_logic_vector(3 downto 0) := X"{code:X}";')
    return 0


def _domain_block_vhdl(reg: dict[str, int]) -> str:
    """The canonical, fenced VHDL C_ZS_DOMAIN_* block."""
    w = max(len(n) for n in reg)
    out = [_RTL_BEGIN]
    for name, code in reg.items():
        out.append(f'  constant C_ZS_DOMAIN_{name.upper():<{w}} : '
                   f'std_logic_vector(3 downto 0) := X"{code:X}";')
    out.append(_RTL_END)
    return "\n".join(out)


def gen_rtl(root: Path) -> int:
    """Rewrite the fenced C_ZS_DOMAIN_* / C_ZS_SEV_* / C_ZS_ERR_EVT_* blocks in
    hw/rtl/pkg/zs_msg_pkg.vhd."""
    p = root / "hw" / "rtl" / "pkg" / "zs_msg_pkg.vhd"
    _replace_fenced(p, _RTL_BEGIN, _RTL_END, _domain_block_vhdl(load_registry(root)))
    _replace_fenced(p, _RTL_SEV_BEGIN, _RTL_SEV_END, _vhdl_const_block(
        load_section(root, "severity"), _RTL_SEV_BEGIN, _RTL_SEV_END,
        "C_ZS_SEV_", 4, "  -- Severity nibble (msg_type[15:12])."))
    _replace_fenced(p, _RTL_ERR_BEGIN, _RTL_ERR_END, _vhdl_const_block(
        load_section(root, "error_events"), _RTL_ERR_BEGIN, _RTL_ERR_END,
        "C_ZS_ERR_EVT_", 8, "  -- Cross-domain error-provenance codes (event_id[7:0], reserved 0xF0-0xFF)."))
    return 0


def gen(root: Path) -> int:
    """Regenerate every generated domain copy (zs_msg.py + zs_msg_pkg.vhd)."""
    return gen_sw(root) or gen_rtl(root)


def main(argv: list[str]) -> int:
    root = _repo_root(Path(__file__).resolve().parent)
    cmd = argv[1] if len(argv) > 1 else "check"
    return {"check": check, "emit-python": emit_python, "emit-vhdl": emit_vhdl,
            "gen-sw": gen_sw, "gen-rtl": gen_rtl, "gen": gen}.get(
                cmd, lambda _r: (print(__doc__), 2)[1])(root)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
