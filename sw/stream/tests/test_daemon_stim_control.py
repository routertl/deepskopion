"""DS-P2.136 — the DAEMON drives the on-board stimulus generator.

DS-T2.78 mirrored the 10 ``STIM_*`` registers into ``Regs`` but nothing in the
daemon ever wrote them: every silicon witness so far armed ``ds_stimulus_gen``
from a side script on the board (``de_c0_production_armer`` over the UART
console), so "the daemon can drive the stimulus" was an unexercised claim.

These tests pin the path a consumer actually reaches: a ``TAG_CONTROL`` frame
with ``op="stim"`` arrives on the daemon's socket, ``_poll_inbound`` hands it to
the source, and the source's REGBANK receives the exact DS-P1.58 production
register sequence — then a CONTROL_ACK reports it. A daemon that silently
ignores the op (the pre-fix behaviour: ``decode_control`` rejects the payload as
malformed and nothing is written) fails here.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import msgpack
import pytest

from sw.stream import protocol as proto
from sw.stream.data_source import RegbankDataSource, StubDataSource
from sw.stream.regbank import FakeRegbank, Regs
from sw.stream.server import _poll_inbound
from sw.stream.testbench import de_c0_stimulus_contract as K

_STIM_OFFSETS = {
    Regs.STIM_UART_CONTROL, Regs.STIM_UART_BAUD_DIV, Regs.STIM_UART_DATA,
    Regs.STIM_I2C_CONTROL, Regs.STIM_I2C_CLK_DIV, Regs.STIM_I2C_DATA,
    Regs.STIM_SPI_CONTROL, Regs.STIM_SPI_CLK_DIV, Regs.STIM_SPI_DATA,
    Regs.STIM_EDGE_CONTROL,
}


@pytest.fixture(autouse=True)
def _legacy_mux(monkeypatch):
    # Zybo/DE25 routing: ds_stimulus_gen, not the PolarFire MSS peripherals.
    monkeypatch.setenv("DS_MSS_FABRIC", "0")


def _send_control_through_socket(source, payload):
    """Push one CONTROL frame through the daemon's real inbound poll and
    return the decoded CONTROL_ACK the daemon wrote back."""
    daemon_end, consumer_end = socket.socketpair()
    try:
        consumer_end.sendall(proto.pack_frame(proto.TAG_CONTROL, payload))
        unp = msgpack.Unpacker(raw=False)
        assert _poll_inbound(daemon_end, source, unp) is False
        consumer_end.settimeout(2.0)
        tag, ack = msgpack.unpackb(consumer_end.recv(65536), raw=False)
    finally:
        daemon_end.close()
        consumer_end.close()
    assert tag == proto.TAG_CONTROL_ACK
    return ack


def _stim_writes(rb):
    return [(o, v) for o, v in rb.writes if o in _STIM_OFFSETS]


def test_stim_control_makes_daemon_write_spi_stim_registers():
    rb = FakeRegbank()
    source = RegbankDataSource(rb)
    # The wire payload a consumer sends, spelled out (not via the builder) so
    # this test pins the wire shape independently of make_stim_control.
    ack = _send_control_through_socket(source, {"op": "stim", "protocol": "spi"})

    assert _stim_writes(rb) == (
        [(Regs.STIM_SPI_CLK_DIV, K.SPI_CLK_DIV), (Regs.STIM_SPI_CONTROL, 0x1)]
        + [(Regs.STIM_SPI_DATA, b) for b in K.SPI_PAYLOAD]
    ), f"daemon did not drive the STIM_SPI_* registers (ack={ack})"
    assert ack["ok"] is True, ack
    assert ack["op"] == "stim" and ack["protocol"] == "spi"
    # The generator output is only observed if the EMIO mux points at it.
    assert (Regs.EMIO_MUX_SEL, K.EMIO_MUX_SPI) in rb.writes


@pytest.mark.parametrize("protocol, first_stim_write", [
    ("uart", (Regs.STIM_UART_BAUD_DIV, K.UART_BAUD_DIV)),
    ("i2c", (Regs.STIM_I2C_CLK_DIV, 125)),
    # edge: the first stimulus write is the restart 0 (DS-P1.108); that EDGE_FINITE_CONTROL follows is pinned by
    # test_arm_edge_order_ds_p2_313.py and test_arm_edge_rearm_ds_p1_108.py, so this row only proves the daemon reaches arm_edge.
    ("edge", (Regs.STIM_EDGE_CONTROL, 0)),
])
def test_stim_control_covers_every_generator_protocol(protocol, first_stim_write):
    rb = FakeRegbank()
    ack = _send_control_through_socket(RegbankDataSource(rb),
                                       proto.make_stim_control(protocol))
    assert ack["ok"] is True, ack
    assert _stim_writes(rb)[0] == first_stim_write


def test_make_stim_control_builds_the_wire_payload():
    # datalogger_id/cfg_word are wave_stream_v1 CONTROL's required keys; the id
    # is the data logger that observes the armed generator.
    assert proto.make_stim_control("edge") == {
        "op": "stim", "protocol": "edge",
        "datalogger_id": K.DATALOGGER_ID_EDGE1, "cfg_word": 0,
    }


def test_make_stim_control_refuses_a_protocol_without_a_generator():
    with pytest.raises(ValueError):
        proto.make_stim_control("jtag")


def test_stim_ack_names_the_observing_datalogger():
    ack = _send_control_through_socket(RegbankDataSource(FakeRegbank()),
                                       proto.make_stim_control("spi"))
    assert ack["ok"] is True, ack
    assert ack["datalogger_id"] == K.DATALOGGER_ID_SPI, ack


def test_stim_control_rejects_unknown_protocol_without_writing():
    rb = FakeRegbank()
    ack = _send_control_through_socket(RegbankDataSource(rb),
                                       {"op": "stim", "protocol": "jtag"})
    assert ack["ok"] is False and "jtag" in ack["reason"]
    assert rb.writes == []


def test_stim_control_on_stub_source_is_a_reported_reject():
    ack = _send_control_through_socket(StubDataSource(),
                                       proto.make_stim_control("uart"))
    assert ack["ok"] is False and ack["reason"]



def test_live_capture_client_stim_drives_the_real_daemon_over_tcp():
    """The witness path end to end: live_capture_client --stim against a real
    serve_fanout daemon on TCP. The daemon (not the client) writes the STIM
    registers, and its CONTROL_ACK lands in the captured live stream."""
    import threading
    import time

    from sw.stream.server import serve_fanout
    from sw.stream.testbench.live_capture_client import capture

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    rb = FakeRegbank()
    source = RegbankDataSource(rb, poll_period_s=0.02, duration_s=3.0)
    threading.Thread(target=serve_fanout, args=(("127.0.0.1", port), source),
                     kwargs=dict(connect_timeout_s=0.3, max_connections=1,
                                 stats_period_s=0),
                     daemon=True).start()
    for _ in range(50):
        try:
            _helo, captured = capture("127.0.0.1", port, 1.0, stim="spi")
            break
        except ConnectionRefusedError:
            time.sleep(0.05)
    else:
        pytest.fail("daemon never started listening")

    acks = [p for tag, p in captured if tag == proto.TAG_CONTROL_ACK]
    assert acks and acks[0]["ok"] is True and acks[0]["protocol"] == "spi", captured
    assert [v for o, v in rb.writes if o == Regs.STIM_SPI_DATA] == K.SPI_PAYLOAD


# ---------------------------------------------------------------------------
# The DE25 production image (review finding, DS-P2.136 revise).
#
# tools/de25_linux_image.py stages sw/stream WITHOUT sw/stream/testbench. Every
# test above runs from the repo, where testbench is importable, so none of them
# can see an armer that lives there. This one stages sw/stream with the
# builder's own exclude list and runs the daemon's CONTROL path from that copy
# only, in a fresh interpreter whose sys.path holds nothing else of the repo.
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[3]

_STAGED_STIM = r"""
import json, socket, sys
import msgpack
from sw.stream import protocol as proto
from sw.stream.data_source import RegbankDataSource
from sw.stream.regbank import FakeRegbank, Regs
from sw.stream.server import _poll_inbound

