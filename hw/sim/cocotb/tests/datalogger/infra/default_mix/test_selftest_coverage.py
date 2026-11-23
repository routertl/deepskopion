# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""Self-test coverage: which default-mix data loggers a board-alone source reaches.

Bench: hw/sim/tb_selftest_coverage.vhd, the real deepskopion_core with the
default mix, header pins idle high (nothing connected), and the board-alone
sources as ports. Every data logger of the mix is armed first. Then each
scenario applies one source the way the product image does and records which
logger ids emit records:

* the in-core stimulus generator, armed by REPLAYING the register writes of the
  production armers in sw/stream/stimulus_armer.py (arm_uart, arm_i2c, arm_spi,
  arm_edge), recorded from the real functions, so the words are the image's.
  DS_MSS_FABRIC selects the target: 0 for the Zybo and DE25 images, 1 for the
  PolarFire image, whose UART arm also selects the MSS MMUART1 (bit 9) and whose
  bytes then come from the MSS_UART_TX leg.
* the edge routes of sw/stream/edge_route.py, composed with a stimulus arm.
* CAN: the Zybo PS CAN pair (PS_CAN0_TX), the PolarFire MSS CAN (MSS_CAN_TX
  with bit 11), each sending one frame.

The measured table is printed and asserted against hard-coded expected ids, one
row per scenario. Which scenario a target's image can run is a property of its
top (which legs it wires) and is recorded in TARGETS, from the product tops.

