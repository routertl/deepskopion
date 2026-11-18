"""DS-P2.253: arming the stimulus generator on a Zynq-7000 image is reported as
taking the PS EMIO legs, and a PS I2C transfer that cannot reach the bus is
refused instead of failing with EAGAIN.

Zybo session 3 (2026-11-10): after `live_capture_client --stim i2c`, 200
`i2cget` reads failed with EAGAIN and none reached the I2C logger. Session 4,
same image, STIM_I2C_CONTROL cleared first: 200/200. The Zynq-7000 top sets
G_STIM_DRIVES_EMIO, so the armed generator drives the EMIO legs in place of the
PS controller. The platform is inferred from the DT root compatible.
"""
from __future__ import annotations

import socket
import sys
import types

import msgpack
import pytest

from sw.stream import protocol as proto
from sw.stream import stim_emio
from sw.stream.data_source import RegbankDataSource
from sw.stream.regbank import FakeRegbank, Regs
from sw.stream.server import _poll_inbound
from sw.stream.testbench import emio_inject

ZYBO_DT = b"digilent,zybo-z7\0xlnx,zynq-7000\0"
DE25_DT = b"terasic,de25\0intel,socfpga-agilex5\0"
STIM_I2C_CONTROL = 0x022C   # hard-coded: a moved slot fails here


@pytest.fixture
def dt(tmp_path, monkeypatch):
    """Point the module's DT root at a fixture file with the given content."""
    def use(content: bytes):
        path = tmp_path / "compatible"
        path.write_bytes(content)
        monkeypatch.setattr(stim_emio, "DT_COMPATIBLE", path)
    return use


@pytest.fixture(autouse=True)
def _legacy_mux(monkeypatch):
    monkeypatch.setenv("DS_MSS_FABRIC", "0")


def test_the_inference_follows_the_dt_root(dt, tmp_path, monkeypatch):
    dt(ZYBO_DT)
    assert stim_emio.is_zynq7000()
    dt(DE25_DT)
    assert not stim_emio.is_zynq7000()
    monkeypatch.setattr(stim_emio, "DT_COMPATIBLE", tmp_path / "absent")
    assert not stim_emio.is_zynq7000()


@pytest.mark.parametrize("protocol", ["uart", "i2c", "spi"])
def test_the_text_names_the_inference_the_generic_and_the_release(dt, protocol):
    dt(ZYBO_DT)
    why = stim_emio.stim_takes_ps_emio(protocol)
    assert why is not None
    for part in ("inferred from the DT compatible xlnx,zynq-7000", "G_STIM_DRIVES_EMIO",
                 "8ebcce01", f"STIM_{protocol.upper()}_CONTROL bit 0",
                 "release_stimulus_generator()"):
        assert part in why, (part, why)


def test_edge_and_other_platforms_report_nothing(dt):
    dt(ZYBO_DT)
    assert stim_emio.stim_takes_ps_emio("edge") is None
    dt(DE25_DT)
    assert stim_emio.stim_takes_ps_emio("i2c") is None


def _stim_ack(protocol: str) -> dict:
    source = RegbankDataSource(FakeRegbank())
    daemon_end, consumer_end = socket.socketpair()
    try:
        consumer_end.sendall(proto.pack_frame(proto.TAG_CONTROL,
                                              proto.make_stim_control(protocol)))
        assert _poll_inbound(daemon_end, source, msgpack.Unpacker(raw=False)) is False
        consumer_end.settimeout(2.0)
        tag, ack = msgpack.unpackb(consumer_end.recv(65536), raw=False)
    finally:
        daemon_end.close()
        consumer_end.close()
    assert tag == proto.TAG_CONTROL_ACK and ack["ok"] is True, ack
    return ack


def test_the_stim_ack_reports_the_taken_legs_on_a_zynq_image(dt):
    dt(ZYBO_DT)
    ack = _stim_ack("i2c")
    assert ack["ps_emio_taken"] is True
    assert "G_STIM_DRIVES_EMIO" in ack["warning"]


def test_the_stim_ack_reports_no_taken_legs_elsewhere(dt):
    dt(DE25_DT)
    ack = _stim_ack("i2c")
    assert ack["ps_emio_taken"] is False
    assert "warning" not in ack
    dt(ZYBO_DT)
    assert "ps_emio_taken" not in _stim_ack("edge")


class _Bus:
    def __init__(self, *_a):
        self.rdwr = []

    def i2c_rdwr(self, *msgs):
        self.rdwr.append(msgs)

    def close(self):
        pass


class _Msg:
    @staticmethod
    def write(addr, data):
        return ("w", addr, bytes(data))

    @staticmethod
    def read(addr, n):
        return bytearray(n)


class _Regs:
    def __init__(self, stim_i2c_control: int):
        self.value = stim_i2c_control

    def read_u32(self, offset):
        assert offset == STIM_I2C_CONTROL
        return self.value


@pytest.fixture
def fake_smbus2(monkeypatch):
    mod = types.SimpleNamespace(SMBus=_Bus, i2c_msg=_Msg)
    monkeypatch.setitem(sys.modules, "smbus2", mod)
    return mod


def test_the_offset_is_the_regbank_contract():
    assert Regs.STIM_I2C_CONTROL == STIM_I2C_CONTROL


def test_a_ps_transfer_is_refused_while_the_generator_holds_the_legs(dt, fake_smbus2):
    dt(ZYBO_DT)
    m = emio_inject.I2CMaster(0, regbank=_Regs(0xA1))
    with pytest.raises(RuntimeError, match="STIM_I2C_CONTROL bit 0 is set"):
        m.write_bytes(0x50, b"\x00")
    assert m.smbus.rdwr == []


def test_a_ps_transfer_runs_once_the_generator_is_released(dt, fake_smbus2):
    dt(ZYBO_DT)
    m = emio_inject.I2CMaster(0, regbank=_Regs(0x0))
    m.write_bytes(0x50, b"\x00")
    assert len(m.smbus.rdwr) == 1


def test_no_refusal_off_zynq_or_without_a_regbank(dt, fake_smbus2):
    dt(DE25_DT)
    m = emio_inject.I2CMaster(0, regbank=_Regs(0xA1))
    m.write_bytes(0x50, b"\x00")
    dt(ZYBO_DT)
    n = emio_inject.I2CMaster(0)
    n.write_bytes(0x50, b"\x00")
    assert len(m.smbus.rdwr) == 1 and len(n.smbus.rdwr) == 1
