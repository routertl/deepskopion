# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.333: the README says how Vivado gets on PATH before the first rr command.

The clean-machine rehearsal of the export had to source a local script before
the build: the README never said how Vivado is found, and the shipped
settings64.sh of an install copied to another directory still names the
original one. What ran there, and what the README now documents for that case,
is `XILINX_VIVADO` set to the copy's `Vivado/2024.1` plus `$XILINX_VIVADO/bin` on PATH.
DS-P2.371: the install directory is a shell variable `INSTALL_DIR`, set on its own
line; a `<install-dir>` placeholder pasted into bash is a redirection.
RouteRTL resolves the target's pinned release through `XILINX_VIVADO` (routertl
5.5.0 tool discovery reports the source "env"), and the preflight reads the same
variable first.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SANDBOX_TICKET = "DS-P3.320"
FENCE = re.compile(r"^```(?:bash|sh|shell)?\s*$\n(.*?)^```\s*$", re.M | re.S)
SECTION = "### Build and run on the Zybo Z7-20"


def _vivado_version() -> str:
    decl = yaml.safe_load((ROOT / "tools/preflight/supported_environment.yml").read_text(encoding="utf-8"))
    return str(decl["vivado"]["version"])


def _zybo_blocks(text: str) -> list[list[str]]:
    start = text.index(SECTION)
    end = text.find("\n### ", start + len(SECTION))
    section = text[start:end if end != -1 else len(text)]
    blocks = []
    for body in FENCE.findall(section):
        lines = [line.split("#", 1)[0].strip() for line in body.splitlines()]
        blocks.append([line for line in lines if line])
    return blocks


def _setup_blocks(text: str) -> list[list[str]]:
    """The blocks that come before the first block running `rr`."""
    blocks = _zybo_blocks(text)
    first_rr = next(i for i, b in enumerate(blocks) if any(c.startswith("rr ") for c in b))
    return blocks[:first_rr]


def _setup_before_first_rr(text: str) -> list[str]:
    """Every command in the blocks that come before the first block running `rr`."""
    return [c for b in _setup_blocks(text) for c in b]


_RECIPE_RUN = r"""
printf 'START\n' >> "$S2_LOG"
command_not_found_handle() { return 0; }
source() { printf 'SRC\t%s\n' "$*" >> "$S2_LOG"; }
"""
_RECIPE_END = """
printf 'ENV\\t%s\\t%s\\t%s\\n' "${INSTALL_DIR-}" "${XILINX_VIVADO-}" "$PATH" >> "$S2_LOG"
"""


def _sandbox(scratch: Path, cwd: Path, script: Path) -> list[str] | None:
    """bwrap argv running *script* with the root read-only and only *scratch* writable,
    or None when that exact configuration cannot start here. The probe uses the same
    flags as the run, so a probe that passes cannot hide a launch that fails."""
    import shutil
    import subprocess
    bw = shutil.which("bwrap")
    if not bw:
        return None
    flags = [bw, "--ro-bind", "/", "/", "--bind", str(scratch), str(scratch), "--dev", "/dev", "--proc", "/proc",
             "--unshare-all", "--die-with-parent", "--chdir", str(cwd)]
    if subprocess.run(flags + ["/bin/true"], capture_output=True).returncode != 0:
        return None
    return flags + ["/bin/bash", str(script)]


