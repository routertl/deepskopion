"""
_core_seam_common — shared helpers for the DS-P2.100 landing-review
Finding B suite (test_stimulus_core_seam_edge.py,
test_stimulus_core_seam_edge_gate.py, test_stimulus_core_seam_uart.py).

Leading underscore so `rr sim run <module>` / cocotb test discovery never
picks this up as a test module in its own right. ONE cocotb test per file —
see hw/sim/cocotb/tests/stimulus/test_stimulus_uart_frame.py's docstring
for why (the nvc multi-@cocotb.test()-per-session hang, reproduced
independent of file/protocol grouping, applies here too).

All of this targets the REAL tich.deepskopion_core via
tb_stimulus_core_seam.vhd — see that file's header for the full rationale.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from sdk.cocotb_helpers import requires  # noqa: E402,F401 (re-exported for the test files)

# Regbank byte-offset slots (system_regbank_pkg.vhd C_INDEX_* — these ARE
# byte offsets, matched 1:1 against the pkg's own `C_INDEX_X / 4` word-index
# convention used everywhere in deepskopion_core.vhd).
SLOT_EMIO_MUX_SEL         = 36
SLOT_STIM_UART_CONTROL    = 544
SLOT_STIM_UART_BAUD_DIV   = 548
SLOT_STIM_UART_DATA       = 552
SLOT_STIM_I2C_CONTROL     = 556
SLOT_STIM_I2C_CLK_DIV     = 560
SLOT_STIM_I2C_DATA        = 564
SLOT_STIM_SPI_CONTROL     = 568
SLOT_STIM_SPI_CLK_DIV     = 572
SLOT_STIM_SPI_DATA        = 576
SLOT_STIM_EDGE_CONTROL    = 580

# EMIO_MUX_SEL bit layout (ds_emio_mux.vhd:47-51 / deepskopion_core.vhd:874-880
# header comments): bit[0]=I2C, bit[1]=UART, bit[2]=SPI EMIO-bus source select;
# bit[7]=DS-P2.100 stimulus edge/pulse source.
SEL_BIT_I2C       = 1 << 0
SEL_BIT_UART      = 1 << 1
SEL_BIT_SPI       = 1 << 2
SEL_BIT_STIM_EDGE = 1 << 7   # DS-P2.100 stimulus edge/pulse source

CLK_PERIOD_NS = 10  # 100 MHz clk_sys, matches deepskopion_core's real fclk_clk0

# DS-P2.100 landing-review Finding 6: bound for every "wait for a toggle that
# a broken regbank unpack would make it never produce" await in this suite.
# A broken unpack's red mode used to be an unbounded hang (bare FallingEdge/
# RisingEdge await, no with_timeout) — reproduced directly: a mutated run
# sat >30 min before manual cancellation, wedging the shared sim queue. This
# bound is generous relative to the slowest real transaction this suite
# drives (an I2C write at the slowest configured clk_div is still well under
# 100us), so it never fires on a healthy DUT, only on a broken one.
SEAM_RED_TIMEOUT_NS = 100_000


async def reset(dut) -> None:
    dut.RST.value = 1
    dut.S_AXI_AWVALID.value = 0
    dut.S_AXI_WVALID.value = 0
    dut.S_AXI_BREADY.value = 0
    dut.S_AXI_ARVALID.value = 0
    dut.S_AXI_RREADY.value = 0
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 8)


async def make_axi_master(dut) -> AxiLiteMaster:
    bus = AxiLiteBus.from_prefix(dut, "S_AXI")
    return AxiLiteMaster(bus, dut.CLK, dut.RST)


async def start(dut) -> AxiLiteMaster:
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await reset(dut)
    return await make_axi_master(dut)


def _seed_regbank_rom_symlinks(deepskopion_root: Path) -> None:
    """deepskopion_core.vhd instantiates tich.system_regbank_wrapper (its
    REAL regbank, not a TB-mocked one), which loads two BRAM-init ROMs
    from a project-root-relative literal path — rom_info_pkg.vhd:20-21,
    C_REGBANK_HEX/C_CONFIG_HEX = "hw/rtl/regbank/{regbank_schema,
    config_schema}.hex". nvc's `-e`/`-r` steps run with cwd=build_dir
    (routertl_core/sim/backends/nvc_backend.py — confirmed by reading the
    `-L`/cwd values in a failing run's own command line), NOT the project
    root, so that literal never resolves and nvc aborts with "failed to
    open ... No such file or directory" — reproduced directly. Every
    OTHER sim bench in this project sidesteps this by instantiating the
    INNER tich.system_regbank (no ROM) instead of the wrapper (see e.g.
    tb_regbank_datalogger_config_seam.vhd's header) — not an option here
    since this suite's whole point is compiling the REAL, unmodified
    deepskopion_core. Symlinking the two .hex files into build_dir at the
    SAME relative path nvc's cwd will look under is a workaround scoped
    to this test module only — it does not touch rom_info_pkg.vhd or any
    production RTL."""
    # Matches the `-L`/cwd value emitted by the current standalone simulation
    # runner. Keep this tied to the actual nvc build directory because the ROM
    # loader resolves its relative generic from there during elaboration.
    # DS-T1.16: the queued `rr sim run` builds under hw/sim/work/nvc (its
    # nvc -L/cwd), the standalone runner under sim/work/nvc — seed both, or
    # elaboration dies on "failed to open hw/rtl/regbank/regbank_schema.hex".
    for build_dir in (deepskopion_root / "sim" / "work" / "nvc",
                      deepskopion_root / "hw" / "sim" / "work" / "nvc"):
        for hex_name in ("regbank_schema.hex", "config_schema.hex"):
            real = deepskopion_root / "hw" / "rtl" / "regbank" / hex_name
            link = build_dir / "hw" / "rtl" / "regbank" / hex_name
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                if link.resolve() == real.resolve():
                    continue
                link.unlink()
            link.symlink_to(real)


def run_core_seam(module: str, top_level: str = "tb_stimulus_core_seam") -> None:
    """Shared `if __name__ == "__main__"` body — ROUTERTL-001 standalone
    entry point, but ALSO what `rr sim run <module>` actually executes for
    this suite (unlike the sibling stimulus/ shard tests, this module's
    top-level entity, tb_stimulus_core_seam, is not registered in any
    target yml's simulation.tags — rr's auto_sources resolver falls back to
    a broad, unpinned whole-package walk for unregistered tops, which hits
    a real routertl bug: it feeds BOTH the xilinx AND intel variants of
    rr_ddr_stream's vendor-conditional FIFO bodies into one nvc analysis
    batch, and the second copy invalidates ("obsolete version") the first
    — reproduced directly, independent of this suite's own RTL. This
    explicit custom_libraries list is hand-verified against `rr sim run
    --print-sources`'s per-file library assignment (deepskopion_core's full
    dependency closure: the CTU CAN-FD stub the default targets use (the
    testbench holds both APB ports idle, and the real core is opt-in, so a
    clean checkout does not have it), the DDR streaming DMA
    AXI4 backend only — no avmm, no intel stub — and the datalogger infra
    chain datalogger_gather.vhd itself pulls in) and bypasses that resolver
    path entirely."""
    from engine.simulation import run_simulation

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
    _seed_regbank_rom_symlinks(DEEPSKOPION_ROOT)

    try:
        _run_core_seam_simulation(run_simulation, module, DEEPSKOPION_ROOT, top_level)
    except RuntimeError:
        # RTL-P2.2396: rr's stale-build guard wipes the links seeded above in
        # the same run. Remove this retry and the seeder once rr stages the
        # ROM files into build_dir itself.
        # A source-newer-than-build_dir staleness check inside
        # run_simulation() auto-`shutil.rmtree(build_dir)`s BEFORE
        # recompiling (routertl_core/sim/simulation.py ~line 1247) — that
        # wipes the symlinks _seed_regbank_rom_symlinks() just planted,
        # one call too early. The resulting nvc Fatal ("failed to open
        # .../regbank_schema.hex") lands in the sim log, not in this
        # exception's message, so retry unconditionally rather than
        # string-matching (tried that first — the exception text is just
        # "NVC elaboration failed: Command [...] exit status 1", the file
        # name never reaches it). Reseed post-recompile (build_dir now
        # holds fresh compiled artifacts, so THIS retry won't re-trigger the
        # staleness auto-clean) and retry once. A genuine second failure
        # (real RTL/test defect, not the staleness race) propagates from
        # this second call uncaught, same as any other test failure.
        _seed_regbank_rom_symlinks(DEEPSKOPION_ROOT)
        _run_core_seam_simulation(run_simulation, module, DEEPSKOPION_ROOT, top_level)


def _run_core_seam_simulation(run_simulation, module: str, DEEPSKOPION_ROOT: Path,
                              top_level: str = "tb_stimulus_core_seam") -> None:
    """``top_level`` is tb_stimulus_core_seam (DE25 loopback wiring) or
    tb_stimulus_core_seam_zybo (Zynq wiring, DS-P1.90); the bench file is
    hw/sim/<top_level>.vhd and the rest of the source list is shared."""
    run_simulation(
        top_level=top_level,
        module=module,
        top_level_lang="vhdl",
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "ctu_can_fd_rtl": [
                f"{DEEPSKOPION_ROOT}/tools/sim/vendor_stubs/ctu_can_fd/can_top_apb_stub.vhd",
            ],
            "rr": [
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/ddr_stream_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/support/functions_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/support/xilinx/rr_fifo_async_mixed_xpm_body.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/support/rr_fifo_async_mixed.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/support/xilinx/rr_fifo_sync_xpm_body.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/support/rr_fifo_sync.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/support/ddr_sync_cdc.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/ddr_fifo_intf.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/axi4r.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/ptr_mgr.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/burstr_setter.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/downloader.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/support/skid_buffer.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/axi4w.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/burstw_setter.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/uploader.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/ddr_stream_axi.vhd",
                # DS-T1.16: deepskopion_core instantiates the PolarFire FIC arm
                # (DS-P2.124) — without it every seam test dies at elaboration
                # on "design unit DDR_STREAM_FIC not found in library RR".
                f"{DEEPSKOPION_ROOT}/libs/routertl/rr_ddr_stream/rtl/ddr_stream_fic.vhd",
            ],
            "olo": [
                f"{DEEPSKOPION_ROOT}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_pkg_array.vhd",
                f"{DEEPSKOPION_ROOT}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_pkg_attribute.vhd",
                f"{DEEPSKOPION_ROOT}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_pkg_math.vhd",
                f"{DEEPSKOPION_ROOT}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_pkg_string.vhd",
                f"{DEEPSKOPION_ROOT}/libs/open-logic/olo_base_wconv_xn2n/src/base/vhdl/olo_base_pkg_logic.vhd",
                f"{DEEPSKOPION_ROOT}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_ram_sdp.vhd",
                f"{DEEPSKOPION_ROOT}/libs/open-logic/olo_base_fifo_async/src/base/vhdl/olo_base_cc_bits.vhd",
                f"{DEEPSKOPION_ROOT}/libs/open-logic/olo_base_fifo_async/src/base/vhdl/olo_base_cc_reset.vhd",
                f"{DEEPSKOPION_ROOT}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_fifo_sync.vhd",
                f"{DEEPSKOPION_ROOT}/libs/open-logic/olo_base_fifo_async/src/base/vhdl/olo_base_fifo_async.vhd",
                f"{DEEPSKOPION_ROOT}/libs/open-logic/olo_base_wconv_n2xn/src/base/vhdl/olo_base_wconv_n2xn.vhd",
                f"{DEEPSKOPION_ROOT}/libs/open-logic/olo_base_wconv_xn2n/src/base/vhdl/olo_base_wconv_xn2n.vhd",
            ],
            "rr_rea": [
                f"{DEEPSKOPION_ROOT}/generated/rr_rea_build_id_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_dpram.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_capture_fsm.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_regbank.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_cdc.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_jtag_iface.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_fill_fsm.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_crc_sweep.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_trust_core.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_axi4lite.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_axis_window.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_top.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_trig_xbar.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/rea/rtl/rr_rea_jtag_intel.vhd",
            ],
            "tich": [
                f"{DEEPSKOPION_ROOT}/hw/rtl/pkg/rom_info_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/pkg/datalogger_records_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/pkg/system_regbank_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/pkg/zs_msg_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/emio/ds_emio_mux.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/stimulus/ds_stimulus_gen.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/infra/edge_batcher/edge_batcher.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/infra/edge_batcher_aggregator/edge_batcher_aggregator.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/infra/error_event_emitter/error_event_emitter.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/infra/error_event_bank/error_event_bank.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/fifo_sync.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/edge_detector/edge_detector.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/i2c/i2c_slave.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/responder/i2c_injector.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/infra/maxfreq_event_emitter/maxfreq_event_emitter.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/infra/maxfreq_event_bank/maxfreq_event_bank.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/pkg/arbiter_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/pkg/system_config_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/pkg/axi_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/infra/arbiter/arbiter.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/can/can_bit_timing.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/can/can_crc15.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/can/can_crc17.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/can/can_crc21.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/can/can_destuff.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/can/can_frame_fsm.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/can/can_datalogger.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/nonstd/edge_datalogger/edge_datalogger.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/emio/glitch_filter.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/i2c/i2c_datalogger.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/jtag/jtag_datalogger.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/infilter/infilter.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/qspi/qspi_datalogger.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/pkg/uart_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/ram_dual_port/ram_dual_port.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/uart/uart_baudrate_gen.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/uart/uart_frame_gen.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/uart/uart_playback.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/uart/uart_stop_finder.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/uart/uart_datalogger.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/infra/datalogger_gather/datalogger_gather.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/responder/spi_slave.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/responder/spi_injector.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/regbank/regbank_rom.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/regbank/system_regbank.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/regbank/system_regbank_wrapper.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/datalogger/infra/zs_msg_framer/zs_msg_framer.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/ring_watermark.vhd",
                f"{DEEPSKOPION_ROOT}/hw/rtl/deepskopion_core.vhd",
                f"{DEEPSKOPION_ROOT}/hw/sim/{top_level}.vhd",
            ],
        },
    )
