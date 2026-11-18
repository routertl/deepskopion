# CAN datalogger silicon bring-up — PS-EMIO 2-node fabric loopback

On-hardware validation of the `can_datalogger` with **zero external
hardware**. The Zynq-7's two PS CAN controllers (CAN0 + CAN1) are routed to EMIO
and ANDed into a single open-drain CAN bus **in fabric** — a real 2-node bus that
arbitrates and ACKs for real. The passive `can_datalogger` (id 20) taps it.

```
   PS CAN0 ──PHY_TX──┐                          ┌──PHY_RX── PS CAN0
                     ├── can_bus_w = TX0 and TX1 ┤
   PS CAN1 ──PHY_TX──┘   (wired-AND, dom '0')   └──PHY_RX── PS CAN1
                              │
                              └── CAPTURE_PINS[28] ── can_datalogger (id 20, passive)
```

Why two controllers: a lone CAN node can't complete a frame — it needs another
node to drive the ACK slot dominant or it retransmits forever. CAN1 ACKs CAN0
(and vice-versa), so frames complete and the datalogger sees **real arbitration +
real ACK**, not BFM-faked.

This is **additive / default-OFF**: with `can0`/`can1` down the bus idles
recessive (`can_bus_w='1'`) and the datalogger (id 20) only wakes when its
`DATALOGGER_CONFIG` is written — so the rest of the PoC is unaffected.

---

## 0. What's wired (RTL/descriptor — already in the tree)

| Piece | File |
|---|---|
| Enable CAN0/CAN1 on EMIO | `hw/bd/ds_emio_can.tcl` (ps7_hook) + `project.yml hardware.ps7_hooks` |
| Export the EMIO pins on the BD wrapper | `hw/bd/deepskopion_bd.tcl` (`CAN0/1_PHY_TX/RX`) |
| Wired-AND bus + `capture_pin(28)` tap | `hw/rtl/deepskopion_top.vhd` (`can_bus_w`) |
| SocketCAN `can0`/`can1` | `project.yml linux.dt_nodes` (`can@e0008000` / `can@e0009000` → `okay`) |
| The datalogger itself (id 20, RX pin 28) | `tools/datalogger-builder/config/datalogger_config.yml` |

## 1. Build + deploy (pure-routertl flow)

```sh
rr build                       # synth+impl+bitstream (regenerates the BD with CAN EMIO)
rr linux dtb                   # regen the DTB (picks up the can@... dt_nodes)
rr linux sdcard                # bootgen → BOOT.bin (+ uImage/system.dtb)
# deploy per the Zynq-7 TFTP/BOOT.bin loop (zynq-bitstream-redeploy-tftp), then reboot.
```

The CTU CAN FD bench build is not part of this distribution. Its target keeps its packages
in its own lock (`packages_lock: target`), so installing them vendors the CTU core under
`libs/.targets/<target>/` and leaves `ip.lock` and `libs/` unchanged. That build rewrites
`datalogger_gather.vhd`, `datalogger_config_pkg.vhd` and `regbank/config_schema.hex` for its own
datalogger mix; those changes belong to its working tree and are not committed.

Each target has its own tracked lock in `targets/`, named after the target with the suffix
`.routertl.lock`; a bench target's lock sits there too, beside the products. A target's lock pins
only the packages that target reaches, so `ctu-fee/ctucanfd_ip_core` appears only in the
locks of the CTU CAN FD bench targets. The pre-commit lock check runs `rr lock --check` for each
target whose inputs a commit changes, and first installs the packages a target's lock pins
when they are absent from the tree.

After boot, confirm the PL runs the built bit before trusting any readback
(`hw-bitstream-confirm` rule): `rr hw bit-sha` (or read the regbank identity
slots). Then check both CAN nodes enumerated:

```sh
ip -details link show | grep -A2 'can[01]:'        # both can0 + can1 present (xilinx_can)
```
**✅ Checkpoint A:** `can0` and `can1` both appear. If absent → the dt_nodes
didn't enable them (re-check `can@e0008000`/`@e0009000` survived the `rr linux
dtb` regen) or the ps7_hook didn't route CAN to EMIO.

## 2. Bring up the bus at 250 kbit/s

250 kbit is the bring-up rate — it's **exact** for the datalogger's fixed 16-TQ
timing at 100 MHz `clk_sys` (`prescaler = 100e6 / (16 × 250e3) = 25`, integer).

```sh
for c in can0 can1; do
  ip link set $c type can bitrate 250000     # both nodes, same wire rate
  ip link set $c up
