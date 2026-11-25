-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : config_control
-- Project    : datalogger
-------------------------------------------------------------------------------
-- File       : config_control.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    :
-- Created    : 2025-06-30
-- Last update: 2025-06-30
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: Config Control. Manages the configuration and control signals
-------------------------------------------------------------------------------
-- Copyright (c) 2025 RouteRTL
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2025-02-11  1.0      dmazure Created
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use IEEE.MATH_REAL.all;
use ieee.std_logic_misc.all;

library tich;
use tich.datalogger_pkg.all;

entity config_control is
    generic (
      G_DEV_ID_WIDTH : integer := 5;
      -- DS-P2.35: range constraint dropped — `range 0 to (2**G_DEV_ID_WIDTH)-1`
      -- references an earlier generic in its own type and segfaults the
      -- Vivado 2024.1 elaborator (DFNode::setLink crash in
      -- adjustIndex). Same user-warned pattern as rr-ddr-stream's
      -- ddr_base_addr_g. Width-bound validity is enforced by the
      -- caller; range constraint here was advisory.
      G_DEV_ID       : natural := 24;
      G_CONFIG_WIDTH : integer                                  := 16;
      G_SIM_EN       : integer                                  := 0; -- 1 for simulation, 0 for synthesis
      G_ILA_EN       : integer                                  := 1;
      -- Optional config BANKING (DS-P3.108b). When G_BANK_EN=1 this instance only
      -- latches a CONFIG write whose bank bit (CONFIG[G_BANK_BIT]) equals G_BANK_VAL,
      -- so two instances on the SAME device_id can latch two independent config
      -- words selected by that bit — one id, banked config (preserves one-id-one-
      -- datalogger). Default G_BANK_EN=0 ⇒ legacy behaviour (latch any id match).
      G_BANK_EN      : integer                                  := 0;
      G_BANK_BIT     : integer                                  := 10;  -- payload MSB by default
      G_BANK_VAL     : std_logic                                := '0'
      );
    port (
      CLK         : in  std_logic;
      RST         : in  std_logic;
      CONFIG      : in  std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
      -- LEVEL
      CONFIG_OUT  : out std_logic_vector(G_CONFIG_WIDTH-G_DEV_ID_WIDTH-1 downto 0);
      -- Per-datalogger reset (DS-T2.31). Asserted under global RST OR while this
      -- device is disabled (latched ENABLE bit = 0). The owning datalogger uses it
      -- as its synchronous reset, so writing ENABLE=0 from SW flushes ALL of
      -- THIS device's calibration / FIFO / BRAM state — a clean, SW-reachable,
      -- per-datalogger reset. Re-asserting ENABLE=1 (with the new format) then
      -- begins a fresh calibration with no stale traces. Contract reset path;
      -- see DATALOGGER_CONFIG in project_regbank.yml.
      CAPTURE_RST   : out std_logic
      );
end config_control;

-- The config bus is not guaranteed to come back to 0 in the next cycle. This module will latch the configuration will apply it until the same ID is issued again.
architecture rtl of config_control is

  signal config_r           : std_logic_vector(G_CONFIG_WIDTH-1 downto 0)                := (others => '0');
  signal devConfiguration_r : std_logic_vector(G_CONFIG_WIDTH-G_DEV_ID_WIDTH-1 downto 0) := (others => '0');
  signal deviceIdMatch_r    : std_logic                                                  := '0';
  signal newConfEvent_r     : std_logic                                                  := '0';

  -- Device-id field of a raw CONFIG word.
  function f_dev_id(cfg : std_logic_vector) return std_logic_vector is
  begin
    return cfg(G_CONFIG_WIDTH-1 downto G_CONFIG_WIDTH-G_DEV_ID_WIDTH);
  end function;

begin

  -- DS-P3.96: both latch terms are derived from the SAME registered CONFIG
  -- sample inside this clocked process. Previously deviceIdMatch came from a
  -- CONCURRENT signal (one delta behind CONFIG) while newConfEvent compared the
  -- direct CONFIG input — so a CONFIG step that landed edge-aligned to the clock
  -- skewed deviceIdMatch_r vs newConfEvent_r by a delta, they never coincided,
  -- and the enable SILENTLY failed to latch (DS-P3.32 / hint H.106). Computing
  -- the match in-process from the same CONFIG read removes the skew entirely;
  -- the 2-cycle latch timing is unchanged for the clean (register-driven) case.
  REG_CONFIG: process(CLK)
  begin
    if rising_edge(CLK) then
      config_r        <= CONFIG;
      deviceIdMatch_r <= '0';
      -- id match AND (banking off OR the bank bit selects this instance). Both
      -- terms read the SAME CONFIG sample as newConfEvent (DS-P3.96 skew fix).
      if to_unsigned(G_DEV_ID, G_DEV_ID_WIDTH) = unsigned(f_dev_id(CONFIG))
         and (G_BANK_EN = 0 or CONFIG(G_BANK_BIT) = G_BANK_VAL) then
        deviceIdMatch_r <= '1';
      end if;
      newConfEvent_r  <= '0';
      if CONFIG /= config_r then
        newConfEvent_r <= '1';
      end if;
      if deviceIdMatch_r = '1' and newConfEvent_r = '1' then
        devConfiguration_r <= config_r(G_CONFIG_WIDTH-G_DEV_ID_WIDTH-1 downto 0);
      end if;
      if RST = '1' then
        devConfiguration_r <= (others => '0');
      end if;
    end if;
  end process;

  CONFIG_OUT <= devConfiguration_r;

  -- Bit 0 of the latched per-device config is ENABLE. Hold the datalogger in reset
  -- whenever it is disabled or the whole block is under global reset.
  CAPTURE_RST  <= RST or (not devConfiguration_r(0));

end rtl;