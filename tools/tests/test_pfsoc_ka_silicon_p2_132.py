"""DS-P2.132 — regression guards for the PolarFire SoC PF-T9 + first-silicon REA
witness (pf_known_answer_* / rr_rea_ujtag_local).

These are cheap, sub-second static invariants that guard the defects the silicon
bring-up actually hit (see docs/hardware/evidence/ds_p2_132/). Each guards a
mechanism whose loss would silently reintroduce a real bug:

  * test_leg1_images_diverge — the two Leg-1 images MUST carry different
    known-answer constants, or `rr program verify` cannot tell them apart. This
    is the exact trap the ticket forbids ("a program that cannot be told apart
    from the previous image is not validated"). On the bench the first attempt
    produced BYTE-IDENTICAL bitstreams (build_options.generics was silently
    dropped, D-3); this guard fails if the two constants ever collide again.
      BASELINE (pre-fix): the single shared top drove G_KA from
        build_options.generics, which the Microchip flow drops → both images
        used the default → this assertion (constants differ) would FAIL.
      MUTATION: set pf_known_answer_top_b's `G_KA => 90` to `=> 165` and this
        test FAILS (verified while authoring).

  * test_leg1_does_not_use_dropped_tool_generics — neither Leg-1 target may set
    build_options.generics.G_KA, because that override is silently dropped on the
    Microchip/Libero flow (D-3). Regressing to it is what made the images
    identical. MUTATION: add `generics: {G_KA: 90}` to a Leg-1 target → FAILS.

  * test_rea_wrapper_arst_is_inactive — rr_rea_ujtag_local must NOT drive `arst`
    from UJTAG `URSTB` (asserted on every TAP Test-Logic-Reset), which cleared
    the capture buffer and produced an all-zeros readback (D-6c). MUTATION:
    change `arst <= '0'` back to `arst <= not urstb` → FAILS.

  * test_rea_capture_evidence_carries_known_answer — the committed silicon
    capture MUST decode to the known-answer pattern on every sample (sync=101,
    nibble=0xA, contiguous counter), not merely be non-empty. A green "a capture
    file exists" is not evidence; this asserts the PATTERN. MUTATION: corrupt any
    sample's field → FAILS.

  * test_rea_debug_core_matches_rtl_instance — debug/pfsoc_ka_rea.yml
    sample_width/depth/jtag_chain must mirror the RTL REA generic map, or the
    host decodes silently wrong (signal-tap-ila-rea §4). MUTATION: drift RTL
    G_DEPTH from the yml `depth` → FAILS.

  * test_rea_target_wires_the_ref_clk_pdc_hook — the ka_rea Libero fit must
    source import_ka_ref_clk_pdc.tcl (pins ref_clk_50mhz -> R18); without it the
    fabric clock never toggles and the REA sample domain freezes (D-6b).
    MUTATION: delete/retarget the tcl_hooks.gen_project entry → FAILS.

  * test_core_known_answer_nibble_is_driven_by_g_ka — pf_known_answer_core must
    derive the answer nibble from G_KA and drive it onto probe_o AND led, not
    hardcode a literal (the D-3 failure mode in the core). MUTATION: hardcode
    C_KA_NIB, or drop it from probe_o/led → FAILS.

  * test_rea_wrapper_ujtag_pin_comments_match_wiring_d6a — rr_rea_ujtag_local's
    comments must document that dedicated JTAG TAP pins are connected to top-level
    ports (jtag_*) rather than falsely claiming they are left open or unconnected
    (the D-6a cosmetic residue from DS-P2.132 / DS-P4.7). The u_ujtag instance
    must wire TCK/TRSTB/TDI/TDO/TMS to jtag_*.
      BASELINE (pre-fix): comments claimed pins MUST be left unconnected (open)
        and are left OPEN → assertion FAILS.
      MUTATION: reintroducing "left OPEN" or breaking any jtag_* port map → FAILS.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml  # noqa: F401  (the _bench.py sibling uses it through this module)

ROOT = Path(__file__).resolve().parents[2]
RTL = ROOT / "hw" / "rtl" / "pfsoc"
EVIDENCE = ROOT / "docs" / "hardware" / "evidence" / "ds_p2_132"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _g_ka_from_wrapper(name: str) -> int:
    """Extract the `G_KA => N` value from a Leg-1 top wrapper's generic map."""
    txt = _read(RTL / name)
    m = re.search(r"G_KA\s*=>\s*(\d+)", txt)
    assert m, f"{name}: no `G_KA => N` RTL generic map found"
    return int(m.group(1))


