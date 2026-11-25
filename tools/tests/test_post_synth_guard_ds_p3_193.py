# rr unknown-llm (AI-generated)
"""DS-P3.193: tools/post_synth_guard.py runs the build-tree guards on DE25 builds.

The runner must (1) pick the DE25 build directories the queue produced,
(2) PASS a tree whose generated IOPLL primitive is right, (3) go RED on a tree
whose dividers drifted, (4) go RED when the test skips on a tree it was given
(a skip there means it never saw the tree), and (5) say NO-TREE, not PASS, when
there is nothing to check.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import post_synth_guard as psg  # noqa: E402

GOOD_PRIMITIVE = """module de25_fabric_pll_altera_iopll_2100_abc (
);
  tennm_iopll #(
    .ref_clk_0_freq(32'd50000000),
    .fb_clk_m_div(64),
    .out_clk_0_c_div(32),
    .out_clk_0_freq(36'd100000000)
  ) pll ();
endmodule
"""


def make_tree(base: Path, primitive: str | None = GOOD_PRIMITIVE,
              rel: str = "build/rr_ip_regen/ip/de25_fabric_pll") -> Path:
    synth = base / rel / "de25_fabric_pll" / "altera_iopll_2100" / "synth"
    synth.mkdir(parents=True)
    if primitive is not None:
        (synth / "de25_fabric_pll_altera_iopll_2100_abc.v").write_text(primitive)
    return base


def job(id_, cwd, finished_at, target="de25_datalogger", stage="synth", state="done"):
    return {"id": id_, "target_name": target, "stage": stage, "state": state,
            "exit_code": 0, "finished_at": finished_at, "cwd": str(cwd)}


def queue_file(tmp_path: Path, jobs: list[dict]) -> str:
    p = tmp_path / "queue.json"
    p.write_text(json.dumps({"data": {"jobs": jobs}}))
    return str(p)


def test_only_finished_de25_build_jobs_are_selected():
    jobs = psg.de25_jobs({"data": {"jobs": [
        job("a", "/w1", 100.0),
        job("b", "/w2", 200.0, target="zybo_datalogger"),
        job("c", "/w3", 300.0, state="cancelled"),
        job("d", "/w4", 400.0, stage="sim"),
        job("e", "/w5", 500.0, stage="impl", state="failed"),
    ]}})
    assert [j.id for j in jobs] == ["e", "a"]


def test_window_keeps_the_newest_job_per_build_directory():
    now = 100_000.0
    jobs = psg.de25_jobs({"data": {"jobs": [
        job("old", "/w1", now - 3600 * 5),
        job("new", "/w1", now - 3600),
        job("other", "/w2", now - 3600 * 2),
        job("stale", "/w3", now - 3600 * 48),
    ]}})
    assert [j.id for j in psg.select(jobs, now, 26)] == ["new", "other"]


def test_an_empty_window_rechecks_the_newest_job():
    now = 1_000_000.0
    jobs = psg.de25_jobs({"data": {"jobs": [
        job("older", "/w1", now - 3600 * 90),
        job("newest", "/w2", now - 3600 * 50),
    ]}})
    assert [j.id for j in psg.select(jobs, now, 26)] == ["newest"]


def test_a_correct_regenerated_tree_passes(tmp_path):
    tree = make_tree(tmp_path / "wt")
    rc = psg.main(["--queue-json", queue_file(tmp_path, [job("j1", tree, 9e9)])])
    assert rc == 0


def test_a_correct_vendored_tree_under_ip_passes(tmp_path):
    tree = make_tree(tmp_path / "wt", rel="ip/de25_fabric_pll")
    assert psg.main(["--build-dir", str(tree)]) == 0


def test_a_drifted_divider_is_red(tmp_path, capsys):
    tree = make_tree(tmp_path / "wt", GOOD_PRIMITIVE.replace("c_div(32)", "c_div(33)"))
    rc = psg.main(["--queue-json", queue_file(tmp_path, [job("j1", tree, 9e9)])])
    out = capsys.readouterr().out
    assert rc == 1
    assert "RED: 1 of 1" in out and "dividers must be M=64 / C=32" in out


def test_a_skip_on_a_given_tree_is_red_not_pass(tmp_path):
    # The runner located a primitive, but the test is pointed at a directory
    # holding none: it skips, and a skip on a tree it was given is a red.
    ok, log = psg.run_tests(tmp_path / "empty")
    assert not ok
    assert "SKIPPED" in log


def test_no_tree_is_reported_as_nothing_tested(tmp_path, capsys):
    tree = make_tree(tmp_path / "wt", primitive=None)
    rc = psg.main(["--queue-json", queue_file(tmp_path, [job("j1", tree, 9e9)])])
    out = capsys.readouterr().out
    assert rc == psg.NO_TREE
    assert "NO DE25 BUILD TREE" in out and "NO-TREE  job j1" in out


def test_the_digest_holds_the_report(tmp_path):
    tree = make_tree(tmp_path / "wt")
    digest = tmp_path / "state" / "digest.txt"
    assert psg.main(["--build-dir", str(tree), "--digest", str(digest)]) == 0
    assert digest.read_text().startswith("post-synth guard (DS-P3.193): PASS: 1")


def test_every_post_synth_test_exists():
    for node in psg.POST_SYNTH_TESTS:
        path, name = node.split("::")
        assert f"def {name}(" in (ROOT / path).read_text(encoding="utf-8"), node
