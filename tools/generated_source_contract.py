#!/usr/bin/env python3
"""DS-X2 generated-source reconstruction and drift contract.

The normal RouteRTL hooks write into the checkout.  This checker instead runs
the same project-owned generators into an empty temporary tree, then compares
their results with every tracked output declared in ``generated_sources.yml``.
Build-invocation metadata is masked only where that manifest explicitly names
it; all other bytes are exact.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

import yaml

_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from active_target import load_active_target  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tools" / "generated_sources.yml"


class ContractError(RuntimeError):
    """The generation contract could not be evaluated safely."""


def load_manifest() -> dict:
    data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ContractError(f"unsupported or missing schema_version in {MANIFEST_PATH}")
    return data


def tracked_outputs(manifest: dict | None = None) -> dict[str, list[dict[str, str]]]:
    manifest = manifest or load_manifest()
    families = manifest.get("tracked_families")
    if not isinstance(families, dict) or not families:
        raise ContractError("generated_sources.yml declares no tracked_families")
    result: dict[str, list[dict[str, str]]] = {}
    seen: set[str] = set()
    for family, declaration in families.items():
        outputs = declaration.get("outputs") if isinstance(declaration, dict) else None
        if not isinstance(outputs, list) or not outputs:
            raise ContractError(f"tracked family {family!r} declares no outputs")
        result[family] = []
        for item in outputs:
            if not isinstance(item, dict) or not item.get("path") or not item.get("candidate"):
                raise ContractError(f"tracked family {family!r} has an invalid output entry")
            rel = str(item["path"])
            if rel in seen:
                raise ContractError(f"tracked generated output declared twice: {rel}")
            seen.add(rel)
            result[family].append({"path": rel, "candidate": str(item["candidate"])})
    return result


def _volatile_rules(rel_path: str, manifest: dict) -> list[dict[str, str]]:
    rules = (manifest.get("volatile_lines") or {}).get(rel_path, [])
    if not isinstance(rules, list):
        raise ContractError(f"volatile_lines[{rel_path!r}] must be a list")
    return rules


def canonical_bytes(rel_path: str, body: bytes, manifest: dict | None = None) -> bytes:
    """Mask explicitly declared volatile lines; preserve every other byte."""
    manifest = manifest or load_manifest()
    rules = _volatile_rules(rel_path, manifest)
    if not rules:
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"volatile declarations require UTF-8 text: {rel_path}") from exc
    for rule in rules:
        field = rule.get("field") if isinstance(rule, dict) else None
        pattern = rule.get("pattern") if isinstance(rule, dict) else None
        if not field or not pattern:
            raise ContractError(f"invalid volatile rule for {rel_path}: {rule!r}")
        matches = list(re.finditer(pattern, text, flags=re.MULTILINE))
        if len(matches) != 1:
            raise ContractError(
                f"volatile field {field!r} in {rel_path} matched {len(matches)} lines; "
                "the declaration no longer describes the generated format"
            )
        text = re.sub(
            pattern,
            f"<DS-X2-VOLATILE:{field}>",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    return text.encode("utf-8")


def _replace_exactly_once(text: str, pattern: str, replacement: str, label: str) -> str:
    result, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ContractError(f"could not normalize {label}: expected one match, found {count}")
    return result


def normalize_candidate(kind: str, body: bytes) -> bytes:
    """Remove generator-local time/path bytes before installing an output."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"{kind} candidate is not UTF-8") from exc

    if kind == "rom-info":
        canonical_paths = {
            "C_REGBANK_HEX": "hw/rtl/regbank/regbank_schema.hex",
            "C_CONFIG_HEX": "hw/rtl/regbank/config_schema.hex",
        }
        for constant, rel in canonical_paths.items():
            text = _replace_exactly_once(
                text,
                rf'^(\s*constant {constant}\s*: string := ").*(";\s*)$',
                rf"\g<1>{rel}\g<2>",
                constant,
            )
    elif kind == "axilite":
        # The SDK renderer's date is commentary, not an input to the entity.
        text = _replace_exactly_once(
            text,
            r"^-- Auto-generated AXI-Lite register file(?: — \d{4}-\d{2}-\d{2})?$",
            "-- Auto-generated AXI-Lite register file",
            "AXI-Lite date header",
        )
    else:
        raise ContractError(f"unknown candidate kind: {kind}")

    return text.encode("utf-8")


