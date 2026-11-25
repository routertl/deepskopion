# rr unknown-llm (AI-generated)
"""DS-P2.128 — the PolarFire REA MMIO door, integrated below the FIC0 CSR plane.

The upstream rr_rea_axi4lite bridge is sim-proven in rr-rea itself
(test_rea_axi4lite_p3_931, test_rea_dump_path_contract_p2_4). What is NEW here
and has no other test is the INTEGRATION the fabric ships:

  * axil_demux4 — the DE25 axil_demux3 FSM with selector 3 decoded instead of
    DECERR. Selectors 0..2 (regbank / CAN A / CAN B on the fabric) must still
    route exactly as before, and selector 3 must reach the REA door.
  * pfsoc_rea_door — upstream rr_rea_top (G_REG_IFACE external) + its bridge +
    the DS-P2.132 known-answer probe, the SAME entity deepskopion_pfsoc_top
    instantiates.

The capture test walks the register map the way a host does over /dev/mem
(arm, value-match trigger, STATUS.done, START_PTR-rotated DATA_BASE walk) and
checks every captured cell against the probe's CONSTRUCTION — not merely that
something came back:
    [7:0] counter (+1 per sample) | [11:8] 0xA | [12] heartbeat | [15:13] 0b101

Run: rr sim run test_pfsoc_rea_csr_door
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

test_dir = Path(__file__).resolve().parent
os.environ["ROUTERTL_WAVES_DIR"] = str(test_dir.parents[3] / "waves")

import sim.cocotb  # noqa: E402

_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation  # noqa: E402

SEL_REA = 0x3_0000          # FIC0 base + 0x3_0000 = 0x6003_0000 on silicon

ADDR_VERSION = 0x00
ADDR_CTRL = 0x04
ADDR_STATUS = 0x08
ADDR_SAMPLE_W = 0x0C
ADDR_DEPTH = 0x10
ADDR_PRETRIG = 0x14
ADDR_POSTTRIG = 0x18
ADDR_CAPTURE_LEN = 0x1C
ADDR_TRIG_MODE = 0x20
ADDR_TRIG_VALUE = 0x24
ADDR_TRIG_MASK = 0x28
ADDR_START_PTR = 0xC8
ADDR_DATA_BASE = 0x100

REA_VERSION = 0x5245410F  # libs/routertl/rea rr_rea_pkg.vhd C_REA_VERSION (1.12)
CTRL_ARM = 0x01
STATUS_DONE = 0x04
TRIG_MODE_VALUE_MATCH = 0x1

PRETRIG = 8
POSTTRIG = 23
# counter[7:0] = 0x40 with the fixed fields; heartbeat (bit 12) masked out.
TRIG_VALUE = (0b101 << 13) | (0xA << 8) | 0x40
TRIG_MASK = 0xEFFF


async def _start(dut):
    cocotb.start_soon(Clock(dut.clk_i, 10, unit="ns").start())
    dut.rst_i.value = 1
    dut.s_awvalid_i.value = 0
    dut.s_wvalid_i.value = 0
    dut.s_arvalid_i.value = 0
    dut.s_bready_i.value = 1
    dut.s_rready_i.value = 1
    dut.s_wstrb_i.value = 0xF
    await ClockCycles(dut.clk_i, 8)
    dut.rst_i.value = 0
    await ClockCycles(dut.clk_i, 4)


async def _write(dut, addr, data):
    dut.s_awaddr_i.value = addr
    dut.s_wdata_i.value = data
    dut.s_awvalid_i.value = 1
    dut.s_wvalid_i.value = 1
    aw_done = w_done = False
    while not (aw_done and w_done):
        await RisingEdge(dut.clk_i)
        if dut.s_awready_o.value == 1:
            aw_done = True
            dut.s_awvalid_i.value = 0
        if dut.s_wready_o.value == 1:
            w_done = True
            dut.s_wvalid_i.value = 0
    for _ in range(200):
        await RisingEdge(dut.clk_i)
        if dut.s_bvalid_o.value == 1:
            assert int(dut.s_bresp_o.value) == 0, f"write 0x{addr:05X}: BRESP={int(dut.s_bresp_o.value)}"
            return
    raise AssertionError(f"write 0x{addr:05X}: no BVALID within 200 cycles")


async def _read(dut, addr):
    dut.s_araddr_i.value = addr
    dut.s_arvalid_i.value = 1
    while True:
        await RisingEdge(dut.clk_i)
        if dut.s_arready_o.value == 1:
            break
    dut.s_arvalid_i.value = 0
    for _ in range(200):
        await RisingEdge(dut.clk_i)
        if dut.s_rvalid_o.value == 1:
            assert int(dut.s_rresp_o.value) == 0, f"read 0x{addr:05X}: RRESP={int(dut.s_rresp_o.value)}"
            return int(dut.s_rdata_o.value)
    raise AssertionError(f"read 0x{addr:05X}: no RVALID within 200 cycles")


@cocotb.test()
async def all_four_selectors_route_reads(dut):
    """Selectors 0..2 reach their own window with the offset intact; 3 reaches REA.

    Interleaved so a demux that latches a stale selector returns the previous
    window's word."""
    await _start(dut)
    order = [0, 3, 1, 3, 2, 0, 2, 1, 3]
    for sel in order:
        off = 0x0100 + 4 * sel
        got = await _read(dut, (sel << 16) | (ADDR_VERSION if sel == 3 else off))
        if sel == 3:
            assert got == REA_VERSION, (
                f"selector 3 (0x3_0000) read 0x{got:08X}, expected REA VERSION "
                f"0x{REA_VERSION:08X} — the demux is not reaching the REA door "
                "(axil_demux3 would DECERR here)")
        else:
            want = 0x5E10_0000 | (sel << 16) | off
            assert got == want, (
                f"selector {sel} read 0x{got:08X}, expected 0x{want:08X} — mis-routed "
                "window or lost offset")
    dut._log.info("PASS: 4 selectors route reads, interleaved")


