# Reading the stream

RouteWave is one client of the versioned `wave_stream_v1` contract. Any program that reads MessagePack can attach to
port 7475: each frame is a `[tag, payload]` pair, the daemon opens with `HELO` (tag 0) and its channel map, and the
client answers `HELO_OK` (tag 1) within 10 s, or the daemon closes the connection. Decoded records arrive in
`ANNOTATIONS` frames (tag 6). This client prints the decoded annotations (`pip install msgpack`):

```python
import socket
import msgpack

HELO, HELO_OK, ANNOTATIONS = 0, 1, 6

sock = socket.create_connection(("board-ip", 7475))
unpacker = msgpack.Unpacker(raw=False)
while data := sock.recv(65536):
    unpacker.feed(data)
    for tag, payload in unpacker:
        if tag == HELO:
            print("producer:", payload["producer"])
            sock.sendall(msgpack.packb([HELO_OK, {"consumer": "example"}]))
        elif tag == ANNOTATIONS:
            for a in payload["annotations"]:
                print(a["ts_start"], a["channel_id"], a["kind"], a["text"])
```

Its output for the I2C write in [Captures](https://github.com/routertl/deepskopion/blob/main/README.md#live-capture) (timestamps in ns; lines left out marked `…`):

```text
producer: deepskopion-daemon/0.1.0
…
785940680941 101 i2c_start START
785940725941 101 i2c_byte 0xA0 ACK
785940770941 101 i2c_byte 0xAB ACK
785940815941 101 i2c_byte 0xCD ACK
785940860941 101 i2c_byte 0x3E ACK
785940867191 101 i2c_stop STOP
```

The frame tags, payload fields and channel IDs are defined in
[`contracts/wave_stream_v1/README.md`](https://github.com/routertl/deepskopion/blob/main/contracts/wave_stream_v1/README.md).
