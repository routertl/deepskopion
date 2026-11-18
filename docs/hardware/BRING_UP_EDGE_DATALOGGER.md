# Edge Datalogger & EVT_EDGE_MAXFREQ Bring-Up Manual

This manual covers the **complete bring-up sequence** for the deepskopion edge
datalogger — from a fresh board power-on to verified `EVT_EDGE_BATCH` and
`EVT_EDGE_MAXFREQ` frames in the DDR ring. It encodes every re-discovery made
across six sessions of silicon bring-up.

---

## 1. Prerequisites & Safety

### Board lease
The Zybo-Z7-10 is a shared resource. Acquire a lease before touching the board:

```bash
rr lease acquire zybo-z7-20 --purpose "edge datalogger bring-up"
export RR_LEASE_HOLDER='<printed-id>'
```

### Linux running
Board must be at the Linux prompt. If you're at U-Boot:

```
boot          # or: run bootcmd
```

### CAN interfaces
If CAN interfaces are dead (after a PL reset or fresh boot), re-arm them:

```bash
ip link set can0 down; ip link set can0 up
ip link set can1 down; ip link set can1 up
```

---

## 2. ZS Frame Format (magic = 0x5A53)

The DDR ring holds ZS frames. **The magic is `0x5A53`, NOT `0xDADA`.**

```
struct ZS_HEADER (20 bytes, little-endian):
  magic      : u16  = 0x5A53
  version    : u8
  header_len : u8
  datalogger_id : u8
  msg_class  : u8
  msg_type   : u16  → domain = (msg_type >> 8) & 0xF
                       event_id = msg_type & 0xFF
  timestamp  : u64
  payload_len: u16
  flags      : u16
```

Domain constants:

| Domain | Hex | Name  |
|--------|-----|-------|
| 0      | 0x0 | SYS   |
| 6      | 0x6 | EDGE  |
| 10     | 0xA | CAN   |

Edge events: `msg_type = 0x0600 | event_id`. Key event IDs:

| Event ID | Name              | Description                        |
|----------|-------------------|------------------------------------|
| 0x01     | EVT_EDGE_BATCH    | Edge-detection batch (normal)      |
| 0x02     | EVT_EDGE_MAXFREQ  | Authoritative max-freq update      |
| 0x03     | EVT_EDGE_ERROR    | FIFO overrun / error event         |

---

## 3. Regbank Addresses (base 0x40000000)

Byte offsets from `regbank.py` (`Regs` enum). Read/write via `devmem2 <abs> w`.

| Offset  | Abs Address  | Register         | Notes                              |
|---------|--------------|------------------|------------------------------------|
| 0x0000  | 0x40000000  | GIT_HASH         | Lower 32 bits of build commit hash |
| 0x0008  | 0x40000008  | BUILD_DATE       | Build timestamp                    |
| 0x0020  | 0x40000020  | DATALOGGER_CONFIG   | Single-write latch (see §5)        |
| 0x0024  | 0x40000024  | EMIO_MUX_SEL     | Signal routing; bit6=can_edge_sel  |
| 0x00E0  | 0x400000E0  | PKT_CNT_EDGE     | Edge FIFO acceptance counter       |
| 0x0100  | 0x40000100  | ERR_CNT_EDGE     | Edge FIFO overrun counter          |
| 0x0110  | 0x40000110  | ARB_GRANT_CNT    | 2nd-layer arbiter output counter   |
| 0x0164  | 0x40000164  | RING_COMMIT_PTR  | Ring write pointer (byte offset)   |

---

## 4. PL Reset & CAN Re-init

After programming a new bitstream, always reset the PL domain so FIFO
synchronisers and state machines start clean:

```bash
echo 1 > /sys/bus/platform/drivers/zynq-reset/amba:zynq_reset/reset
echo 0 > /sys/bus/platform/drivers/zynq-reset/amba:zynq_reset/reset
```

Then re-init CAN (the PS→PL routing is disturbed by PL reset):

```bash
ip link set can0 down; ip link set can0 up
ip link set can1 down; ip link set can1 up
```

---

## 5. DATALOGGER_CONFIG — Enabling an Edge Datalogger Channel

