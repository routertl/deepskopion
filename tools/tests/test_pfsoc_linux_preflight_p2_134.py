"""DS-P2.125 / PF-L0: the ``rr linux`` pfsoc build_env board-pinout gate is OPEN.

## History: what this file used to guard, and why it changed polarity

PF-L0 (DS-P2.125) landed a full ``linux:`` section on
``targets/pfsoc_datalogger.yml`` (bootargs, DT nodes, riscv64 rootfs) and a
suite of *config* guards (``test_pfsoc_dt_nodes_p2_125.py``,
``test_pfsoc_linux_image_p2_125.py``, ``test_pfsoc_target_contract.py``) that
all passed — so the target *looked* Linux-boot-ready. PF-L0a (DS-P2.134), a
live boot smoke on the Discovery Kit, found that **every** ``rr linux`` command
for this target died immediately, before any Linux artefact was built:

    sdk.engine.board_pinout.BoardPinoutError: No emitter registered for
    vendor 'microchip'. Supported: xilinx, altera (intel), lattice/gowin.

The cause was a coupling in the shared SDK: ``rr linux doctor`` / ``hss`` /
``uboot`` / ``kernel`` regenerate ``build_env``
(``routertl_core.project_manager.generate_mk_file``), which unconditionally
materialises the *fabric* board pinout for ``hardware.vendor``
(``_materialize_board_pinout`` → ``constraint_emitters.emit_for_vendor``). The
pfsoc target is ``hardware.vendor: microchip`` with an active board
subscription, and the SDK had **no microchip board-pinout emitter** (the PF-H0
fabric-emitter gap). PF-L0a encoded that blocker as two **RED-on-close** guards
that pinned the exact ``BoardPinoutError`` with ``pytest.raises(match=...)``:
they PASSED while the gap was open and were designed to go **RED the moment the
SDK stopped raising that error** — the true "re-run PF-L0a end to end" signal.

## The gap is now CLOSED — the guards fired, and are retired here

The shared SDK registered a microchip board-pinout emitter
(``RTL-P2.1211`` / ``RTL-P3.238``; the same fix RTL-P2.1215 decouples the Linux
flow around). ``_materialize_board_pinout`` for the pfsoc target now **succeeds**
— it materialises the 21-pin ``polarfire-soc-discovery`` PDC instead of raising —
so the two ``pytest.raises(BoardPinoutError)`` guards turned red exactly as their
own design note said they would. Their job is done: they were a one-shot signal,
not a standing invariant, and a suite cannot pass with them red.

Retiring a fired RED-on-close guard is not the same as deleting a live guard to
force a green bar. What replaces them below is a **forward guard on the same
caller surface** (``_materialize_board_pinout`` with the pfsoc target's own
config) that pins the *closed* state: the pfsoc build_env regen now emits a
Microchip PDC and does **not** raise. That guard has power in the direction that
now matters — if the microchip emitter is ripped back out (or the Linux flow is
re-coupled to a missing fabric emitter), ``_materialize_board_pinout`` raises
``BoardPinoutError`` again and this test goes RED, re-blocking the PF-L0 boot
leg loudly instead of shipping an image whose ``rr linux`` chain is dead on
arrival. Asserted at ``_materialize_board_pinout`` — the exact function the live
boot smoke crashed in — not at a helper return value.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PFSOC_TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"


def _pfsoc_config() -> dict:
    return yaml.safe_load(PFSOC_TARGET.read_text())


def test_rr_linux_build_env_regen_emits_microchip_pinout_no_longer_blocked() -> None:
    """FORWARD guard at the real caller surface of the (now-closed) PF-L0a blocker.

    ``rr linux <doctor|hss|uboot|kernel>`` regenerates build_env, which calls
    ``routertl_core.project_manager.generate_mk_file`` →
    ``_materialize_board_pinout`` → ``constraint_emitters.emit_for_vendor`` for
    ``hardware.vendor``. This drives that exact function with the pfsoc target's
    **own** config, reproducing the path the live boot smoke crashed on.

    TODAY it SUCCEEDS: the shared SDK now has a microchip emitter, so the call
    materialises a PDC and returns the emitted path(s) instead of raising
    ``BoardPinoutError``. Pinning that closed state means the test goes RED the
    moment the SDK stops emitting a microchip pinout (emitter reverted, or the
    Linux flow re-coupled to a missing fabric emitter) — which is precisely the
    signal that the PF-L0 boot leg is re-blocked.
    """
    from routertl_core.project_manager import _materialize_board_pinout
    from sdk.engine.board_pinout import BoardPinoutError

    cfg = _pfsoc_config()
    hardware = cfg.get("hardware") or {}
    # Premise: the coupling only reaches emit_for_vendor for a microchip target
    # with an active board subscription. If either fact changes the chain below
    # no longer exercises the microchip emitter, so re-derive this guard.
    assert str(hardware.get("vendor", "")).lower() == "microchip", (
        "premise drift: pfsoc target is no longer microchip; re-derive this guard "
        "against the new shape before trusting it."
    )
    assert str(hardware.get("platform", "")) and (hardware.get("pins") or []), (
        "premise drift: pfsoc target no longer has an active board subscription "
        "(platform + pins); the fabric-pinout emit would short-circuit and this "
        "guard would no longer exercise the microchip emitter."
    )

    # The closed state: no BoardPinoutError, and a real PDC was materialised. If
    # the microchip emitter is removed, this call raises BoardPinoutError again
    # and the assertion below never runs — the test goes RED.
    try:
        emitted = _materialize_board_pinout(cfg, proj_root=str(ROOT))
    except BoardPinoutError as exc:  # pragma: no cover - only if the gap reopens
        raise AssertionError(
            "PF-L0 boot leg re-blocked: rr linux build_env regen for the pfsoc "
            "target raised BoardPinoutError again — the microchip board-pinout "
            f"emitter appears to have been removed:\n{exc}"
        )

    assert emitted, (
        "build_env regen returned no board-pinout artefact for the pfsoc target; "
        "expected the materialised polarfire-soc-discovery PDC path(s)."
    )
    pdc_paths = [ROOT / p for p in emitted]
    assert any(p.suffix == ".pdc" and p.exists() for p in pdc_paths), (
        "expected a materialised Microchip .pdc on disk, got: "
        f"{[str(p) for p in pdc_paths]}"
    )


def test_microchip_pinout_and_extension_emitters_both_present() -> None:
    """Unit-level FORWARD guard on both microchip vendor hooks the build_env regen
    touches.

    ``_materialize_board_pinout`` calls ``emit_for_vendor`` and then
    ``vendor_extension``; PF-L0a required BOTH to gain microchip support before
    the Linux flow could be unblocked. Pin each independently so a *partial*
    regression (one hook removed, the other kept) is still caught red.
    """
    from sdk.engine.board_pinout import BoundPin
    from sdk.engine.constraint_emitters import emit_for_vendor, vendor_extension

    part = str((_pfsoc_config().get("hardware") or {}).get("part", ""))
    bound = [
        BoundPin(
            port_name="led[0]",
            package_pin="A1",
            io_standard="LVCMOS18",
            drive_strength_ma=None,
            voltage_v=1.8,
            pull=None,
            diff_partner=None,
            input_termination=None,
            required_drive=None,
        )
    ]
    pdc = emit_for_vendor(
        "microchip", bound, board_id="polarfire-soc-discovery", fpga_part=part
    )
    assert isinstance(pdc, str) and "set_io" in pdc, (
        "microchip emit_for_vendor no longer returns a Microchip PDC "
        f"(set_io constraints); got: {pdc!r}"
    )
    assert vendor_extension("microchip", part) == "pdc", (
        "microchip vendor_extension no longer maps to the .pdc constraint format"
    )


# ── DS-P2.125 / PF-L0 boot-session guard: never build Icicle firmware for the
# Discovery Kit (GAP B). ──────────────────────────────────────────────────────
#
# Measured 2026-09-12 on the live board leg (docs/evidence/pf-l0/
# BOOT_SESSION_2026-09-12.md): the SDK boot-chain gaps this ticket filed are now
# CLOSED — `rr linux hss|uboot|kernel|dtb` all built for the Discovery Kit, and
# the built U-Boot carried `microchip-mpfs-disco-kit.dtb`. The single most
# dangerous of those gaps was GAP B: `_ARCH_TABLE["mpfs-disco-kit"]` was a *copy*
# of the Icicle entry, so `rr linux` silently built MPFS250T/LPDDR4-x32 Icicle
# firmware for this MPFS095T/DDR4-x16 board. The eNVM flash is a one-time,
# owner-authorised, semi-irreversible act; an Icicle DTB/HSS baked into it cannot
# bring up this board's DRAM. RTL-P2.1219 gave the disco/discovery arch entries
# their real board facts. This guard pins that at the exact selection surface the
# build consumes (`override or arch.default`, mirroring `_resolve_pfsoc_kernel_dtb`
# / `_resolve_pfsoc_uboot_device_tree` / `_resolve_pfsoc_hss_board`), keyed on the
# pfsoc target's OWN declared config rather than whichever target is active — so a
# regression to Icicle facts goes RED here before it can reach the board.


def test_pfsoc_build_selects_discovery_kit_never_icicle_firmware() -> None:
    """GAP-B guard: the pfsoc target must resolve to Discovery-Kit DTB/HSS facts.

    BASELINE (pre-RTL-P2.1219): the disco/discovery arch entries were Icicle
    copies, so `kernel_dtb`/`uboot_device_tree` (which the target does NOT
    override) resolved to `microchip-mpfs-icicle-kit` → this test would have
    failed RED, catching the Icicle-firmware-on-a-Disco-Kit defect.
    MUTATION: flip either arch entry back to an Icicle DTB and the same
    assertion fails — verified 2026-09-12 by monkeypatching `pfsoc_arch`.
    """
    from sdk.cli.commands.linux_steps._pfsoc import pfsoc_arch

    cfg = _pfsoc_config()
    hardware = cfg.get("hardware") or {}
    platform = str(hardware.get("platform", "")).strip()
    assert platform, (
        "premise drift: pfsoc target no longer declares hardware.platform; the "
        "board→arch resolution this guard mirrors no longer applies."
    )

    arch = pfsoc_arch(platform)
    linux_cfg = cfg.get("linux") or {}

    # Mirror the SDK resolvers exactly: an explicit target override wins, else the
    # arch-table default. These three feed `rr linux uboot|kernel|hss`.
    effective = {
        "uboot_device_tree": linux_cfg.get("uboot_device_tree") or arch.uboot_device_tree,
        "kernel_dtb": linux_cfg.get("kernel_dtb") or arch.kernel_dtb,
        "hss_board": linux_cfg.get("hss_board") or arch.hss_board,
    }
    # The HSS build also reads the family token from system_memory_map.yml.
    smm = yaml.safe_load((ROOT / "sw" / "linux" / "pfsoc" / "system_memory_map.yml").read_text())
    effective["family_token"] = str((smm or {}).get("target", "")).strip()

    for knob, value in effective.items():
        low = str(value).lower()
        assert "disco" in low, (
            f"pfsoc {knob} resolves to {value!r} — expected a Discovery-Kit value "
            "(containing 'disco'). A non-disco selection means rr linux would build "
            "the wrong board's firmware (GAP B)."
        )
        assert "icicle" not in low, (
            f"pfsoc {knob} resolves to {value!r} — an ICICLE-Kit value on the "
            "Discovery-Kit target. This is exactly the GAP-B defect that would bake "
            "Icicle firmware into this board's eNVM; RTL-P2.1219 must not regress."
        )


# ── PF-L0a (DS-P2.134 re-dispatch, 2026-09-13): the transient HSS proof cannot
# resolve MMUART0 on the real bench descriptor. ───────────────────────────────
#
# The 2026-09-12 boot session's blocker 1 ("rr has no PolarFire SoC programmer
# backend") is CLOSED: RTL-P2.1253 landed an MPFS-OpenOCD/FlashPro5 transport.
# On the live Discovery Kit (2026-09-13, docs/evidence/pf-l0a/
# DS-P2.134-pf-l0a-boot-smoke.md) `rr program pfsoc hss-load` now examines the
# 5-hart chain (live idcode 0x0F8181CF) and DOWNLOADS the transient HSS to
# 0x0A000000 (148096 bytes) — real, verified progress. (DS-P2.144: 0x0A000000 is
# the L2 scratchpad, unbacked without firmware WayEnable composition; the backed
# target is L2-LIM 0x08000000, which is the RTL-P2.1253 wrong-target defect.) But
# it then dies at `transport.read_console()`, verbatim:
#
#     RuntimeError: PolarFire SoC MMUART0 console device not resolved from live
#     bench inventory (expected the FlashPro5 UART by-id handle).
#
# Root cause (reproduced, not inferred): read_console reads
# `resolve_pfsoc_live_board_identity(board)["console"]`, and that resolver used to
# look the board up with `targets.get(board)` — a lookup BY TARGET-DICT KEY. The host
# bench descriptor (runtimes/hw.local.yml) keys its target by a bench-target NAME
# (`polarfire_bench`) and carries the board id in a `board:` FIELD, exactly as
# `jtag_cable_for` (which DID resolve the serial) expects.
# RouteRTL resolved the defect by traversing targets by their `board:` field.
# This test inverts the former RED-on-close guard into a permanent regression guard
# ensuring `resolve_pfsoc_live_board_identity` resolves MMUART0 for a descriptor
# whose target is keyed by name.


def test_pfsoc_transient_hss_console_resolves_from_bench_descriptor(tmp_path, monkeypatch) -> None:
    """Regression guard on the transient HSS proof's MMUART0 console resolution.

    Asserted at the exact input `read_console` consumes: read_console raises its
    "MMUART0 console device not resolved" error precisely when
    `resolve_pfsoc_live_board_identity(board).get("console")` is falsy. This
    pins that resolver's output for a descriptor of the REAL host shape (target
    keyed by a bench-target name, board id in a `board:` field).

    Inverted from the former RED-on-close guard (DS-P3.161) now that RouteRTL's
    resolver traverses targets by their `board:` field.
    """
    from sdk.cli.commands.linux_steps._pfsoc import resolve_pfsoc_live_board_identity

    board = "polarfire-soc-discovery"
    console_dev = (
        "/dev/serial/by-id/usb-Microsemi_Embedded_FlashPro5_03ASSOGW-if02-port0"
    )

    def _write_bench(target_key: str) -> str:
        cfg = {
            "bench": {
                "resources": {
                    "uart_pf": {"kind": "uart", "dev": console_dev},
                    "jtag_pf": {"kind": "jtag", "cable": "03ASSOGW"},
                },
                "targets": {
                    target_key: {"board": board, "uses": ["jtag_pf", "uart_pf"]},
                },
            }
        }
        path = tmp_path / f"bench_{target_key}.yml"
        path.write_text(yaml.safe_dump(cfg))
        return str(path)

    # The real host shape: target keyed by a bench-target NAME, board in a field.
    monkeypatch.setenv("RR_BENCH_CONFIG", _write_bench("polarfire_bench"))
    ident_named = resolve_pfsoc_live_board_identity(board, tmp_path)
    assert ident_named.get("console") == console_dev, (
        "PF-L0a transient HSS console resolution regressed: read_console could not "
        "resolve MMUART0 from a bench descriptor whose target is keyed by name "
        f"(expected {console_dev!r}, got {ident_named.get('console')!r})."
    )

    # Mutation: same data, target dict keyed by the board id -> console resolves.
    monkeypatch.setenv("RR_BENCH_CONFIG", _write_bench(board))
    ident_keyed = resolve_pfsoc_live_board_identity(board, tmp_path)
    assert ident_keyed.get("console") == console_dev, (
        "the resolver could not resolve a console even when the target dict is "
        "keyed by the board id — the guard's premise (a resolvable descriptor) "
        "has drifted; re-derive it before trusting the assertion above."
    )
