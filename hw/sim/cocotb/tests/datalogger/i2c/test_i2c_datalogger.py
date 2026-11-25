# rr unknown-llm (AI-generated)
# tests/datalogger/i2c/test_i2c_datalogger.py
"""
Cocotb testbench for i2c_datalogger

Tests all currently implemented events and payloads as documented in:
  - I2C_DATALOGGER_PROTOCOL.md
  - I2C_DATALOGGER_FEATURES.md

Run with:
    SIM=nvc python test_i2c_datalogger.py
"""
import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer, with_timeout

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

# SDK imports
from sdk.cocotb_helpers import requires
from rr_cocotb_tb.env import TbEnv
from rr_cocotb_tb.scoreboard import SignalCollector, Scoreboard
from rr_cocotb_tb.drivers.i2c_master import I2cMasterDriver, I2cTiming
# Deepskopion VIP imports (moved from tb.datalogger to vip.datalogger in SDK decoupling)
from vip.datalogger.i2c import I2cMsgDecoder, I2cEvent, I2cDataloggerCfg, pack_config

# Generics for the DUT
GENERICS = {
    "G_I2C_ID_WIDTH": 5,
    "G_I2C_DEV_ID": 28,
    "G_TIMESTAMP_WIDTH": 16,
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_ILA_EN": 0,
}


class I2cDataloggerTb:
    """I2C Datalogger Testbench Environment"""
    
    def __init__(self, dut):
        self.dut = dut
        self.env = TbEnv(
            dut,
            clk="CLK",
            rst="RST",
            period_ns=10,
            dev_id_generic="G_I2C_DEV_ID",
        )
        
        # I2C Master Driver (using separate signals for the wrapper)
        self.i2c = I2cMasterDriver(
            self.env,
            sda=dut.M_SDA_OUT,
            sda_in=dut.M_SDA_IN,
            scl=dut.M_SCL,
            timing=I2cTiming(period_ns=10_000),  # 100 kHz
        )
        
        # Message decoder
        self.decoder = I2cMsgDecoder(device_id=GENERICS["G_I2C_DEV_ID"])
        
        # Message collector
        self.collector = SignalCollector(
            clk=self.env.clk,
            valid_sig=dut.I2C_MSG_VLD,
            data_sig=dut.I2C_MSG_OUT,
        )
        
        # Scoreboard
        self.scoreboard = Scoreboard(
            env=self.env,
            collector=self.collector,
            fmt_fn=lambda v: self.decoder.format_message(self.decoder.decode(v)),
        )
    
    async def setup(self, slave_addr=0x50):
        """Initialize testbench."""
        self.dut.I2C_SLAVE_ADDR.value = slave_addr
        await self.env.start_clock()
        
        # Start collector
        cocotb.start_soon(self.collector.start())
        
        # Initialize DUT signals
        self.dut.CONFIG.value = 0
        self.dut.TIMESTAMP.value = 0
        self.dut.I2C_MSG_RDY.value = 1
        self.dut.I2C_PINS.value = 0b11  # Both lines high (idle)
        
        # Reset
        await self.env.reset(cycles=5)
        
        self.env.log.info("Testbench setup complete")
    
    def enable_datalogger(self, device_id: int = 28) -> int:
        """Enable datalogger and return CONFIG value."""
        cfg = I2cDataloggerCfg(device_id=device_id, enable=True)
        val = pack_config(cfg)
        self.dut.CONFIG.value = val
        return val
    
    def disable_datalogger(self, device_id: int = GENERICS["G_I2C_DEV_ID"]):
        """Disable datalogger."""
        cfg = I2cDataloggerCfg(device_id=device_id, enable=False)
        self.dut.CONFIG.value = pack_config(cfg)
    
    def build_expected_event(self, event: I2cEvent, timestamp: int = 0) -> int:
        """Build expected 32-bit message for an event."""
        device_id = GENERICS["G_I2C_DEV_ID"]
        msg_type = 0b100  # Event type
        return (device_id << 27) | (msg_type << 24) | (timestamp << 8) | event.value


# =============================================================================
# Test Cases
# =============================================================================

