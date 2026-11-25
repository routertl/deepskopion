# rr unknown-llm (AI-generated)
# tb/datalogger/i2c/config.py
"""
I2C Datalogger Configuration Helpers

CONFIG register format (16 bits):
  [15:11] device_id  (5 bits) - Target device ID
  [10:1]  reserved   (10 bits)
  [0]     enable     (1 bit)  - Datalogger enable
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class I2cDataloggerCfg:
    """I2C Datalogger Configuration"""
    device_id: int = 28
    enable: bool = True
    
    def __post_init__(self):
        if not 0 <= self.device_id < 32:
            raise ValueError(f"device_id must be 0-31, got {self.device_id}")


def pack_config(cfg: I2cDataloggerCfg) -> int:
    """Pack I2cDataloggerCfg into a 16-bit register value."""
    val = 0
    val |= (cfg.device_id & 0x1F) << 11
    val |= int(cfg.enable) & 0x01
    return val


def unpack_config(val: int) -> I2cDataloggerCfg:
    """Unpack a 16-bit register value into I2cDataloggerCfg."""
    return I2cDataloggerCfg(
        device_id=(val >> 11) & 0x1F,
        enable=bool(val & 0x01),
    )
