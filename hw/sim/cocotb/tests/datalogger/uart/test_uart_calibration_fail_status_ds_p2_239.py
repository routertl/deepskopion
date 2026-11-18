"""DS-P2.239: a failed autoSync calibration emits a STATUS frame naming it.

Before the fix, an autoSync calibration that matched no format instance set
calibrationFail_r and held the frame generator in IDLE
(startStreaming = calibrationDone and not calibrationFail), so after the
ENABLED lifecycle frame the data logger emitted nothing for the whole capture.

The stimulus here cannot match any format: each "frame" is low for 13 bit-times
and high for 1, so every candidate stop-bit position (6..11 bit-times after the
start edge, 5..9 data bits with or without parity) samples 0. The test first
asserts that the calibration really failed (calibrationFail_r = 1), so a
stimulus that stops failing cannot pass the STATUS checks vacuously.

Expected, per failed calibration: exactly one STATUS frame
(type 0b100) with code 0x01, CALIBRATION_TIMEOUT ("no valid frames detected",
uart_datalogger_protocol.yml status_codes), dev id 23. The frame is held until
UART_MSG_RDY accepts it: one transfer under backpressure, none lost.
"""

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import sim.cocotb  # noqa: F401  (SDK path setup, as the other UART tests)

from engine.simulation import run_simulation
from sdk.cocotb_helpers import requires
from uart_datalogger_test_utils import GENERICS, CUSTOM_LIBRARIES, setup_dut

CLK_HZ = 100_000_000
BAUD = 1_000_000
PER_BIT = CLK_HZ // BAUD
AUTOSYNC = 0x03            # enable | autoSync
EXPLICIT_8N1 = 0x0D        # enable | dataBits=8 (autoSync off)
DEV = 23                   # GENERICS["G_UART_DEV_ID"], hard-coded (ROUTERTL-002)
STATUS_TYPE = 0b100
CODE_CALIBRATION_TIMEOUT = 0x01
CODE_ENABLED = 0x05
CODE_DISABLED = 0x06
BREAKS = 80                # > the 70-byte calibration buffer (yml min_bytes)


def _fields(word):
    """32-bit data logger word -> (dev, type, code)."""
    return (word >> 27) & 0x1F, (word >> 24) & 0x7, word & 0xFF


class _Transfers:
    """Every word the consumer accepted (UART_MSG_VLD and UART_MSG_RDY)."""

    def __init__(self, dut):
        self.dut = dut
        self.words = []
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await RisingEdge(self.dut.CLK)
            if int(self.dut.UART_MSG_VLD.value) and int(self.dut.UART_MSG_RDY.value):
                self.words.append(int(self.dut.UART_MSG_OUT.value))

    def status_codes(self):
        return [c for d, t, c in map(_fields, self.words) if t == STATUS_TYPE]


async def _arm(dut, cfg):
    dut.CONFIG.value = DEV << 11
    await ClockCycles(dut.CLK, 20)
    dut.CONFIG.value = (DEV << 11) | cfg
    await ClockCycles(dut.CLK, 200)


async def _send_breaks(dut, n=BREAKS):
    for _ in range(n):
        dut.UART_IN.value = 0
        await ClockCycles(dut.CLK, 13 * PER_BIT)
        dut.UART_IN.value = 1
        await ClockCycles(dut.CLK, PER_BIT)


async def _send_8n1(dut, words):
    for w in words:
        for bit in [0] + [(w >> i) & 1 for i in range(8)] + [1]:
            dut.UART_IN.value = bit
            await ClockCycles(dut.CLK, PER_BIT)


async def _wait_calibration(dut):
    for _ in range(3000):
        if int(dut.calibrationDone_r.value) == 1:
            return
        await ClockCycles(dut.CLK, 100)
    raise AssertionError("calibration never finished (calibrationDone_r stayed 0)")


