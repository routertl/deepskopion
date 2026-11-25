# rr unknown-llm (AI-generated)
"""DE-C0 (DS-P1.44) — falsifiable tests for the capture-correctness harness.

These bind the four DE-C0 done-conditions to executable checks. Every test that
guards a mechanism also demonstrates its teeth (a mutation that makes it red),
because a green capture check that cannot fail is exactly the false-green DE-C0
exists to forbid.

WHAT IS AND ISN'T PROVEN HERE (see sw/stream/testbench/capture_correctness.py
and docs/GOLDEN_PATH.md "DE-C0"):
  * The decode + producer + WS-C0-checker chain is REAL board code; only the
    DDR ring's byte source is a fake, because DE-C0's board (DE-B1) has never
    booted — a LIVE DE25 capture is blocked, not run here.
  * "The released viewer decodes it" is bound via the vendored WS-C0
    check_session (the contract both products are held to); RW-WS1's own C++
    checker lives in the logic-trace repo, outside this
    worktree.

Deploy environment: these are pure pytest unit tests (no board, no queued sim,
no external repo, no network beyond an AF_UNIX socket in a tempdir) and run in a
fresh clone / cron / sandbox. They are NOT wired into any commit gate — the
project's hooks.pre_commit.tests is [] and there is no CI, the same status as
the sibling DE-B1 guards (tools/tests/test_de25_boot_precondition_p1_43.py). Run
them with `python3 -m pytest sw/stream/tests/test_capture_correctness.py`.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from sw.stream import channels as ch
from sw.stream import protocol as proto
from sw.stream.data_source import Annotation, Batch
from sw.stream.testbench import capture_correctness as cc
from sw.stream.testbench import de_c0_stimulus_contract as stim_contract

# The vendored WS-C0 contract (put on sys.path by the harness import above).
C = cc.C

_REPO_ROOT = Path(__file__).resolve().parents[3]
_XILINX_ORACLE = (_REPO_ROOT / "hw/sim/cocotb/tests/datalogger/infra"
                  / "full_data_path/test_datalogger_to_ddr.py")


# ── #1 + #2: exact decoded payload + WS-C0 conformance, per protocol ──────────

@pytest.mark.parametrize("case", cc.known_cases(), ids=lambda c: c.name)
def test_case_decodes_exact_payload_and_conforms(case):
    """Each known stimulus decodes to its EXACT expected payload in order AND
    the produced stream is WS-C0 conformant (real server, real socket, vendored
    check_session). This is DE-C0 done-conditions #1 and #2 at the surface a
    consumer reaches — the wire — not a helper's return value: run_case reads
    ``observed`` back out of the captured wire frames (see
    test_observed_payload_is_read_from_the_wire_not_the_batch)."""
    v = cc.run_case(case)
    assert v.status == "PASS", (
        f"{case.name}: {v.detail}\n  expected={v.expected!r}\n"
        f"  observed={v.observed!r}\n  {v.conformance}")
    assert v.observed == case.expected
    assert v.conformance == "ACCEPT"


def test_run_all_is_green_on_the_real_cases():
    verdicts, code = cc.run_all()
    assert code == 0, [(v.name, v.detail) for v in verdicts if v.status != "PASS"]
    assert {v.protocol for v in verdicts} == {"uart", "i2c", "spi", "edge"}


def test_edge_oracle_uses_the_clock_the_source_advertises(monkeypatch):
    """The edge oracle must derive its tick period from the real wire HELO.

    This runs the actual DDR decoder and socket server as a 50 MHz source.  The
    transitions are deliberately not all whole microseconds, so decoding them
    with the old independent 100 MHz oracle constant changes the observed
    timestamps and turns the case RED.  The source clock and oracle clock can
    therefore no longer silently agree on an unrelated board default.
    """
    sample_clk_hz = 50_000_000
    transitions = [(10_000, 1), (10_100, 0), (10_240, 1), (10_400, 0)]
    edge_case = replace(
        next(c for c in cc.known_cases() if c.name == "edge_scl"),
        build_frames=lambda: cc._edge_batch_frame(
            cc.EDGE_CHANNEL, transitions, clk_mhz=sample_clk_hz // 1_000_000),
        expected=transitions,
    )

    real_ddr_source = cc.DdrRingDataSource
    real_capture_session = cc.capture_session
    advertised = []

    def source_at_50mhz(*args, **kwargs):
        kwargs["sample_clk_hz"] = sample_clk_hz
        return real_ddr_source(*args, **kwargs)

    def capture_from_50mhz(batches, sample_clk_hz=sample_clk_hz):
        helo, captured, session, rc = real_capture_session(
            batches, sample_clk_hz=sample_clk_hz)
        advertised.append(helo["sample_clk_hz"])
        return helo, captured, session, rc

    monkeypatch.setattr(cc, "DdrRingDataSource", source_at_50mhz)
    monkeypatch.setattr(cc, "capture_session", capture_from_50mhz)

    verdict = cc.run_case(edge_case)
    assert advertised == [sample_clk_hz], "test source did not advertise 50 MHz"
    assert verdict.status == "PASS", (
        f"oracle ignored HELO sample_clk_hz={sample_clk_hz}: {verdict.detail}; "
        f"expected={verdict.expected!r}, observed={verdict.observed!r}")
    assert verdict.observed == transitions


def test_decode_uses_the_real_per_protocol_decoder():
    """Guards that the harness exercises the REAL sw.stream.decoders path (not a
    reimplementation): the i2c_write case must produce `i2c_byte` annotations
    carrying the decoder's contracted role keys {data:int, ack:bool}. If the
    harness ever hand-rolled its own decode, these decoder-specific fields would
    not appear."""
    case = next(c for c in cc.known_cases() if c.name == "i2c_write")
    batch = cc.decode_case(case)
    i2c_bytes = [a for a in batch.annotations if a.kind == "i2c_byte"]
    assert len(i2c_bytes) == 4, [a.kind for a in batch.annotations]
    for a in i2c_bytes:
        assert set(a.fields) >= {"data", "ack"}
        assert isinstance(a.fields["ack"], bool)
    # The address byte 0xA0 is decoded first, immediately before the data run —
    # the exact ordering DS-X3's oracle pins for the Xilinx baseline.
    assert [a.fields["data"] for a in i2c_bytes] == [0xA0, 0xAB, 0xCD, 0x3E]


# ── #3: a wrong expected payload fails the run ────────────────────────────────

def test_wrong_expected_payload_fails():
    """DE-C0 done-condition #3, made executable: corrupting only the EXPECTED
    bytes (the stimulus/decode is untouched, so the hardware path still behaves)
    must turn the run RED. A capture check whose expectation cannot disagree
    with reality can never fail.

    MUTATION: this IS the mutation — the last data byte's expectation flipped
    0x3E -> 0x3F while the decoded wire stream stays 0x3E.
    BASELINE: the unmutated case (test_case_decodes_exact_payload_and_conforms)
    passes, so the flip is what makes the difference."""
    case = next(c for c in cc.known_cases() if c.name == "i2c_write")
    good = list(case.expected)                         # [..., (0x3E,True), "stop"]
    assert good[-2] == (0x3E, True) and good[-1] == "stop"
    wrong_expected = good[:-2] + [(0x3F, True), "stop"]   # flip 0x3E -> 0x3F
    wrong = replace(case, expected=wrong_expected)
    v = cc.run_case(wrong)
    assert v.status == "FAIL"
    assert v.observed == good                          # the real wire capture
    assert v.observed[-2] == (0x3E, True)
    assert v.expected[-2] == (0x3F, True)              # the wrong expectation


def test_corrupted_stimulus_is_detected():
    """The dual of #3: corrupting the STIMULUS (one ring byte) while leaving the
    expectation correct must also fail — proving the payload check reads the
    real decoded output, not a constant. Exercises the guarded mechanism (the
    decode) rather than the expectation."""
    case = next(c for c in cc.known_cases() if c.name == "spi_mosi")
    corrupt = replace(
        case,
        build_frames=lambda: cc._spi_frames().replace(
            bytes([0xBE, 0, 0, 0]), bytes([0xB6, 0, 0, 0])))
    v = cc.run_case(corrupt)
    assert v.status == "FAIL"
    assert v.observed == [0xDE, 0xAD, 0xB6, 0xEF]      # the corrupted capture
    assert v.expected == [0xDE, 0xAD, 0xBE, 0xEF]


# ── #2 teeth: an ACCEPT means something — a bad capture is REJECTED ───────────

def _real_helo() -> dict:
    """The exact HELO the real server opens a session with (the production
    channel table). Used to judge a deliberately-non-conformant annotation
    against a REAL declared channel set, not a hand-built one."""
    good = cc.decode_case(next(c for c in cc.known_cases() if c.name == "uart_marker"))
    helo, _, _, _ = cc.capture_session([good])
    return helo


def test_ws_c0_check_rejects_a_renamed_role_key():
    """"The check is not decorative": a decoded i2c_byte annotation with its
    contracted `ack` role key renamed to `acked` — the same class of one-field
    drift WS-C0's README names as a shipped defect (D.14, the detail/message
    mislabel) and DS-WS1 caught live — must be REJECTED by the vendored checker
    the capture harness relies on. Judged with the REAL server HELO so the
    rejection is about the annotation, not the handshake.

    This proves an ACCEPT in test_case_... is a real signal: the same checker
    that accepts the good capture rejects a subtly-broken one."""
    helo = _real_helo()
    bad_ann = {
        "ts_start": 1, "ts_end": 2, "channel_id": 101, "kind": "i2c_byte",
        "text": "0xAB ACK", "color": "#4a8c2a", "is_error": False,
        "payload": {"data": 0xAB, "acked": True},   # 'ack' renamed -> 'acked'
    }
    session = [
        ("p2c", proto.TAG_HELO, helo),
        ("c2p", proto.TAG_HELO_OK, {}),
        ("p2c", proto.TAG_ANNOTATIONS, {"seq": 1, "annotations": [bad_ann]}),
    ]
    result = C.check_session(session)
    assert not result.accepted, "renamed role key was accepted — check is dead"
    assert any("ack" in f for f in result.fields), result.fields


def test_ws_c0_check_rejects_an_undeclared_channel():
    """A decoded annotation on a channel HELO never declared (the exact defect
    class `reject_annotation_channel_ref` pins) must be REJECTED — a capture
    that decodes bytes onto a lane the viewer has no row for renders as a
    working session that silently drops events."""
    helo = _real_helo()
    bad_ann = {
        "ts_start": 1, "ts_end": 2, "channel_id": 987, "kind": "uart_byte",
        "text": "0x41 'A'", "color": "#4a8c2a", "is_error": False,
        "payload": {"byte": 0x41},
    }
    session = [
        ("p2c", proto.TAG_HELO, helo),
        ("c2p", proto.TAG_HELO_OK, {}),
        ("p2c", proto.TAG_ANNOTATIONS, {"seq": 1, "annotations": [bad_ann]}),
    ]
    result = C.check_session(session)
    assert not result.accepted
    assert any("channel_id" in f for f in result.fields), result.fields


# ── #1/#2 teeth ON THE HARNESS PATH: the wire binding, not the vendored check ─
#
# The two test_ws_c0_check_rejects_* tests above re-prove the vendored checker
# (DS-WS1/WS-C0) against hand-built sessions; they never call run_case, so
# breaking run_case's wire binding would leave them green. The tests below drive
# run_case / capture_session itself and go RED the instant the observed payload
# stops being read from the captured wire frames — that is the binding DE-C0
# exists to keep true.

def _decode_patched(monkeypatch, mutate):
    """Patch cc.decode_case so it applies ``mutate`` to the decoded Batch before
    it is fed to the real server — i.e. inject a decoder defect and watch the
    whole run_case pipeline (server + socket + WS-C0 + wire read-back) react."""
    orig = cc.decode_case

    def patched(case):
        b = orig(case)
        mutate(b)
        return b

    monkeypatch.setattr(cc, "decode_case", patched)


def test_observed_payload_is_read_from_the_wire_not_the_batch(monkeypatch):
    """The load-bearing DE-C0 binding: a decoded annotation the producer's WS-C0
    guard REFUSES to broadcast (here: the `ack` role key renamed to `acked`, the
    D.14 one-field-drift class) never reaches the wire, so run_case reads an
    empty payload back and FAILS — even though the in-process Batch still carries
    the 'right' bytes and WS-C0 ACCEPTs the (now data-less) session.

    MUTATION: rename ack->acked on every i2c_byte after decode.
    If run_case ever reverts to extracting from the in-process Batch, observed
    would be the full byte run and this would PASS — that regression is exactly
    what this test forbids."""
    def rename_ack(batch):
        for a in batch.annotations:
            if "ack" in a.fields:
                a.fields["acked"] = a.fields.pop("ack")

    _decode_patched(monkeypatch, rename_ack)
    case = next(c for c in cc.known_cases() if c.name == "i2c_write")
    v = cc.run_case(case)
    assert v.status == "FAIL", v.detail
    assert v.observed == []                     # nothing reached the wire
    assert v.conformance == "ACCEPT"            # the empty session still conforms


def test_run_case_fails_when_decode_targets_an_undeclared_channel(monkeypatch):
    """A decoder that stamps the right bytes on a channel_id HELO never declared
    (channel 987) is refused by the producer guard — zero data frames on the
    wire — so run_case FAILs. The exact 'ships a capture the viewer silently
    drops' defect the module docstring names."""
    def restamp(batch):
        for a in batch.annotations:
            a.channel_id = 987

    _decode_patched(monkeypatch, restamp)
    case = next(c for c in cc.known_cases() if c.name == "uart_marker")
    v = cc.run_case(case)
    assert v.status == "FAIL", v.detail
    assert v.observed == []