Run (from the deepskopion dir): rr sim run test_selftest_coverage
"""
from __future__ import annotations

import builtins
import contextlib
import io
import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer
from cocotb.utils import get_sim_time
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

_HERE = Path(__file__).resolve()
_COCOTB = _HERE.parents[4]                          # hw/sim/cocotb
_ROOT = _HERE.parents[7]                            # repository root
sys.path.insert(0, str(_COCOTB))                                    # can_bfm
sys.path.insert(0, str(_COCOTB / "tests" / "stimulus" / "core_seam"))  # run_core_seam
sys.path.insert(0, str(_ROOT))                                      # sw.stream

from can_bfm import CanBusBFM  # noqa: E402
from _core_seam_common import run_core_seam  # noqa: E402
from sw.stream import protocol_codes as _pc, zs_msg as _zm  # noqa: E402

TOP = "tb_selftest_coverage"
CLK_PERIOD_NS = 10
SLOT_DATALOGGER_CONFIG = 0x20
SLOT_EMIO_MUX_SEL = 0x24
# An armer sleep is replayed as a wait of at most this long: one SPI byte at SPI_CLK_DIV 25 takes 18 x 25 clocks
# (4.5 us at CLK_PERIOD_NS), so the stimulus master is idle again before the next STIM_SPI_DATA write.
SLEEP_CAP_US = 25.0
# DS-P2.343: logger 8's bytes per SPI transfer, by value. MOSI is SPI_PAYLOAD; MISO is what arm_spi preloads into
# the on-chip responder (SPI_MISO_PAYLOAD). Hard-coded, not imported.
SPI_MOSI_BYTES = [0xDE, 0xAD, 0xBE, 0xEF]
SPI_MISO_BYTES = [0xA5, 0x5A, 0xC3, 0x3C]
STIM_CONTROL_SLOTS = (544, 556, 568, 580)          # UART, I2C, SPI, EDGE control

# Every data logger of the default mix, armed as tb_default_mix_core's test arms them.
ARM = {
    1: 0x0801, 2: 0x1001, 3: 0x1801,               # EDGE#1-3
    4: 0x2001, 5: 0x2801,                          # I2C#4, I2C#5
    8: 0x4001,                                     # SPI#8
    12: 0x600D, 13: 0x680D,                        # UART#12, UART#13
    20: 0xA009,                                    # CAN#20
}
MIX_IDS = sorted(ARM)

# Board-alone sources each product image has, from its top's wiring of the core:
#   deepskopion_top.vhd       PS EMIO + PS CAN pair; generator drives EMIO
#   deepskopion_de25_top.vhd  generator loopback into EMIO; emio_can tied '1'
#   deepskopion_pfsoc_fabric  generator loopback; MSS UART/CAN legs wired;
#                             emio_can tied '1'
TARGETS = {
    "zybo": ("stim_uart", "stim_i2c", "stim_spi", "stim_edge",
             "route_i2c", "route_spi", "route_uart", "route_can_ps", "ps_can"),
    "de25": ("stim_uart", "stim_i2c", "stim_spi", "stim_edge",
             "route_i2c", "route_spi", "route_uart"),
    "pfsoc": ("mss_uart", "stim_i2c", "stim_spi", "stim_edge",
              "route_i2c", "route_spi", "route_uart_mss", "mss_can"),
}

# Hard-coded expectation per scenario: the logger ids that emit, read from the
# core's board_pin mux (deepskopion_core.vhd) against the mix's BoardPins.
EXPECTED = {
    "stim_uart": [12],
    "mss_uart": [12],
    "stim_i2c": [4],
    "stim_spi": [8],
    "stim_edge": [1],
    "route_i2c": [1, 2, 4],
    "route_spi": [1, 2, 3, 8, 20],
    "route_uart": [12],
    "route_uart_mss": [1, 12],
    "route_can_ps": [1],
    "ps_can": [],                       # the PS CAN bus reaches no mix logger
    "mss_can": [],
}

# Records that come from a source but are not that logger's traffic: under the
# SPI edge route the core drives JB5 with the SPI chip-select, and JB5 is the
# CAN#20 RX pin of the mix. Excluded from "reached" (README, In-System Self-Test).
NOT_TRAFFIC = {"route_spi": [20]}


def _code(events: dict, name: str) -> int:
    return next(c for c, n in events.items() if n == name)


# Housekeeping records: a logger reports its own state, not traffic on its pin.
# Read from the generated tables the product decoder uses (sw/stream/decoders.py):
# for the 1:1-framed loggers the msg_type event_id is the record TYPE and
# payload[0] (word[7:0]) the event code; edge records carry their event in the
# event_id. Everything else is traffic: data bytes, START/STOP and CS edges,
# frame boundaries, edge batches and RLE, and protocol errors (activity on the
# wire). Cross-domain loss/provenance codes (event_id 0xF0-0xFF) are not traffic.
HOUSEKEEPING = {
    _zm.DOMAIN_UART: {(_pc.UART_TYPES["config"], None)} | {
        (_pc.UART_TYPES["status"], _code(_pc.UART_EVENTS, n))
        for n in ("enabled", "disabled", "calibration_timeout")},
    _zm.DOMAIN_I2C: {(_pc.I2C_TYPES["event"], _code(_pc.I2C_EVENTS, n))
                     for n in ("enabled", "disabled")},
    _zm.DOMAIN_SPI: {(_pc.SPI_TYPES["event"], _code(_pc.SPI_EVENTS, n))
                     for n in ("start", "stop", "timeout")},
    _zm.DOMAIN_CAN: {(_pc.CAN_TYPES["event"], _code(_pc.CAN_EVENTS, n))
                     for n in ("enabled", "disabled")},
    _zm.DOMAIN_EDGE: {(_zm.EVT_EDGE_LIFECYCLE, None), (_zm.EVT_EDGE_MAXFREQ, None)},
}


def is_traffic(msg_type: int, payload: int) -> bool:
    """True when a gather record is traffic on the logger's pin, False when it
    is housekeeping (lifecycle, config, max-frequency, loss provenance)."""
    domain, event_id = _zm.domain_of(msg_type), _zm.event_id_of(msg_type)
    if event_id >= 0xF0:
        return False
    rules = HOUSEKEEPING.get(domain, set())
    return (event_id, None) not in rules and (event_id, payload & 0xFF) not in rules


# Loss: a SEV_ERROR cross-domain record (event_id 0xF0-0xFF: FIFO overflow,
# arbiter drop, ring full, decode error, or an unknown code). None is allowed:
# no scenario here is expected to drop or fail to decode anything.
def is_loss(msg_type: int) -> bool:
    return _zm.severity_of(msg_type) == _zm.SEV_ERROR and _zm.event_id_of(msg_type) >= 0xF0


def counts_after(msg_type: int, ts: float, settled: float) -> bool:
    """An edge record counts only inside the settled window. A route's register
    writes switch an edge channel's source while the channel is armed, and the
    switch itself is recorded as an edge (DS-T2.100: Edge 3, 230 ns after
    EMIO_MUX_SEL moved it from header JA6 to the EMIO MISO). The product enables
    a routed channel after the switch, so on the board that edge never exists."""
    return _zm.domain_of(msg_type) != _zm.DOMAIN_EDGE or ts > settled


def reached(target: str) -> list[int]:
    """Mix loggers a target's board-alone sources give real traffic, from EXPECTED."""
    ids: set[int] = set()
    for scenario in TARGETS[target]:
        ids |= set(EXPECTED[scenario]) - set(NOT_TRAFFIC.get(scenario, []))
    return sorted(ids)


