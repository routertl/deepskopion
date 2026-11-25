#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-X6 (DS-P1.37) — the build must not reach into the SDK by source path.

On the first off-bench rehearsal (samsung-wsl, clean clone, `pip install
routertl` 4.8.1), two of the four pre_build hooks died before synthesis:

    ❌ Hook 'rr_rea_build_id': Script not found:
       .../site-packages/sdk/scripts/rr_rea_build_id.py
    ❌ Hook 'regbank-regen' ... ERROR: SDK_ROOT=... but payload_encoder.py
       not found.

Both modules DO ship in the wheel — compiled (`*.cpython-*.so`). A literal
``.py`` under the SDK root exists only on a source checkout, which is what
this bench's editable install is and what no stranger's machine has. So the
failure class is "reach the SDK by source-file path", and this test bans the
class, not the two instances:

1. no hook `script:` in project.yml or any target may use the `routertl:`
   prefix with a ``.py`` — that form resolves a literal file under the SDK
   root (run_hooks.py RTL-P3.502) and cannot survive a compiled wheel;
2. no project script may reference an ``sdk/**/*.py`` file path — the
   sanctioned bridge is ``tools/hooks/run_sdk_generator.py``, which imports
   the module and calls its entry function (works on both install forms);
3. the bridge itself must resolve every generator it advertises against the
   routertl actually installed here — a shim whose import target rots is the
   same defect one release later.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
SHIM = ROOT / "tools" / "hooks" / "run_sdk_generator.py"

# An sdk/... source file reached as a FILE (payload_encoder.py, …), not as a
# module. Matches "$SDK_ROOT/sdk/generators/..." and "routertl:sdk/scripts/...".
_SDK_PY_PATH = re.compile(r"sdk/(?:generators|scripts|engine|cli)/[\w/]+\.py\b")


def _hook_scripts() -> list[tuple[str, str]]:
    """Every hook script declared by project.yml and every target yml."""
    out: list[tuple[str, str]] = []
    for path in [ROOT / "project.yml", *all_target_files(ROOT)]:
        try:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        hooks = cfg.get("hooks") or {}
        for stage, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            for hook in entries:
                if isinstance(hook, dict) and hook.get("script"):
                    out.append((f"{path.name}:{stage}:{hook.get('name')}",
                                str(hook["script"])))
    return out


def test_no_routertl_source_path_hooks():
    """Hook scripts must not resolve a .py file under the SDK root."""
    bad = [(where, script) for where, script in _hook_scripts()
           if script.startswith("routertl:") and script.endswith(".py")]
    assert not bad, (
        "hook(s) reach the SDK by source path; the published wheel ships "
        "these modules compiled, so a stranger's build dies with 'Script "
        f"not found' (measured, DS-X6): {bad}"
    )


def test_no_sdk_py_file_references_in_project_scripts():
    """Shell/python tooling must not name sdk/**/*.py files.

    The shim and this test are the two places allowed to KNOW the module
    layout; they reference modules by import path, never as files.
    """
    offenders = []
    for path in sorted((ROOT / "tools").rglob("*")):
        if path.suffix not in {".sh", ".py"} or not path.is_file():
            continue
        if path == SHIM or path == Path(__file__).resolve():
            continue
        for n, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _SDK_PY_PATH.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not offenders, (
        "project tooling references SDK source files by path; that only "
        "resolves on a source checkout, never on the pip wheel (DS-X6):\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("generator,argv", [
    ("rea-build-id", ["--help"]),
    ("payload-encoder", ["--help"]),
    ("build-metadata", ["--help"]),
])
def test_shim_resolves_generator(generator, argv):
    """The bridge must reach a live entry point in the INSTALLED routertl.

    For these three generators the shim imports the module FIRST and hands
    argv to its entry function, so --help exercises import + dispatch
    without writing anything; exit 0 proves the module resolved and its CLI
    parsed. If routertl renames a module or drops an entry function, this
    goes red on the bench before a stranger finds it. regbank-pkg is NOT in
    this list — its --help is answered by the shim's local argparse mirror
    before any import, so it gets a real-generation test below.
    """
    proc = subprocess.run(
        [sys.executable, str(SHIM), generator, *argv],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"run_sdk_generator.py {generator} --help failed "
        f"(rc={proc.returncode}):\n{proc.stderr}"
    )


def test_shim_regbank_pkg_reaches_installed_module(tmp_path):
    """regbank-pkg must be proven against the installed SDK, not its mirror.

    The shim hand-mirrors this generator's argparse CLI (its real one lives
    only under ``__main__`` in the compiled core module), so ``--help``
    SystemExit(0)s before ``_import`` runs — the landing review mutation-
    confirmed that a renamed import target still passed a --help probe. The
    only honest probe is a real generation: encode the project's own
    manifest to a scratch file, which cannot succeed without importing
    ``generate_regbank_pkg`` and dispatching ``generate_pkg``.
    """
    out = tmp_path / "system_regbank_pkg.vhd"
    proc = subprocess.run(
        [sys.executable, str(SHIM), "regbank-pkg",
         str(ROOT / "project_regbank.yml"), str(out)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"run_sdk_generator.py regbank-pkg failed (rc={proc.returncode}):\n"
        f"{proc.stderr}"
    )
    assert out.is_file() and out.stat().st_size > 0, (
        "regbank-pkg exited 0 without writing the package — the entry "
        "function was not reached"
    )
    text = out.read_text(encoding="utf-8", errors="replace")
    assert "package" in text.lower(), (
        "regbank-pkg output is not a VHDL package:\n" + text[:400]
    )


def test_shim_unknown_generator_fails_loudly():
    proc = subprocess.run(
        [sys.executable, str(SHIM), "no-such-generator"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()