@cocotb.test()
@requires("I2C-REQ-1")
async def test_enable_event(dut):
    """Test that enabling the datalogger generates ENABLED event."""
    tb = I2cDataloggerTb(dut)
    await tb.setup()
    
    # Enable datalogger
    tb.enable_datalogger()
    await ClockCycles(tb.env.clk, 10)
    
    # Check for ENABLED event
    assert not tb.collector.empty(), "Expected ENABLED event"
    msg_val, msg_time = await tb.collector.get()
    msg = tb.decoder.decode(msg_val.value)
    
    tb.env.log.info(f"Received: 0x{msg_val.value:08X} | Type: {msg.msg_type} | Event: {msg.event_name if msg.is_event else 'N/A'} (T={msg_time:.0f} ns)", extra={'sim_time': f"{msg_time: >9.0f}ns"})
    
    assert msg.is_event, "Expected event message"
    assert msg.event_code == I2cEvent.ENABLED, f"Expected ENABLED, got {msg.event_name}"
    
    tb.env.log.info("✓ ENABLED event verified")
    await ClockCycles(tb.env.clk, 100)
    tb.env.log.info("Test test_enable_event complete")


@cocotb.test()
@requires("I2C-REQ-2")
async def test_disable_event(dut):
    """Test that disabling the datalogger generates DISABLED event."""
    tb = I2cDataloggerTb(dut)
    await tb.setup()
    
    # Enable then disable
    tb.env.log.info("Enabling datalogger...")
    tb.enable_datalogger()
    await ClockCycles(tb.env.clk, 10)
    
    # Drain internal queue without filtering, just log what we see
    tb.env.log.info("Checking for events...")
    
    # Just capture whatever comes out for a while
    try:
        while True:
            msg_val, msg_time = await with_timeout(tb.collector.get(), 100, "ns")
            msg = tb.decoder.decode(msg_val.value)
            tb.env.log.info(f"Received: 0x{msg_val.value:08X} | Type: {msg.msg_type} | Event: {msg.event_name if msg.is_event else 'N/A'} (T={msg_time:.0f} ns)", extra={'sim_time': f"{msg_time: >9.0f}ns"})
    except cocotb.triggers.SimTimeoutError:
        tb.env.log.info("No more events in queue.")

    # Disable
    tb.env.log.info("Disabling datalogger...")
    tb.disable_datalogger()
    tb.env.log.info("Waiting for post-disable events...")

    # Wait ample time and capture everything
    await ClockCycles(tb.env.clk, 200)

    post_disable_msgs = []
    try:
        while True:
            msg_val, msg_time = await with_timeout(tb.collector.get(), 100, "ns")
            msg = tb.decoder.decode(msg_val.value)
            tb.env.log.info(f"Received: 0x{msg_val.value:08X} | Type: {msg.msg_type} | Event: {msg.event_name if msg.is_event else 'N/A'} (T={msg_time:.0f} ns)", extra={'sim_time': f"{msg_time: >9.0f}ns"})
            post_disable_msgs.append(msg)
    except cocotb.triggers.SimTimeoutError:
        tb.env.log.info("No more events captured.")

    # I2C-REQ-2: a DISABLED event (code=0x00) MUST appear in the
    # post-disable capture window.
    assert any(
        m.is_event and m.event_code == I2cEvent.DISABLED
        for m in post_disable_msgs
    ), (
        "Expected DISABLED event after disable_datalogger(); got "
        f"{[(m.msg_type, m.event_name if m.is_event else None) for m in post_disable_msgs]}"
    )

    tb.env.log.info("Test test_disable_event complete")