def assert_clone_relative(rel_path: str, body: bytes) -> None:
    """Reject machine-local references in generated text."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return
    markers = (
        str(ROOT),
        "/home/",
        "/Users/",
        "/media/",
        "file:///",
        "../" + "routertl",
    )
    hits = [marker for marker in markers if marker in text]
    if hits:
        raise ContractError(
            f"generated {rel_path} contains machine-local path marker(s): "
            + ", ".join(repr(hit) for hit in hits)
        )


def _write_if_changed(path: Path, body: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_bytes() == body:
            return False
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return True


_GIT_HASH_PATTERN = re.compile(
    r'^\s*constant C_GIT_HASH\s*: std_logic_vector\(C_ROM_DATA_WIDTH-1 downto 0\) := x"([0-9a-fA-F]{8})";$',
    re.MULTILINE,
)
_SYSTEM_VERSION_PATTERN = re.compile(
    r'^\s*constant C_SYSTEM_VERSION\s*: std_logic_vector\(C_ROM_DATA_WIDTH-1 downto 0\) := x"([0-9a-fA-F]{8})";$',
    re.MULTILINE,
)


def _extract_git_hash(text: str) -> str | None:
    match = _GIT_HASH_PATTERN.search(text)
    return match.group(1).lower() if match else None


def _extract_system_version(text: str) -> str | None:
    match = _SYSTEM_VERSION_PATTERN.search(text)
    return match.group(1).lower() if match else None


def _stage_canonical_bytes(rel_path: str, body: bytes, manifest: dict | None = None) -> bytes:
    """Mask intra-build volatile fields (timestamp, build date, and system version dirty bit)."""
    manifest = manifest or load_manifest()
    rules = [
        rule
        for rule in _volatile_rules(rel_path, manifest)
        if isinstance(rule, dict)
        and rule.get("field") in ("generated_at", "build_date", "system_version_dirty_bit")
    ]
    if not rules:
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"volatile declarations require UTF-8 text: {rel_path}") from exc
    for rule in rules:
        field = rule.get("field")
        pattern = rule.get("pattern")
        if not field or not pattern:
            raise ContractError(f"invalid volatile rule for {rel_path}: {rule!r}")
        matches = list(re.finditer(pattern, text, flags=re.MULTILINE))
        if len(matches) != 1:
            raise ContractError(
                f"volatile field {field!r} in {rel_path} matched {len(matches)} lines; "
                "the declaration no longer describes the generated format"
            )
        text = re.sub(
            pattern,
            f"<STAGE-VOLATILE:{field}>",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    return text.encode("utf-8")


def _active_target_build_dir() -> Path | None:
    """The active target's ``project.output_dir``, resolved as rr resolves the
    target (tools/active_target.py: $RR_TARGET, then ``rr target set``'s local
    file, then project.yml). DS-P2.336: reading only $RR_TARGET and project.yml
    put a Zybo build's stamp under the DE25 build directory after
    ``rr target set zybo_datalogger``."""
    _, target_cfg = load_active_target(ROOT)
    project = target_cfg.get("project") if isinstance(target_cfg, dict) else None
    output_dir = project.get("output_dir") if isinstance(project, dict) else None
    if not output_dir:
        return None
    out_path = Path(str(output_dir))
    return out_path if out_path.is_absolute() else (ROOT / out_path)


def resolve_stage_stamp(
    stage_stamp: Path | str | None = None,
    output: Path | None = None,
) -> Path | None:
    """Resolve the stage stamp path for preserving intra-build volatility.

    DS-P1.102: the stamp belongs to the BUILD, never to the queue job. Each queued stage (synth, then
    bitstream) is its own rr queue job with its own RR_JOB_ID; a stamp keyed on the job id is never seen by
    the next stage of the same build, which then writes a new C_BUILD_DATE, and routertl >= 5.4.0
    (RTL-P1.310) stops the bitstream on that changed hook output. RR_JOB_ID only names the per-job record
    (job_stamp_record)."""
    if stage_stamp:
        return Path(stage_stamp)
    env_stamp = os.environ.get("STAGE_STAMP") or os.environ.get("RR_STAGE_STAMP")
    if env_stamp:
        return Path(env_stamp)
    build_dir = os.environ.get("BUILD_DIR") or os.environ.get("RR_BUILD_DIR")
    if build_dir:
        return Path(build_dir) / ".rom_info_stage_stamp"
    target_build = _active_target_build_dir()
    if target_build:
        return target_build / ".rom_info_stage_stamp"
    if output is not None:
        try:
            if not output.resolve().is_relative_to(ROOT.resolve()):
                return output.parent / ".rom_info_stage_stamp"
        except (ValueError, OSError):
            return output.parent / ".rom_info_stage_stamp"
    return ROOT / "build" / ".rom_info_stage_stamp"


def job_stamp_record() -> Path | None:
    """DS-P1.102: where a queued job records the rom_info it ran with (<root>/build/.rom_info_stage_stamp_<job>),
    the file DE-G0's ROM-identity point reads. None outside an rr queue job."""
    job_id = os.environ.get("RR_JOB_ID")
    return ROOT / "build" / f".rom_info_stage_stamp_{job_id}" if job_id else None