@cocotb.test()
@requires("UART-REQ-19")
async def failed_calibration_emits_one_calibration_timeout_status(dut):
    # UART_MSG_OUT / UART_MSG_VLD / UART_MSG_RDY are sampled by _Transfers;
    # explicit for the @requires signal-reference check (coverage-map).
    _uart_msg_out, _uart_msg_vld = dut.UART_MSG_OUT, dut.UART_MSG_VLD
    await setup_dut(dut)
    log = _Transfers(dut)
    await _arm(dut, AUTOSYNC)
    await _send_breaks(dut)
    await _wait_calibration(dut)
    assert int(dut.calibrationFail_r.value) == 1, \
        "precondition: the break stimulus must fail calibration"
    await ClockCycles(dut.CLK, 20 * PER_BIT)
    codes = log.status_codes()
    assert codes == [CODE_ENABLED, CODE_CALIBRATION_TIMEOUT], (
        f"DS-P2.239: STATUS codes {[hex(c) for c in codes]}; expected the arm's "
        f"enabled, then exactly one calibration_timeout (0x01)")
    fail = [w for w in log.words if _fields(w)[1:] == (STATUS_TYPE, CODE_CALIBRATION_TIMEOUT)]
    assert _fields(fail[0])[0] == DEV, f"dev id {_fields(fail[0])[0]}, expected {DEV}"


@cocotb.test()
@requires("UART-REQ-19")
async def the_failure_status_waits_for_ready_and_is_sent_once(dut):
    # UART_MSG_OUT / UART_MSG_VLD / UART_MSG_RDY are sampled by _Transfers;
    # explicit for the @requires signal-reference check (coverage-map).
    _uart_msg_out, _uart_msg_vld = dut.UART_MSG_OUT, dut.UART_MSG_VLD
    await setup_dut(dut)
    log = _Transfers(dut)
    await _arm(dut, AUTOSYNC)
    await _send_breaks(dut, BREAKS - 1)
    dut.UART_MSG_RDY.value = 0              # consumer stalls across the decision
    await _send_breaks(dut, 1)
    await _wait_calibration(dut)
    assert int(dut.calibrationFail_r.value) == 1, "precondition: calibration failed"
    await ClockCycles(dut.CLK, 5 * PER_BIT)
    assert int(dut.UART_MSG_VLD.value) == 1, "DS-P2.239: the STATUS frame is not held while not ready"
    held = int(dut.UART_MSG_OUT.value)
    assert _fields(held) == (DEV, STATUS_TYPE, CODE_CALIBRATION_TIMEOUT), hex(held)
    dut.UART_MSG_RDY.value = 1
    await ClockCycles(dut.CLK, 20 * PER_BIT)
    assert log.status_codes() == [CODE_ENABLED, CODE_CALIBRATION_TIMEOUT], \
        [hex(c) for c in log.status_codes()]


@cocotb.test()
@requires("UART-REQ-19")
async def each_failed_calibration_reports_and_a_good_one_does_not(dut):
    """A re-arm clears the failure; the next failure reports again; a
    calibration that locks emits no calibration_timeout."""
    # UART_MSG_OUT / UART_MSG_VLD / UART_MSG_RDY are sampled by _Transfers;
    # explicit for the @requires signal-reference check (coverage-map).
    _uart_msg_out, _uart_msg_vld = dut.UART_MSG_OUT, dut.UART_MSG_VLD
    await setup_dut(dut)
    log = _Transfers(dut)
    for _ in range(2):
        await _arm(dut, AUTOSYNC)
        await _send_breaks(dut)
        await _wait_calibration(dut)
        assert int(dut.calibrationFail_r.value) == 1, "precondition: calibration failed"
        await ClockCycles(dut.CLK, 20 * PER_BIT)
    assert log.status_codes().count(CODE_CALIBRATION_TIMEOUT) == 2, \
        [hex(c) for c in log.status_codes()]
    await _arm(dut, EXPLICIT_8N1)
    await _send_8n1(dut, [0x41 + (i % 26) for i in range(80)])
    await _wait_calibration(dut)
    assert int(dut.calibrationFail_r.value) == 0, "precondition: explicit 8N1 locks"
    await ClockCycles(dut.CLK, 200 * PER_BIT)
    assert log.status_codes().count(CODE_CALIBRATION_TIMEOUT) == 2, \
        [hex(c) for c in log.status_codes()]


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_calibration_fail_status_ds_p2_239",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
