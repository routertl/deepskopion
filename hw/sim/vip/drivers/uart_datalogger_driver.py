# rr unknown-llm (AI-generated)
from cocotb.handle import SimHandleBase
from rr_cocotb_tb.env import TbEnv
from hw.sim.vip.drivers.base import DataloggerDriver, DataloggerInputs

class UartDriver(DataloggerDriver):
    def __init__(self, env: TbEnv, dut: SimHandleBase, inputs: DataloggerInputs, **kwargs):
        super().__init__(env, dut, inputs, **kwargs)
