"""DS-P2.244: autoSync must not report 7 data bits + parity for an 8N1 line
because the first calibration frames happen to share one parity.

On an 8N1 line the 8-sample stop-finder instance (GEN_STOP_FINDER(3)) can read
d0..d6 as data and d7 as a parity bit. Its parity check (uart_stop_finder.vhd,
parityFail_r) only records a change of parity between consecutive frames, so
while every frame so far has the same parity the instance reports parity
(PARITY(0) <= not parityFail_r). At 97073018 the detector took the first single
winner at G_MIN_MATCHES = 7 (uart_datalogger.vhd:690, :703-707) and read the
parity bit at that moment: seed 1234 of test_integration_1M_8N1, whose first 11
calibration bytes all have an odd number of ones, locked 7O1 (nbitsConf 7,
parityConf "11") and reassembled 134/250. Since df1d12c4 the format is read
after the replay has drained, when 0xB7 at frame 11 has set parityFail_r.

1. seed 1234's own bytes: (done, fail, nbits) = (1, 0, 8), no parity, all 250
   bytes; parityFail_r of the 8-sample instance first rises at frame 11.
2. the same condition from a designed pattern: 11 odd-parity frames, one even
   frame, filler.
3. the protocol limit: when every calibration byte has the same parity, 8N1
   and 7 data bits + parity cannot be told apart on the line; the test pins
   what autoSync reports (UART_DATALOGGER_FEATURES.md, "Calibration data limits").
"""

import cocotb
from cocotb.triggers import ClockCycles, Edge, ReadOnly, RisingEdge, Timer
from cocotb.utils import get_sim_time

import sim.cocotb  # noqa: F401  (SDK path setup, as the other UART tests)

from engine.simulation import run_simulation
from uart_datalogger_test_utils import GENERICS, CUSTOM_LIBRARIES, setup_dut
from rr_cocotb_tb.drivers.uart_master import UartSource

BAUD = 1_000_000
CLK_PERIOD_NS = 10
AUTOSYNC = 0x03            # enable | autoSync

