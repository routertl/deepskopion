"""Executable guard for the DS-P2.1 EtherCAT wire-format research.

These tests intentionally stop at the research artifact.  There is no EtherCAT
RTL or capture path yet, so claiming DUT coverage here would be false.  Instead,
the caller-visible surface is the machine-readable framing contract that later
parser/RTL tickets will consume.  The independent byte fixtures below pin the
endianness and boundaries most likely to be implemented incorrectly.

Deploy note: this is a pure repository/YAML test and runs in a fresh clone,
cron, or sandbox with pytest and PyYAML; it needs no simulator or hardware.
"""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "docs/design/ethercat/ethercat_wire_contract.yml"


def _contract() -> dict:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def _ethercat_region(frame: bytes) -> bytes:
    """Return exactly the declared EtherCAT datagram region, excluding pad."""
    assert frame[12:14] == bytes.fromhex("88 a4")
    frame_header = int.from_bytes(frame[14:16], "little")
    length = frame_header & 0x07FF
    assert frame_header >> 12 == 0x1
    return frame[16 : 16 + length]


def _split_datagrams(region: bytes) -> list[tuple[bytes, bytes, bytes]]:
    datagrams = []
    cursor = 0
    while cursor < len(region):
        header = region[cursor : cursor + 10]
        assert len(header) == 10
        length_flags = int.from_bytes(header[6:8], "little")
        payload_length = length_flags & 0x07FF
        end = cursor + 10 + payload_length + 2
        assert end <= len(region)
        datagrams.append((header, region[cursor + 10 : end - 2], region[end - 2 : end]))
        cursor = end
    assert cursor == len(region)
    return datagrams


def test_physical_layer_and_ethernet_discriminator_are_pinned() -> None:
    contract = _contract()

    assert contract["status"] == "research-only; no RTL or capture path"
    assert contract["physical_layer"] == {
        "copper_phy": "100BASE-TX",
        "line_rate_bit_s": 100_000_000,
        "duplex": "full",
        "mac_interface": ["MII", "RMII"],
    }
    assert contract["ethernet"]["ethertype"] == 0x88A4
    assert contract["ethernet"]["ethertype_wire_bytes"] == [0x88, 0xA4]
    assert contract["ethernet"]["ethercat_header_offset_untagged"] == 14


def test_single_datagram_fixture_pins_header_payload_and_wkc_offsets() -> None:
    contract = _contract()
    # DA, SA, EtherType, ECAT header (length=14,type=1), then one LRD:
    # cmd,idx,logical-address,len/flags,irq,2-byte data,2-byte WKC.
    frame = bytes.fromhex(
        "ff ff ff ff ff ff 02 00 00 00 00 01 88 a4 0e 10 "
        "0a 5a 78 56 34 12 02 00 ef be ca fe 03 00"
    )
    region = _ethercat_region(frame)
    datagrams = _split_datagrams(region)

    assert contract["ethercat_frame_header"]["bytes"] == 2
    assert contract["datagram"]["header_bytes"] == 10
    assert contract["datagram"]["payload_bytes"] == "0..1486"
    assert contract["datagram"]["working_counter_bytes"] == 2
    assert contract["datagram"]["working_counter_scope"] == "each datagram"
    assert len(datagrams) == 1
    header, payload, wkc = datagrams[0]
    assert header == bytes.fromhex("0a 5a 78 56 34 12 02 00 ef be")
    assert payload == bytes.fromhex("ca fe")
    assert int.from_bytes(wkc, "little") == 3


def test_ethercat_length_excludes_header_and_stops_before_ethernet_padding() -> None:
    contract = _contract()
    # The same 14-byte datagram region followed by 30 bytes of Ethernet pad.
    # A parser that consumes the captured Ethernet payload instead of the ECAT
    # 11-bit length will invent one or more datagrams from the zero padding.
    frame = bytes.fromhex(
        "ff ff ff ff ff ff 02 00 00 00 00 01 88 a4 0e 10 "
        "0a 5a 78 56 34 12 02 00 ef be ca fe 03 00" + " 00" * 30
    )
    region = _ethercat_region(frame)

    length_field = contract["ethercat_frame_header"]["length"]
    assert length_field["bits"] == [10, 0]
    assert length_field["counts"] == "datagram region only"
    assert length_field["excludes"] == [
        "2-byte EtherCAT frame header",
        "Ethernet padding",
        "Ethernet FCS",
    ]
    assert len(region) == 14
    assert len(_split_datagrams(region)) == 1


def test_multiple_flag_chains_datagrams_with_an_independent_wkc_each() -> None:
    contract = _contract()
    # First datagram: NOP, zero data, M=1, WKC=0. Second: LWR, one data
    # byte, M=0, WKC=2. ECAT region length is 12 + 13 = 25 (0x19).
    frame = bytes.fromhex(
        "ff ff ff ff ff ff 02 00 00 00 00 01 88 a4 19 10 "
        "00 01 00 00 00 00 00 80 00 00 00 00 "
        "0b 02 04 03 02 01 01 00 00 00 aa 02 00"
    )
    datagrams = _split_datagrams(_ethercat_region(frame))

    assert contract["datagram"]["length_flags"]["more_bit"] == 15
    assert len(datagrams) == 2
    assert int.from_bytes(datagrams[0][0][6:8], "little") >> 15 == 1
    assert datagrams[0][1] == b""
    assert int.from_bytes(datagrams[0][2], "little") == 0
    assert int.from_bytes(datagrams[1][0][6:8], "little") >> 15 == 0
    assert datagrams[1][1] == b"\xaa"
    assert int.from_bytes(datagrams[1][2], "little") == 2