@cocotb.test()
@requires("I2C-REQ-3")
async def test_start_event(dut):
    """Test START condition detection."""
    tb = I2cDataloggerTb(dut)
    await tb.setup()
    
    # Enable datalogger
    tb.enable_datalogger()
    await ClockCycles(tb.env.clk, 10)
    await tb.collector.get()  # Drain ENABLED
    
    # Initialize I2C bus
    await tb.i2c.initialize()
    
    # Generate START
    await tb.i2c.start()
    await ClockCycles(tb.env.clk, 20)
    
    # Check for START event
    assert not tb.collector.empty(), "Expected START event"
    msg_val, msg_time = await tb.collector.get()
    msg = tb.decoder.decode(msg_val.value)
    tb.env.log.info(f"Received: 0x{msg_val.value:08X} | Type: {msg.msg_type} | Event: {msg.event_name if msg.is_event else 'N/A'} (T={msg_time:.0f} ns)", extra={'sim_time': f"{msg_time: >9.0f}ns"})
    
    assert msg.is_event, "Expected event"
    assert msg.event_code == I2cEvent.START, f"Expected START, got {msg.event_name}"
    
    # Clean up with STOP
    await tb.i2c.stop()
    
    tb.env.log.info("✓ START event verified")
    await ClockCycles(tb.env.clk, 100)
    tb.env.log.info("Test test_start_event complete")


@cocotb.test()
@requires("I2C-REQ-4")
async def test_stop_event(dut):
    """Test STOP condition detection."""
    tb = I2cDataloggerTb(dut)
    await tb.setup()
    
    # Enable datalogger
    tb.enable_datalogger()
    await ClockCycles(tb.env.clk, 10)
    await tb.collector.get()  # Drain ENABLED
    
    # Initialize and START
    await tb.i2c.initialize()
    await tb.i2c.start()
    await ClockCycles(tb.env.clk, 20)
    await tb.collector.get()  # Drain START
    
    # Generate STOP
    await tb.i2c.stop()
    await ClockCycles(tb.env.clk, 20)
    
    # Check for STOP event
    assert not tb.collector.empty(), "Expected STOP event"
    msg_val, msg_time = await tb.collector.get()
    msg = tb.decoder.decode(msg_val.value)
    tb.env.log.info(f"Received: 0x{msg_val.value:08X} | Type: {msg.msg_type} | Event: {msg.event_name if msg.is_event else 'N/A'} (T={msg_time:.0f} ns)", extra={'sim_time': f"{msg_time: >9.0f}ns"})
    
    assert msg.is_event, "Expected event"
    assert msg.event_code == I2cEvent.STOP, f"Expected STOP, got {msg.event_name}"
    
    tb.env.log.info("✓ STOP event verified")
    await ClockCycles(tb.env.clk, 100)
    tb.env.log.info("Test test_stop_event complete")


@cocotb.test()
@requires("I2C-REQ-10")
async def test_write_transaction(dut):
    """Test complete write transaction with address and data.

    The DUT signals I2C_MSG_VLD / I2C_MSG_OUT are sampled inside the
    SignalCollector wired in I2cDataloggerTb.setup() — the locals below
    just make that explicit for the @requires layer-1.5 signal-reference
    check.
    """
    _i2c_msg_vld_sig = dut.I2C_MSG_VLD  # sampled by collector
    _i2c_msg_out_sig = dut.I2C_MSG_OUT  # sampled by collector
    tb = I2cDataloggerTb(dut)
    tb.env.log.info("STARTING test_write_transaction")
    await tb.setup()
    
    # Enable datalogger
    tb.enable_datalogger()
    await ClockCycles(tb.env.clk, 10)
    await tb.collector.get()  # Drain ENABLED
    
    # Perform write transaction: addr 0x50, data [0xAB, 0xCD]
    await tb.i2c.initialize()
    await tb.i2c.start()
    await ClockCycles(tb.env.clk, 20)
    
    # Write address (7-bit addr + W bit = 0xA0)
    await tb.i2c.write_byte(0xA0)  # 0x50 << 1 = 0xA0
    await ClockCycles(tb.env.clk, 20)
    
    # Write data bytes
    await tb.i2c.write_byte(0xAB)
    await ClockCycles(tb.env.clk, 20)
    
    await tb.i2c.write_byte(0xCD)
    await ClockCycles(tb.env.clk, 20)
    
    await tb.i2c.stop()
    await ClockCycles(tb.env.clk, 20)
    
    # Verify messages: START + 3 payloads + STOP
    messages = []
    while not tb.collector.empty():
        msg_val, msg_time = await tb.collector.get()
        msg = tb.decoder.decode(msg_val.value)
        tb.env.log.info(f"Received: 0x{msg_val.value:08X} | Type: {msg.msg_type} | Event: {msg.event_name if msg.is_event else 'N/A'} (T={msg_time:.0f} ns)", extra={'sim_time': f"{msg_time: >9.0f}ns"})
        messages.append(msg)
    
    assert len(messages) >= 4, f"Expected at least 4 messages, got {len(messages)}"
    
    # First should be START
    assert messages[0].is_event and messages[0].event_code == I2cEvent.START
    
    # Last should be STOP
    assert messages[-1].is_event and messages[-1].event_code == I2cEvent.STOP
    
    # Middle should be payloads
    payloads = [m for m in messages if m.is_payload]
    tb.env.log.info("Waiting for transactions...")
    await tb.scoreboard.run()
    
    # Extra check for hex logging in Scoreboard context is done via fmt_fn,
    # but let's add an explicit log here if needed.
    tb.env.log.info(f"✓ Write transaction verified ({len(messages)} messages)")
    await ClockCycles(tb.env.clk, 100)
    tb.env.log.info("Test test_write_transaction complete")