SEED1234_CAL = [
    0x85, 0xDA, 0x32, 0xF4, 0x02, 0xB3, 0xD5, 0x8F, 0x7F, 0x34, 0x01, 0xB7,
    0xF8, 0x0F, 0x6B, 0x37, 0x24, 0xCC, 0x82, 0x4C, 0x48, 0x34, 0x69, 0x43,
    0x5E, 0x67, 0xE6, 0x34, 0xDD, 0x75, 0x8E, 0x6A, 0x00, 0xBE, 0xC1, 0xD2,
    0xA9, 0xA2, 0xEA, 0xDB, 0x98, 0x2B, 0x24, 0xDA, 0x29, 0x88, 0x1C, 0x98,
    0x00, 0x03, 0xAB, 0x9A, 0xD0, 0xFB, 0x2A, 0xDA, 0x35, 0x89, 0xF9, 0x74,
    0xEA, 0xC4, 0x24, 0x5F, 0x33, 0xBB, 0x43, 0xFD, 0x03, 0x96, 0xF9, 0x4E,
    0xEE, 0x09, 0xBE, 0x52, 0x44, 0x3D, 0x82, 0xFB, 0x02, 0x05, 0x76, 0xFE,
    0xE7, 0x34, 0xCF, 0xBC, 0xE5, 0x79, 0xA2, 0xAC, 0x3A, 0xCA, 0x42, 0x44,
    0x25, 0x7A, 0x37, 0x32, 0x6B, 0xB6, 0xE1, 0x8F, 0xC1, 0x66, 0xCC, 0x7E,
    0xC6, 0xEC, 0xA4, 0x75, 0x65, 0x8D, 0x9E, 0xF4, 0x4D, 0xD8, 0x56, 0x6F,
    0xF4, 0x39, 0x18, 0x01, 0x54, 0xBC, 0x87, 0x72, 0x6D, 0xB3, 0x45, 0xBA,
    0x93, 0xFD, 0x16, 0xA0, 0x7B, 0x94, 0xC0, 0x7D, 0xDD, 0xEC, 0x5B, 0x97,
    0x24, 0xF2, 0xC6, 0x15, 0x62, 0x69,
]
SEED1234_PAYLOAD = [
    0x38, 0xB7, 0x00, 0x1C, 0x59, 0x5C, 0x38, 0x49, 0x0E, 0x42, 0xEC, 0x07,
    0x5A, 0xDB, 0x8C, 0xD3, 0xD2, 0xB5, 0xE6, 0x15, 0xA9, 0x46, 0x6E, 0x94,
    0xCA, 0x54, 0xB7, 0xE8, 0x32, 0x5A, 0xC1, 0xB0, 0xA4, 0x0F, 0xF4, 0x77,
    0x93, 0x79, 0x8F, 0xDC, 0x91, 0x1C, 0x0E, 0x26, 0xF4, 0xF4, 0x60, 0x08,
    0x62, 0xC6, 0x13, 0x16, 0xEA, 0x15, 0x88, 0x45, 0x39, 0xB2, 0x6B, 0x95,
    0x81, 0x1D, 0xA9, 0xC5, 0x55, 0x39, 0x38, 0x9F, 0xCD, 0xAB, 0x23, 0x97,
    0x89, 0x9E, 0x3E, 0xE7, 0x86, 0xE1, 0x61, 0x8F, 0xA2, 0xCA, 0x44, 0xA1,
    0x4F, 0xC0, 0xAC, 0xFF, 0xFF, 0x55, 0x20, 0x98, 0x0E, 0x9E, 0xD1, 0xF8,
    0xDB, 0x75, 0xE6, 0xD5,
]
# 11 frames with an odd number of ones, then one with an even number, then
# filler. Each of the five instances is exercised: 0x80 refutes nothing, the
# single-bit bytes give the 5-, 6- and 7-bit instances a 0 at their stop sample.
MIN_LEAD = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x07, 0x0B, 0x0D]
MIN_BREAKER = 0x03
MIN_FILLER = [0x55, 0xAA, 0x33, 0xCC, 0x0F, 0xF0, 0x5A, 0xA5,
              0x55, 0xAA, 0x33, 0xCC, 0x0F, 0xF0, 0x5A, 0xA5]
MIN_PAYLOAD = [0x3C, 0xC3, 0x69, 0x96, 0x12, 0x48, 0x21, 0x84]
# Every byte has an odd number of ones.
SAME_PARITY_CAL = (MIN_LEAD * 3)[:30]
SAME_PARITY_PAYLOAD = [0x01, 0x07, 0x1F, 0x7F, 0x0E, 0x70]


async def _record(dut, out):
    """(type, data byte) of every accepted transfer: VLD and RDY at a CLK edge."""
    while True:
        await RisingEdge(dut.CLK)
        await ReadOnly()
        if int(dut.UART_MSG_VLD.value) == 1 and int(dut.UART_MSG_RDY.value) == 1:
            raw = int(dut.UART_MSG_OUT.value)
            out.append(((raw >> 24) & 0x7, raw & 0xFF))


async def _rises(sig, out):
    """Sim time of every 0 -> 1 of sig."""
    while True:
        await RisingEdge(sig)
        out.append(get_sim_time("ns"))


