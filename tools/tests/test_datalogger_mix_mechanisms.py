# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.65 — one isolated `rr sim sweep` bench per arbitrary-mix mechanism.

Each bench is built so that only its own mechanism is on the path. Frozen
generator output (tools/tests/datalogger_mix/fixtures, kept equal to the live
generator by test_mix_fixtures_match_generator) stands in for the generator
mechanisms upstream of a gather or core guard:

  - the generator emits an absent protocol's IDs / PROTO_IDX / DOMAIN
  - the generator emits C_EDGE_MAXFREQ_WIDTH with no EDGE instance
  - the gather template declares the EDGE port interface with no EDGE instance
  - the core skips the EDGE pipeline and zeroes EDGE telemetry with no EDGE
  - the core zeroes an absent protocol's telemetry slots (one bench each)
"""

from __future__ import annotations

import importlib.util

import pytest

import datalogger_mix_bench as bench


@pytest.fixture(scope="module", autouse=True)
def _ensure_workspace_synced():
    yield from bench.workspace_synced()


def test_generator_emits_absent_protocol_constants():
    """datalogger_gather_gen.py: an absent protocol (JTAG) still gets its
    t_<p>Index / C_<p>_IDS / C_PROTO_IDX_<p> / C_DOMAIN_<p> declarations, which
    deepskopion_core references unconditionally, so the mix elaborates."""
    failure = bench.bench_failure("gen_absent_constants")
    assert failure is None, failure


def test_generator_emits_edge_maxfreq_width_without_edge():
    """datalogger_gather_gen.py: C_EDGE_MAXFREQ_WIDTH is emitted with zero
    EDGE instances (it sizes the gather's and the core's EDGE signals)."""
    failure = bench.bench_failure("gen_edge_maxfreq_width")
    assert failure is None, failure


def test_gather_declares_edge_ports_without_edge():
    """datalogger_gather.vhd.j2: EDGE_ERR_CNTS / EDGE_MAX_IVALS / EDGE_MAX_NEW
    exist with zero EDGE instances, so the core's port map elaborates."""
    failure = bench.bench_failure("gather_edge_ports")
    assert failure is None, failure


def test_core_skips_edge_pipeline_without_edge():
    """deepskopion_core.vhd gen_edge_pipeline / gen_tel_edge: with zero EDGE
    instances the aggregator, error and max-frequency banks are not built and
    the EDGE telemetry slots read zero."""
    failure = bench.bench_failure("core_edge_pipeline")
    assert failure is None, failure


@pytest.mark.parametrize("protocol", ["uart", "i2c", "spi", "jtag"])
def test_core_absent_protocol_telemetry_reads_zero(protocol: str):
    """deepskopion_core.vhd gen_tel_<p>: an absent protocol's PKT/ERR(/DROP)
    slots read zero instead of the slot its fallback C_PROTO_IDX (0) aliases."""
    failure = bench.bench_failure(f"core_telemetry_{protocol}")
    assert failure is None, failure


def test_mix_fixtures_match_generator():
    """The frozen fixtures are the current generator's output for their mix."""
    spec = importlib.util.spec_from_file_location("ds_mix_sweep", bench.SWEEP_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stale = [
        str(path.relative_to(bench.ROOT))
        for path, text in module.fixture_expectations().items()
        if not path.is_file() or path.read_text(encoding="utf-8") != text
    ]
    assert not stale, (
        f"frozen datalogger-mix fixtures differ from the generator: {stale}; "
        f"refresh with `python {bench.SWEEP_MODULE.relative_to(bench.ROOT)} "
        f"--refresh-fixtures`")
