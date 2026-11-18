# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""DS-P2.145 (PF-L0e) — Regression guards for the HSS firmware-leg fixes.

Two real defects blocked the HSS firmware leg on the PolarFire SoC Discovery Kit,
both proven on live silicon (evidence:
``docs/hardware/pf-l0e/DS-P2.145-pf-l0e-hss-l2lim-firmware-leg.md``):

  1. LINK ADDRESS. The upstream ``hss-l2scratch.elf`` links its entry + LOAD
     segment at ``0x0A000000`` — the L2 *scratchpad*, which is UNBACKED at reset
     (``WayEnable=0``). ``rr program pfsoc hss-load`` correctly refuses to resume
     the E51 into it (RTL-P2.1253 / PF-L0d). HSS therefore cannot run at all, so
     it can never compose the scratchpad it is supposed to compose. The fix
     (``sw/linux/pfsoc/hss/link_hss_l2lim.py``) relinks the same objects for the
     BACKED L2-LIM base ``0x08000000``. A regression here (the transform silently
     leaving the placement at ``0x0A000000``, or being deleted) puts us straight
     back to a transient load the backend refuses.

  2. CONSOLE UART. The upstream ``boards/mpfs-disco-kit/uart_device_list.c`` routes
     the E51 debug console to ``pUartDeviceList[1]`` == MMUART1, which the verified
     ``disco_mss`` design muxes to FABRIC (no physical console pin). The physical
     console (the bench FlashPro5 UART) is MMUART0 == ``pUartDeviceList[0]``
     (``MMUART_0 -> MSSIO_B2_B`` in ``disco_mss.cfg``). So stock HSS prints to a
     dead UART and the E51 console is silent even when HSS runs. The fix
     (``sw/linux/pfsoc/hss/disco_uart0_console.py``) retargets the E51 console to
     MMUART0. A regression here re-silences the console.

