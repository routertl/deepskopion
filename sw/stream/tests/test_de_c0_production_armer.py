"""DS-P1.59 — guard the SHARED production armer (de_c0_production_armer).

The DS-P2.101 live witness failed to retire `i2c_write` because the on-board
driver never armed the I2C responder (`I2C_INJ_CONTROL`), so every ACK slot was
a NACK, and the generator emitted one transaction per DATA write instead of the
one ACK-bound transaction the oracle binds. DS-P1.58 fixed the RTL + register
contract; this guard pins that the shared armer every witness now uses actually
EMITS that contract — responder-first, ACK-bound, one transaction — and that the
stop-daemon driver and the daemon-live witness cannot drift because they share
this one armer.

Each assertion has a mutation twin: remove the mechanism it guards and the
assertion fails (a guard that still passes with the responder-arm deleted would
be vacuous — the exact failure mode DS-P2.101 shipped).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sw.stream.testbench import de_c0_production_armer as A
from sw.stream.testbench import de_c0_stimulus_contract as K


class RecordingRegbank:
    """Captures the exact (offset, value) write trace — no /dev/mem needed."""

    def __init__(self):
        self.writes = []

    def write_u32(self, offset, value):
        self.writes.append((offset, value))


def _trace(protocol):
    rb = RecordingRegbank()
    A.arm(rb, protocol)
    return rb.writes


def test_all_protocols_arm_without_reading_the_ring():
    """The armer is writes-only (so the daemon keeps running): every protocol
    produces a non-empty write trace and the module never opens a DDR ring."""
    for p in A.PROTOCOLS:
        assert _trace(p), f"{p}: armer emitted no writes"
    from sw.stream import stimulus_armer
    src = Path(A.__file__).read_text() + Path(stimulus_armer.__file__).read_text()
    assert "RealDdrMmap" not in src and "DdrRingDataSource" not in src, (
        "the shared armer must NOT read the DDR ring — that would race the "
        "running daemon's read pointer (Haz.59)")


def test_i2c_arms_the_responder_before_the_master():
    """The load-bearing DS-P1.58 fix: I2C_INJ_CONTROL (responder) is written
    BEFORE STIM_I2C_DATA (master), or every ACK slot is a NACK (DS-P2.101)."""
    offs = [o for o, _ in _trace("i2c")]
    assert K.I2C_INJ_CONTROL in offs, (
        "arm_i2c must write I2C_INJ_CONTROL to arm the on-chip responder — "
        "without it the transaction is NACK-bound (the DS-P2.101 gap)")
    assert K.STIM_I2C_DATA in offs
    assert offs.index(K.I2C_INJ_CONTROL) < offs.index(K.STIM_I2C_DATA), (
        "the responder must be armed BEFORE the master DATA write")


def test_i2c_emits_exactly_the_ack_bound_register_contract():
    """arm_i2c emits the full DS-P1.58 I2C_REGISTER_SEQUENCE, in order — one
    ACK-bound transaction addr=0x50 data=[0xAB,0xCD,0x3E]."""
    writes = _trace("i2c")
    # the last four writes are the register sequence, in order
    tail = writes[-len(K.I2C_REGISTER_SEQUENCE):]
    assert tail == list(K.I2C_REGISTER_SEQUENCE), (
        f"arm_i2c register sequence drift: {tail!r} != "
        f"{list(K.I2C_REGISTER_SEQUENCE)!r}")
    # the responder value carries the 7-bit address + write bit (bound, not free)
    assert K.I2C_REGISTER_SEQUENCE[0] == (K.I2C_INJ_CONTROL,
                                          (K.I2C_ADDRESS_7B << 1) | 0x1)


def test_edge_drives_the_finite_canonical_vector():
    """arm_edge writes the finite EDGE control (EDGE_FINITE_CONTROL), which the
    DS-P1.58 RTL binds to the transition-exact vector — not a free-running
    toggle whose count is all a coarse witness could check (DS-P2.101 edge gap)."""
    writes = _trace("edge")
    assert (K.STIM_EDGE_CONTROL, K.EDGE_FINITE_CONTROL) in writes, (
        "arm_edge must drive the finite canonical EDGE vector")


def test_uart_and_spi_drive_the_xilinx_oracle_payloads():
    """UART/SPI stimulus bytes are the DS-X3/DE-R0 Xilinx-oracle runs, so the
    live capture is comparable to the Xilinx baseline for the same stimuli."""
    uart_bytes = [v for o, v in _trace("uart") if o == K.STIM_UART_DATA]
    assert uart_bytes == K.UART_PAYLOAD
    spi_bytes = [v for o, v in _trace("spi") if o == K.STIM_SPI_DATA]
    assert spi_bytes == K.SPI_PAYLOAD


def test_datalogger_enable_is_a_reset_then_enable_pulse():
    """The disable→enable pair is load-bearing (a config write alone leaves the
    calibration FSM's configEnable high). Every protocol pulses it (UART too,
    since DS-P2.243)."""
    for p in ("i2c", "spi", "edge", "uart"):
        cfg = [v for o, v in _trace(p) if o == K.DATALOGGER_CONFIG]
        assert len(cfg) == 2, f"{p}: expected reset+enable pair, got {cfg!r}"
        assert cfg[0] & 0x1 == 0, f"{p}: first DATALOGGER_CONFIG must be enable=0"
        assert cfg[1] & 0x1 == 1, f"{p}: second DATALOGGER_CONFIG must be enable=1"


def test_uart_enables_its_datalogger_at_8n1_before_the_stimulus():
    """DS-P2.243 (DE-G0 run 12): arm_uart relied on the boot-time arming,
    which had failed on the card, so UART produced 0 packets while I2C/SPI/edge
    passed. It must pulse datalogger 12 itself, at the 8N1 word the boot
    arming writes, before the first stimulus byte. DS-P2.315: and after the
    mux, so the logger calibrates its baud on the stimulus line, not on the
    header-to-stimulus step (test_arm_order_uart_i2c_spi_ds_p2_315)."""
    from sw.stream import arm_datalogger_defaults as D
    trace = _trace("uart")
    cfg = [(i, v) for i, (o, v) in enumerate(trace) if o == K.DATALOGGER_CONFIG]
    assert [v for _, v in cfg] == [
        D.encode_datalogger_config(12, False, 0),
        D.encode_datalogger_config(12, True, D.UART_EXTRA_8N1)], cfg
    assert [v for _, v in cfg][1] == 0x600D
    mux = [i for i, (o, _) in enumerate(trace) if o == K.EMIO_MUX_SEL]
    first_byte = min(i for i, (o, _) in enumerate(trace) if o == K.STIM_UART_DATA)
    assert len(mux) == 1 and mux[0] < cfg[0][0], f"EMIO_MUX_SEL must precede the reset+enable pair: {trace}"
    assert cfg[-1][0] < first_byte


def test_unknown_protocol_is_refused():
    with pytest.raises(SystemExit):
        A.arm(RecordingRegbank(), "modbus")


def test_stop_daemon_driver_shares_this_armer_no_drift():
    """The stop-daemon driver must call the shared armer, not carry its own
    inline arm code — otherwise the two can drift and only one gets fixed."""
    drv = Path(A.__file__).with_name("de_c0_live_stimulus_driver.py").read_text()
    assert "de_c0_production_armer" in drv and "arm_protocol(rb, proto)" in drv, (
        "de_c0_live_stimulus_driver must arm via the shared armer")
    # and must NOT re-implement the I2C responder sequence inline
    assert "I2C_REGISTER_SEQUENCE" not in drv, (
        "driver re-implements the I2C arm inline — drift risk")


# ── DS-P2.126: main() must map the SAME CSR base the boot arming maps ──────
# The kit's FIC0 CSR sits at ds_mmio_base=0x60000000; a main() that still maps
# the DE25 0x20000000 writes every arm sequence into the wrong window and the
# loggers never see stimulus. Resolution order: DS_MMIO_BASE env, then the
# kernel cmdline's ds_mmio_base=, then the historical DE25 default.

PFSOC_CMDLINE = ("console=ttyS0,115200 root=/dev/mmcblk0p3 ds_source=ddr "
                 "ds_mmio_base=0x60000000 ds_clk_hz=100000000 ds_mss_fabric=1")


def test_mmio_base_env_wins(monkeypatch):
    monkeypatch.setenv("DS_MMIO_BASE", "0x60000000")
    assert A.resolve_mmio_base(cmdline="ds_mmio_base=0x40000000") == 0x60000000


def test_mmio_base_from_kernel_cmdline_when_env_unset(monkeypatch):
    monkeypatch.delenv("DS_MMIO_BASE", raising=False)
    assert A.resolve_mmio_base(cmdline=PFSOC_CMDLINE) == 0x60000000


def test_mmio_base_falls_back_to_de25_default(monkeypatch):
    monkeypatch.delenv("DS_MMIO_BASE", raising=False)
    assert A.resolve_mmio_base(cmdline="console=ttyS1,115200") == 0x20000000


def test_main_maps_the_resolved_base(monkeypatch):
    """Effect, not detector: main() must hand the resolved base to MmioRegbank."""
    import sys
    import types

    seen = {}

    class FakeRegbank(RecordingRegbank):
        def __init__(self, base_addr, size):
            super().__init__()
            seen["base"] = base_addr

        def close(self):
            pass

    fake_mod = types.ModuleType("sw.stream.regbank")
    fake_mod.MmioRegbank = FakeRegbank
    monkeypatch.setitem(sys.modules, "sw.stream.regbank", fake_mod)
    monkeypatch.setattr(A, "_read_cmdline", lambda: PFSOC_CMDLINE)
    monkeypatch.delenv("DS_MMIO_BASE", raising=False)
    assert A.main(["edge"]) == 0
    assert seen["base"] == 0x60000000