def _w(v: int) -> bytes:
    return (v & 0xFFFFFFFF).to_bytes(4, "little")


class _Recorder:
    """A regbank that records writes, so a production armer's sequence can be replayed."""

    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []

    def write_u32(self, offset: int, value: int) -> None:
        self.writes.append((int(offset), int(value) & 0xFFFFFFFF))

    def read_u32(self, offset: int) -> int:
        for off, val in reversed(self.writes):
            if off is not None and off == offset:
                return val
        return 0


MMUART1_SENT: list[bytes] = []


class HostDeviceOpened(AssertionError):
    """A simulation must never open a host device."""


@contextlib.contextmanager
def _no_host_devices():
    """Refuse, loudly, any open of a /dev path while a production armer runs.

    The armers open a device on the board (the PolarFire arm_uart writes
    /dev/ttyS1). Run from a simulation, the same call would open the bench
    host's device of that name."""
    real_open, real_os_open = builtins.open, os.open

    def _check(path):
        if str(os.fspath(path)).startswith("/dev"):
            raise HostDeviceOpened(f"simulation tried to open host device {path}")

    def guarded_open(file, *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)):
            _check(file)
        return real_open(file, *a, **k)

    def guarded_os_open(path, *a, **k):
        _check(path)
        return real_os_open(path, *a, **k)

    builtins.open, io.open, os.open = guarded_open, guarded_open, guarded_os_open
    try:
        yield
    finally:
        builtins.open, io.open, os.open = real_open, real_open, real_os_open


def _armer_writes(fn_name: str, mss: bool, route: str | None = None,
                  arm_route: str | None = None) -> list[tuple[int | None, float]]:
    """The register writes of a production armer, recorded in order with its sleeps.

    A sleep is recorded as ``(None, seconds)`` and replayed as a simulation wait: the stimulus master takes a
    STIM_SPI_DATA write only when idle (ds_stimulus_gen p_spi), so a replay without the waits sends one SPI byte
    where the board sends four, and that byte overlapped the edge route written after the arm, which on the board
    is applied only after the armer returns. ``arm_route`` is an edge route the armer takes itself (op=stim with a
    route, DS-P2.343), written before its burst. The armer's MMUART1 transmit (PolarFire) is captured, never sent:
    on this host /dev/ttyS1 is the host's own serial port."""
    os.environ["DS_MSS_FABRIC"] = "1" if mss else "0"
    import time as _time
    from sw.stream import edge_route, stimulus_armer
    rec = _Recorder()
    sleep, _time.sleep = _time.sleep, (lambda s=0, *_a, **_k: rec.writes.append((None, float(s))))
    send = stimulus_armer.transmit_mmuart1
    stimulus_armer.transmit_mmuart1 = lambda data: MMUART1_SENT.append(bytes(data)) or data
    try:
        with _no_host_devices():
            if fn_name:
                arm = getattr(stimulus_armer, fn_name)
                arm(rec, route=arm_route) if arm_route else arm(rec)
            if route:
                edge_route.apply_edge_route(rec, route)
        return rec.writes
    finally:
        _time.sleep = sleep
        stimulus_armer.transmit_mmuart1 = send


def _collect(dut, got: list) -> None:
    async def run():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.PROBE_DOUT_VALID.value) == 1 and int(dut.PROBE_DOUT_REQ.value) == 1:
                got.append((int(dut.PROBE_DOUT_DATALOGGER_ID.value),
                            int(dut.PROBE_DOUT_MSG_TYPE.value),
                            int(dut.PROBE_DOUT_PAYLOAD.value),
                            get_sim_time("ns")))
    cocotb.start_soon(run())


