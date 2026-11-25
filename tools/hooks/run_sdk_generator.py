#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Invoke a routertl SDK generator through its MODULE, not its source file.

DS-X6 (DS-P1.37): on the first off-bench rehearsal, every pre_build hook that
reached into the SDK by literal ``.py`` path died on a stranger's machine —
the published routertl wheel ships those modules COMPILED
(``*.cpython-*.so``), so ``<sdk-root>/sdk/scripts/rr_rea_build_id.py`` and
``$SDK_ROOT/sdk/generators/regbank/payload_encoder.py`` exist only on a
source checkout such as this bench's editable install. ``python -m`` cannot
run them either (no code object in an extension module). What DOES work on
both install forms is importing the module and calling its entry function,
which is all this shim does.

Usage: run_sdk_generator.py <generator> [args...]

Generators:
    rea-build-id      sdk.scripts.rr_rea_build_id.main(argv)
    payload-encoder   sdk.generators.regbank.payload_encoder.main()  (sys.argv)
    build-metadata    sdk.generators.regbank.gen_build_metadata.main()  (sys.argv)
    regbank-pkg       generate_pkg(...) — its CLI lives only under
                      ``__main__`` in the compiled core module, so the
                      argparse surface is mirrored here (same arguments,
                      same defaults).

Exit code is the generator's own; an unimportable SDK is a loud exit 2
naming the module, never a silent skip.
"""
from __future__ import annotations

import importlib
import sys


def _import(module: str):
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        print(
            f"ERROR: cannot import {module!r} from the installed routertl "
            f"({exc}). Is routertl installed in this interpreter? "
            "(pip install routertl)",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _run_argv_main(module: str, argv: list[str]) -> int:
    """Entry functions that read sys.argv themselves (no argv parameter)."""
    mod = _import(module)
    sys.argv = [module.rsplit(".", 1)[-1], *argv]
    rc = mod.main()
    return int(rc or 0)


def _run_rea_build_id(argv: list[str]) -> int:
    mod = _import("sdk.scripts.rr_rea_build_id")
    # DS-T2.75: in `--design-identity` mode the generator resolves the git HEAD
    # by running `git rev-parse HEAD` with cwd = the `--out` PARENT dir (so the
    # stamp tracks the design's own commit). On a fresh clone / clean build that
    # dir (e.g. generated/) does not exist yet — the generator only mkdir's it
    # AFTER head resolution — so the subprocess fails and the hook dies with
    # "--design-identity needs a git HEAD". Pre-create the --out parent here so
    # the resolution runs inside the worktree on the very first build too. (The
    # legacy --ip-yml/--rtl-dir modes never needed a HEAD, so this only started
    # mattering when the hook moved to the design identity.)
    from pathlib import Path
    if "--out" in argv:
        out_val = argv[argv.index("--out") + 1]
        Path(out_val).resolve().parent.mkdir(parents=True, exist_ok=True)
    # DS-P1.96: dirt confined to rom_info_pkg.vhd's declared volatile lines
    # stamps the clean design identity (tools/rea_stamp_policy.py).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import rea_stamp_policy
    done = rea_stamp_policy.stamp_clean_if_only_build_metadata_changed(mod, argv)
    if done is not None:
        return done
    return int(mod.main(argv) or 0)


def _run_regbank_pkg(argv: list[str]) -> int:
    # Mirror of the argparse CLI that exists only inside the compiled
    # module's ``__main__`` block (routertl_core generate_regbank_pkg).
    import argparse

    parser = argparse.ArgumentParser(prog="regbank-pkg")
    parser.add_argument("yaml_file")
    parser.add_argument("output_file")
    parser.add_argument("--format", "-f", choices=["hex", "dec"], default="dec")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--library", "-l", default="work")
    args = parser.parse_args(argv)
    mod = _import("sdk.generators.regbank.generate_regbank_pkg")
    mod.generate_pkg(args.yaml_file, args.output_file, args.format,
                     args.verbose, args.library)
    return 0


GENERATORS = {
    "rea-build-id": _run_rea_build_id,
    "payload-encoder":
        lambda argv: _run_argv_main("sdk.generators.regbank.payload_encoder", argv),
    "build-metadata":
        lambda argv: _run_argv_main("sdk.generators.regbank.gen_build_metadata", argv),
    "regbank-pkg": _run_regbank_pkg,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in GENERATORS:
        names = ", ".join(sorted(GENERATORS))
        print(f"usage: run_sdk_generator.py <{names}> [args...]", file=sys.stderr)
        return 2
    return GENERATORS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