DATALOGGER_CONFIG is a **single-write broadcast**: one 32-bit write to
`0x40000020`. Each `config_control` inside the gather latches it independently
when it sees its own `device_id` in the write. **No separate latch strobe.**

The encoding (16 significant bits):

```
[15:11] = device_id (5 bits)   ← the datalogger's unique device ID
[10:1]  = extra / config_data
[0]     = ENABLE bit
```

EDGE0 has `C_EDGE0_ID = 1` (device_id=1). To enable it:

```bash
# enable_datalogger(device_id=1, enable=True, extra=0)
# = ((1 << 11) | 0x001) = 0x0801
devmem2 0x40000020 w 0x0801
```

To disable EDGE0 (e.g. for the disable/enable test):

```bash
devmem2 0x40000020 w 0x0800   # same device_id, ENABLE=0
```

**Best practice:** reset before enable (clears FIFO state):

```bash
devmem2 0x40000020 w 0x0800   # disable (RST)
sleep 0.1                      # wait for RST_BUSY to clear
devmem2 0x40000020 w 0x0801   # enable
```

---

## 6. EMIO_MUX_SEL — Routing CAN TX to the Edge Datalogger

For CAN FD loopback testing (capturing CAN TX), set bit 6 of EMIO_MUX_SEL:

```bash
# EMIO_MUX_CAN = 0x40 (bit 6)
devmem2 0x40000024 w 0x0040
```

This routes the CAN0 TX line to the EDGE0 datalogger input. Without this,
`PKT_CNT_EDGE` will stay at zero even with the datalogger enabled.

EMIO_MUX_SEL bit map:

| Bit | Signal             | Description                       |
|-----|--------------------|-----------------------------------|
| 0   | i2c_sel            | Route PS EMIO I2C0 to datalogger     |
| 1   | uart_sel           | Route PS EMIO UART0 to datalogger    |
| 2   | spi_sel            | Route PS EMIO SPI0 to datalogger     |
| 3   | edge_spi_self_test | SPI TX→edge (self-test)           |
| 4   | edge_i2c_sel       | I2C SCL/SDA → EDGE0/EDGE1         |
| 6   | can_edge_sel       | CAN TX→edge (loopback test)       |

---

## 7. CAN FD Loopback Source

To generate edges for the datalogger to count, start a CAN FD loopback test:

```bash
# On the board:
cangen can0 -f -I 0x1 -L 64 -g 1 &     # sends at ~1 kfps
```

`PKT_CNT_EDGE` should start incrementing at approximately
`frame_rate × bits_per_frame × 2` (rising + falling edges).

---

## 8. Deadlock Detection

The edge datalogger and arbiter FIFOs can deadlock if `PKT_CNT_EDGE` stops
growing while the CAN source is active. This is caused by the arbiter's
`fifos_busy` holding during reset. Recovery: PL reset (step 4) → re-arm CAN
→ reconfigure DATALOGGER_CONFIG and EMIO_MUX_SEL.

Diagnostic counters:

```bash
pkt0=$(devmem2 0x400000E0 w | tail -1)
arb0=$(devmem2 0x40000110 w | tail -1)
sleep 1
pkt1=$(devmem2 0x400000E0 w | tail -1)
arb1=$(devmem2 0x40000110 w | tail -1)
echo "PKT_EDGE delta: $((pkt1-pkt0))  ARB_GRANT delta: $((arb1-arb0))"
# PKT_EDGE grows, ARB_GRANT stuck  → arbiter deadlock → PL reset
# PKT_EDGE doesn't grow            → datalogger not receiving → check DATALOGGER_CONFIG + EMIO_MUX_SEL
```

---

## 9. Disable/Enable Test

The most reliable smoke-test: 100% of ring writes come from EDGE0.
When disabled, RING_COMMIT_PTR must not advance.

```bash
# Disable EDGE0:
devmem2 0x40000020 w 0x0800

# Read RING_COMMIT_PTR twice, 1 second apart:
p0=$(devmem2 0x40000164 w | tail -1)
sleep 1
p1=$(devmem2 0x40000164 w | tail -1)
echo "commit_delta = $((p1 - p0))"
# Expected: 0  (no ring writes when EDGE0 disabled)

# Re-enable:
devmem2 0x40000020 w 0x0801
```