done
```

## 3. Prove the FABRIC BUS (two PS nodes talk) — independent of the datalogger

```sh
candump can1 &                  # listen on node 1
cansend can0 123#ABCD           # node 0 transmits ID 0x123, data AB CD
# expect candump: can1  123   [2]  AB CD
```
**✅ Checkpoint B:** `candump can1` shows the frame `cansend can0` put out. This
proves the fabric wired-AND bus + EMIO routing + ACK (can0's send only *completes*
because can1 ACKed it — check `ip -s link show can0` has TX, 0 error-passive).
If `cansend` hangs / `ip -s` shows bus-off → the wired-AND or RX-feedback is
wrong, or only one controller is up.

## 4. Prove the DATALOGGER (byte-exact decode of the live bus)

Enable the CAN datalogger (id 20) via `DATALOGGER_CONFIG` — `[15:11]=id`, `[10:1]=
bit_prescaler`, `[0]=enable`. For id 20 + prescaler 25 + enable:
`(20<<11) | (25<<1) | 1 = 0xA033`.

```sh
# DATALOGGER_CONFIG @ regbank 0x4000_0020 (regbank base 0x4000_0000)
devmem 0x40000020 32 0xA033
```

Then watch the datalogger's decoded stream (the wave_stream daemon / RouteWave, the
same path the other dataloggers use), and replay a known frame:

```sh
cansend can0 123#ABCD
```
**✅ Checkpoint C (the headline):** the datalogger emits, routed to **device_id 20**,
byte-exact: `frame_begin`, `control`(IDE0/RTR0/DLC2), `id` `0x01`,`0x23`,
`data` `0xAB`,`0xCD`, `crc` ×2, `ack_observed`, `frame_end` — **no `crc_error`**.
This is the same byte-exact contract `test_datalogger_gather_can` asserts in sim,
now on silicon against a *real* controller. Any drop / mangled byte / spurious
`crc_error` is a **real DUT defect → file a T-ticket** (do NOT loosen the check).

Vary it: extended ID (`cansend can0 12345678#5A`), remote frame
(`cansend can0 7A0#R2`), a heavily-stuffed payload (`cansend can0 000#0000`) —
each must decode byte-exact (control IDE/RTR bits, de-stuffed data).

## 5. EDGE cross-check (ground truth)

Re-point an EDGE datalogger at the same RX line and confirm the raw bit pattern
matches the CAN frame on the wire — this separates a *conditioning* fault from a
*decode* fault (same discipline as the UART/edge cross-check). The CAN bus is on
`capture_pin(28)`; capture it raw via the edge path and compare bit-for-bit.
**✅ Checkpoint D:** EDGE raw bits == the SocketCAN-sent frame (post-stuffing).

---

## Phase 2 (later) — route to PMOD + real transceivers

Once the fabric loopback is green, take it physical: route `CAN0_PHY_TX/RX` and
`CAN1_PHY_TX/RX` to PMOD pins (XDC), wire each to a 3.3 V CAN transceiver
(SN65HVD230 / MCP2562), tie CAN_H/CAN_L together with 120 Ω termination at both
ends. Now the wired-AND happens *electrically* on the real bus and external CAN
devices can join — the datalogger already understands the traffic. (The fabric
wired-AND stays as the internal-loopback self-test path.)

## Notes / gotchas

- **Bitrate must match** on both the PS controllers (`ip link ... bitrate`) and
  the datalogger (`prescaler` in `DATALOGGER_CONFIG`). 250 kbit ↔ prescaler 25. Other
  clean rates: 125 kbit ↔ 50. 1 Mbit is `6.25` cyc/TQ — NOT integer; auto-bitrate
  + non-16-TQ timing is the CAN-FD job.
- **CAN FD with BRS: the datalogger needs the bus's sample points.** A CAN FD
  transmitter switches to the data rate at its own nominal sample point, and the
  rest of the BRS bit is its data phase-2 segment. Linux sets 87.5 % at 250 kbit/s
  and 0.740 at 2 Mbit/s; the datalogger resets to 75 %, where it reads every field
  after BRS one bit late. After `can_fd_link setup`, program the datalogger from the
  link's own timing (it writes both sample points, then the data bit period):

  ```sh
  python3 -m sw.can.can_fd_link setup can0 --bitrate 250000 --dbitrate 2000000
  python3 -m sw.can.can_fd_link setup can1 --bitrate 250000 --dbitrate 2000000
  python3 -m sw.can.can_fd_link arm-datalogger can0   # logs nominal 224/256, data 189/256, data_cyc 50
  ```

  `ip -details link show can0` prints the same `sample-point` / `dsample-point`.
  Measured on the Zybo-Z7-20 (the CTU CAN FD bench build at `2b50f7ef`): with these three commands
  and the datalogger device 20 primary word 0x033, can_fd_link's DEFAULT_SEQUENCE
  decodes 15 of 15 frames exactly over three passes, the 0x7A0 BRS=1 DLC 0 frame
  included.
- The two CAN controllers idle recessive when down → `can_bus_w='1'` → pin 28
  idle high → **PoC unchanged** until you explicitly bring CAN up.
- `ds_emio_can.tcl` is **integrator-only TCL** (runs under Vivado). The PCW CAN
  clock property values are confirmed by Checkpoint A on real silicon; if `can0`/
  `can1` don't enumerate, adjust `PCW_CAN_PERIPHERAL_*` per the board's CAN clock
  plan and rebuild.
