"""DS-P2.217 — back-to-back `i2cget`-shaped reads against i2c_injector in the
configuration `CONTROL stim i2c` leaves on the board.

Silicon (Zybo-Z7-20, image canfd_demo @ deepskopion 884726c7): after
`CONTROL stim i2c` + `CONTROL edge_route i2c` (EMIO_MUX_SEL=0x11), a loop of
200 x `i2cget -y 0 0x50 0x00` on PS-I2C0 returned data ONCE and failed 199
times (NACK at 0x50).

What `stim i2c` writes (sw/stream/stimulus_armer.py arm_i2c +
sw/stream/stimulus_contract.py I2C_REGISTER_SEQUENCE):
    DATALOGGER_CONFIG 0x20 <- 0x2000 then 0x2001
    EMIO_MUX_SEL      0x24 <- 0x1          (whole word)
    I2C_INJ_CONTROL   0x30 <- 0xA1         (enable, addr 0x50)
    STIM_I2C_CLK_DIV  0x230 <- 125
    STIM_I2C_CONTROL  0x22C <- 0x2A1
    STIM_I2C_DATA     0x234 <- 0x003ECDAB
It NEVER writes I2C_INJ_DATAIN (0x34), so the injector TX FIFO stays empty.

`i2cget -y 0 0x50 0x00` on i2c-cadence at 100 kHz is:
    START, 0xA0 (W), 0x00 (reg), Sr, 0xA1 (R), 1 byte, NACK, STOP
and on an address NACK the driver aborts the message with a STOP.

Tests (hard-coded expected values, ROUTERTL-002):
  * test_stim_config_i2cget_x5_empty_fifo — the exact board configuration
    (CONTROL=0xA1, FIFO empty). REQ-6 says an empty-FIFO read NAKs or returns
    0xFF; the implementation takes the ACK + 0xFF path on transaction 1, so every
    later identical transaction must too. Asserts all 5 ACK every address/data
    byte and return 0xFF.
  * test_preloaded_i2cget_x5 — the same 5 transactions with 5 bytes preloaded
    through DATAIN. Isolates the transaction shape (write + Sr + read) from the
    empty-FIFO path.
  * test_empty_fifo_read_then_long_idle_gap — after one empty-FIFO read,
    a gap longer than the 25 ms inactivity watchdog lets the next transaction be
    ACKed again (before the i2c_slave fix this was the only exit).

Before the fix in i2c_slave.vhd (I2C_WAIT_INTERNAL_DATA counted as driving
SDA after releasing it, which masked STOP and START), the first test failed:
transactions 2..5 NACKed at the address, FSM left in I2C_WAIT_INTERNAL_DATA.

Generics are the Zybo-Z7-20 build values: G_CLK_FREQ_HZ = 100 MHz (core
G_SYS_CLK_HZ default, deepskopion_core.vhd:24 -> :1652), so the watchdog is the
real 25 ms. G_FIFO_DEPTH = 6 (the injector default the core uses).

Run:  rr sim run test_i2c_inj_i2cget_back_to_back --wait
"""
import sys
from pathlib import Path

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
_ds_root = Path(__file__).resolve().parents[5]
if str(_ds_root / "hw" / "sim") not in sys.path:
    sys.path.insert(0, str(_ds_root / "hw" / "sim"))

import cocotb
from cocotb.triggers import ClockCycles, Timer

from sdk.cocotb_helpers import requires

# Reuse the existing injector testbench harness (shipped rr_cocotb_tb I2C BFM).
from test_i2c_injector import I2cInjectorTb


GENERICS = {
    "G_FIFO_DEPTH": 6,
    "G_CLK_FREQ_HZ": 100_000_000,
}

# Register value the stim i2c sequence writes to I2C_INJ_CONTROL (0x30).
STIM_I2C_INJ_CONTROL = 0xA1
ADDR_W = 0xA0              # 0x50 << 1 | W
ADDR_R = 0xA1              # 0x50 << 1 | R
REG = 0x00                 # i2cget register argument
EMPTY_FIFO_BYTE = 0xFF     # REQ-6 released-bus value
N_TRANSACTIONS = 5
# Gap between i2cget invocations. On the board a shell loop spaces them by
# ~ms, far below the 25 ms watchdog; any gap below the watchdog is equivalent.
GAP_NS = 200_000           # 200 us
WATCHDOG_NS = 25_000_000   # (100e6/1000)*25 cycles at 10 ns


async def stim_config(tb):
    """Leave the injector exactly as `CONTROL stim i2c` does: CONTROL=0xA1,
    no DATAIN write."""
    await tb.setup()
    tb.dut.CONTROL.value = STIM_I2C_INJ_CONTROL
    tb.dut.ENABLE.value = 1   # deepskopion_core.vhd:1658 ENABLE => '1'
    await ClockCycles(tb.env.clk, 4)


def fsm_state(tb) -> str:
    """i2c_slave FSM state, for the log only (never asserted)."""
    try:
        return str(tb.dut.i_injector.u_slave.fsmI2c_r.value)
    except Exception:  # noqa: BLE001 - hierarchy access is simulator-dependent
        return "?"