---

## 10. Ring Read & Frame Verification

```python
import struct, mmap, os

RING_BASE = 0x10000000
RING_SIZE = 0x10000000
REGBANK_BASE = 0x40000000
RING_COMMIT_PTR_OFF = 0x0164

# Read commit pointer via /dev/mem:
with open('/dev/mem', 'r+b') as f:
    mm = mmap.mmap(f.fileno(), 0x1000, offset=REGBANK_BASE)
    commit = struct.unpack('<I', mm[RING_COMMIT_PTR_OFF:RING_COMMIT_PTR_OFF+4])[0]
    mm.close()
print(f"Ring commit ptr: {commit} bytes ({commit//(20+8):.0f} frames approx)")

# Scan ring for ZS frames:
ZS_MAGIC = 0x5A53
ZS_HEADER = struct.Struct('<HBBBBHQHH')
HDR_SZ = ZS_HEADER.size  # 20

with open('/dev/mem', 'r+b') as f:
    mm = mmap.mmap(f.fileno(), min(commit, 65536), offset=RING_BASE)
    data = bytes(mm[:min(commit, 65536)])
    mm.close()

i = 0
count = 0
while i + HDR_SZ <= len(data):
    magic, ver, hlen, sid, mclass, mtype, ts, plen, flags = ZS_HEADER.unpack_from(data, i)
    if magic != ZS_MAGIC:
        i += 1
        continue
    domain = (mtype >> 8) & 0xF
    evt_id = mtype & 0xFF
    payload = data[i+HDR_SZ:i+HDR_SZ+plen]
    print(f"@{i:06x}: domain={domain} evt=0x{evt_id:02x} sid={sid} "
          f"plen={plen} payload={payload.hex()}")
    i += HDR_SZ + plen
    count += 1
    if count > 20:
        break
```

A healthy ring contains:

| Field        | Healthy value                                     |
|--------------|---------------------------------------------------|
| `domain`     | 6 (EDGE)                                          |
| `evt_id`     | 0x01 (EVT_EDGE_BATCH) or 0x02 (EVT_EDGE_MAXFREQ) |
| `datalogger_id` | non-zero (G_ED_DEV_ID=1 for EDGE0)                |
| `payload`    | ≥ 0x08000000 (bits[31:27]="00001")                |

If you see only `domain=0, payload=0x00000000` → see §12 Root Cause History.

---

## 11. EVT_EDGE_MAXFREQ Verification

Once `EVT_EDGE_BATCH` (evt_id=0x01) frames appear, verify `EVT_EDGE_MAXFREQ`
(evt_id=0x02). The payload encodes a 24-bit minimum interval in PL clock ticks:

```python
# From ZS_HEADER above, look for evt_id=0x02:
if domain == 6 and evt_id == 0x02:
    interval_ticks = struct.unpack('<I', (payload + b'\x00')[:4])[0] & 0xFFFFFF
    PL_CLK_HZ = 100_000_000   # Zybo-Z7-10 CAN FD bench build
    freq_hz = PL_CLK_HZ / interval_ticks if interval_ticks else 0
    print(f"EVT_EDGE_MAXFREQ: {freq_hz/1e6:.3f} MHz ({interval_ticks} ticks)")
```

Expected: frequency matches the CAN FD bit rate × edges-per-bit.
A max-freq event is emitted each time a new maximum is detected (event-driven).
Trigger a re-arm to reset the running maximum: write `CONFIG[2]` set in
`DATALOGGER_CONFIG` (RE_ARM is a global broadcast to all edge channels).

---

## 12. Stream Daemon Setup (Persistent Configuration)

To make DATALOGGER_CONFIG + EMIO_MUX_SEL persistent across reboots, add them to
the S99deepskopion-stream init script:

```bash
# /etc/init.d/S99deepskopion-stream (relevant lines):
devmem2 0x40000020 w 0x0800    # reset EDGE0 first
sleep 0.1
devmem2 0x40000020 w 0x0801    # enable EDGE0
devmem2 0x40000024 w 0x0040    # can_edge_sel (bit6)
```

