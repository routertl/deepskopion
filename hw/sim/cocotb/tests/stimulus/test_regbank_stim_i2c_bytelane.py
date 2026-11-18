"""
test_regbank_stim_i2c_bytelane — DS-T2.82 sim reproduction at the pfsoc clock.

Silicon (PolarFire kit, DS-P2.126 evidence, 2026-09-23): the DE-C0 I2C
self-test decoded ['start',(0xA0,ack),(0xAB,ack),(0x00,ack),(0x00,ack),'stop']
where the contract expects 0xAB/0xCD/0x3E. The same contract passes on DE25.

Root cause (DS-T2.82): the kit's riscv64 CPython (Buildroot 2024.11, rv64gc,
strict-align tuning) compiles ctypes' integer setter
(`memcpy(ptr, &x, 4)` on an unknown-alignment pointer) to four `sb` byte
stores — no `sw` exists in any integer setter of its _ctypes module. So
MmioRegbank.write_u32(STIM_I2C_DATA, 0x003ECDAB) reached FIC0 as four
single-lane AXI writes. The FIRST one (byte0 = 0xAB, WSTRB 0x1) fires the
slot's valid, and ds_stimulus_gen latches the whole word while bytes 1..3
still hold their reset value 0 — the later lanes land while the master is
busy and are (correctly, by design) not a new trigger.

This bench drives the REAL regbank + REAL ds_stimulus_gen at clk_sys =
100 MHz with the contract-fixed STIM_I2C_CLK_DIV = 125 and pins BOTH halves:

  1. the RISC-V store shape (four byte-lane writes, register at reset value)
     reproduces the silicon observation byte-for-byte: A0 AB 00 00;
  2. one 32-bit store at the same clock/divider yields A0 AB CD 3E —

which exonerates the pfsoc clock, the divider and the RTL sequencing, and
fixes the defect in the register writer (sw/stream/regbank.py), guarded by
sw/stream/tests/test_regbank_word_store_ds_t2_82.py.

ONE cocotb test per file (nvc multi-test-session hang — see
test_stimulus_uart_frame.py).

Run (from the deepskopion dir):  rr sim run test_regbank_stim_i2c_bytelane
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, First, RisingEdge, with_timeout
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

CLK_PERIOD_NS = 10          # 100 MHz — the pfsoc clk_sys (FIC0 CSR domain)

# Contract values — sw/stream/testbench/de_c0_stimulus_contract.py.
STIM_I2C_CONTROL = 0x22C
STIM_I2C_CLK_DIV = 0x230
STIM_I2C_DATA = 0x234
I2C_ADDRESS_7B = 0x50
I2C_DATA = [0xAB, 0xCD, 0x3E]
I2C_PACKED_DATA = 0x003ECDAB
I2C_CLK_DIV = 125

# Per-edge bound: one SCL phase is CLK_DIV * 10 ns = 1.25 us, so 50 us only
# ever fires on a master that stopped toggling.
EDGE_TIMEOUT_NS = 50_000


async def _decode_transaction(dut) -> list[int]:
    """Return the bytes (address byte first) of the next START..STOP frame,
    sampled on SCL rising edges. ACK bits are dropped (no responder here)."""
    scl, sda = dut.I2C_BUS_SCL, dut.I2C_BUS_SDA
    while True:
        await with_timeout(FallingEdge(sda), EDGE_TIMEOUT_NS, "ns")
        if int(scl.value) == 1:
            break                                   # START
    # A bit is sampled on SCL rise but only committed when SCL falls again:
    # the STOP condition also raises SCL (with SDA low) and then raises SDA
    # while SCL is high, so its SCL rise must not be counted as a data bit.
    bits: list[int] = []
    pending = None
    while True:
        scl_rise, scl_fall, sda_rise = RisingEdge(scl), FallingEdge(scl), RisingEdge(sda)
        fired = await with_timeout(
            First(scl_rise, scl_fall, sda_rise), EDGE_TIMEOUT_NS, "ns"
        )
        if fired is sda_rise:
            if int(scl.value) == 1:
                break                               # STOP
        elif fired is scl_rise:
            pending = int(sda.value)
        elif pending is not None:
            bits.append(pending)
            pending = None
    assert len(bits) % 9 == 0, f"frame is not whole bytes+ACK: {len(bits)} bits"
    out = []
    for i in range(0, len(bits), 9):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        out.append(byte)
    return out


async def _transact(dut, write_coro) -> list[int]:
    decode = cocotb.start_soon(_decode_transaction(dut))
    await write_coro
    return await decode


@cocotb.test()
async def test_stim_i2c_data_word_vs_bytelane_at_pfsoc_clock(dut):
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, unit="ns").start())
    dut.RST.value = 1
    dut.S_AXI_AWVALID.value = 0
    dut.S_AXI_WVALID.value = 0
    dut.S_AXI_BREADY.value = 0
    dut.S_AXI_ARVALID.value = 0
    dut.S_AXI_RREADY.value = 0
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 8)
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)

    control = ((len(I2C_DATA) - 1) << 8) | (I2C_ADDRESS_7B << 1) | 0x1
    await axi.write(STIM_I2C_CLK_DIV, I2C_CLK_DIV.to_bytes(4, "little"))
    await axi.write(STIM_I2C_CONTROL, control.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)
    expected_addr = I2C_ADDRESS_7B << 1

    # (1) The kit's store shape: four single-byte writes, lane 0 first, with
    # STIM_I2C_DATA still at its reset value — reproduces the silicon bytes.
    async def byte_stores():
        for lane, b in enumerate(I2C_PACKED_DATA.to_bytes(4, "little")):
            await axi.write(STIM_I2C_DATA + lane, bytes([b]))

    got = await _transact(dut, byte_stores())
    dut._log.info("byte-lane stores decoded: %s", [hex(b) for b in got])
    assert got == [expected_addr, 0xAB, 0x00, 0x00], (
        f"byte-lane store shape no longer reproduces the DS-T2.82 silicon "
        f"observation: {[hex(b) for b in got]}"
    )
    await ClockCycles(dut.CLK, 200)

    # (2) One 32-bit store at the same clock + contract divider: all bytes.
    got = await _transact(
        dut, axi.write(STIM_I2C_DATA, I2C_PACKED_DATA.to_bytes(4, "little"))
    )
    dut._log.info("word store decoded: %s", [hex(b) for b in got])
    assert got == [expected_addr] + I2C_DATA, (
        f"single 32-bit STIM_I2C_DATA store at 100 MHz / div {I2C_CLK_DIV} "
        f"decoded {[hex(b) for b in got]}, expected "
        f"{[hex(b) for b in [expected_addr] + I2C_DATA]}"
    )


if __name__ == "__main__":
    from engine.simulation import run_simulation

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[5]
    DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

    run_simulation(
        top_level="tb_regbank_stim_i2c_bytelane",
        module="test_regbank_stim_i2c_bytelane",
        top_level_lang="vhdl",
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/rom_info_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/system_regbank_pkg.vhd",
                f"{DEEPSKOPION_RTL}/regbank/system_regbank.vhd",
                f"{DEEPSKOPION_RTL}/stimulus/ds_stimulus_gen.vhd",
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_regbank_stim_i2c_bytelane.vhd",
            ],
        },
    )