def test_run_case_fails_when_decode_targets_a_declared_but_wrong_channel(monkeypatch):
    """Subtler than the undeclared case: stamp UART bytes onto 101 (i2c's
    DECLARED lane). WS-C0 ACCEPTs (the channel exists), but run_case filters the
    wire annotations by the case's own channel_id (100), finds none, and FAILs.
    channel_id is bound, not merely type-checked."""
    def restamp(batch):
        for a in batch.annotations:
            a.channel_id = 101      # i2c's declared channel, not uart's 100

    _decode_patched(monkeypatch, restamp)
    case = next(c for c in cc.known_cases() if c.name == "uart_marker")
    v = cc.run_case(case)
    assert v.status == "FAIL", v.detail
    assert v.observed == []
    assert v.conformance == "ACCEPT"


def test_run_case_fails_when_i2c_start_stop_events_are_dropped(monkeypatch):
    """I2C 'exact payload AND ordering': dropping the START/STOP event frames
    (which the copied DdrRing test REQUIRES around the byte run) must turn the
    run RED. The old harness kept only i2c_byte.data, so a session missing its
    framing events still PASSed; the ordered extract binds the events too."""
    def drop_events(batch):
        batch.annotations[:] = [
            a for a in batch.annotations
            if not (a.kind.startswith("i2c_") and a.kind not in ("i2c_byte",))]

    _decode_patched(monkeypatch, drop_events)
    case = next(c for c in cc.known_cases() if c.name == "i2c_write")
    v = cc.run_case(case)
    assert v.status == "FAIL", v.detail
    # the byte tuples survive; the START/STOP markers do not, so ordering breaks
    assert "start" not in v.observed and "stop" not in v.observed
    assert v.expected[0] == "start" and v.expected[-1] == "stop"