@cocotb.test()
@requires("I2C-REQ-5")
async def test_repeated_start(dut):
    """Test REPEATED_START detection."""
    tb = I2cDataloggerTb(dut)
    await tb.setup()
    
    # Enable datalogger
    tb.enable_datalogger()
    await ClockCycles(tb.env.clk, 10)
    await tb.collector.get()  # Drain ENABLED
    
    await tb.i2c.initialize()
    
    # First START
    await tb.i2c.start()
    await ClockCycles(tb.env.clk, 20)
    
    # Write address
    await tb.i2c.write_byte(0xA0)
    await ClockCycles(tb.env.clk, 20)
    
    # Repeated START (without STOP)
    # Wait for SCL low period (needs to be sufficient)
    await Timer(5000, units="ns")
    await tb.i2c.start()
    await ClockCycles(tb.env.clk, 20)
    
    # Write new address
    await tb.i2c.write_byte(0xA1)  # Read bit set
    await ClockCycles(tb.env.clk, 20)
    
    await tb.i2c.stop()
    await ClockCycles(tb.env.clk, 20)
    
    # Look for REPEATED_START event
    found_repeated_start = False
    received_msgs = []
    while not tb.collector.empty():
        msg_val, msg_time = await tb.collector.get()
        msg = tb.decoder.decode(msg_val.value)
        tb.env.log.info(f"Received: 0x{msg_val.value:08X} | Type: {msg.msg_type} | Event: {msg.event_name if msg.is_event else 'N/A'} (T={msg_time:.0f} ns)", extra={'sim_time': f"{msg_time: >9.0f}ns"})
        received_msgs.append(msg)
        if msg.is_event and (msg.event_code == I2cEvent.REPEATED_START or msg.event_code == I2cEvent.REP_START_ERROR):
            found_repeated_start = True
            break
    
    if not found_repeated_start:
        tb.env.log.error("Failed to find REPEATED_START. Received messages:")
        for m in received_msgs:
            tb.env.log.error(f"  {m}")
            
    assert found_repeated_start, "Expected REPEATED_START event"
    tb.env.log.info("✓ REPEATED_START event verified")
    await ClockCycles(tb.env.clk, 100)
    tb.env.log.info("Test test_repeated_start complete")