@cocotb.test()
async def writes_land_on_exactly_one_window(dut):
    """One write, one hit, on the addressed window only; REA register writes read back."""
    await _start(dut)
    hits = [0, 0, 0]

    async def _count():
        while True:
            await RisingEdge(dut.clk_i)
            v = int(dut.hit_o.value)
            for n in range(3):
                if v >> n & 1:
                    hits[n] += 1

    mon = cocotb.start_soon(_count())
    for sel in (0, 1, 2, 1):
        await _write(dut, (sel << 16) | 0x10, 0xC0FFEE00 | sel)
    await _write(dut, SEL_REA | ADDR_PRETRIG, 0x2A)
    await ClockCycles(dut.clk_i, 4)
    mon.kill()
    assert hits == [1, 2, 1], f"per-window write hits {hits}, expected [1, 2, 1]"
    got = await _read(dut, SEL_REA | ADDR_PRETRIG)
    assert got == 0x2A, f"REA PRETRIG through selector 3 read back 0x{got:08X}, wrote 0x2A"
    dut._log.info("PASS: writes route 1:1, REA register write/readback through demux4")


@cocotb.test()
async def capture_through_selector_3_matches_known_answer(dut):
    """Arm, value-match trigger, done, walk DATA_BASE — every cell checked."""
    await _start(dut)
    assert await _read(dut, SEL_REA | ADDR_SAMPLE_W) == 16
    depth = await _read(dut, SEL_REA | ADDR_DEPTH)
    assert depth == 4096, f"DEPTH={depth}"

    await _write(dut, SEL_REA | ADDR_PRETRIG, PRETRIG)
    await _write(dut, SEL_REA | ADDR_POSTTRIG, POSTTRIG)
    await _write(dut, SEL_REA | ADDR_TRIG_MODE, TRIG_MODE_VALUE_MATCH)
    await _write(dut, SEL_REA | ADDR_TRIG_VALUE, TRIG_VALUE)
    await _write(dut, SEL_REA | ADDR_TRIG_MASK, TRIG_MASK)
    await _write(dut, SEL_REA | ADDR_CTRL, CTRL_ARM)

    for _ in range(200):
        status = await _read(dut, SEL_REA | ADDR_STATUS)
        if status & STATUS_DONE:
            break
        await ClockCycles(dut.clk_i, 64)
    else:
        raise AssertionError(f"capture never reached STATUS.done (STATUS=0x{status:08X})")

    cap_len = await _read(dut, SEL_REA | ADDR_CAPTURE_LEN)
    assert cap_len == PRETRIG + POSTTRIG + 1, f"CAPTURE_LEN={cap_len}"
    start = await _read(dut, SEL_REA | ADDR_START_PTR) % depth
    cells = []
    for i in range(cap_len):
        phys = (start + i) % depth
        cells.append(await _read(dut, SEL_REA | (ADDR_DATA_BASE + 4 * phys)) & 0xFFFF)

    trig = cells[PRETRIG]
    assert trig & TRIG_MASK == TRIG_VALUE, (
        f"cell[{PRETRIG}] = 0x{trig:04X}, expected the trigger word 0x{TRIG_VALUE:04X} "
        f"(mask 0x{TRIG_MASK:04X}); window={[hex(c) for c in cells]}")
    for i, c in enumerate(cells):
        assert c >> 13 == 0b101, f"cell {i} = 0x{c:04X}: sync field [15:13] != 0b101"
        assert (c >> 8) & 0xF == 0xA, f"cell {i} = 0x{c:04X}: known-answer nibble != 0xA"
        if i:
            prev = cells[i - 1] & 0xFF
            assert c & 0xFF == (prev + 1) & 0xFF, (
                f"cell {i} counter 0x{c & 0xFF:02X} after 0x{prev:02X}: a dropped, "
                "duplicated or stale sample (the 1.1.1 stale-read defect reads the "
                "previous cell)")
    dut._log.info(
        f"PASS: {cap_len} cells, trigger 0x{trig:04X} at index {PRETRIG}, START_PTR={start}, "
        f"first=0x{cells[0]:04X} last=0x{cells[-1]:04X}")