# ── #4: same stimuli as the Xilinx baseline oracle ───────────────────────────

def _oracle_list(text, name):
    """Parse the RHS of a `NAME = [ ... ]` assignment in the oracle source into a
    list[int]. Binds to the actual STIMULUS LITERAL, not any substring: a value
    that only appears in a comment or an error string does not satisfy this."""
    import ast
    m = re.search(rf"^\s*{re.escape(name)}\s*=\s*(\[[^\]]*\])", text, re.MULTILINE)
    assert m, f"oracle has no `{name} = [...]` assignment any more"
    return [int(x) & 0xFF for x in ast.literal_eval(m.group(1))]


def _oracle_transitions(text, name):
    import ast
    tree = ast.parse(text)
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            return [tuple(x) for x in ast.literal_eval(node.value)]
    raise AssertionError(f"oracle has no `{name} = [...]` assignment any more")


def test_capture_stimuli_match_the_xilinx_baseline_oracle():
    """DE-C0 done-condition #4: the capture is compared against the Xilinx
    baseline for the SAME stimuli. The exact-payload comparison is only a
    comparison to the Xilinx baseline if these stimuli ARE the Xilinx oracle's
    stimuli — so bind them to the assignment LITERALS in test_datalogger_to_ddr.py
    (the module DS-X3 runs under xilinx_baseline and DE-R0 proved byte-identical
    under de25_baseline). A drift on either side turns this red.

    This does NOT re-measure the PL cross-vendor (that is DE-R0's queued sim);
    it pins that the decoded-payload baseline this harness checks against is the
    same one the Xilinx rehearsal drives, so the two cannot silently diverge.
    Values are parsed from the `NAME = [...]` assignments (not substring-matched)
    so an assertion moved into a comment cannot keep this green."""
    text = _XILINX_ORACLE.read_text()

    # UART: the oracle asserts MARKER + FLUSH as ONE contiguous run; the harness
    # drives and expects exactly that run (cc._uart_frames == MARKER + FLUSH).
    assert _oracle_list(text, "MARKER") == cc.UART_MARKER
    assert _oracle_list(text, "FLUSH") == cc.UART_FLUSH
    uart_case = next(c for c in cc.known_cases() if c.name == "uart_marker")
    assert uart_case.expected == cc.UART_MARKER + cc.UART_FLUSH

    # I2C: the oracle drives DATA as a write to 0x50 (so the address byte on the
    # bus is 0x50<<1 = 0xA0) and asserts that exact address immediately precedes
    # the data run. Bind BOTH the data literal and the real address assertion —
    # the exact line, so deleting `payload_bytes[idx - 1] == 0xA0` turns this red
    # (that token also appears in comments, which is why the assertion — not a
    # bare `0xA0` substring — is what is required here).
    assert _oracle_list(text, "DATA") == cc.I2C_DATA
    assert "i2c.write_transaction(0x50, DATA, read=False)" in text
    assert "payload_bytes[idx - 1] == 0xA0" in text
    assert (0x50 << 1) == cc.I2C_ADDR

    # SPI: the oracle asserts the DDR MOSI run EQUALS the driven MOSI, exactly.
    assert _oracle_list(text, "MOSI") == cc.SPI_MOSI
    assert "mosi_bytes == MOSI" in text

    # EDGE: bind the complete transition literal and the live simulation entry
    # point. Changing one level/interval or deleting the DDR test is red.
    assert _oracle_transitions(text, "EDGE_TRANSITIONS") == cc.EDGE_TRANSITIONS
    assert re.search(r"async def test_edge_to_ddr\(dut\):", text)


