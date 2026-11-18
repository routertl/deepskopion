#!/usr/bin/env python3
"""DS-T2.49 ground-truth capture — runs ON the board.

Drives a KNOWN CAN-FD frame, captures the EDGE datalogger (raw bus transitions =
ground truth) AND the CAN burst datalogger (the suspect decode path) for the same
traffic, and dumps both as JSON for host-side wire reconstruction + oracle
comparison. No analysis here — keep the on-board half dumb and robust.

Usage (on board):  python3 -m sw.stream.testbench.ds_t249_edge_capture
The daemon MUST be SIGSTOP'd first (it drains the same ring).
"""
import json
import struct
import subprocess
import sys
import time
import os
import signal

sys.path.insert(0, "/usr/local/share/deepskopion")
from sw.stream.testbench.emio_inject import SelfTestBench
from sw.stream.testbench.ring_oracle import RingOracle, open_ring
from sw.stream.regbank import Regs

EDGE_RX_ID = 1            # EMIO_MUX_SEL[6]=1 -> EDGE0 <- can_bus (RX, the real bus)
CAN_BURST_ID = 20         # the PL CAN datalogger (suspect decode path)
EMIO_MUX_CAN = 0x40       # route CAN edges into the edge datalogger matrix
EDGE_EVENT = struct.Struct("<HBB")   # ts_delta(u16), channel(u8), flags(u8)
EDGE_FLAG_E = 0x01

# Known frame: 456##01122334455667788  (non-BRS FD, 8 data bytes)
FRAME_ARG = os.environ.get("DS_FRAME", "456##01122334455667788")
N_SENDS = 6


def main():
    bench = SelfTestBench()
    rb = bench.regbank
    mux_saved = rb._rb.read_u32(Regs.EMIO_MUX_SEL)
    rb._rb.write_u32(Regs.EMIO_MUX_SEL, EMIO_MUX_CAN)

    # arm both dataloggers from a clean state, edge first so it calibrates on the
    # leading SOF (a datalogger 'late to the party' mis-calibrates).
    rb.reset_and_enable_datalogger(EDGE_RX_ID)
    # CAN burst datalogger: NOMINAL config is BANKED — 0xA033 (prescaler 25 = 250k,
    # enable). Do NOT use enable_datalogger(extra=0): that writes prescaler 0 and the
    # datalogger mis-samples every bit (spurious StuffErr storm). Restore canonical.
    from sw.stream.data_source import apply_datalogger_config, set_can_data_prescaler
    apply_datalogger_config(rb._rb, CAN_BURST_ID, 0x033)   # nominal: presc 25 + enable
    # DATA-phase prescaler (BRS): clk_sys cycles per data bit. 2 Mbit @100MHz = 50.
    # DS_DATA_CYC lets us deliberately mismatch to probe the BRS CRC-sampling bug.
    data_cyc = int(os.environ.get("DS_DATA_CYC", "50"))
    set_can_data_prescaler(rb._rb, data_cyc)
    time.sleep(0.05)

    oracle = open_ring(rb)
    oracle.snapshot()

    mode = sys.argv[1] if len(sys.argv) > 1 else "iso"
    gen = None
    if mode == "b2b":
        # continuous back-to-back IDENTICAL frame (gap 0) -> every frame is a
        # b2b frame, all with the SAME true wire CRC (0x02012). The storm, if
        # b2b-triggered, shows up as SEV_ERROR burst frames.
        gen_data = os.environ.get("DS_GEN_DATA", "1122334455667788")
        gen_id = os.environ.get("DS_GEN_ID", "456")
        cmd = ["cangen", "can0", "-f", "-g", "0", "-I", gen_id, "-L", "8",
               "-D", gen_data]
        if os.environ.get("DS_GEN_BRS") == "1":
            cmd.append("-b")          # bit-rate switch (data phase at dbitrate)
        gen = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        time.sleep(0.25)
    else:
        for _ in range(N_SENDS):
            subprocess.run(["cansend", "can0", FRAME_ARG], check=False)
            time.sleep(0.01)

    try:
        frames = oracle.collect_new_frames(min_count=2, timeout_s=2.0,
                                           max_frames=512)
    except TimeoutError as e:
        frames = []
        sys.stderr.write(f"collect timeout: {e}\n")
    finally:
        if gen is not None:
            gen.send_signal(signal.SIGINT)
            try:
                gen.wait(timeout=1.0)
            except Exception:
                gen.kill()

    edge = RingOracle.filter_by_datalogger(frames, EDGE_RX_ID)
    burst = RingOracle.filter_by_datalogger(frames, CAN_BURST_ID)

    out = {
        "frame_arg": FRAME_ARG,
        "mux_saved": mux_saved,
        "n_total": len(frames),
        "edge_frames": [],
        "burst_frames": [],
    }
    for f in edge:
        trans = []
        p = f.payload
        for o in range(0, len(p) - 3, 4):
            td, ch, fl = EDGE_EVENT.unpack_from(p, o)
            trans.append([f.header.timestamp + td, 0 if (fl & EDGE_FLAG_E) else 1])
        out["edge_frames"].append({
            "ts": f.header.timestamp,
            "msg_type": f.msg_type,
            "payload_hex": p.hex(),
            "transitions": trans,
        })
    for f in burst:
        out["burst_frames"].append({
            "ts": f.header.timestamp,
            "msg_type": f.msg_type,
            "payload_hex": f.payload.hex(),
        })

    # restore mux
    rb._rb.write_u32(Regs.EMIO_MUX_SEL, mux_saved)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