---

## 13. Root Cause History (Important for Future Sessions)

### FWFT latency mismatch (FIXED, commit c39499a)

`fifo_xpm_sync` had `FIFO_READ_LATENCY=1` in FWFT mode (must be 0) and
`USE_ADV_FEATURES="0707"` which fed `rd_data_count` back into the Synth
8-5966 self-loop eliminator. Fixed by:
- `C_READ_LATENCY = 1 - G_FIFO_FWFT`  (= 0 in FWFT mode)
- `USE_ADV_FEATURES = "0206"` (no rd_data_count fanout to DOUT logic)

### xpm_vhdl synthesis shadowing → all FIFO reads = 0 (FIXED, commit 1864e78)

**Root cause of all-zero ring frames.** `libs/xpm_vhdl/` provides VHDL
simulation models for nvc/GHDL. These files are compiled into the Vivado
project with `UsedIn=synthesis`, **shadowing** Vivado's built-in XPM
primitives. Vivado synthesises the behavioural `xpm_memory_base` model and
hits Synth 8-6858: 100 multi-driven `Q[N]` nets where a constant-zero driver
overrides the FIFO memory output → every FIFO read returns 0x00.

**Diagnostic:** `build/zybo_datalogger/reports/multiDrivenNets.rpt`:
- Present: `"No multi-driven nets detected"` = **FIXED** ✓
- Missing/old report has `"8-6858=100  8-6859=100"` = **BUG**

**Fix (current):** `dependencies.xpm_vhdl.sim_only: true` in
`targets/*.yml` keeps the models out of
the synthesis compile set entirely — lint + sim only.

**Fix (historical):** `hw/bd/exclude_xpm_sim_from_synth.tcl` (gen_project
hook) set `USED_IN_SYNTHESIS=false` on all `libs/xpm_vhdl` files inside the
Vivado project. Retired once the synth job log proved it
matched zero files (`0 xpm_vhdl simulation-model files excluded`,
2026-08-02). If you ever need the pattern again: `FILE_TYPE` is
`"VHDL 2008"` (with a space and year) — do NOT filter by
`FILE_TYPE == VHDL`; match by path.

### Deadlock after PL reset
`PKT_CNT_EDGE` stops; `ARB_GRANT_CNT` stuck. Always run PL reset after
programming, then re-arm CAN and reconfigure DATALOGGER_CONFIG + EMIO_MUX_SEL.

### CAN rate limits (soft)
I2C SCL maxes ~400 kHz from PS software → `PKT_CNT_EDGE` ≤ ~800 kcounts/s.
CAN FD at 1 Mbps provides ≥ 2 Medges/s (reliable saturation).

### ZS magic is 0x5A53 (not 0xDADA)
`0xDADA` was an early placeholder in some SW prototypes. The RTL and all
production SW use `C_ZS_MAGIC = 0x5A53`. If a ring scanner reads 0xDADA
frames, it was trained on stale docs — update it.

---

## 14. Quick Checklist

```
[ ] rr lease acquire zybo-z7-20
[ ] Board at Linux prompt
[ ] New bitstream programmed (rr program run)
[ ] rr hw bit-sha matches the built bit
[ ] PL reset performed
[ ] CAN interfaces re-armed (can0, can1)
[ ] devmem2 0x40000020 w 0x0800  (reset EDGE0)
[ ] sleep 0.1
[ ] devmem2 0x40000020 w 0x0801  (enable EDGE0)
[ ] devmem2 0x40000024 w 0x0040  (can_edge_sel)
[ ] CAN source active (cangen can0 -f ...)
[ ] PKT_CNT_EDGE (0x400000E0) growing
[ ] ARB_GRANT_CNT (0x40000110) growing (if not → PL reset)
[ ] RING_COMMIT_PTR (0x40000164) advancing
[ ] Ring frames: domain=6, evt_id=0x01, datalogger_id≠0, payload≥0x08000000
[ ] Ring frames: domain=6, evt_id=0x02 (EVT_EDGE_MAXFREQ)
[ ] Check multiDrivenNets.rpt: "No multi-driven nets detected"
[ ] rr lease release
```