assert "/testbench" not in str(sys.modules["sw.stream"].__path__)
rb = FakeRegbank()
a, b = socket.socketpair()
b.sendall(proto.pack_frame(proto.TAG_CONTROL, {"op": "stim", "protocol": sys.argv[1]}))
_poll_inbound(a, RegbankDataSource(rb), msgpack.Unpacker(raw=False))
b.settimeout(2.0)
tag, ack = msgpack.unpackb(b.recv(65536), raw=False)
print(json.dumps({"ack": ack, "writes": rb.writes,
                  "testbench": [m for m in sys.modules if ".testbench" in m]}))
"""


def _load_de25_image_tool():
    spec = importlib.util.spec_from_file_location(
        "de25_linux_image", _ROOT / "tools" / "de25_linux_image.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("protocol", ["spi", "uart", "i2c", "edge"])
def test_de25_staged_daemon_arms_stimulus_without_testbench(tmp_path, protocol):
    tool = _load_de25_image_tool()
    share = tmp_path / "usr/local/share/deepskopion"
    tool._copy_tree(_ROOT / "sw/stream", share / "sw/stream",
                    exclude=tool.STREAM_EXCLUDE)
    tool._copy_file(_ROOT / "sw/__init__.py", share / "sw/__init__.py")
    tool._copy_tree(_ROOT / "contracts/wave_stream_v1",
                    share / "contracts/wave_stream_v1",
                    exclude=tool.CONTRACT_EXCLUDE)
    assert not (share / "sw/stream/testbench").exists()

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(PYTHONPATH=str(share), DS_MSS_FABRIC="0")
    run = subprocess.run([sys.executable, "-c", _STAGED_STIM, protocol],
                         cwd=share, env=env, capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    ack = out["ack"]
    assert ack["ok"] is True, (
        f"the DE25-staged daemon cannot arm {protocol}: {ack.get('reason')}")
    stim = [(o, v) for o, v in out["writes"] if o in _STIM_OFFSETS]
    assert stim, f"the DE25-staged daemon wrote no STIM_* register for {protocol}"
    assert out["testbench"] == [], out["testbench"]
