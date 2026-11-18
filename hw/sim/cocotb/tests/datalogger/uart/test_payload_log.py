#!/usr/bin/env python3
"""
Full payload comparison test at 460800 baud.
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

# Locate routertl SDK cocotb dir via the pip-installed routertl_core
# (DS-P2.29 fallout — old shim walked up the tree for an `engine/`
# folder that no longer exists post-pip-decouple).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

# Deepskopion VIP path (hw/sim/vip lives under hw/sim/)
_ds_root = Path(__file__).resolve().parents[6]
_vip_parent = _ds_root / "hw" / "sim"
if str(_vip_parent) not in sys.path:
    sys.path.insert(0, str(_vip_parent))

from engine.simulation import run_simulation
from rr_cocotb_tb.drivers.uart_master import UartSource
from uart_datalogger_test_utils import CUSTOM_LIBRARIES

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
G_SYSTEM_CLK_FREQ = 100_000_000

GENERICS = {
    "G_SYSTEM_CLK_FREQ": G_SYSTEM_CLK_FREQ,
    "G_UART_DEV_ID": 28,
    "G_TIMESTAMP_WIDTH": 16,
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_ILA_EN": 0,
}

from rr_cocotb_tb.env import TbEnv
from rr_cocotb_tb.monitors import bv_to_int, StreamMonitor
# Deepskopion VIP imports (moved from tb.datalogger to vip.datalogger in SDK decoupling)
from vip.datalogger.uart.decode import decode_uart_msg, decode_config_msg
from vip.datalogger.uart.formatter import describe_uart_msg

# RTL-matched constants
RTL_CONFIG_TYPE      = 1
RTL_PAYLOAD_TYPE     = 2
RTL_PAYLOAD_9B_TYPE  = 3
RTL_STATUS_TYPE      = 4

STATUS_NAMES = {
    0: "CALIBRATION_DONE",
    1: "CALIBRATION_TIMEOUT",
    2: "PARITY_ERROR",
    3: "START_BIT_ERROR",
    4: "STOP_BIT_ERROR",
}

BAUD = 460800
BITS = 8
PARITY = "none" 
STOP_BITS = 1
CAL_BYTES = 150
POST_CAL_BYTES = 10
LOG_FILE = Path(__file__).parent / "payload_comparison.log"

def custom_decode(raw: int) -> str:
    dev_id = (raw >> 27) & 0x1F
    msg_type = (raw >> 24) & 0x07
    ts = (raw >> 8) & 0xFFFF
    data = raw & 0xFF
    
    type_name = "UNKNOWN"
    info = ""
    
    if msg_type == RTL_CONFIG_TYPE:
        type_name = "CONFIG"
        # Config bit field: BR[11:0] | 0 | STOP | PPAR | HPAR | NBITS[3:0]
        nbits = raw & 0x0F
        hpar = (raw >> 4) & 0x1
        ppar = (raw >> 5) & 0x1
        stop = (raw >> 6) & 0x1
        br = (raw >> 8) & 0xFFFF
        info = f"nbits={nbits}, parity={hpar}({'odd' if ppar else 'even'}), stop={stop}, br_cycles={br}"
    elif msg_type == RTL_PAYLOAD_TYPE:
        type_name = "PAYLOAD"
        info = f"data=0x{data:02X} ts={ts}"
    elif msg_type == RTL_PAYLOAD_9B_TYPE:
        type_name = "PAYLOAD_9B"
        info = f"data=0x{data:02X} ts={ts} (9th bit set)"
    elif msg_type == RTL_STATUS_TYPE:
        type_name = "STATUS"
        st_name = STATUS_NAMES.get(data, f"CODE_{data}")
        info = f"event={st_name} ts={ts}"
        
    return f"ID={dev_id} TYPE={type_name} | {info}"

@cocotb.test()
async def test_payload_comparison(dut):
    """Refactored payload comparison using standard TbEnv and StreamMonitor."""
    print("DEBUG: Entered test_payload_comparison")
    try:
        env = TbEnv(dut, dev_id_generic="G_UART_DEV_ID")
        print("DEBUG: Initialized TbEnv")
        await env.start_clock()
        print("DEBUG: Started clock")
        
        dut.UART_IN.value = 1
        dut.CONFIG.value = 0
        dut.TIMESTAMP.value = 0
        dut.UART_MSG_RDY.value = 1
        
        await env.reset()
        print("DEBUG: Reset released")
        
        # Optional: timestamp driver if not already handled
        async def timestamp_driver():
            val = 0
            while True:
                await Timer(1000, units="ns")
                val = (val + 1) % (2**16)
                dut.TIMESTAMP.value = val
        cocotb.start_soon(timestamp_driver())
        
        dev_id = GENERICS["G_UART_DEV_ID"]
        config = (dev_id << 11) | 0x001
        dut.CONFIG.value = config
        await ClockCycles(dut.CLK, 10)
        
        uart_source = UartSource(dut.UART_IN, baud=BAUD, bits=BITS, stop_bits=STOP_BITS, parity=PARITY)
        
        # Initialize Monitor
        monitor = StreamMonitor(
            env,
            clk=dut.CLK,
            valid_fn=lambda: bv_to_int(dut.UART_MSG_VLD.value),
            data_fn=lambda: bv_to_int(dut.UART_MSG_OUT.value),
            name="DATALOGGER_MSG",
            decode_fn=custom_decode
        )
        print("DEBUG: Initialized StreamMonitor")
        cocotb.start_soon(monitor.start())
        print("DEBUG: Started monitor task")
    except Exception as e:
        print(f"DEBUG: Error during test init: {e}")
        import traceback
        traceback.print_exc()
        raise e
    
    bit_time_ns = 1_000_000_000 / BAUD
    byte_time_ns = bit_time_ns * (1 + BITS + STOP_BITS)
    
    cal_data = [(i % 256) for i in range(CAL_BYTES)]
    post_data = [0xA0 + i for i in range(POST_CAL_BYTES)]
    all_sent = cal_data + post_data
    
    env.log.info(f"Sending {CAL_BYTES} cal bytes...")
    await uart_source.write_bytes(cal_data)
    
    # Wait for calibration
    cal_timeout_ns = byte_time_ns * 20
    start_time = cocotb.utils.get_sim_time(units="ns")
    while (cocotb.utils.get_sim_time(units="ns") - start_time) < cal_timeout_ns:
        if dut.calibrationDone_r.value == 1:
            break
        await ClockCycles(dut.CLK, 50)
    
    cal_time_us = cocotb.utils.get_sim_time(units="ns") / 1000
    env.log.info(f"Calibration @ {cal_time_us:.1f} µs | Fail={dut.calibrationFail_r.value}")
    
    if dut.calibrationFail_r.value == 1:
        env.log.error("CALIBRATION FAILED!")
        return

    await Timer(int(byte_time_ns * 2), units="ns")
    
    env.log.info(f"Sending {POST_CAL_BYTES} post-cal bytes (0xA0-0xA9)...")
    await uart_source.write_bytes(post_data)
    
    # Wait for all bytes + replay
    await Timer(int(byte_time_ns * (POST_CAL_BYTES + 30)), units="ns")
    
    final_time_us = cocotb.utils.get_sim_time(units="ns") / 1000
    env.log.info(f"End @ {final_time_us:.1f} µs")
    
    # Collect messages from monitor queue
    all_messages = []
    while not monitor.empty():
        all_messages.append(await monitor.get())
    
    # Analysis logic
    log_lines = [f"PAYLOAD ANALYSIS @ {BAUD} baud", "=" * 60, ""]
    log_lines.append(f"Total messages captured: {len(all_messages)}")
    
    payload_samples = [s for s in all_messages if (s.value >> 24) & 0x07 in (RTL_PAYLOAD_TYPE, RTL_PAYLOAD_9B_TYPE)]
    
    matches = 0
    mismatches = 0
    log_lines.append("\nPAYLOAD COMPARISON:")
    log_lines.append("-" * 40)
    
    for i in range(min(len(payload_samples), len(all_sent))):
        sent = all_sent[i]
        received = payload_samples[i].value & 0xFF
        match = "✓" if received == sent else "✗"
        if received == sent: matches += 1
        else: mismatches += 1
        log_lines.append(f"{i:3d} | Sent: 0x{sent:02X} | Recv: 0x{received:02X} | {match}")
        
    log_lines.append("\nSUMMARY:")
    log_lines.append(f"  Matches: {matches}")
    log_lines.append(f"  Mismatches: {mismatches}")
    log_lines.append(f"  Total Payloads: {len(payload_samples)} / {len(all_sent)} expected")
    
    with open(LOG_FILE, 'w') as f:
        f.write('\n'.join(log_lines))
    
    env.log.info(f"Results written to {LOG_FILE}")
    env.log.info(f"Matches: {matches}, Mismatches: {mismatches}")



def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_payload_log",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
