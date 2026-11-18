# EtherCAT link and framing research

Status: **research only**. Deepskopion has no EtherCAT RTL, Ethernet frame-ingest
path, PHY attachment, decoder, or live-capture claim yet. This note fixes the
wire contract those later designs must start from; the executable companion is
[`ethercat_wire_contract.yml`](ethercat_wire_contract.yml).

## Result

For the copper link in scope, EtherCAT uses standard **100BASE-TX**, 100 Mbit/s,
full-duplex Ethernet PHYs. An EtherCAT SubDevice Controller connects to external
Ethernet PHYs through MII or RMII; EtherCAT does not define a replacement copper
PHY. Other media exist in the wider EtherCAT ecosystem, but they are outside
this first Deepskopion ingest target.

EtherCAT commands are carried directly in a standard Ethernet frame identified
by EtherType **`0x88A4`**. The two EtherType octets appear on the captured MAC
byte stream as **`88 a4`**, not `a4 88`. This Ethernet field uses its normal
network byte order; multibyte EtherCAT fields that follow are little-endian.

An untagged frame at the MAC-byte boundary is:

```text
0              6             12     14     16
+--------------+--------------+------+------+
| destination  | source MAC   |88 a4 | ECAT | datagram 1 ... datagram n | pad | FCS |
| MAC (6 B)    | (6 B)        | (2 B)| (2 B)|                            |     | 4 B |
+--------------+--------------+------+------+
```

Preamble and SFD belong below this boundary. Whether a particular MAC exposes
the Ethernet FCS is an integration choice; the EtherCAT parser must not treat
it as EtherCAT content.

## EtherCAT frame header

The 2-byte EtherCAT frame header is little-endian:

| Bits | Field | Required interpretation |
|---:|---|---|
| 10:0 | Length | Bytes in the following datagram region; excludes this header, Ethernet padding, and FCS |
| 11 | Reserved | Zero |
| 15:12 | Type | `0x1` for EtherCAT commands |

The enclosing Ethernet payload can contain padding to satisfy the Ethernet
minimum frame size. A decoder therefore stops at the 11-bit EtherCAT length,
not at the end of the captured Ethernet payload. The maximum datagram-region
length in a normal 1500-byte Ethernet payload is 1498 bytes.

## Datagram framing

An EtherCAT frame contains one or more consecutive datagrams. Every datagram is
independently delimited as:

```text
+----------------------+----------------------+----------------------+
| datagram header      | data                 | Working Counter      |
| 10 bytes             | 0..N bytes           | 2 bytes              |
+----------------------+----------------------+----------------------+
```

The 10-byte header layout is:

| Offset | Bytes | Field | Meaning |
|---:|---:|---|---|
| 0 | 1 | Cmd | Access command (`APRD`, `FPWR`, `LRW`, and so on) |
| 1 | 1 | Idx | MainDevice identifier for duplicate/loss correlation |
| 2 | 4 | Address | Auto-increment + offset, configured address + offset, or 32-bit logical address, depending on Cmd |
| 6 | 2 | Length/flags | data length bits 10:0; reserved 13:11; circulating bit 14; more-datagrams bit 15 |
| 8 | 2 | IRQ | EtherCAT event requests combined by logical OR |
| 10 | N | Data | Variable payload selected by the 11-bit datagram length |
| 10+N | 2 | WKC | This datagram's little-endian Working Counter |

Consequently a datagram consumes exactly `10 + N + 2` bytes. A single-datagram
frame can carry at most 1486 data bytes: `1498 - 10 - 2`. With multiple
datagrams, every additional datagram spends its own 10-byte header and 2-byte
WKC inside the same frame-header length budget. Bit 15 (`M`) is one for a
datagram followed by another and zero for the final datagram.

## Working Counter semantics

The Working Counter is **at the tail of each datagram**, not once at the tail of
the Ethernet frame. A successfully addressed operation increments that
datagram's WKC: a successful read contributes 1, a successful write contributes
1, and a combined read/write can contribute 3 (read 1 plus write 2). The
MainDevice compares the returned value with the expected value to decide whether
all intended accesses completed; an unexpected WKC makes the returned data
untrustworthy.

For a passive datalogger this means WKC is a captured diagnostic field, not a CRC:
do not combine it across datagrams and do not confuse it with the trailing
Ethernet FCS.

## Parser obligations and prohibitions

- Filter on Ethernet EtherType bytes `88 a4`; `a4 88` is not EtherCAT.
- Decode the 2-byte frame header and all EtherCAT multibyte fields little-endian.
- Bound the datagram walk by the frame header's 11-bit length.
- For each datagram, use its own 11-bit data length to locate that datagram's
  WKC, then use `M` to validate whether another datagram follows.
- Reject truncation and any nested length that crosses the enclosing EtherCAT
  region. Never reinterpret Ethernet padding or FCS as a datagram.

These are future parser/RTL requirements, not test coverage claims. This document's
test only guards this published research contract with independent byte
fixtures; a later implementation ticket must first make equivalent behavioral
tests fail against a port-shape stub.

## Authoritative sources

- EtherCAT Technology Group, [EtherCAT implementation and protocol overview](https://www.ethercat.org/en/technology.html):
  standard Ethernet frame, `0x88A4`, 100BASE-TX, and one-or-more-datagram model.
- Beckhoff InfoSys, [EtherCAT telegram, datagram and Working Counter](https://infosys.beckhoff.com/content/1033/tc3_io_intro/1257993099.html):
  frame-header fields, datagram fields, command table, physical interfaces, and
  WKC increments.
- Beckhoff InfoSys, [EtherCAT frame-size example](https://infosys.beckhoff.com/content/1033/tc3_io_intro/1446515467.html):
  2-byte frame header, 10-byte header and 2-byte WKC per datagram, and 100 Mbit/s
  transmission accounting.
- IEEE Registration Authority, [Use of the IEEE Assigned EtherType](https://standards.ieee.org/wp-content/uploads/import/documents/tutorials/ethertype.pdf):
  the two-octet Length/Type field and most-significant-octet-first evaluation.