def install_candidate(
    *,
    kind: str,
    candidate: Path,
    output: Path,
    stamp_candidate: Path | None = None,
    stage_stamp: Path | None = None,
) -> bool:
    """Install a normalized candidate without churning stage-volatile fields."""
    manifest = load_manifest()
    body = normalize_candidate(kind, candidate.read_bytes())
    if output.resolve().is_relative_to(ROOT):
        rel = output.resolve().relative_to(ROOT).as_posix()
    elif kind == "rom-info":
        # Scratch reconstruction redirects the package directory, but its
        # volatility contract is attached to the canonical project output.
        rel = "hw/rtl/pkg/rom_info_pkg.vhd"
    else:
        rel = output.name
    assert_clone_relative(rel, body)

    # DS-P2.156: preserve volatile fields only against a same-build stage stamp,
    # never against the committed output file. The git hash from a different
    # HEAD is never preserved.
    resolved_stamp = resolve_stage_stamp(stage_stamp, output=output)
    unchanged = False
    try:
        if kind == "rom-info":
            if resolved_stamp is not None and resolved_stamp.is_file() and output.is_file():
                stamp_bytes = resolved_stamp.read_bytes()
                out_bytes = output.read_bytes()
                cand_text = body.decode("utf-8", errors="replace")
                stamp_text = stamp_bytes.decode("utf-8", errors="replace")
                out_text = out_bytes.decode("utf-8", errors="replace")
                cand_hash = _extract_git_hash(cand_text)
                stamp_hash = _extract_git_hash(stamp_text)
                out_hash = _extract_git_hash(out_text)
                cand_ver = _extract_system_version(cand_text)
                stamp_ver = _extract_system_version(stamp_text)
                out_ver = _extract_system_version(out_text)
                if (
                    cand_hash
                    and cand_hash == stamp_hash
                    and cand_hash == out_hash
                    and cand_ver
                    and stamp_ver
                    and out_ver
                    and cand_ver[:6] == stamp_ver[:6]
                    and cand_ver[:6] == out_ver[:6]
                    and _stage_canonical_bytes(rel, body, manifest)
                    == _stage_canonical_bytes(rel, stamp_bytes, manifest)
                    and _stage_canonical_bytes(rel, stamp_bytes, manifest)
                    == _stage_canonical_bytes(rel, out_bytes, manifest)
                ):
                    unchanged = True
            elif resolved_stamp is None and output.is_file():
                unchanged = output.read_bytes() == body
        else:
            unchanged = output.read_bytes() == body
    except OSError:
        pass
    changed = False if unchanged else _write_if_changed(output, body)

    if resolved_stamp is not None and kind == "rom-info" and not unchanged:
        try:
            resolved_stamp.parent.mkdir(parents=True, exist_ok=True)
            _write_if_changed(resolved_stamp, body)
        except OSError:
            pass
    record = job_stamp_record() if kind == "rom-info" else None
    if record is not None and output.is_file():
        # The bytes this job leaves for synthesis: the installed body, or the preserved output.
        try:
            record.parent.mkdir(parents=True, exist_ok=True)
            _write_if_changed(record, output.read_bytes())
        except OSError:
            pass

    if stamp_candidate is not None and stamp_candidate.is_file():
        _write_if_changed(output.with_name(output.name + ".producer.json"), stamp_candidate.read_bytes())
    return changed


def run_generation(destination: Path) -> None:
    """Run the tracked generators into an initially empty destination."""
    destination.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    commands = (
        [
            sys.executable,
            "tools/datalogger-builder/datalogger_gather_gen.py",
            "tools/datalogger-builder/config/datalogger_config.yml",
            "--pkg-dir",
            str(destination / "pkg"),
            "--rtl-dir",
            str(destination / "rtl"),
            "--regbank-dir",
            str(destination / "regbank"),
        ],
        [
            "bash",
            "tools/regen_regbank.sh",
            "--output-dir",
            str(destination / "pkg"),
            "--regbank-dir",
            str(destination / "regbank"),
            "--library",
            "tich",
        ],
    )
    for command in commands:
        proc = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode:
            raise ContractError(
                f"generator failed ({' '.join(command)}):\n{proc.stdout}{proc.stderr}"
            )


