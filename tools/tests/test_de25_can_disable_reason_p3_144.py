"""Guard the evidence recorded beside the DE25 CAN DT nodes.

DS-P3.144 exists because an earlier comment converted a real boot7/boot8 NoC
wedge into a false "the fabric does not decode CAN" root cause.  Comments are
the deployed surface here: this test reads the exact block a maintainer sees
before the two nodes and requires the measured observation, the decoded
address map, and DS-T1.7's settled write-response mechanism and fix.

DS-T2.71 re-enabled the two nodes, so the block this test reads is no longer
a "why it is disabled" note.  The requirement did not soften — it grew.  The
same three facts must still be there (that is what stops the false root cause
coming back), and the block must now also say what makes ENABLING them safe:
the build-time crosscheck in tools/de25_linux_image.py and the declaration in
system_memory_map.yml that it checks against.  Losing either half is how a
future maintainer relearns boot7/boot8 on silicon.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml


ROOT = Path(
    os.environ.get("DE25_TEST_ROOT", Path(__file__).resolve().parents[2])
).resolve()
TARGET = ROOT / "targets/de25_datalogger.yml"


def _can_reason_comment() -> str:
    source = TARGET.read_text(encoding="utf-8")
    start = source.index("  # DS-T2.71 re-enables these two nodes")
    end = source.index("  - path: /soc@0/ethernet@10810000", start)
    return source[start:end]


def test_can_reason_records_decode_and_settled_wedge_mechanism() -> None:
    """The operator-facing reason must distinguish observation from cause."""
    comment = _can_reason_comment()

    # Observation: preserve what boot7/boot8 actually established.
    assert "boot7/boot8" in comment
    assert "ctucanfd probe hung the kernel" in comment

    # Refuted cause: all three selected apertures already exist in u_demux.
    assert "u_demux decodes all three lwh2f apertures" in comment
    assert all(address in comment for address in (
        "0x20000000", "0x20010000", "0x20020000"
    ))
    assert "nothing at 0x2001x/0x2002x" not in comment

    # Settled cause and state: cite DS-T1.7, name the failed handshake, and
    # state the landed RTL correction.
    assert "DS-T1.7" in comment
    assert "s_bvalid_o" in comment and "m_bvalid_i(wr_sel_r)" in comment
    assert "gating s_bvalid_o" in comment


def test_can_reason_explains_what_makes_enabling_them_safe() -> None:
    """DS-T2.71: the block must name the gate that replaced `status: disabled`."""
    comment = _can_reason_comment()
    assert "DS-T2.71" in comment
    assert "check_lwh2f_dt_nodes_are_decoded" in comment
    assert "system_memory_map.yml" in comment
    # The interim measure must be named as interim, not silently dropped.
    assert "DS-P3.144" in comment
    assert "status: disabled" in comment


def test_the_reason_matches_the_configuration_it_describes() -> None:
    """DS-P2.121: de25_datalogger is CAN-free by default; CTU DT nodes are omitted."""
    target = yaml.safe_load(TARGET.read_text(encoding="utf-8"))
    can = [n for n in target["linux"]["dt_nodes"]
           if str(n.get("path", "")).startswith("/soc@0/can@")]
    assert len(can) == 0, (
        f"CTU CAN nodes {can} present in default target {TARGET.name}; "
        "DS-P2.121 requires de25_datalogger to be CAN-free"
    )