async def _uart_byte(tx, byte: int, bit_ns: int = 8680) -> None:
    for level in [0] + [(byte >> i) & 1 for i in range(8)] + [1]:
        tx.value = level
        await Timer(bit_ns, unit="ns")


async def _quiesce(axi, dut) -> None:
    await axi.write(SLOT_EMIO_MUX_SEL, _w(0))
    for slot in STIM_CONTROL_SLOTS:
        await axi.write(slot, _w(0))
    await Timer(200, unit="us")


async def _scenario(name: str, axi, dut) -> float:
    """Apply one board-alone source; return the sim time (ns) its last source-select or logger-enable write
    (EMIO_MUX_SEL, DATALOGGER_CONFIG) completed, the start of the scenario's settled traffic window. Those writes make
    records that are not traffic (a mux switch is a line change, an enable is a lifecycle record); the data writes
    after them (STIM_*_DATA, SPI_INJ_DATAIN) and the armer's waits are the burst, which counts (DS-P2.343)."""
    mss = name in ("mss_uart", "route_uart_mss", "mss_can")
    # (armer, edge route applied after the arm, edge route the arm takes itself). route_spi is the board's op=stim
    # spi with route spi (DS-P2.343): live_capture_client sends no separate edge_route then.
    plan = {
        "stim_uart": ("arm_uart", None, None), "mss_uart": ("arm_uart", None, None),
        "stim_i2c": ("arm_i2c", None, None), "stim_spi": ("arm_spi", None, None),
        "stim_edge": ("arm_edge", None, None),
        "route_i2c": ("arm_i2c", "i2c", None), "route_spi": ("arm_spi", None, "spi"),
        "route_uart": ("arm_uart", "uart", None), "route_uart_mss": ("arm_uart", "uart", None),
        "route_can_ps": ("", "can", None), "ps_can": ("", None, None), "mss_can": ("", None, None),
    }[name]
    # The window starts after the last source-select or logger-enable write, not after the armer's last step: the
    # armer's trailing wait follows its last byte, so a window starting after it never saw the burst it judges
    # (DS-P2.343: the route-before-burst arm measured edges 1-3 as housekeeping only). The mux switch and the enables
    # still fall before settled, so their records stay excluded; this is not a loosening.
    settled = get_sim_time("ns")
    for off, val in _armer_writes(plan[0], mss, plan[1], plan[2]):
        if off is None:
            await Timer(min(val * 1e6, SLEEP_CAP_US), unit="us")
            continue
        await axi.write(off, _w(val))
        await ClockCycles(dut.CLK, 4)
        if off in (SLOT_EMIO_MUX_SEL, SLOT_DATALOGGER_CONFIG):
            settled = get_sim_time("ns")
    if name in ("mss_uart", "route_uart_mss"):
        payload = MMUART1_SENT.pop() if MMUART1_SENT else b""
        assert payload, "the MSS-mode arm_uart sent no MMUART1 payload"
        for b in payload[:4]:            # the first bytes of the image's payload
            await _uart_byte(dut.MSS_UART_TX, b)
    if name in ("ps_can", "route_can_ps"):
        bfm = CanBusBFM(dut.PS_CAN0_TX, dut.CLK, prescaler=4)
        await bfm.idle(bits=8)
        await bfm.send(can_id=0x123, data=[0xAB], ack=0)
        await bfm.idle(bits=10)
    if name == "mss_can":
        await axi.write(SLOT_EMIO_MUX_SEL, _w(1 << 11))
        settled = get_sim_time("ns")
        bfm = CanBusBFM(dut.MSS_CAN_TX, dut.CLK, prescaler=4)
        await bfm.idle(bits=8)
        await bfm.send(can_id=0x123, data=[0xAB], ack=0)
        await bfm.idle(bits=10)
    await Timer(1500, unit="us")
    return settled