These guards assert at the surface a CALLER (the build flow / ``rr program pfsoc
hss-load``'s ELF validator) actually reaches — the transformed linker script's
placement and, end-to-end, the entry point the SDK's own
``validate_pfsoc_transient_elf`` derives from an ELF linked with it — not a helper
return value. They run with NO hardware and NO network clone (the upstream inputs
are reconstructed as minimal fixtures), so they are alive under cron/CI, not just
in an interactive shell.

DS-P2.145 revision — what the landing refusal fixed here:
  * The mislabeled ``..._rejects_scratchpad_entry`` test (which never called the
    transform and asserted the scratchpad ELF is ACCEPTED) is replaced by
    ``test_transformed_linker_script_yields_a_lim_entry_the_sdk_validator_accepts``
    (derives the asserted entry FROM ``transform_linker_script`` output, so a
    no-op mutant goes RED) plus
    ``test_sdk_static_validator_accepts_both_ranges_scratchpad_refusal_is_runtime``
    (pins the TRUE fact: the static validator accepts both; the scratchpad refusal
    is the runtime ``_probe_ram_backing`` canary).
  * ``collect_link_objects`` is now implemented (was ``NotImplementedError``) and
    unit-tested; ``stage_lim_as_transient`` (the caller-path wiring) and the in-repo
    caller ``tools/pfsoc_hss_lim_stage.py`` are tested — the helpers are no longer
    sideband.
  * The unauditable ``verified 148400 bytes ... MATCH`` pin is removed; the evidence
    test now pins only auditable BUILD facts + that the live witness is not-captured.

Falsifiability / test power:
  * BASELINE (pre-change ``origin/main``): the helper modules do not exist ->
    ``_load`` fails its ``is_file()`` assertion -> RED.
  * CANDIDATE (HEAD): helpers present + wired, transform places at 0x08000000,
    console at MMUART0, LIM ELF staged onto the SDK-searched path -> GREEN.
  * MUTANTS: a no-op ``transform_linker_script`` -> the entry-derivation and PLACE
    tests go RED; staging to a non-searched name -> the stage test goes RED; leaving
    the E51 console at index 1 -> the console test goes RED.
"""

from __future__ import annotations

import importlib.util
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HSS_TOOLS = ROOT / "sw" / "linux" / "pfsoc" / "hss"
LINK_MOD = HSS_TOOLS / "link_hss_l2lim.py"
UART_MOD = HSS_TOOLS / "disco_uart0_console.py"


def _load(mod_path: Path, name: str):
    assert mod_path.is_file(), (
        f"DS-P2.145 helper missing: {mod_path.relative_to(ROOT)} — the HSS "
        "firmware-leg fix (L2-LIM relink / MMUART0 console) is not present."
    )
    spec = importlib.util.spec_from_file_location(name, mod_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# Minimal fixture reproducing the placement-relevant lines of the upstream,
# already-preprocessed ``hss-l2scratch.ld`` (the real file is ~160 lines; only the
# MEMORY origins, the ``__l2_*`` PROVIDEs, the section cursor, and the output
# region attachments matter for placement).
_UPSTREAM_LD = """\
OUTPUT_ARCH( "riscv" )
MEMORY
{
    l2lim (rwx) : ORIGIN = 0x08000000, LENGTH = 512k
    l2zerodevice (rwx) : ORIGIN = 0x0A000000, LENGTH = 512k
    switch_code (rx) : ORIGIN = 0x01001c00, LENGTH = 1k
}
SECTIONS
{
    PROVIDE(__l2lim_start = ORIGIN(l2lim));
    PROVIDE(__l2_start = ORIGIN(l2zerodevice));
    . = __l2_start;
    .text : ALIGN(0x10) { *(.entry) *(.text .text.*) } >l2zerodevice
    .ram_code : ALIGN(0x10) { *(.ram_codetext) } >switch_code
    .data : ALIGN(0x40) { *(.data .data.*) } >l2zerodevice
    .bss : ALIGN(0x40) { *(.bss .bss.*) } >l2zerodevice
    .stack : ALIGN(0x40) { . += 16k; } >l2zerodevice
}
"""

# Minimal fixture reproducing the E51-console selection of the upstream
# ``boards/mpfs-disco-kit/uart_device_list.c`` (default + E51 pre-boot -> index 1;
# U54 mappings use other indices and must be left alone).
_UPSTREAM_UART_C = """\
void *HSS_UART_GetInstance(int hartid)
{
    mss_uart_instance_t *pUart;
    const bool postBoot = HSS_Trigger_IsNotified(EVENT_POST_BOOT);
    switch (hartid) {
    default:
        pUart = pUartDeviceList[1];
        break;
    case HSS_HART_E51:
        if (postBoot) {
            pUart = pUartDeviceList[CONFIG_UART_POST_BOOT];
        } else {
            pUart = pUartDeviceList[1];
        }
        break;
    case HSS_HART_U54_1:
        pUart = pUartDeviceList[4];
        break;
    case HSS_HART_U54_2:
        pUart = pUartDeviceList[0];
        break;
    }
    return pUart;
}
"""

_UPSTREAM_DEF_CONFIG = "CONFIG_SERVICE_BOOT=y\nCONFIG_UART_POST_BOOT=1\nCONFIG_OPENSBI=y\n"


def test_linker_transform_places_image_in_backed_l2lim_not_unbacked_scratchpad(tmp_path: Path) -> None:
    """The transform must move EVERY loadable placement off 0x0A000000 onto 0x08000000.

    Surface asserted: the linker script text the ``gcc -T`` link actually consumes.
    A no-op / scratchpad-retargeting mutant leaves ``. = __l2_start`` and
    ``>l2zerodevice`` in place -> this fails.
    """
    mod = _load(LINK_MOD, "ds_p2_145_link")
    src = tmp_path / "hss-l2scratch.ld"
    dst = tmp_path / "hss-lim.ld"
    src.write_text(_UPSTREAM_LD, encoding="utf-8")

    mod.transform_linker_script(src, dst)
    out = dst.read_text(encoding="utf-8")

    # The section cursor must start at LIM, not the scratchpad.
    assert ". = __l2lim_start;" in out, (
        "L2-LIM relink did not move the section cursor to __l2lim_start "
        "(0x08000000) — image still starts at the unbacked scratchpad."
    )
    assert ". = __l2_start;" not in out, (
        "L2-LIM relink left the scratchpad section cursor '. = __l2_start;' — "
        "the loadable image would still be placed at the unbacked 0x0A000000."
    )
    # No loadable output section may still target the scratchpad zero-device.
    assert ">l2zerodevice" not in out, (
        "L2-LIM relink left an output section attached to '>l2zerodevice' "
        "(0x0A000000) — a segment would still load into unbacked scratchpad."
    )
    assert out.count(">l2lim") >= 4, (
        "L2-LIM relink did not retarget the text/data/bss/stack output sections "
        "to '>l2lim' (0x08000000)."
    )
    # The switch_code region (clock-switch trampoline) must NOT be disturbed.
    assert ">switch_code" in out, "L2-LIM relink wrongly moved the .ram_code/switch_code region."


def test_linker_transform_refuses_a_non_hss_linker_script(tmp_path: Path) -> None:
    """Guard the guard: the transform must not silently 'succeed' on the wrong file."""
    mod = _load(LINK_MOD, "ds_p2_145_link_neg")
    bogus = tmp_path / "random.ld"
    bogus.write_text("SECTIONS { .text : { *(.text) } }\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.transform_linker_script(bogus, tmp_path / "out.ld")


# A fixture line reproducing the single upstream ``make -n`` link line that produces
# the transient ELF. ``relink`` recovers the ORDERED object list from the tail after
# ``-o build/hss-l2scratch.elf`` — LTO whole-program is order-sensitive, so order and
# duplicates must be preserved verbatim.
_MAKE_DRY_STDOUT = (
    "mkdir -p build\n"
    "riscv64-unknown-elf-gcc -T boards/mpfs-disco-kit/hss-l2scratch.ld -Os -flto=auto "
    "-o build/hss-l2scratch.elf build/hss_entry.o build/startup.o build/hss_state_machine.o "
    "build/hss_boot_pmp.o build/hss_state_machine.o\n"
    "riscv64-unknown-elf-objcopy -O binary build/hss-l2scratch.elf build/hss-l2scratch.bin\n"
)


def test_collect_link_objects_recovers_ordered_object_list() -> None:
    """``collect_link_objects`` returns the ordered object tail after the -o token,
    preserving order AND duplicates (LTO whole-program is order-sensitive)."""
    mod = _load(LINK_MOD, "ds_p2_145_link_objs")
    objs = mod.collect_link_objects(_MAKE_DRY_STDOUT, "riscv64-unknown-elf-")
    assert objs == [
        "build/hss_entry.o",
        "build/startup.o",
        "build/hss_state_machine.o",
        "build/hss_boot_pmp.o",
        "build/hss_state_machine.o",  # duplicate kept — order/dupes are significant
    ], objs


def test_collect_link_objects_raises_when_link_line_absent() -> None:
    """A build/toolchain drift that removes the transient link line must fail loud,
    not silently produce an empty object list."""
    mod = _load(LINK_MOD, "ds_p2_145_link_objs_neg")
    with pytest.raises(SystemExit):
        mod.collect_link_objects("mkdir -p build\nsome unrelated line\n", "riscv64-unknown-elf-")
    # And a matching line with NOTHING after -o must also raise (empty list guard).
    with pytest.raises(SystemExit):
        mod.collect_link_objects(
            "riscv64-unknown-elf-gcc -o build/hss-l2scratch.elf\n", "riscv64-unknown-elf-"
        )


def test_stage_lim_as_transient_overwrites_the_sdk_searched_path(tmp_path: Path) -> None:
    """The wiring fix: staging must copy the LIM ELF over the FIRST path the SDK's
    ``transient_candidates`` search reaches (``build/hss-l2scratch.elf``), so the next
    ``rr program pfsoc hss-load`` loads the backed image, not a sideband file.

    A regression that stages to a non-searched name (the sideband failure the landing
    refused) leaves ``build/hss-l2scratch.elf`` as the scratchpad ELF -> this fails.
    """
    mod = _load(LINK_MOD, "ds_p2_145_stage")
    hss_dir = tmp_path / "hart-software-services"
    build = hss_dir / "build"
    build.mkdir(parents=True)
    # Pre-existing upstream scratchpad ELF at the FIRST searched path.
    scratch = build / "hss-l2scratch.elf"
    _synth_lim_elf(scratch, 0x0A000000)
    # Our LIM ELF.
    lim = tmp_path / "hss-lim.elf"
    _synth_lim_elf(lim, 0x08000000)

    staged = mod.stage_lim_as_transient(hss_dir, lim)

    # The path the SDK stages MUST now be the LIM ELF.
    assert staged == build / "hss-l2scratch.elf", staged
    assert staged.read_bytes() == lim.read_bytes(), (
        "staging did not overwrite build/hss-l2scratch.elf with the LIM ELF — the "
        "next `rr program pfsoc hss-load` would still pick up the scratchpad image."
    )
    # And the SDK-searched path's entry is the backed LIM base.
    import struct as _struct

    with (build / "hss-l2scratch.elf").open("rb") as fh:
        fh.seek(24)  # e_entry offset for ELF64
        (entry,) = _struct.unpack("<Q", fh.read(8))
    assert entry == 0x08000000, hex(entry)


def test_stage_lim_as_transient_refuses_missing_lim_elf(tmp_path: Path) -> None:
    mod = _load(LINK_MOD, "ds_p2_145_stage_neg")
    with pytest.raises(SystemExit):
        mod.stage_lim_as_transient(tmp_path / "hss", tmp_path / "nope-hss-lim.elf")


def _synth_lim_elf(path: Path, entry: int) -> None:
    """Write a minimal but valid RV64 ELF with one PT_LOAD at ``entry`` (>= one page).

    Mirrors what the SDK's ``validate_pfsoc_transient_elf`` inspects: ELF ident,
    RISC-V machine, entry, and a PT_LOAD program header whose paddr is in RAM.
    """
    e_phoff = 64
    e_phentsize = 56
    e_phnum = 1
    filesz = 0x2000
    # ELF header
    e_ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    ehdr = e_ident + struct.pack(
        "<HHIQQQIHHHHHH",
        2,            # e_type EXEC
        243,          # e_machine RISC-V
        1,            # e_version
        entry,        # e_entry
        e_phoff,      # e_phoff
        0,            # e_shoff
        0,            # e_flags
        64,           # e_ehsize
        e_phentsize,  # e_phentsize
        e_phnum,      # e_phnum
        0, 0, 0,      # shentsize, shnum, shstrndx
    )
    phdr = struct.pack(
        "<IIQQQQQQ",
        1,        # p_type PT_LOAD
        0x5,      # p_flags R+X
        0x1000,   # p_offset
        entry,    # p_vaddr
        entry,    # p_paddr
        filesz,   # p_filesz
        filesz,   # p_memsz
        0x1000,   # p_align
    )
    data = ehdr + phdr
    data += b"\x00" * (0x1000 - len(data)) + b"\x13\x00\x00\x00" * (filesz // 4)
    path.write_bytes(data)


def _import_sdk_validator():
    """Import the SDK's ``validate_pfsoc_transient_elf`` or skip if unavailable."""
    import sys

    try:
        from sdk.cli.commands.linux_steps._pfsoc import validate_pfsoc_transient_elf

        return validate_pfsoc_transient_elf
    except ImportError:
        candidates = []
        for parent in ROOT.parents:
            candidates.append(parent / "routertl")
        routertl = next(
            (c for c in candidates if (c / "sdk" / "cli" / "commands" / "linux_steps" / "_pfsoc.py").is_file()),
            None,
        )
        if routertl is None:
            pytest.skip("routertl SDK not importable and no sibling checkout found")
        sys.path.insert(0, str(routertl))
        from sdk.cli.commands.linux_steps._pfsoc import validate_pfsoc_transient_elf

        return validate_pfsoc_transient_elf


def test_transformed_linker_script_yields_a_lim_entry_the_sdk_validator_accepts(tmp_path: Path) -> None:
    """END-TO-END at the caller: an ELF linked with the TRANSFORMED script has its
    entry at 0x08000000, and the SDK validator ``rr program pfsoc hss-load`` runs
    accepts it there.

    Unlike the prior version of this test, this one actually calls
    ``transform_linker_script`` and derives the asserted entry FROM its output, so a
    no-op mutant (transform leaves the placement at 0x0A000000) makes the synthesised
    ELF entry 0x0A000000 and the ``== 0x08000000`` assertion goes RED. The old test
    hardcoded two entries and never invoked the transform, so it stayed green when
    the mechanism was deleted.

    HONEST framing (the DS-P2.145 landing finding): the SDK's *static* validator's
    ``VALID_RAM_RANGES`` includes BOTH ``0x08000000..`` (LIM) and ``0x0A000000..``
    (scratchpad), so it ACCEPTS both entries. What refuses the scratchpad on live
    silicon is the runtime ``_probe_ram_backing`` canary (PF-L0d / RTL-P2.1253),
    NOT this static check. So this test proves only that the transform yields an
    entry the validator places in the LIM range — it does not (and must not) claim
    the static validator rejects scratchpad.
    """
    link_mod = _load(LINK_MOD, "ds_p2_145_link_entry")
    src = tmp_path / "hss-l2scratch.ld"
    dst = tmp_path / "hss-lim.ld"
    src.write_text(_UPSTREAM_LD, encoding="utf-8")
    link_mod.transform_linker_script(src, dst)

    # The entry the transformed script places the image at, read from ITS OWN text:
    # the section cursor line drives the first PT_LOAD paddr. A no-op transform
    # leaves ``. = __l2_start;`` (scratchpad) -> entry 0x0A000000 -> RED below.
    entry = 0x08000000 if ". = __l2lim_start;" in dst.read_text(encoding="utf-8") else 0x0A000000

    validate = _import_sdk_validator()
    elf = tmp_path / "hss-lim.elf"
    _synth_lim_elf(elf, entry)
    info = validate(elf, declared_board="mpfs-disco-kit")
    assert info["entry_point_int"] == 0x08000000, (
        "The transformed linker script did not place the HSS entry in the backed "
        "L2-LIM at 0x08000000 (a no-op transform leaves it at the unbacked "
        "scratchpad 0x0A000000)."
    )


def test_sdk_static_validator_accepts_both_ranges_scratchpad_refusal_is_runtime(tmp_path: Path) -> None:
    """Pin the TRUE division of labour so the evidence doc cannot regress the claim:
    the SDK's static ELF validator accepts BOTH a LIM (0x08000000) and a scratchpad
    (0x0A000000) entry (both are in ``VALID_RAM_RANGES``); the scratchpad refusal is
    the runtime ``_probe_ram_backing`` canary, not this validator.
    """
    validate = _import_sdk_validator()
    lim = tmp_path / "hss-lim.elf"
    scratch = tmp_path / "hss-l2scratch.elf"
    _synth_lim_elf(lim, 0x08000000)
    _synth_lim_elf(scratch, 0x0A000000)

    info = validate(lim, declared_board="mpfs-disco-kit")
    sinfo = validate(scratch, declared_board="mpfs-disco-kit")
    assert info["entry_point_int"] == 0x08000000
    # The static validator ACCEPTS the scratchpad entry too (returns info, no raise).
    assert sinfo["entry_point_int"] == 0x0A000000, (
        "SDK static validator unexpectedly did not return the scratchpad entry — "
        "if it now REJECTS scratchpad statically, update the evidence doc: the "
        "refusal would no longer be purely the runtime _probe_ram_backing canary."
    )


def test_console_overlay_retargets_e51_console_to_mmuart0(tmp_path: Path) -> None:
    """DS-P3.160: The E51 console selection must move from MMUART1 (fabric) to MMUART4 (pins).

    Surface asserted: the ``uart_device_list.c`` the HSS build compiles. A mutant
    that leaves the E51 selection at ``pUartDeviceList[1]`` or selects ``pUartDeviceList[0]``
    (fabric on reference routing) fails here.
    """
    mod = _load(UART_MOD, "ds_p2_145_uart")
    board = tmp_path / "boards" / "mpfs-disco-kit"
    board.mkdir(parents=True)
    udl = board / "uart_device_list.c"
    udl.write_text(_UPSTREAM_UART_C, encoding="utf-8")
    (board / "def_config").write_text(_UPSTREAM_DEF_CONFIG, encoding="utf-8")

    changed = mod.apply(tmp_path)
    assert changed, "console overlay reported no change against the stock upstream files."

    out = udl.read_text(encoding="utf-8")
    # The two console selections (default + E51 pre-boot) must now be MMUART4.
    assert "pUartDeviceList[1];" not in out, (
        "E51/default console still selects pUartDeviceList[1] (MMUART1 -> fabric); "
        "the console would stay silent on this design."
    )
    assert "pUartDeviceList[0]; /* DS" not in out, (
        "E51 console was retargeted to MMUART0, but reference routing sends MMUART0 to fabric."
    )
    assert out.count("pUartDeviceList[4]") >= 3, (
        "console overlay did not retarget the default + E51 pre-boot selections "
        "to pUartDeviceList[4] (MMUART4)."
    )
    # The U54_2 mapping (already index 0) must remain; the U54_1 mapping (index 4)
    # must be untouched — we only move the CONSOLE, not the per-hart OS UARTs.
    assert "pUartDeviceList[0];" in out, "console overlay wrongly altered the U54_2 UART mapping."

    # CONFIG_UART_POST_BOOT must be forced to 4 (MMUART4) too.
    assert "CONFIG_UART_POST_BOOT=4" in (board / "def_config").read_text(encoding="utf-8"), (
        "console overlay did not set CONFIG_UART_POST_BOOT=4 — the post-boot E51 "
        "console would not be MMUART4."
    )


def test_hss_lim_stage_wrapper_wires_the_helpers(tmp_path: Path, monkeypatch) -> None:
    """The in-repo caller ``tools/pfsoc_hss_lim_stage.py`` must invoke the console
    overlay, relink, and stage the LIM ELF onto the SDK-searched transient path.

    This is the test that would have caught the landing's central finding — that the
    helpers had NO production caller. We stub the actual gcc relink (no toolchain
    needed) and assert the wrapper (a) applies the console overlay, (b) stages the LIM
    ELF over ``build/hss-l2scratch.elf``.
    """
    wrapper = _load(ROOT / "tools" / "pfsoc_hss_lim_stage.py", "ds_p2_145_wrapper")

    # Build a minimal HSS clone fixture the overlay + staging operate on.
    hss_dir = tmp_path / "hart-software-services"
    board = hss_dir / "boards" / "mpfs-disco-kit"
    board.mkdir(parents=True)
    (board / "uart_device_list.c").write_text(_UPSTREAM_UART_C, encoding="utf-8")
    (board / "def_config").write_text(_UPSTREAM_DEF_CONFIG, encoding="utf-8")
    (board / "hss-l2scratch.ld").write_text(_UPSTREAM_LD, encoding="utf-8")
    (hss_dir / "build").mkdir()
    _synth_lim_elf(hss_dir / "build" / "hss-l2scratch.elf", 0x0A000000)

    out_elf = tmp_path / "out" / "hss-lim.elf"

    # Stub the real relink (needs riscv gcc + built objects) with one that just emits
    # a LIM-entry ELF at out_elf — the wrapper's WIRING is what we assert here.
    link_mod = _load(LINK_MOD, "ds_p2_145_wrapper_link")

    def _fake_relink(hd, cross, oe):
        _synth_lim_elf(Path(oe), 0x08000000)

    monkeypatch.setattr(link_mod, "relink", _fake_relink)
    # The wrapper imports its own module instances; patch on the wrapper's loaded refs.
    monkeypatch.setattr(wrapper, "_load", lambda p, n: link_mod if "link" in p.name else _load(p, n))

    summary = wrapper.stage(hss_dir, out_elf)

    assert summary["console_overlay_changed"] is True
    staged = Path(summary["staged_transient"])
    assert staged == hss_dir / "build" / "hss-l2scratch.elf"
    # Console overlay actually retargeted the E51 console to MMUART4.
    assert "pUartDeviceList[1];" not in (board / "uart_device_list.c").read_text(encoding="utf-8")
    assert "pUartDeviceList[4];" in (board / "uart_device_list.c").read_text(encoding="utf-8")
    assert "CONFIG_UART_POST_BOOT=4" in (board / "def_config").read_text(encoding="utf-8")


def test_payload_config_wraps_u54_next_stage_not_the_hss_firmware(tmp_path: Path) -> None:
    """ROUND-3 FINDING #2 GUARD — payload semantics.

    HSS is the E51 FIRST-STAGE FIRMWARE; it boots from eNVM (or transiently from
    L2-LIM) and is NEVER wrapped in payload.bin. ``payload.bin`` is the image HSS
    LOADS FOR THE U54s — the next stage (U-Boot), ``owner-hart u54_*``, executing at
    the U54 DDR address (``0x80200000``) in supervisor mode.

    Surface asserted: the hss-payload-generator YAML the wrapper actually feeds the
    generator (``build_payload_config``). The prior revision wrapped the HSS LIM ELF
    itself as ``owner-hart u54_1`` / ``exec-addr 0x08000000`` / ``priv-mode prv_m``
    (a category error). This test pins the CORRECTED semantics:

      * the wrapped path is the NEXT-STAGE image, not ``hss-lim.elf``;
      * the exec-addr is the U54 DDR address ``0x80200000``, NOT the E51 LIM base
        ``0x08000000``;
      * priv-mode is supervisor (``prv_s``), the U54 next-stage mode, NOT ``prv_m``;
      * the U54 harts are the entry harts.

    MUTATION: revert ``build_payload_config`` to wrap the HSS ELF at ``0x08000000`` /
    ``prv_m`` (the old body) -> the ``0x80200000`` / ``prv_s`` / not-the-HSS-ELF
    assertions all go RED. A no-op that keeps the old semantics cannot pass.
    """
    wrapper = _load(ROOT / "tools" / "pfsoc_hss_lim_stage.py", "ds_p2_145_payload_cfg")

    hss_elf = tmp_path / "hss-lim.elf"
    _synth_lim_elf(hss_elf, 0x08000000)
    next_stage = tmp_path / "u-boot.bin"
    next_stage.write_bytes(b"\x00" * 4096)  # stand-in U54 next stage

    cfg = wrapper.build_payload_config(next_stage)

    # The U54 next-stage exec address, NOT the E51 firmware LIM base.
    assert "0x80200000" in cfg, (
        "payload config must place the U54 next stage at the DDR exec-addr 0x80200000."
    )
    assert "0x08000000" not in cfg, (
        "payload.bin must NOT carry the E51 firmware LIM base 0x08000000 — HSS is not "
        "wrapped in payload.bin; wrapping it at 0x08000000 is the round-3 category error."
    )
    # Supervisor mode (U54 next stage), never machine mode (that was the HSS-ELF wrap).
    assert "prv_s" in cfg, "U54 next-stage payload must be priv-mode prv_s."
    assert "prv_m" not in cfg, (
        "payload config uses prv_m — that is the E51/HSS-firmware mode, evidence the HSS "
        "ELF is being wrapped instead of the U54 next stage."
    )
    # It wraps the next-stage image, not the HSS ELF.
    assert str(next_stage) in cfg
    assert "hss-lim.elf" not in cfg, "payload.bin must not wrap the HSS firmware ELF."
    # Owner + secondary U54 harts carry the payload.
    assert "owner-hart: u54_1" in cfg
    assert "u54_2" in cfg and "u54_3" in cfg and "u54_4" in cfg


def test_stage_refuses_payload_without_next_stage(tmp_path: Path) -> None:
    """Asking for payload.bin without a --next-stage image must be REFUSED, not
    silently fall back to wrapping the HSS ELF (the finding-#2 regression).

    Surface asserted: ``stage()`` — the caller entry point — raises SystemExit when
    ``payload_out`` is given but ``next_stage_image`` is None.
    """
    wrapper = _load(ROOT / "tools" / "pfsoc_hss_lim_stage.py", "ds_p2_145_payload_refuse")
    hss_dir = tmp_path / "hart-software-services"
    board = hss_dir / "boards" / "mpfs-disco-kit"
    board.mkdir(parents=True)
    (board / "uart_device_list.c").write_text(_UPSTREAM_UART_C, encoding="utf-8")
    (board / "def_config").write_text(_UPSTREAM_DEF_CONFIG, encoding="utf-8")
    (board / "hss-l2scratch.ld").write_text(_UPSTREAM_LD, encoding="utf-8")
    (hss_dir / "build").mkdir()
    _synth_lim_elf(hss_dir / "build" / "hss-l2scratch.elf", 0x0A000000)
    out_elf = tmp_path / "out" / "hss-lim.elf"

    link_mod = _load(LINK_MOD, "ds_p2_145_payload_refuse_link")

    def _fake_relink(hd, cross, oe):
        _synth_lim_elf(Path(oe), 0x08000000)

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(link_mod, "relink", _fake_relink)
        monkeypatch.setattr(
            wrapper, "_load", lambda p, n: link_mod if "link" in p.name else _load(p, n)
        )
        with _pytest.raises(SystemExit):
            wrapper.stage(hss_dir, out_elf, payload_out=tmp_path / "payload.bin", next_stage_image=None)
    finally:
        monkeypatch.undo()


def _built_payload_generator(wrapper) -> Path | None:
    """Locate a built ``hss-payload-generator`` (clone or PATH), else None.

    The HSS clone (and thus the generator source) is gitignored, so a bare CI/cron
    checkout cannot build it. This locator lets the END-TO-END generator test run
    for real on the bench (where the clone exists and the tool is built) and skip
    honestly elsewhere — the YAML-shape test + the committed ``-d`` analyzer
    evidence carry the invariant in the no-clone case.
    """
    hss_dir = ROOT / "sw" / "linux" / "pfsoc" / "src" / "hart-software-services"
    return wrapper.find_payload_generator(hss_dir)


def test_generate_payload_produces_a_u54_next_stage_image_not_the_hss_firmware(tmp_path: Path) -> None:
    """END-TO-END GUARD (the receipt-invariant fix) — run the REAL generator.

    The landing refusal's sharpest finding: the payload receipt only asserted the
    YAML string (``build_payload_config``); it never called ``generate_payload``,
    never ran ``hss-payload-generator``, and never inspected ``payload.bin``. A
    mutant that leaves the YAML correct but makes ``generate_payload`` wrap
    ``hss-lim.elf`` again stayed green.

    This test closes that gap: it drives the project caller ``generate_payload()``
    with a real (stand-in) U54 next-stage blob, produces an actual ``payload.bin``,
    and inspects it with the generator's own ``-d`` analyzer. It asserts on the
    PRODUCED IMAGE the caller-visible facts:

      * ``entryPoint`` is the U54 DDR address ``0x80200000`` (NOT the E51 LIM base
        ``0x8000000``);
      * ``privMode`` is ``1 (PRV_S)`` (NOT ``3 (PRV_M)``, the HSS-firmware wrap);
      * the wrapped ``name`` is the next-stage blob, NOT ``hss-lim.elf``.

    MUTATION: revert ``build_payload_config``/``generate_payload`` to wrap the HSS
    ELF at ``0x8000000`` / ``PRV_M`` -> the produced ``-d`` output flips to
    ``0x8000000`` / ``PRV_M`` / ``hss-lim.elf`` and every assertion below goes RED.
    A YAML-only no-op cannot satisfy this — the bytes of ``payload.bin`` are checked.

    Skips (does not fail) when no ``hss-payload-generator`` binary is available
    (gitignored clone not built), keeping the guard alive on the bench without
    breaking a no-clone CI checkout.
    """
    wrapper = _load(ROOT / "tools" / "pfsoc_hss_lim_stage.py", "ds_p2_145_gen_e2e")
    generator = _built_payload_generator(wrapper)
    if generator is None:
        # No live generator in this checkout (the HSS clone is gitignored, so a bare
        # CI/cron checkout has no binary to build). Do NOT skip — a skip reads as
        # "green" to a test-power receipt. The invariant is still carried here by the
        # sibling committed-evidence guard
        # (test_committed_payload_analyzer_evidence_shows_the_corrected_wrapping) and
        # the YAML-shape guard; this branch asserts that committed evidence exists so
        # the test is never a silent no-op.
        cap = (
            ROOT / "docs" / "hardware" / "pf-l0e" / "evidence"
            / "payload_u54_next_stage_analyzer_2026-09-15.txt"
        )
        # The evidence is a measurement record: the public export drops it
        # (DS-P2.197), and the builder checks below carry the guard there.
        # context.json marks the private tree, the switch the commit gate uses.
        if (ROOT / "context.json").is_file():
            assert cap.is_file() and "0x80200000" in cap.read_text(encoding="utf-8"), (
                "no live generator AND no committed corrected-payload analyzer evidence — "
                "the A2 fix would be unguarded."
            )
        # 2026-09-15 land-review rounds 1-2: with the generator absent this branch
        # never CALLS generate_payload(). Exercise the config BUILDER behaviorally —
        # it is pure text generation, needs no binary — so a config-level regression
        # (rewrapping hss-lim.elf, dropping the U54 entry point or privilege mode)
        # goes RED even in a bare checkout, not just a source-string grep.
        cfg = wrapper.build_payload_config("u54-next-stage.bin")
        assert "u54-next-stage.bin" in cfg and "hss-lim.elf" not in cfg, (
            "build_payload_config() must wrap the PASSED next-stage image, never the HSS firmware."
        )
        assert "exec-addr: '0x80200000'" in cfg, (
            "the U54 next-stage exec-addr must be 0x80200000 — the A2 category-error fix."
        )
        assert "priv-mode: prv_s" in cfg and "owner-hart: u54_1" in cfg, (
            "the payload must target the U54 owner hart in supervisor mode."
        )
        return

    next_stage = tmp_path / "u54-next-stage.bin"
    next_stage.write_bytes(b"\xa5" * 8192)  # stand-in U54 next stage (raw blob is a valid payload)
    payload_out = tmp_path / "payload.bin"

    wrapper.generate_payload(generator, next_stage, payload_out)
    assert payload_out.is_file() and payload_out.stat().st_size > 0, "generator produced no payload.bin"

    dump = subprocess.run(
        [str(generator), "-d", str(payload_out)], capture_output=True, text=True
    )
    assert dump.returncode == 0, f"analyzer -d failed: {dump.stderr}"
    out = dump.stdout

    # Magic proves a real HSS boot image was produced.
    assert "0xb007c0de" in out, "produced payload.bin is not a valid HSS boot image (magic missing)."
    # U54 DDR exec-addr, NOT the E51 firmware LIM base.
    assert "0x80200000" in out, "payload.bin must place the U54 next stage at 0x80200000."
    assert "0x8000000\n" not in out and "entryPoint[0]:      0x8000000" not in out, (
        "payload.bin carries the E51 LIM base 0x8000000 — the round-3 category error is back."
    )
    # Supervisor mode, not machine mode.
    assert "1 (PRV_S)" in out or "PRV_S" in out, "U54 next-stage payload must start in PRV_S."
    assert "3 (PRV_M)" not in out, (
        "payload.bin wraps a PRV_M image — that is the HSS firmware wrap (category error)."
    )
    # The wrapped image is the next stage, not the HSS firmware ELF.
    assert next_stage.name in out, "payload.bin does not wrap the provided next-stage image."
    assert "hss-lim.elf" not in out, "payload.bin must not wrap the HSS firmware ELF."
