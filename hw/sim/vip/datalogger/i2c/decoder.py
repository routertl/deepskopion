# tb/datalogger/i2c/decoder.py
"""
I2C Datalogger Message Decoder

Parses 32-bit messages from the i2c_datalogger RTL module.

Message format (32 bits):
  [31:27] device_id  (5 bits)
  [26:24] type       (3 bits) - 0b100=event, 0b00x=payload
  [23:8]  timestamp  (16 bits)
  [7:0]   data       (8 bits) - event_code or payload data
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum


class I2cEvent(IntEnum):
    """I2C Datalogger Event Codes"""
    DISABLED = 0x00
    ENABLED = 0x03
    STOP_ERROR = 0x05
    STRETCH_ERROR = 0x06
    REP_START_ERROR = 0x07
    GLITCH_ERROR = 0x08
    START = 0x09
    STOP = 0x0A
    REPEATED_START = 0x0B


# Type identifiers
TYPE_EVENT = 0b100
TYPE_PAYLOAD_ACK = 0b000
TYPE_PAYLOAD_NACK = 0b001


@dataclass(frozen=True, slots=True)
class I2cMessage:
    """Decoded I2C datalogger message"""
    raw: int
    device_id: int
    msg_type: int
    timestamp: int
    data: int
    
    @property
    def is_event(self) -> bool:
        return self.msg_type == TYPE_EVENT
    
    @property
    def is_payload(self) -> bool:
        return (self.msg_type & 0b110) == 0b000
    
    @property
    def ack(self) -> bool:
        """Returns True if ACK (0), False if NACK (1). Only valid for payloads."""
        return (self.msg_type & 0b001) == 0
    
    @property
    def event_code(self) -> int:
        """Returns event code. Only valid for events."""
        return self.data
    
    @property
    def event_name(self) -> str:
        """Returns human-readable event name."""
        try:
            return I2cEvent(self.data).name
        except ValueError:
            return f"UNKNOWN(0x{self.data:02X})"


class I2cMsgDecoder:
    """
    Decoder for I2C datalogger messages.
    
    Usage:
        decoder = I2cMsgDecoder(device_id=28)
        msg = decoder.decode(raw_value)
        if msg.is_event:
            print(f"Event: {msg.event_name}")
        else:
            print(f"Data: 0x{msg.data:02X} ACK={msg.ack}")
    """
    
    def __init__(self, device_id: int = 28, id_width: int = 5):
        self.expected_device_id = device_id
        self.id_width = id_width
    
    def decode(self, raw: int) -> I2cMessage:
        """Decode a 32-bit raw message value."""
        # Extract fields
        device_id = (raw >> 27) & ((1 << self.id_width) - 1)
        msg_type = (raw >> 24) & 0b111
        timestamp = (raw >> 8) & 0xFFFF
        data = raw & 0xFF
        
        return I2cMessage(
            raw=raw,
            device_id=device_id,
            msg_type=msg_type,
            timestamp=timestamp,
            data=data,
        )
    
    def decode_check_id(self, raw: int) -> I2cMessage:
        """Decode and verify device ID matches expected."""
        msg = self.decode(raw)
        if msg.device_id != self.expected_device_id:
            raise ValueError(
                f"Device ID mismatch: got {msg.device_id}, expected {self.expected_device_id}"
            )
        return msg
    
    @staticmethod
    def format_message(msg: I2cMessage) -> str:
        """Format message for logging."""
        if msg.is_event:
            return f"EVENT[{msg.event_name}] ts={msg.timestamp}"
        else:
            ack_str = "ACK" if msg.ack else "NACK"
            return f"PAYLOAD[0x{msg.data:02X}] {ack_str} ts={msg.timestamp}"
