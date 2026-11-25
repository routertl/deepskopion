# rr unknown-llm (AI-generated)
"""Adversarial behavioral cocotb test for commit-address clock domain crossing (DS-T1.21).

Verifies the toggle req/ack handshake between clk_ddr and clk_sys:
1. clk_ddr and clk_sys run at different, non-integer-related periods (7 ns and 10 ns).
2. ddr_wr_commit_addr alternates every clk_ddr cycle between patterns whose
   upper and lower halves differ (0x0000FFFF and 0xFFFF0000).
3. Upper and lower halves are updated with deliberate skew (offset by half a cycle)
   so an un-synchronized or torn read exposes mixed halves (0x00000000 or 0xFFFFFFFF).
4. On every clk_sys update of ddr_commit_addr_sys, asserts that the captured 32-bit word
   equals one value the DDR side actually held (never a mix of halves).
5. Asserts that commit_addr_held_ddr does not change while a req/ack exchange is in flight.

Kills Mutant A (in-flight guard removed -> held value changes in flight) and
Mutant B (ddr_commit_addr_sys directly assigned from ddr_wr_commit_addr -> captures torn mixed halves).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

def _find_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "project.yml").exists() and (p / "hw" / "rtl").is_dir():
            return p
    raise RuntimeError("deepskopion root (project.yml + hw/rtl) not found")

DEEPSKOPION_ROOT = _find_root(Path(__file__).resolve())
os.environ["ROUTERTL_WAVES_DIR"] = str(DEEPSKOPION_ROOT / "sim" / "waves")
sys.path.insert(0, str(DEEPSKOPION_ROOT))

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from engine.simulation import run_simulation

CLK_DDR_PERIOD_NS = 7   # ~142.857 MHz
CLK_SYS_PERIOD_NS = 10  # 100 MHz
# Non-integer-related periods: 10 / 7 is non-integer

PATTERN_A = 0x0000FFFF
PATTERN_B = 0xFFFF0000
VALID_PATTERNS = {PATTERN_A, PATTERN_B}


@cocotb.test()
async def test_commit_addr_crossing_coherence(dut):
    # Start both clocks with different, non-integer-related periods
    cocotb.start_soon(Clock(dut.CLK_DDR, CLK_DDR_PERIOD_NS, units="ns").start())
    cocotb.start_soon(Clock(dut.CLK_SYS, CLK_SYS_PERIOD_NS, units="ns").start())

    # Initial reset
    dut.RST_DDR.value = 1
    dut.RST_SYS.value = 1
    dut.DDR_WR_COMMIT_ADDR_HI.value = 0
    dut.DDR_WR_COMMIT_ADDR_LO.value = 0

    await Timer(50, units="ns")
    dut.RST_DDR.value = 0
    dut.RST_SYS.value = 0
    await Timer(30, units="ns")

    test_done = False
    updates_seen = 0

    async def driver():
        cycle = 0
        while not test_done:
            await RisingEdge(dut.CLK_DDR)
            val = PATTERN_A if (cycle % 2 == 0) else PATTERN_B
            val_hi = (val >> 16) & 0xFFFF
            val_lo = val & 0xFFFF

            # Update upper half at +1 ns after clk_ddr rising edge
            await Timer(1, units="ns")
            dut.DDR_WR_COMMIT_ADDR_HI.value = val_hi

            # Skew: update lower half half-cycle later (+3 ns -> 4 ns from rising edge)
            await Timer(3, units="ns")
            dut.DDR_WR_COMMIT_ADDR_LO.value = val_lo
            cycle += 1

    async def in_flight_checker():
        in_flight_value = None
        while not test_done:
            await RisingEdge(dut.CLK_DDR)
            req = int(dut.COMMIT_REQ_TGL_DDR.value)
            ack_vec = int(dut.COMMIT_ACK_SYNC_DDR.value)
            ack = (ack_vec >> 1) & 1
            held = int(dut.COMMIT_ADDR_HELD_DDR.value)

            if req != ack:
                # Exchange is in flight
                if in_flight_value is None:
                    in_flight_value = held
                    assert in_flight_value in VALID_PATTERNS, (
                        f"Held value 0x{in_flight_value:08X} at transfer start is not a valid pattern!"
                    )
                else:
                    assert held == in_flight_value, (
                        f"commit_addr_held_ddr changed while req/ack exchange was in flight! "
                        f"Expected 0x{in_flight_value:08X}, got 0x{held:08X}"
                    )
            else:
                in_flight_value = None

    async def sys_monitor():
        nonlocal updates_seen
        prev_captured = int(dut.DDR_COMMIT_ADDR_SYS.value)
        while not test_done:
            await RisingEdge(dut.CLK_SYS)
            captured = int(dut.DDR_COMMIT_ADDR_SYS.value)
            if captured != prev_captured:
                # On every clk_sys update of ddr_commit_addr_sys, assert the captured
                # 32-bit word equals one value the DDR side actually held (never a mix of halves).
                assert captured in VALID_PATTERNS, (
                    f"Torn/mixed word captured in clk_sys: 0x{captured:08X} "
                    f"is not one of valid held patterns {[hex(p) for p in VALID_PATTERNS]}!"
                )
                prev_captured = captured
                updates_seen += 1

    cocotb.start_soon(driver())
    cocotb.start_soon(in_flight_checker())
    cocotb.start_soon(sys_monitor())

    # Run for 1500 ns: plenty of time for 20+ handshake roundtrips and dozens of clk_sys samples
    await Timer(1500, units="ns")
    test_done = True
    await Timer(20, units="ns")

    assert updates_seen >= 5, (
        f"Too few coherent handshake updates observed in clk_sys: {updates_seen} (expected >= 5)"
    )
    dut._log.info(
        f"DS-T1.21 commit address crossing coherence PASS: {updates_seen} updates captured with 0 torn words"
    )


def _seed_regbank_rom_symlinks(root: Path) -> None:
    for build_dir in (root / "sim" / "work" / "nvc",
                      root / "hw" / "sim" / "work" / "nvc"):
        for hex_name in ("regbank_schema.hex", "config_schema.hex"):
            real = root / "hw" / "rtl" / "regbank" / hex_name
            link = build_dir / "hw" / "rtl" / "regbank" / hex_name
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                if link.resolve() == real.resolve():
                    continue
                link.unlink()
            link.symlink_to(real)


def get_commit_addr_crossing_sources(root: Path) -> dict[str, list[str]]:
    r = str(root)
    pkg_path = f"{r}/hw/rtl/pkg/datalogger_config_pkg.vhd"
    gather_path = f"{r}/hw/rtl/datalogger/infra/datalogger_gather/datalogger_gather.vhd"
    tb_path = f"{r}/hw/sim/cocotb/tests/infra/commit_addr_crossing/tb_commit_addr_crossing.vhd"

    return {
        "ctu_can_fd_rtl": [
            f"{r}/tools/sim/vendor_stubs/ctu_can_fd/can_top_apb_stub.vhd",
        ],
        "rr": [
            f"{r}/libs/routertl/rr_ddr_stream/rtl/ddr_stream_pkg.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/support/functions_pkg.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/support/xilinx/rr_fifo_async_mixed_xpm_body.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/support/rr_fifo_async_mixed.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/support/xilinx/rr_fifo_sync_xpm_body.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/support/rr_fifo_sync.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/support/ddr_sync_cdc.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/ddr_fifo_intf.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/axi4r.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/ptr_mgr.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/burstr_setter.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/downloader.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/support/skid_buffer.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/axi4w.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/burstw_setter.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/uploader.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/ddr_stream_axi.vhd",
            f"{r}/libs/routertl/rr_ddr_stream/rtl/ddr_stream_fic.vhd",
        ],
        "olo": [
            f"{r}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_pkg_array.vhd",
            f"{r}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_pkg_attribute.vhd",
            f"{r}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_pkg_math.vhd",
            f"{r}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_pkg_string.vhd",
            f"{r}/libs/open-logic/olo_base_wconv_xn2n/src/base/vhdl/olo_base_pkg_logic.vhd",
            f"{r}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_ram_sdp.vhd",
            f"{r}/libs/open-logic/olo_base_fifo_async/src/base/vhdl/olo_base_cc_bits.vhd",
            f"{r}/libs/open-logic/olo_base_fifo_async/src/base/vhdl/olo_base_cc_reset.vhd",
            f"{r}/libs/open-logic/olo_base_fifo_sync/src/base/vhdl/olo_base_fifo_sync.vhd",
            f"{r}/libs/open-logic/olo_base_fifo_async/src/base/vhdl/olo_base_fifo_async.vhd",
            f"{r}/libs/open-logic/olo_base_wconv_n2xn/src/base/vhdl/olo_base_wconv_n2xn.vhd",
            f"{r}/libs/open-logic/olo_base_wconv_xn2n/src/base/vhdl/olo_base_wconv_xn2n.vhd",
        ],
        "rr_rea": [
            f"{r}/generated/rr_rea_build_id_pkg.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_pkg.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_dpram.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_capture_fsm.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_regbank.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_cdc.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_jtag_iface.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_fill_fsm.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_crc_sweep.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_trust_core.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_axi4lite.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_axis_window.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_top.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_trig_xbar.vhd",
            f"{r}/libs/routertl/rea/rtl/rr_rea_jtag_intel.vhd",
        ],
        "tich": [
            f"{r}/hw/rtl/pkg/rom_info_pkg.vhd",
            pkg_path,
            f"{r}/hw/rtl/pkg/datalogger_pkg.vhd",
            f"{r}/hw/rtl/pkg/datalogger_records_pkg.vhd",
            f"{r}/hw/rtl/pkg/system_regbank_pkg.vhd",
            f"{r}/hw/rtl/pkg/zs_msg_pkg.vhd",
            f"{r}/hw/rtl/emio/ds_emio_mux.vhd",
            f"{r}/hw/rtl/stimulus/ds_stimulus_gen.vhd",
            f"{r}/hw/rtl/datalogger/infra/edge_batcher/edge_batcher.vhd",
            f"{r}/hw/rtl/datalogger/infra/edge_batcher_aggregator/edge_batcher_aggregator.vhd",
            f"{r}/hw/rtl/datalogger/infra/error_event_emitter/error_event_emitter.vhd",
            f"{r}/hw/rtl/datalogger/infra/error_event_bank/error_event_bank.vhd",
            f"{r}/hw/rtl/fifo_sync.vhd",
            f"{r}/libs/routertl/edge_detector/edge_detector.vhd",
            f"{r}/hw/rtl/datalogger/i2c/i2c_slave.vhd",
            f"{r}/hw/rtl/responder/i2c_injector.vhd",
            f"{r}/hw/rtl/datalogger/infra/maxfreq_event_emitter/maxfreq_event_emitter.vhd",
            f"{r}/hw/rtl/datalogger/infra/maxfreq_event_bank/maxfreq_event_bank.vhd",
            f"{r}/hw/rtl/pkg/arbiter_pkg.vhd",
            f"{r}/hw/rtl/pkg/system_config_pkg.vhd",
            f"{r}/hw/rtl/pkg/axi_pkg.vhd",
            f"{r}/hw/rtl/datalogger/infra/arbiter/arbiter.vhd",
            f"{r}/hw/rtl/datalogger/can/can_bit_timing.vhd",
            f"{r}/hw/rtl/datalogger/can/can_crc15.vhd",
            f"{r}/hw/rtl/datalogger/can/can_crc17.vhd",
            f"{r}/hw/rtl/datalogger/can/can_crc21.vhd",
            f"{r}/hw/rtl/datalogger/can/can_destuff.vhd",
            f"{r}/hw/rtl/datalogger/can/can_frame_fsm.vhd",
            f"{r}/hw/rtl/datalogger/infra/config_control/config_control.vhd",
            f"{r}/hw/rtl/datalogger/can/can_datalogger.vhd",
            f"{r}/hw/rtl/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
            f"{r}/hw/rtl/datalogger/nonstd/edge_datalogger/edge_datalogger.vhd",
            f"{r}/hw/rtl/emio/glitch_filter.vhd",
            f"{r}/hw/rtl/datalogger/i2c/i2c_datalogger.vhd",
            f"{r}/hw/rtl/datalogger/jtag/jtag_datalogger.vhd",
            f"{r}/libs/routertl/infilter/infilter.vhd",
            f"{r}/hw/rtl/datalogger/qspi/qspi_datalogger.vhd",
            f"{r}/hw/rtl/pkg/uart_pkg.vhd",
            f"{r}/libs/routertl/ram_dual_port/ram_dual_port.vhd",
            f"{r}/hw/rtl/datalogger/uart/uart_baudrate_gen.vhd",
            f"{r}/hw/rtl/datalogger/uart/uart_frame_gen.vhd",
            f"{r}/hw/rtl/datalogger/uart/uart_playback.vhd",
            f"{r}/hw/rtl/datalogger/uart/uart_stop_finder.vhd",
            f"{r}/hw/rtl/datalogger/uart/uart_datalogger.vhd",
            gather_path,
            f"{r}/hw/rtl/responder/spi_slave.vhd",
            f"{r}/hw/rtl/responder/spi_injector.vhd",
            f"{r}/hw/rtl/regbank/regbank_rom.vhd",
            f"{r}/hw/rtl/regbank/system_regbank.vhd",
            f"{r}/hw/rtl/regbank/system_regbank_wrapper.vhd",
            f"{r}/hw/rtl/datalogger/infra/zs_msg_framer/zs_msg_framer.vhd",
            f"{r}/hw/rtl/ring_watermark.vhd",
            f"{r}/hw/rtl/deepskopion_core.vhd",
            tb_path,
        ],
    }


def main():
    root = DEEPSKOPION_ROOT
    _seed_regbank_rom_symlinks(root)
    libs = get_commit_addr_crossing_sources(root)

    try:
        run_simulation(
            top_level="tb_commit_addr_crossing",
            module="test_commit_addr_crossing",
            top_level_lang="vhdl",
            waves=False,
            verbose=False,
            work_lib="tich",
            custom_libraries=libs,
        )
    except RuntimeError:
        _seed_regbank_rom_symlinks(root)
        run_simulation(
            top_level="tb_commit_addr_crossing",
            module="test_commit_addr_crossing",
            top_level_lang="vhdl",
            waves=False,
            verbose=False,
            work_lib="tich",
            custom_libraries=libs,
        )


if __name__ == "__main__":
    main()