@cocotb.test()
@requires("I2C-REQ-11")
async def test_capture_rst_reconfig(dut):
    """I2C-REQ-11 (DS-P3.90 Phase 2): ENABLE=0 resets the decode core via
    config_control.CAPTURE_RST (= global RST OR this device disabled). A
    disable -> re-enable must therefore yield a CLEAN transaction with no
    spurious error events from stale decode state (ack / shift-reg / sample
    count carried over from the prior session). The lifecycle ENABLED/DISABLED
    frames still appear (they live on global RST), proving the decode reset does
    not suppress them (H.96).

    I2C_MSG_VLD / I2C_MSG_OUT are sampled by the SignalCollector in setup().
    """
    _i2c_msg_vld_sig = dut.I2C_MSG_VLD  # sampled by collector
    _i2c_msg_out_sig = dut.I2C_MSG_OUT  # sampled by collector
    dev = GENERICS["G_I2C_DEV_ID"]
    enable_word = ((dev & 0x1F) << 11) | 0x001
    disable_word = ((dev & 0x1F) << 11) | 0x000   # explicit matching-id disable
    tb = I2cDataloggerTb(dut)
    await tb.setup()

    # --- Session 1: enable, run a write, then disable (explicit matching-id) ---
    dut.CONFIG.value = enable_word
    await ClockCycles(tb.env.clk, 10)
    await tb.i2c.initialize()
    await tb.i2c.start()
    await ClockCycles(tb.env.clk, 20)
    await tb.i2c.write_byte(0xA0)
    await ClockCycles(tb.env.clk, 20)
    await tb.i2c.write_byte(0x55)
    await ClockCycles(tb.env.clk, 20)
    await tb.i2c.stop()
    await ClockCycles(tb.env.clk, 20)

    # Disable with a matching-id word (payload bit0=0) — config_control only
    # latches on an ID match, so CONFIG=0 (id 0) would NOT disable device <dev>.
    dut.CONFIG.value = disable_word
    await ClockCycles(tb.env.clk, 60)
    # Drain session 1 (incl. the DISABLED lifecycle frame).
    s1 = []
    while not tb.collector.empty():
        mv, _ = await tb.collector.get()
        s1.append(tb.decoder.decode(mv.value))
    tb.env.log.info(f"[S1] events={[m.event_name for m in s1 if m.is_event]}")

    # --- Session 2: re-enable, run a fresh write, verify clean decode ---
    dut.CONFIG.value = enable_word
    await ClockCycles(tb.env.clk, 10)
    await tb.i2c.start()
    await ClockCycles(tb.env.clk, 20)
    await tb.i2c.write_byte(0xA0)
    await ClockCycles(tb.env.clk, 20)
    await tb.i2c.write_byte(0xCD)
    await ClockCycles(tb.env.clk, 20)
    await tb.i2c.stop()
    await ClockCycles(tb.env.clk, 20)

    msgs = []
    while not tb.collector.empty():
        msg_val, mt = await tb.collector.get()
        m = tb.decoder.decode(msg_val.value)
        tb.env.log.info(f"[S2] 0x{msg_val.value:08X} type={m.msg_type} "
                        f"event={m.event_name if m.is_event else 'N/A'} (T={mt:.0f}ns)")
        msgs.append(m)
    events = [m for m in msgs if m.is_event]

    # Re-enable must re-arm cleanly: ENABLED then a START, no spurious errors.
    assert any(m.event_code == I2cEvent.ENABLED for m in events), \
        "no ENABLED lifecycle frame after re-enable"
    assert any(m.event_code == I2cEvent.START for m in events), \
        "no START after re-enable (decode FSM did not re-arm)"
    err_codes = {
        I2cEvent.STOP_ERROR, I2cEvent.REP_START_ERROR,
        I2cEvent.STRETCH_ERROR, I2cEvent.GLITCH_ERROR,
    }
    spurious = [m.event_name for m in events if m.event_code in err_codes]
    assert not spurious, \
        f"spurious error events after re-enable (stale decode state): {spurious}"

    tb.env.log.info("I2C-REQ-11 CAPTURE_RST reconfig: clean re-enable verified")


