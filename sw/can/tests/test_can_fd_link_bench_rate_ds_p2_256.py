"""DS-P2.256: the CAN FD link helper defaults to, and documents, the nominal rate the
datalogger can capture.

The datalogger's nominal bit is 16 TQ x prescaler at 100 MHz, so 250 kbit/s
(prescaler 25) is representable and 500 kbit/s (prescaler 12.5) is not
(docs/hardware/BRING_UP_CAN.md:64). A bench session that set the PS controllers
to 500 kbit/s left the datalogger, armed at 250 kbit/s, decoding nothing.
"""
from __future__ import annotations

from sw.can import can_fd_link as L


def test_setup_defaults_to_250k_nominal_2m_data(monkeypatch):
    seen = {}

    def fake_setup(ifname, bitrate, dbitrate):
        seen.update(ifname=ifname, bitrate=bitrate, dbitrate=dbitrate)
        return {}

    monkeypatch.setattr(L, "setup", fake_setup)
    assert L.main(["setup", "can0"]) == 0
    assert seen == {"ifname": "can0", "bitrate": 250_000, "dbitrate": 2_000_000}


def test_usage_example_uses_250k_nominal():
    doc = L.__doc__
    assert "--bitrate 250000 --dbitrate 2000000" in doc
    assert "--bitrate 500000" not in doc
