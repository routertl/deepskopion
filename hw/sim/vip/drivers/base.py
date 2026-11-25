# rr unknown-llm (AI-generated)
# tb/drivers.py (excerpt)
from __future__ import annotations
from dataclasses import asdict, dataclass
import cocotb
from cocotb.triggers import RisingEdge, ClockCycles, Timer
from hw.sim.vip.datalogger.uart.config import DataloggerCfg, pack_config

# Map signals names to internals
@dataclass(frozen=True, slots=True)
class DataloggerInputs:
    clk       : str
    rst       : str
    config    : str
    timestamp : str
    ready     : str

__all__ = ["DataloggerInputs", "DataloggerDriver"]


class DataloggerDriver:
    def __init__(self, env, dut, inputs: DataloggerInputs):
        self.log        = env.log
        self.clk        = getattr(dut, inputs.clk)
        self.rst        = getattr(dut, inputs.rst)
        self.config_bus = getattr(dut, inputs.config)
        self.timestamp  = getattr(dut, inputs.timestamp)
        self.ready      = getattr(dut, inputs.ready)

    async def initialize(self, cfg_value: int | None = None):
        """Legacy reset; optionally slam a raw CONFIG value now."""
        await RisingEdge(self.clk)
        self.rst.value = 1
        self.config_bus.value = 0
        self.timestamp.value = 0
        self.ready.value = 0
        await ClockCycles(self.clk, 5)

        self.rst.value = 0
        self.ready.value = 1
        self.log.info("Datalogger released from reset")

    async def apply_config(self, cfg: DataloggerCfg, hold_cycles: int = 20):
        """Pack and apply the structured datalogger config."""
        val = pack_config(cfg)

        # Drive on a clock edge, hold N cycles, then clear
        await RisingEdge(self.clk)
        self.config_bus.value = val
        await ClockCycles(self.clk, hold_cycles)
        self.config_bus.value = 0

        # Nicer logging if DataloggerCfg is a dataclass; otherwise fallback
        try:
            fields = asdict(cfg)
            field_str = ", ".join(f"{k}={v}" for k, v in fields.items())
        except TypeError:
            # not a dataclass
            field_str = ", ".join(
                f"{k}={getattr(cfg, k)}"
                for k in dir(cfg)
                if not k.startswith("_") and not callable(getattr(cfg, k, None))
            )

        self.log.info("Applied DataloggerCfg: %s (0x%04X)", field_str, val)

    async def timestamp_gen(self, ts: int):
        """Continuously increment TIMESTAMP every 1us."""
        while True:
            await Timer(1, units="us")
            await RisingEdge(self.clk)
            ts = (ts + 1) & 0xFFFF
            if ts == 0xFFFF:
                ts = 0
            self.timestamp.value = ts

    async def start_timestamp_gen(self, ts: int = 0):
        """Start the timestamp generator coroutine."""
        cocotb.start_soon(self.timestamp_gen(ts=ts))
        self.log.info("Started timestamp generator with initial value %d", ts)