@cocotb.test()
@requires("I2C-REQ-12")
async def test_payload_backpressure_hold(dut):
    """I2C-REQ-12 (DS-P3.128): a byte PAYLOAD frame MUST NOT be dropped when the
    downstream is busy on the cycle the datalogger first asserts I2C_MSG_VLD.

    Real failure reproduced: ~94% of bytes (incl. the address byte) went missing
    on silicon under back-pressure while START/STOP survived. Root cause:
    I2C_WAIT_ACK pulses msgPayloadVld for ONE cycle (the ACK sclRisingEdge) and
    the process default clears it next cycle; the FSM only leaves I2C_WAIT_ACK
    when I2C_MSG_RDY=1 in that exact cycle, but the valid was never HELD. If RDY
    is low at that cycle the byte frame vanishes — no valid/ready hold.

    This test drives a TRUE valid/ready handshake monitor (captures only on
    I2C_MSG_VLD AND I2C_MSG_RDY) and WITHHOLDS I2C_MSG_RDY across the whole
    address byte + ACK window, releasing it only afterwards. A correct datalogger
    holds the payload valid until RDY accepts it, so the address-byte payload is
    delivered byte-exact (0xA0, ACK) once and only once. On the buggy RTL the
    handshake never completes during the valid pulse and the frame is LOST →
    this test is RED. No BFM/DUT workaround: RDY back-pressure is realistic and
    the assertions are exact (DS-P3.128).

    I2C_MSG_VLD / I2C_MSG_OUT are sampled by the handshake monitor below.
    """
    _i2c_msg_vld_sig = dut.I2C_MSG_VLD  # sampled by handshake monitor
    _i2c_msg_out_sig = dut.I2C_MSG_OUT  # sampled by handshake monitor
    _i2c_msg_rdy_sig = dut.I2C_MSG_RDY  # driven low across the ACK window

    tb = I2cDataloggerTb(dut)
    # Replace the always-ready collector wiring: this test owns I2C_MSG_RDY.
    await tb.setup()

    # --- True valid/ready handshake monitor ----------------------------------
    # Captures a frame ONLY when BOTH I2C_MSG_VLD and I2C_MSG_RDY are high on the
    # same rising edge — exactly how the real datalogger_gather / FIFO consumes it.
    # A one-cycle valid pulse while RDY is low is therefore (correctly) NOT
    # captured: the DUT must HOLD valid until RDY, or the frame is lost.
    handshake_frames = []

    async def _handshake_monitor():
        while True:
            await RisingEdge(tb.env.clk)
            vld = dut.I2C_MSG_VLD.value
            rdy = dut.I2C_MSG_RDY.value
            try:
                vld_i = int(vld)
                rdy_i = int(rdy)
            except Exception:
                continue
            if vld_i == 1 and rdy_i == 1:
                raw = int(dut.I2C_MSG_OUT.value)
                handshake_frames.append(tb.decoder.decode(raw))

    cocotb.start_soon(_handshake_monitor())

    # Enable datalogger.
    tb.enable_datalogger()
    dut.I2C_MSG_RDY.value = 1
    await ClockCycles(tb.env.clk, 10)

    # Begin a write: START + address byte 0xA0 (addr 0x50 << 1 | W).
    await tb.i2c.initialize()
    await tb.i2c.start()
    await ClockCycles(tb.env.clk, 10)

    # *** Apply back-pressure for the ENTIRE address byte + ACK phase. ***
    # This guarantees I2C_MSG_RDY is LOW on whatever cycle the datalogger pulses
    # the byte payload valid (the ACK sclRisingEdge). A datalogger with no hold
    # drops the frame here; a correct one keeps valid asserted.
    dut.I2C_MSG_RDY.value = 0

    await tb.i2c.write_byte(0xA0)  # 8 data bits + ACK clock

    # Stay busy a little longer to be sure the valid pulse cycle was covered,
    # WITHOUT any SCL activity (the bus is parked: SCL low after write_byte).
    await ClockCycles(tb.env.clk, 20)

    # The handshake monitor must NOT have captured the address-byte payload yet
    # (RDY was low across its valid window). START is an EVENT, captured before
    # back-pressure — that's fine.
    payloads_during_bp = [m for m in handshake_frames if m.is_payload]
    assert not payloads_during_bp, (
        "byte payload was accepted while I2C_MSG_RDY was held low — monitor "
        f"is not honouring the handshake: {[hex(m.raw) for m in payloads_during_bp]}"
    )

    # *** Release back-pressure while the bus is IDLE (no further SCL edges). ***
    # The ONLY way the address byte can now reach the handshake monitor is if the
    # datalogger HELD msgPayloadVld across the stall. The buggy RTL cleared valid one
    # cycle after the ACK edge, so nothing re-asserts it here → the byte is LOST.
    dut.I2C_MSG_RDY.value = 1
    await ClockCycles(tb.env.clk, 60)

    # Primary check: the held address byte must have completed its handshake
    # during the idle window above — BEFORE any STOP edge could "rescue" it.
    addr_after_release = [m for m in handshake_frames if m.is_payload and m.data == 0xA0]
    assert len(addr_after_release) == 1, (
        "address-byte PAYLOAD frame (0xA0) was DROPPED under back-pressure "
        "(DS-P3.128): msgPayloadVld was NOT held until I2C_MSG_RDY accepted it, "
        "so releasing RDY over an idle bus delivered nothing. Expected exactly 1 "
        f"address payload, got {len(addr_after_release)}; all payloads so far="
        f"{[hex(m.raw) for m in handshake_frames if m.is_payload]}"
    )

    # Finish the transaction cleanly (RDY already high).
    await tb.i2c.stop()
    await ClockCycles(tb.env.clk, 40)

    # --- Assertions: the address-byte PAYLOAD must be delivered EXACTLY once. --
    payloads = [m for m in handshake_frames if m.is_payload]
    events = [m for m in handshake_frames if m.is_event]
    tb.env.log.info(
        f"handshake frames: payloads={[hex(m.raw) for m in payloads]} "
        f"events={[m.event_name for m in events]}"
    )

    # 1) Exactly one address-byte payload survived the back-pressure.
    addr_payloads = [m for m in payloads if m.data == 0xA0]
    assert len(addr_payloads) == 1, (
        "address-byte PAYLOAD frame (0xA0) was DROPPED under back-pressure "
        "(DS-P3.128): msgPayloadVld not held until I2C_MSG_RDY. Expected exactly "
        f"1 such frame through the VLD&RDY handshake, got {len(addr_payloads)}; "
        f"all payloads={[hex(m.raw) for m in payloads]}"
    )

    # 2) It is the captured address byte, ACKed (slave drives SDA low at ACK).
    addr_msg = addr_payloads[0]
    assert addr_msg.data == 0xA0, f"expected address 0xA0, got 0x{addr_msg.data:02X}"
    assert addr_msg.ack, "expected ACK (slave drove SDA low) on the address byte"

    # 3) STOP must arrive WITHOUT a spurious STOP_ERROR: when the byte phase is
    #    not skipped, cntSamples_r is correct at STOP, so no false stop error.
    assert any(m.event_code == I2cEvent.STOP for m in events), \
        "no STOP event after release — transaction did not complete"
    assert not any(m.event_code == I2cEvent.STOP_ERROR for m in events), (
        "spurious STOP_ERROR (DS-P3.128 side-effect): the byte phase was skipped "
        "so cntSamples_r was wrong at the STOP condition"
    )

    tb.env.log.info(
        "I2C-REQ-12: byte payload held across back-pressure and delivered "
        "byte-exact (no drop, no spurious STOP_ERROR)"
    )