@cocotb.test()
async def test_selftest_coverage(dut):
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 16)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 16)
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)

    got: list = []
    _collect(dut, got)

    async def arm_all():
        for dev_id, word in ARM.items():
            await axi.write(SLOT_DATALOGGER_CONFIG, _w(dev_id << 11))
            await ClockCycles(dut.CLK, 20)
            await axi.write(SLOT_DATALOGGER_CONFIG, _w(word))
            await ClockCycles(dut.CLK, 20)

    # Baseline: arming emits housekeeping records from every logger, so the
    # quiet window starts 20 us after arming, as every scenario window does,
    # and lasts as long as one. With no source driven it must stay empty.
    await arm_all()
    await Timer(20, unit="us")
    got.clear()
    await Timer(1500, unit="us")
    baseline = sorted({r[0] for r in got})

    measured: dict[str, set[int]] = {}
    housekeeping: dict[str, set[int]] = {}
    losses: dict[str, list] = {}
    spi_bytes: dict[str, tuple[list[int], list[int]]] = {}
    for name in EXPECTED:
        await _quiesce(axi, dut)
        await arm_all()
        await Timer(20, unit="us")
        got.clear()
        settled = await _scenario(name, axi, dut)
        measured[name] = {i for i, t, p, ts in got
                          if is_traffic(t, p) and counts_after(t, ts, settled)} & set(MIX_IDS)
        housekeeping[name] = {i for i, t, p, *_ in got if not is_traffic(t, p)} & set(MIX_IDS)
        losses[name] = sorted({(i, hex(_zm.event_id_of(t))) for i, t, *_ in got if is_loss(t)})
        if name == "route_spi":
            per_id = {i: (sum(1 for j, t, p, ts in got if j == i and is_traffic(t, p) and counts_after(t, ts, settled)),
                          sum(1 for j, t, p, _ts in got if j == i and not is_traffic(t, p)))
                      for i in (1, 2, 3, 6, 8, 20)}
            dut._log.info(f"RECORDS route_spi (id: traffic, housekeeping): {per_id}")
        spi8 = [(_zm.event_id_of(t), p & 0xFF) for i, t, p, _ts in got
                if i == 8 and _zm.domain_of(t) == _zm.DOMAIN_SPI]
        spi_bytes[name] = ([b for e, b in spi8 if e == _pc.SPI_TYPES["payload_a"]],
                           [b for e, b in spi8 if e == _pc.SPI_TYPES["payload_b"]])

    dut._log.info(f"idle baseline (headers high, no source): ids {baseline}")
    for target in TARGETS:
        got_reach = sorted(set().union(*(measured[s] - set(NOT_TRAFFIC.get(s, []))
                                         for s in TARGETS[target])))
        dut._log.info(f"TARGET {target}: reached {got_reach} "
                      f"not reached {sorted(set(MIX_IDS) - set(got_reach))}")
    for name in EXPECTED:
        dut._log.info(f"SCENARIO {name}: traffic {sorted(measured[name])} "
                      f"housekeeping only {sorted(housekeeping[name] - measured[name])} "
                      f"expected {sorted(EXPECTED[name])}")
    # Positive control: an edge route enables its edge channels, and each enable
    # is a housekeeping record. If none is seen the classifier, not the source, is
    # what decided reach.
    routes = [n for n in EXPECTED if n.startswith("route_")]
    no_hk = [n for n in routes if not housekeeping[n] & {1, 2, 3}]
    assert not no_hk, f"no edge housekeeping record seen under {no_hk}"
    lost = {n: v for n, v in losses.items() if v}
    assert not lost, f"loss records (datalogger id, event_id) in a scenario: {lost}"
    failures = [f"{n}: traffic {sorted(measured[n])}, expected {sorted(EXPECTED[n])}"
                for n in EXPECTED if measured[n] != set(EXPECTED[n])]
    assert baseline == [], f"records with no source at all: {baseline}"
    for name in ("stim_spi", "route_spi"):
        mosi, miso = spi_bytes[name]
        dut._log.info(f"SPI {name}: logger 8 MOSI {[hex(b) for b in mosi]} MISO {[hex(b) for b in miso]}")
        assert (mosi, miso) == (SPI_MOSI_BYTES, SPI_MISO_BYTES), \
            f"{name}: logger 8 MOSI {mosi} MISO {miso}, expected {SPI_MOSI_BYTES} {SPI_MISO_BYTES}"
    assert not failures, "coverage differs from the source reading:\n  " + "\n  ".join(failures)


if __name__ == "__main__":
    run_core_seam("test_selftest_coverage", top_level=TOP)