def test_live_i2c_armer_uses_the_production_register_sequence():
    """The board armer must execute the sequence that arms the responder and
    triggers one packed transaction; changing oracle data alone is insufficient.

    DS-P1.59 moved this arm into the SHARED production armer
    (``de_c0_production_armer``) so the stop-daemon driver and the daemon-live
    witness cannot drift. This checks the deployed call site — the driver
    delegates to the shared armer, and the armer executes the sequence — not a
    disconnected helper. Removing the loop or the I2C_INJ_CONTROL tuple makes
    it red (see also sw/stream/tests/test_de_c0_production_armer.py)."""
    driver = (_REPO_ROOT / "sw/stream/testbench/de_c0_live_stimulus_driver.py").read_text()
    assert "de_c0_production_armer" in driver and "arm_protocol(rb, proto)" in driver, (
        "the deployed driver must arm via the shared production armer")
    # DS-P2.136: the sequences live in the production module the daemon also
    # calls; the testbench armer re-exports them.
    armer = (_REPO_ROOT / "sw/stream/stimulus_armer.py").read_text()
    assert "for offset, value in I2C_REGISTER_SEQUENCE:" in armer
    assert "rb.write_u32(offset, value)" in armer
    assert stim_contract.I2C_REGISTER_SEQUENCE == (
        (0x30, 0xA1), (0x230, 125), (0x22C, 0x2A1), (0x234, 0x003ECDAB))
    assert stim_contract.I2C_REGISTER_SEQUENCE[0] == (0x30, 0xA1), (
        "I2C_INJ_CONTROL must be armed before stimulus starts")