def _worktree_reader(root: Path) -> Callable[[str], bytes]:
    return lambda rel: (root / rel).read_bytes()


def _index_reader(rel: str) -> bytes:
    proc = subprocess.run(
        ["git", "show", f":{rel}"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        raise FileNotFoundError(f"{rel} is absent from the git index")
    return proc.stdout


def compare_outputs(
    generated_root: Path,
    *,
    actual_root: Path = ROOT,
    actual: str = "worktree",
) -> dict[str, list[str]]:
    manifest = load_manifest()
    families = tracked_outputs(manifest)
    reader = _index_reader if actual == "index" else _worktree_reader(actual_root)
    drift: dict[str, list[str]] = {}
    for family, outputs in families.items():
        for item in outputs:
            rel = item["path"]
            candidate_path = generated_root / item["candidate"]
            try:
                expected = candidate_path.read_bytes()
                observed = reader(rel)
                assert_clone_relative(rel, expected)
                assert_clone_relative(rel, observed)
                if canonical_bytes(rel, expected, manifest) != canonical_bytes(rel, observed, manifest):
                    drift.setdefault(family, []).append(rel)
            except (OSError, ContractError) as exc:
                drift.setdefault(family, []).append(f"{rel} ({exc})")
    return drift


def missing_runtime_outputs(family: str, root: Path = ROOT) -> list[str]:
    manifest = load_manifest()
    families = manifest.get("runtime_families") or {}
    declaration = families.get(family)
    if not isinstance(declaration, dict):
        raise ContractError(f"unknown runtime family: {family}")
    outputs = declaration.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise ContractError(f"runtime family {family!r} declares no outputs")
    return [str(rel) for rel in outputs if not (root / str(rel)).is_file()]


def check(actual: str) -> int:
    with tempfile.TemporaryDirectory(prefix="ds-x2-generated-") as tmp:
        generated_root = Path(tmp)
        run_generation(generated_root)
        drift = compare_outputs(generated_root, actual=actual)
    if drift:
        print("generated-source drift detected (DS-X2):", file=sys.stderr)
        for family, paths in drift.items():
            print(f"  {family}:", file=sys.stderr)
            for path in paths:
                print(f"    - {path}", file=sys.stderr)
        print("run the declared pre-build generators and stage their outputs", file=sys.stderr)
        return 1
    print("generated-source contract PASS — all tracked families reconstruct exactly")
    return 0


def verify_runtime(family: str) -> int:
    missing = missing_runtime_outputs(family)
    if missing:
        print(f"runtime generated family {family!r} is incomplete:", file=sys.stderr)
        for path in missing:
            print(f"  - {path}", file=sys.stderr)
        return 1
    declaration = load_manifest()["runtime_families"][family]
    for rel in declaration["outputs"]:
        assert_clone_relative(str(rel), (ROOT / str(rel)).read_bytes())
    print(f"runtime generated family {family!r} PASS — all declared outputs exist")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check_parser = sub.add_parser("check", help="regenerate in scratch space and detect drift")
    check_parser.add_argument("--actual", choices=("worktree", "index"), default="worktree")

    runtime_parser = sub.add_parser(
        "verify-runtime", help="require a declared untracked generated family"
    )
    runtime_parser.add_argument("family", choices=tuple((load_manifest().get("runtime_families") or {}).keys()))

    install_parser = sub.add_parser("install", help="normalize and substance-install one candidate")
    install_parser.add_argument("--kind", choices=("rom-info", "axilite"), required=True)
    install_parser.add_argument("--candidate", type=Path, required=True)
    install_parser.add_argument("--output", type=Path, required=True)
    install_parser.add_argument("--stamp-candidate", type=Path)
    install_parser.add_argument("--stage-stamp", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "check":
            return check(args.actual)
        if args.command == "verify-runtime":
            return verify_runtime(args.family)
        install_candidate(
            kind=args.kind,
            candidate=args.candidate,
            output=args.output,
            stamp_candidate=args.stamp_candidate,
            stage_stamp=args.stage_stamp,
        )
        return 0
    except (ContractError, OSError, ValueError) as exc:
        print(f"generated-source contract error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