def _run_recipe(lines: list[str]) -> dict:
    """Run one recipe alone in a clean bash (nothing on PATH, `source` stubbed) and
    return what it sourced and the variables it left set."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "log"
        log.write_text("", encoding="utf-8")
        script = Path(d) / "recipe.sh"
        script.write_text(_RECIPE_RUN + "\n".join(lines) + "\n" + _RECIPE_END, encoding="utf-8")
        argv = _sandbox(Path(d), Path(d), script)
        if argv is None:  # README text is never executed outside the sandbox
            import pytest
            pytest.skip(f"bwrap cannot run here; the recipe check needs it ({SANDBOX_TICKET})")
        proc = subprocess.run(argv, cwd=d, capture_output=True, text=True, timeout=30,
                              env={"PATH": "/nonexistent", "S2_LOG": str(log), "HOME": d})
        lines_out = log.read_text(encoding="utf-8").splitlines()
        if not lines_out or lines_out[0] != "START":  # the sandbox launched and bash ran the recipe
            raise AssertionError(f"the sandboxed bash never started ({proc.stderr[-300:]!r})")
        out = {"sourced": []}
        for entry in log.read_text(encoding="utf-8").splitlines():
            kind, *rest = entry.split("\t")
            if kind == "SRC":
                out["sourced"].append(rest[0])
            elif kind == "ENV":
                out["INSTALL_DIR"], out["XILINX_VIVADO"], out["PATH"] = rest
        return out


def _recipe_errors(text: str) -> list[str]:
    """DS-P2.371: each recipe (block) that uses $INSTALL_DIR, pasted alone into a clean
    shell, gives INSTALL_DIR a real directory and then sources, or sets XILINX_VIVADO and
    PATH, from it. A recipe that borrows the other's assignment, sets it after use, or
    scopes it to one command (`INSTALL_DIR=x true`) leaves it empty and fails."""
    version = _vivado_version()
    errors = []
    users = [b for b in _setup_blocks(text) if any("$INSTALL_DIR" in c for c in b)]
    if len(users) != 2:
        errors.append(f"expected 2 recipes using $INSTALL_DIR, found {len(users)}")
    # Roles by position: the first recipe is the standard install (it sources the
    # installer's script), the second the copied install (it sets both variables).
    for role, b in zip(("source", "copy"), users):
        env = _run_recipe(b)
        root = env.get("INSTALL_DIR", "")
        if not root.startswith("/") or root == "/" or re.search(r"[<>]", root):
            errors.append(f"recipe leaves INSTALL_DIR {root!r}: {b}")
            continue
        if role == "source":
            if env["sourced"] != [f"{root}/Vivado/{version}/settings64.sh"]:
                errors.append(f"recipe sources {env['sourced']}: {b}")
        else:
            if env.get("XILINX_VIVADO") != f"{root}/Vivado/{version}":
                errors.append(f"recipe leaves XILINX_VIVADO {env.get('XILINX_VIVADO')!r}: {b}")
            if not env.get("PATH", "").startswith(f"{root}/Vivado/{version}/bin:"):
                errors.append(f"recipe leaves PATH {env.get('PATH')!r}: {b}")
    return errors


def test_the_vivado_setup_comes_before_the_first_rr_command():
    version = _vivado_version()
    setup = _setup_before_first_rr((ROOT / "README.md").read_text(encoding="utf-8"))
    assert any(c.startswith("INSTALL_DIR=") for c in setup), setup
    values = [c.split("=", 1)[1].strip().strip('"\'') for c in setup if c.startswith("INSTALL_DIR=")]
    assert values and all(v and not re.fullmatch(r"<[^<>]*>", v) for v in values), values
    assert f'source "$INSTALL_DIR/Vivado/{version}/settings64.sh"' in setup, setup
    assert f'export XILINX_VIVADO="$INSTALL_DIR/Vivado/{version}"' in setup, setup
    assert 'export PATH="$XILINX_VIVADO/bin:$PATH"' in setup, setup
    assert _recipe_errors((ROOT / "README.md").read_text(encoding="utf-8")) == []


def test_the_recipe_check_refuses_a_borrowed_or_late_assignment():
    """Mutants: the second recipe without its assignment, and with it after the use."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    copy_assign = next(ln for ln in text.splitlines() if ln.startswith("INSTALL_DIR=") and "copy" in ln)
    deleted = text.replace(copy_assign + "\n", "", 1)
    assert deleted != text
    assert _recipe_errors(deleted)
    export_line = next(ln for ln in text.splitlines() if ln.startswith("export XILINX_VIVADO="))
    reordered = text.replace(copy_assign + "\n" + export_line, export_line + "\n" + copy_assign, 1)
    assert reordered != text
    assert _recipe_errors(reordered)
    scoped = text.replace(copy_assign, copy_assign.split("#", 1)[0].rstrip() + " true", 1)
    assert scoped != text
    assert _recipe_errors(scoped)
    hidden = text.replace(export_line, f"if false; then {export_line}; fi", 1)
    assert hidden != text
    assert _recipe_errors(hidden)
    path_line = 'export PATH="$XILINX_VIVADO/bin:$PATH"\n'
    moved = text.replace(path_line, "", 1)
    assert moved != text
    assert _recipe_errors(moved)


def test_the_readme_states_the_declared_vivado_release():
    version = _vivado_version()
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text[text.index(SECTION):]
    assert f"Vivado {version}" in section.split("```", 1)[0]
    assert f"`vivado -version` then prints `vivado v{version}`" in text


def test_the_setup_names_no_machine_path():
    setup = " ".join(_setup_before_first_rr((ROOT / "README.md").read_text(encoding="utf-8")))
    assert not re.search(r"/home/|/Users/|/tools/Xilinx", setup), setup


def test_a_recipe_cannot_write_outside_its_scratch_directory(tmp_path):
    """A recipe runs under bwrap with only its scratch directory writable."""
    outside = tmp_path / "outside.txt"
    _run_recipe([f"echo x > {outside}", f"true; /usr/bin/touch {outside}.2"])
    assert not outside.exists()
    assert not Path(f"{outside}.2").exists()


def test_the_check_refuses_a_readme_without_the_setup():
    """Mutant: the README as it was, with the build steps but no Vivado setup."""
    text = (f"{SECTION}\n\n```bash\n# 1. Install\npip install routertl\n"
            "rr target set zybo_datalogger\nrr synth run\n```\n")
    assert _setup_before_first_rr(text) == []