# ── fail-closed: no false green on missing/failed cases ──────────────────────

def test_run_all_fails_closed_on_a_bad_case():
    """A set containing one un-satisfiable case makes run_all() exit non-zero —
    a single FAIL cannot be rounded up to a green run (mirrors run_self_test's
    fail-closed exit)."""
    good = next(c for c in cc.known_cases() if c.name == "uart_marker")
    bad = replace(good, name="uart_wrong", expected=[0x00])
    verdicts, code = cc.run_all([good, bad])
    assert code == 1
    assert [v.status for v in verdicts] == ["PASS", "FAIL"]


def test_run_all_fails_closed_on_empty_case_set():
    """Zero cases is not a pass — an empty run has confirmed nothing, so it must
    be RED, never a silent green. (The run_self_test lesson: missing evidence is
    not success.)"""
    verdicts, code = cc.run_all([])
    assert verdicts == []
    assert code == 1


def test_extract_i2c_excludes_datalogger_lifecycle_control():
    """DS-P1.59 FINAL: ``_extract_i2c`` decodes the I2C BUS transaction and
    excludes datalogger LIFECYCLE control annotations (``i2c_enabled`` /
    ``i2c_disabled``), mirroring how ``_extract_spi`` keeps only ``spi_mosi``
    (dropping the ``spi_event`` a live SPI capture also carries).

    Silicon-measured (2026-08-29): the shared production armer disables→enables
    the I2C datalogger before the transaction, so a daemon-live capture carries a
    leading ``i2c_enabled`` control annotation ahead of ``start``. Excluding it
    is NOT a loosening — START/STOP/repeated-start, bytes, ACK bits and every
    I2C *error* event stay, so any of those defects still fails the exact match.
    """
    def ann(kind, payload):
        return (proto.TAG_ANNOTATIONS,
                {"seq": 0, "annotations": [{"channel_id": 101, "kind": kind,
                                            "payload": payload}]})

    # lifecycle 'enabled' ahead of a clean ACK-bound transaction -> exactly the
    # bus transaction is decoded (the 'enabled' is dropped).
    captured = [
        ann("i2c_enabled", {"event": "enabled"}),
        ann("i2c_start", {"event": "start"}),
        ann("i2c_byte", {"data": 0xA0, "ack": True}),
        ann("i2c_byte", {"data": 0xAB, "ack": True}),
        ann("i2c_stop", {"event": "stop"}),
    ]
    assert cc._extract_i2c(captured, 101, 50_000_000) == [
        "start", (0xA0, True), (0xAB, True), "stop"]

    # STILL falsifiable: an error event is NOT lifecycle -> it stays and breaks
    # the exact match (an unexpected error is an extra element).
    with_err = [
        ann("i2c_start", {"event": "start"}),
        ann("i2c_stop_error", {"event": "stop_error"}),
        ann("i2c_stop", {"event": "stop"}),
    ]
    assert cc._extract_i2c(with_err, 101, 50_000_000) == [
        "start", "stop_error", "stop"]