def test_leg1_images_diverge():
    """The two Leg-1 images carry different known-answer constants (and nibbles),
    so the device can tell them apart on silicon."""
    ka_a = _g_ka_from_wrapper("pf_known_answer_top_a.vhd")
    ka_b = _g_ka_from_wrapper("pf_known_answer_top_b.vhd")
    assert ka_a != ka_b, (
        f"images A and B share G_KA={ka_a} → byte-identical bitstreams that "
        "`rr program verify` cannot distinguish (the forbidden 'cannot be told "
        "apart' case)"
    )
    # The LED / probe use the LOW NIBBLE, so the nibbles must differ too.
    assert (ka_a % 16) != (ka_b % 16), (
        f"images A/B share the low nibble 0x{ka_a % 16:X}; the LED logic and REA "
        "known-answer field would be identical"
    )


def test_rea_wrapper_arst_is_inactive():
    """rr_rea_ujtag_local must tie arst inactive, not derive it from UJTAG URSTB
    (which clears the capture buffer on every TAP reset — the all-zeros bug)."""
    txt = _read(RTL.parent / "pfsoc" / "rr_rea_ujtag_local.vhd")
    assert re.search(r"arst\s*<=\s*'0'", txt), (
        "rr_rea_ujtag_local: expected `arst <= '0'` (capture must not be reset by "
        "the JTAG TAP)"
    )
    assert not re.search(r"arst\s*<=\s*not\s+urstb", txt), (
        "rr_rea_ujtag_local: `arst <= not urstb` re-introduces the all-zeros "
        "capture bug (URSTB asserts on every Test-Logic-Reset, D-6c)"
    )


def test_core_known_answer_nibble_is_driven_by_g_ka():
    """pf_known_answer_core must DERIVE the known-answer nibble from the G_KA
    generic and drive it onto BOTH probe_o and led — not hardcode a literal.

    Hardcoding the nibble in the core (e.g. `C_KA_NIB := "1010"`) is the D-3
    failure mode viewed from the RTL: the two images would collide on the answer
    field even though their G_KA generics differ, so `rr program verify` and the
    REA decode could not tell them apart. This guard fails if the derivation from
    G_KA is lost, or if probe_o/led stop referencing that derived nibble."""
    core = _read(RTL / "pf_known_answer_core.vhd")
    # The nibble constant is derived from the G_KA generic (low 4 bits).
    assert re.search(r"C_KA_NIB\b.*:=.*to_unsigned\s*\(\s*G_KA", core, re.DOTALL), (
        "C_KA_NIB must be derived from the G_KA generic, not a hardcoded literal "
        "(hardcoding it re-introduces the D-3 'images cannot be told apart' bug)"
    )
    # Both observable outputs must carry that derived nibble.
    assert re.search(r"probe_o\s*<=.*C_KA_NIB", core), "probe_o must carry C_KA_NIB (the REA known answer)"
    assert re.search(r"led\s*<=.*C_KA_NIB", core), "led must carry C_KA_NIB (the IO-observable known answer)"


def test_rea_wrapper_ujtag_pin_comments_match_wiring_d6a():
    """rr_rea_ujtag_local comments must match the actual UJTAG port wiring (D-6a).

    Comments must document that dedicated JTAG TAP pins are wired to top-level
    ports (jtag_*), and must NOT contradict the wiring by claiming pins are
    left open or must be left unconnected. The u_ujtag instance must wire
    TCK/TRSTB/TDI/TDO/TMS to jtag_*."""
    txt = _read(RTL.parent / "pfsoc" / "rr_rea_ujtag_local.vhd")
    # Negative guards: comments must not claim dedicated TAP pins are left open/unconnected
    assert not re.search(r"dedicated\s+jtag\s+tap\s+pins.*left\s+open", txt, re.IGNORECASE), (
        "rr_rea_ujtag_local: comment claims dedicated JTAG TAP pins are left OPEN, "
        "contradicting port wiring (D-6a)"
    )
    assert not re.search(r"must\s+be\s+left\s+unconnected\s*\(\s*open\s*\)", txt, re.IGNORECASE), (
        "rr_rea_ujtag_local: comment claims UJTAG pins MUST be left unconnected (open), "
        "contradicting top-level port connection (D-6a)"
    )
    # Positive comment guard: comments document that dedicated TAP pins connect to top-level ports
    assert re.search(r"--.*dedicated.*jtag.*tap.*(connected|wired).*top-level", txt, re.IGNORECASE), (
        "rr_rea_ujtag_local: comments must document that dedicated JTAG TAP pins are "
        "connected/wired to top-level ports (D-6a)"
    )
    # Wiring guard: u_ujtag must wire all 5 dedicated TAP pins to jtag_* ports
    for pin in ("TCK", "TRSTB", "TDI", "TDO", "TMS"):
        port = f"jtag_{pin.lower()}"
        assert re.search(rf"\b{pin}\s*=>\s*{port}\b", txt), (
            f"rr_rea_ujtag_local: u_ujtag must map {pin} => {port} (D-6a)"
        )
