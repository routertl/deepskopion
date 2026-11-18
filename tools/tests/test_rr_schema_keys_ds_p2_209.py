"""DS-P2.209: every project.yml and target-yml key is in the installed rr's schema.

rr ignores a key it does not know, so a key that only a newer RouteRTL release
reads stays green on the newest wheel and does nothing on the declared floor.
tools/ci/rr_schema_keys.py reports such keys against ``rr schema --json``, and
both CI legs run it. These tests use a hand-written schema, not the installed
rr, so the expected verdicts are literals.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _module():
    spec = importlib.util.spec_from_file_location(
        "rr_schema_keys", ROOT / "tools" / "ci" / "rr_schema_keys.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


K = _module()

SCHEMA = {
    "project.name": {"scope": "root"},
    "project.target": {"scope": "root"},
    "hardware.board": {"scope": "both"},
    "linux.bootargs": {"scope": "target"},
    "tcl_hooks": {"scope": "both"},
    "sources.data": {"scope": "both"},
}


def _tree(tmp_path: Path, project: dict, targets: dict) -> Path:
    (tmp_path / "project.yml").write_text(yaml.safe_dump(project))
    (tmp_path / "targets").mkdir()
    for name, body in targets.items():
        (tmp_path / "targets" / f"{name}.yml").write_text(yaml.safe_dump(body))
    return tmp_path


def test_known_keys_and_free_form_leaves_pass(tmp_path):
    root = _tree(tmp_path, {"project": {"name": "x", "target": "targets/a.yml"}},
                 {"a": {"hardware": {"board": "b"},
                        "tcl_hooks": {"anything": {"below": "a leaf"}},
                        "linux": {"bootargs": "console=ttyPS0"}}})
    assert K.check(root, SCHEMA, {}) == []


def test_a_key_this_release_does_not_know_is_reported(tmp_path):
    root = _tree(tmp_path, {"project": {"name": "x"}},
                 {"a": {"linux": {"bootargs": "c", "first_stage": "fsbl"},
                        "newsection": {"k": 1}}})
    assert K.check(root, SCHEMA, {}) == [
        "targets/a.yml: linux.first_stage: not in this rr release's schema",
        "targets/a.yml: newsection: not in this rr release's schema",
    ]


def test_a_key_in_the_wrong_file_is_reported(tmp_path):
    root = _tree(tmp_path, {"linux": {"bootargs": "c"}},
                 {"a": {"project": {"name": "x"}}})
    assert K.check(root, SCHEMA, {}) == [
        "project.yml: linux.bootargs: scope target: belongs in a target yml",
        "targets/a.yml: project.name: scope root: belongs in project.yml",
    ]


def test_an_exception_accepts_its_key_and_reports_once_unused(tmp_path):
    root = _tree(tmp_path, {}, {"a": {"linux": {"first_stage": "fsbl"}}})
    exc = {"linux.first_stage": "RTL-P2.2169", "linux.gone": "RTL-P2.2169"}
    assert K.check(root, SCHEMA, exc) == [
        "tools/ci/rr_schema_key_exceptions.txt: linux.gone (RTL-P2.2169): no file uses "
        "this key, or the schema now lists it; remove the line",
    ]


def test_an_exception_does_not_hide_a_scope_error(tmp_path):
    root = _tree(tmp_path, {}, {"a": {"project": {"name": "x"}}})
    assert K.check(root, SCHEMA, {"project.name": "RTL-P2.2169"}) == [
        "targets/a.yml: project.name: scope root: belongs in project.yml",
        "tools/ci/rr_schema_key_exceptions.txt: project.name (RTL-P2.2169): no file uses "
        "this key, or the schema now lists it; remove the line",
    ]


@pytest.mark.parametrize("line", ["linux.first_stage", "linux.first_stage because",
                                  "linux.first_stage TODO missing from schema"])
def test_an_exception_without_a_ticket_is_refused(line):
    with pytest.raises(ValueError, match="<key> <ticket> <reason>"):
        K.load_exceptions(line + "\n")


def test_the_committed_exception_list_names_a_ticket_per_key():
    text = (ROOT / "tools" / "ci" / "rr_schema_key_exceptions.txt").read_text(encoding="utf-8")
    exc = K.load_exceptions(text)
    assert exc and all(t for t in exc.values())


def test_a_schema_envelope_with_no_entries_is_refused():
    with pytest.raises(K.SchemaError):
        K.load_schema(json.dumps({"data": {"entries": {}}}))
    with pytest.raises(K.SchemaError):
        K.load_schema("not json")


def test_public_ci_runs_the_key_check_on_both_legs():
    steps = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["desk-checks"]["steps"]
    check = [s for s in steps if s.get("run", "").strip() == "python tools/ci/rr_schema_keys.py"]
    assert check, "ci.yml no longer runs tools/ci/rr_schema_keys.py"
    assert "if" not in check[0], "the key check must run on the floor leg and on latest"
    names = [s.get("name") for s in steps]
    assert names.index(check[0]["name"]) > names.index("Install RouteRTL and the test requirements")