def main():
    ds_root = test_dir.parents[4]
    rea = ds_root / "libs/routertl/rea/rtl"
    run_simulation(
        top_level="tb_pfsoc_rea_csr",
        module="test_pfsoc_rea_csr_door",
        top_level_lang="vhdl",
        waves=False,
        custom_libraries={
            # The routertl/rea package in library rr_rea, exactly as the pfsoc
            # target's `packages` + ip.lock place it (DS-P1.73); its regbank's
            # `use work.rr_rea_build_id_pkg` resolves inside rr_rea.
            "rr_rea": [
                str(rea / "rr_rea_pkg.vhd"),
                str(ds_root / "generated/rr_rea_build_id_pkg.vhd"),
                str(rea / "rr_rea_dpram.vhd"),
                str(rea / "rr_rea_capture_fsm.vhd"),
                str(rea / "rr_rea_regbank.vhd"),
                str(rea / "rr_rea_cdc.vhd"),
                str(rea / "rr_rea_jtag_iface.vhd"),
                str(rea / "rr_rea_crc_sweep.vhd"),
                str(rea / "rr_rea_fill_fsm.vhd"),
                str(rea / "rr_rea_trust_core.vhd"),
                str(rea / "rr_rea_trig_xbar.vhd"),
                str(rea / "rr_rea_axi4lite.vhd"),
                str(rea / "rr_rea_axis_window.vhd"),
                str(rea / "rr_rea_top.vhd"),
            ],
            "work": [
                str(ds_root / "hw/rtl/pfsoc/pf_known_answer_core.vhd"),
                str(ds_root / "hw/rtl/pfsoc/axil_demux4.vhd"),
                str(ds_root / "hw/pfsoc_rea/pfsoc_rea_door.vhd"),
                str(ds_root / "hw/sim/tb_pfsoc_rea_csr.vhd"),
            ],
        },
    )


if __name__ == "__main__":
    main()