async def _run(dut, cal, payload):
    """Arm autoSync, send cal, wait for the lock, send payload, drain.

    Returns the lock tuple, the decoded data bytes and the 8-sample instance's
    parityFail_r rises as frame indices from its first match.
    """
    dev = GENERICS["G_UART_DEV_ID"] & 0x1F
    dut.CONFIG.value = dev << 11
    await ClockCycles(dut.CLK, 20)
    dut.CONFIG.value = (dev << 11) | AUTOSYNC
    await ClockCycles(dut.CLK, 200)
    i3 = dut.GEN_STOP_FINDER[3].i_uart_stop_finder
    assert int(i3.G_CONF_NBITS.value) == 8
    msgs, matches, pfail = [], [], []
    tasks = [cocotb.start_soon(_record(dut, msgs)),
             cocotb.start_soon(_rises(i3.parityFail_r, pfail))]

    async def _match_times():
        prev = int(i3.cntMatches_r.value)
        while True:
            await Edge(i3.cntMatches_r)
            cur = int(i3.cntMatches_r.value)
            if cur > prev:
                matches.append(get_sim_time("ns"))
            prev = cur

    tasks.append(cocotb.start_soon(_match_times()))
    uart = UartSource(dut.UART_IN, baud=BAUD, bits=8, stop_bits=1, parity="none")
    await uart.write_bytes(cal)
    for _ in range(2000):
        if int(dut.calibrationDone_r.value) == 1:
            break
        await ClockCycles(dut.CLK, 100)
    lock = (int(dut.calibrationDone_r.value), int(dut.calibrationFail_r.value),
            int(dut.nbitsConf_r.value), int(dut.parityBitFound_r.value),
            str(dut.parityConf_r.value))
    br = int(dut.baudRateMin_r.value)
    await uart.write_bytes(payload)
    await Timer(2, units="ms")
    for t in tasks:
        t.kill()
    data = [d for t, d in msgs if t in (2, 3)]
    frame_ns = 10 * br * CLK_PERIOD_NS
    # Whole replayed frames since the first match (the rise lands one clock
    # into its frame).
    pfail_frames = ([round((t - matches[0]) / frame_ns) for t in pfail]
                    if matches else [])
    dut._log.info(f"lock (done, fail, nbits, parityBitFound, parityConf) = {lock}; "
                  f"BR {br}; parityFail_r rises at frames {pfail_frames}; "
                  f"{len(data)} data bytes: {bytes(data).hex()}")
    return lock, data, pfail_frames


@cocotb.test()
async def seed_1234_bytes_lock_8n1_not_7o1(dut):
    assert len(SEED1234_CAL) == 150 and len(SEED1234_PAYLOAD) == 100
    await setup_dut(dut)
    lock, data, pfail = await _run(dut, SEED1234_CAL, SEED1234_PAYLOAD)
    assert pfail[:1] == [11], f"parityFail_r first rise at frames {pfail}, expected 11"
    assert lock == (1, 0, 8, 0, "00"), f"lock {lock}, expected (1, 0, 8, 0, '00')"
    assert data == SEED1234_CAL + SEED1234_PAYLOAD, (
        f"{len(data)} data bytes: {bytes(data).hex()}")


@cocotb.test()
async def eleven_same_parity_frames_then_a_breaker_lock_8n1(dut):
    await setup_dut(dut)
    cal = MIN_LEAD + [MIN_BREAKER] + MIN_FILLER
    lock, data, pfail = await _run(dut, cal, MIN_PAYLOAD)
    assert pfail[:1] == [11], f"parityFail_r first rise at frames {pfail}, expected 11"
    assert lock == (1, 0, 8, 0, "00"), f"lock {lock}, expected (1, 0, 8, 0, '00')"
    assert data == cal + MIN_PAYLOAD, f"{len(data)} data bytes: {bytes(data).hex()}"


@cocotb.test()
async def all_same_parity_buffer_is_the_protocol_limit(dut):
    await setup_dut(dut)
    lock, data, pfail = await _run(dut, SAME_PARITY_CAL, SAME_PARITY_PAYLOAD)
    assert pfail == [], f"parityFail_r rose at frames {pfail} on an all-odd buffer"
    # Measured on 91ad5913 and 97073018 alike: the 8-sample instance reads d7
    # as odd parity, so autoSync reports 7 data bits + odd parity and the
    # decoded data is each byte's d0..d6.
    assert lock == (1, 0, 7, 1, "11"), f"lock {lock}, expected (1, 0, 7, 1, '11')"
    assert data == [b & 0x7F for b in SAME_PARITY_CAL + SAME_PARITY_PAYLOAD], (
        f"{len(data)} data bytes: {bytes(data).hex()}")


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_autosync_parity_ambiguity_ds_p2_244",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