async def i2cget(tb):
    """One `i2cget -y 0 0x50 0x00` as i2c-cadence issues it.

    Returns dict(ack_w, ack_reg, ack_r, data). On an address NACK the
    transfer is aborted with STOP (what the Linux driver does), and the
    remaining fields stay None.
    """
    i2c = tb.i2c
    r = {"ack_w": None, "ack_reg": None, "ack_r": None, "data": None}
    await i2c.start()
    r["ack_w"] = await i2c.write_byte(ADDR_W)
    if not r["ack_w"]:
        await i2c.stop()
        return r
    r["ack_reg"] = await i2c.write_byte(REG)
    if not r["ack_reg"]:
        await i2c.stop()
        return r
    await i2c.start()                      # repeated START
    r["ack_r"] = await i2c.write_byte(ADDR_R)
    if not r["ack_r"]:
        await i2c.stop()
        return r
    r["data"] = await i2c.read_byte(send_ack=False)
    await i2c.stop()
    return r


def fmt(results):
    rows = []
    for i, r in enumerate(results, 1):
        d = "None" if r["data"] is None else f"0x{r['data']:02X}"
        rows.append(f"  #{i}: ack_w={r['ack_w']} ack_reg={r['ack_reg']} "
                    f"ack_r={r['ack_r']} data={d} fsm_after={r['fsm']} "
                    f"STATUS=0x{r['status']:08X}")
    return "\n".join(rows)


async def run_loop(tb, n):
    results = []
    for _ in range(n):
        r = await i2cget(tb)
        await Timer(GAP_NS, unit="ns")
        r["fsm"] = fsm_state(tb)
        r["status"] = tb.dut.STATUS.value.integer
        results.append(r)
        tb.env.log.info(f"i2cget -> {r}")
    return results


@cocotb.test()
@requires("I2C-INJ-REQ-6")
async def test_stim_config_i2cget_x5_empty_fifo(dut):
    """5 back-to-back i2cget reads, stim-i2c configuration (FIFO empty):
    every one must be ACKed and return 0xFF, like the first."""
    tb = I2cInjectorTb(dut)
    await stim_config(tb)
    assert dut.STATUS.value.integer & 0x1 == 1, "precondition: FIFO must be empty"

    results = await run_loop(tb, N_TRANSACTIONS)

    expected = {"ack_w": True, "ack_reg": True, "ack_r": True,
                "data": EMPTY_FIFO_BYTE}
    bad = [i + 1 for i, r in enumerate(results)
           if any(r[k] != v for k, v in expected.items())]
    assert not bad, (
        f"transactions {bad} of {N_TRANSACTIONS} not ACKed with 0x{EMPTY_FIFO_BYTE:02X} "
        f"(stim-i2c config, I2C_INJ_CONTROL=0x{STIM_I2C_INJ_CONTROL:02X}, FIFO empty):\n"
        + fmt(results))


@cocotb.test()
@requires("I2C-INJ-REQ-1", "I2C-INJ-REQ-2")
async def test_preloaded_i2cget_x5(dut):
    """Same 5 i2cget reads with 5 bytes preloaded: each ACKed, data in order."""
    tb = I2cInjectorTb(dut)
    await stim_config(tb)
    payload = [0x11, 0x22, 0x33, 0x44, 0x55]
    await tb.push_bytes(payload)
    await ClockCycles(tb.env.clk, 4)

    results = await run_loop(tb, N_TRANSACTIONS)

    bad = [i + 1 for i, r in enumerate(results)
           if not (r["ack_w"] and r["ack_reg"] and r["ack_r"]
                   and r["data"] == payload[i])]
    assert not bad, (
        f"transactions {bad} wrong; want data {[hex(b) for b in payload]}:\n"
        + fmt(results))


@cocotb.test()
@requires("I2C-INJ-REQ-6")
async def test_empty_fifo_read_then_long_idle_gap(dut):
    """After an empty-FIFO read and an idle gap longer than the 25 ms
    watchdog, the next i2cget is ACKed (before the fix this was the slave's
    only exit; the CONTROL value was never consumed)."""
    tb = I2cInjectorTb(dut)
    await stim_config(tb)

    first = await i2cget(tb)
    assert first["ack_w"] and first["ack_r"], f"first read not ACKed: {first}"

    await Timer(WATCHDOG_NS + 1_000_000, unit="ns")   # 26 ms idle
    second = await i2cget(tb)
    assert second["ack_w"] and second["ack_reg"] and second["ack_r"] \
        and second["data"] == EMPTY_FIFO_BYTE, (
            f"after a {WATCHDOG_NS + 1_000_000} ns idle gap the read is still "
            f"not ACKed: {second}")


if __name__ == "__main__":
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[5]
    DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"
    from hw.sim.portable_fifo_sources import olo_fifo_sources

    run_simulation(
        top_level="tb_i2c_injector",
        module="test_i2c_inj_i2cget_back_to_back",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/i2c/i2c_slave.vhd",
                f"{DEEPSKOPION_RTL}/responder/i2c_injector.vhd",
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_i2c_injector.vhd",
            ],
        },
    )