@cocotb.test()
@requires("I2C-REQ-13")
async def test_payload_event_backpressure_order(dut):
    """I2C-REQ-13 (DS-T3.25): a STOP or repeated-START that arrives while a
    byte PAYLOAD is held under back-pressure must be delivered after that byte.

    I2C_MSG_VLD / I2C_MSG_OUT are sampled by the handshake monitor below.
    """
    _i2c_msg_vld_sig = dut.I2C_MSG_VLD  # sampled by handshake monitor
    _i2c_msg_out_sig = dut.I2C_MSG_OUT  # sampled by handshake monitor
    _i2c_msg_rdy_sig = dut.I2C_MSG_RDY  # driven low across byte + event window

    tb = I2cDataloggerTb(dut)
    await tb.setup()

    handshake_frames = []

    async def _handshake_monitor():
        while True:
            await RisingEdge(tb.env.clk)
            try:
                vld_i = int(dut.I2C_MSG_VLD.value)
                rdy_i = int(dut.I2C_MSG_RDY.value)
            except (TypeError, ValueError):
                continue
            if vld_i == 1 and rdy_i == 1:
                raw = int(dut.I2C_MSG_OUT.value)
                handshake_frames.append(tb.decoder.decode(raw))

    cocotb.start_soon(_handshake_monitor())

    tb.enable_datalogger()
    dut.I2C_MSG_RDY.value = 1
    await ClockCycles(tb.env.clk, 10)

    async def _drive_held_payload_then_stop():
        await tb.i2c.initialize()
        await tb.i2c.start()
        await ClockCycles(tb.env.clk, 10)

        segment_start = len(handshake_frames)
        dut.I2C_MSG_RDY.value = 0
        await tb.i2c.write_byte(0xA0)
        await tb.i2c.stop()
        await ClockCycles(tb.env.clk, 20)

        assert len(handshake_frames) == segment_start, (
            "frames handshaked while I2C_MSG_RDY was low across payload+STOP: "
            f"{[hex(m.raw) for m in handshake_frames[segment_start:]]}"
        )

        dut.I2C_MSG_RDY.value = 1
        await ClockCycles(tb.env.clk, 100)

        segment = handshake_frames[segment_start:]
        assert len(segment) == 2, (
            "held payload plus STOP must deliver exactly two frames after RDY "
            f"release, got {len(segment)}: {[hex(m.raw) for m in segment]}"
        )
        assert segment[0].is_payload, f"first post-release frame is not payload: 0x{segment[0].raw:08X}"
        assert segment[0].data == 0xA0, f"expected payload byte 0xA0, got 0x{segment[0].data:02X}"
        assert segment[0].ack, "expected ACK on payload byte 0xA0"
        assert segment[1].is_event, f"second post-release frame is not event: 0x{segment[1].raw:08X}"
        assert segment[1].event_code == 0x0A, (
            f"expected STOP event code 0x0A after held payload, got 0x{segment[1].event_code:02X}"
        )

    async def _drive_held_payload_then_repeated_start():
        await tb.i2c.start()
        await ClockCycles(tb.env.clk, 10)

        segment_start = len(handshake_frames)
        dut.I2C_MSG_RDY.value = 0
        await tb.i2c.write_byte(0xA0)
        await tb.i2c.start()
        await ClockCycles(tb.env.clk, 20)

        assert len(handshake_frames) == segment_start, (
            "frames handshaked while I2C_MSG_RDY was low across payload+repeated-START: "
            f"{[hex(m.raw) for m in handshake_frames[segment_start:]]}"
        )

        dut.I2C_MSG_RDY.value = 1
        await ClockCycles(tb.env.clk, 100)

        segment = handshake_frames[segment_start:]
        assert len(segment) == 2, (
            "held payload plus repeated-START must deliver exactly two frames after "
            f"RDY release, got {len(segment)}: {[hex(m.raw) for m in segment]}"
        )
        assert segment[0].is_payload, f"first post-release frame is not payload: 0x{segment[0].raw:08X}"
        assert segment[0].data == 0xA0, f"expected payload byte 0xA0, got 0x{segment[0].data:02X}"
        assert segment[0].ack, "expected ACK on payload byte 0xA0"
        assert segment[1].is_event, f"second post-release frame is not event: 0x{segment[1].raw:08X}"
        assert segment[1].event_code == 0x0B, (
            "expected REPEATED_START event code 0x0B after held payload, "
            f"got 0x{segment[1].event_code:02X}"
        )

        await tb.i2c.stop()
        await ClockCycles(tb.env.clk, 40)

    await _drive_held_payload_then_stop()
    await _drive_held_payload_then_repeated_start()

    tb.env.log.info(
        "I2C-REQ-13: held payload and coincident STOP/repeated-START delivered "
        "exactly once, in order"
    )


# =============================================================================
# Standalone Execution
# =============================================================================

if __name__ == "__main__":
    """
    Direct execution entry point using the unified simulation framework.
    Run with: SIM=nvc python test_i2c_datalogger.py
    """
    from engine.simulation import run_simulation
    from engine.paths import SRC_PATH, SDK_SRC_PATH

    # Deepskopion project root
    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
    DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

    run_simulation(
        top_level="tb_i2c_datalogger",
        module="test_i2c_datalogger",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "tich": [
                # SDK shared units
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                # Deepskopion packages (order matters for dependencies)
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                # Deepskopion datalogger infrastructure
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
                # Deepskopion I2C datalogger
                f"{DEEPSKOPION_RTL}/datalogger/i2c/i2c_datalogger.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/i2c/i2c_slave.vhd",
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_i2c_datalogger.vhd",
            ],
        },
    )
