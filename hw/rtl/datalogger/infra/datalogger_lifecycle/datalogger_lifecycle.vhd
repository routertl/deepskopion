-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : datalogger_lifecycle
-- Project    : datalogger
-------------------------------------------------------------------------------
-- File       : datalogger_lifecycle.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    : RouteRTL
-------------------------------------------------------------------------------
-- Description: Uniform per-datalogger lifecycle event emitter (DS-P3.90 Phase 1).
--
--   Emits a SYMMETRIC pair of status frames on the per-device ENABLE edge:
--     * ENABLED  on the rising edge of CONFIG_EN  (datalogger just armed)
--     * DISABLED on the falling edge of CONFIG_EN (datalogger just disarmed)
--
--   It lives in the ALWAYS-ON (global RST) domain — deliberately NOT gated by
--   the per-datalogger CAPTURE_RST decode reset. That separation is the whole point
--   (DS-T2.31 / H.96): if the lifecycle lived inside the decode core, asserting
--   CAPTURE_RST while disabled would suppress the DISABLED frame. Keeping it here
--   lets every datalogger (a) report a uniform enable/disable lifecycle AND (b)
--   adopt CAPTURE_RST on its decode core without losing the markers.
--
--   The frame LAYOUT is the datalogger's existing one, passed in via generics, so
--   adoption is backward-compatible per protocol:
--     MSG_OUT = DEV_ID & G_TYPE & TIMESTAMP & code   (= C_DATALOGGER_MSG_WIDTH)
--   where code is G_CODE_ENABLED / G_CODE_DISABLED.
-------------------------------------------------------------------------------
-- Copyright (c) Daniel J. Mazure 2026
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_pkg.all;

entity datalogger_lifecycle is
  generic (
    G_DEV_ID         : natural          := 0;
    G_DEV_ID_WIDTH   : integer          := 5;
    G_TYPE           : std_logic_vector := "100";   -- event-type field
    G_TS_WIDTH       : integer          := 16;       -- timestamp slice width
    G_CODE_ENABLED   : std_logic_vector := x"03";    -- per-protocol ENABLED code
    G_CODE_DISABLED  : std_logic_vector := x"00"     -- per-protocol DISABLED code
    );
  port (
    CLK       : in  std_logic;
    RST       : in  std_logic;                                  -- GLOBAL reset
    CONFIG_EN : in  std_logic;                                  -- latched ENABLE bit
    TIMESTAMP : in  std_logic_vector(G_TS_WIDTH-1 downto 0);
    MSG_OUT   : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
    MSG_VLD   : out std_logic := '0'
    );
end datalogger_lifecycle;

architecture rtl of datalogger_lifecycle is
  constant C_DEV : std_logic_vector(G_DEV_ID_WIDTH-1 downto 0)
                 := std_logic_vector(to_unsigned(G_DEV_ID, G_DEV_ID_WIDTH));
  signal en_r  : std_logic := '0';
  signal msg_r : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
  signal vld_r : std_logic := '0';
begin

  -- Elaboration-time width check: the assembled frame must be exactly one
  -- datalogger message wide. Catches a mis-sized generic at the instantiation.
  assert (G_DEV_ID_WIDTH + G_TYPE'length + G_TS_WIDTH + G_CODE_ENABLED'length)
         = C_DATALOGGER_MSG_WIDTH
    report "datalogger_lifecycle: DEV_ID+TYPE+TS+CODE width /= C_DATALOGGER_MSG_WIDTH"
    severity failure;

  process(CLK)
  begin
    if rising_edge(CLK) then
      en_r  <= CONFIG_EN;
      vld_r <= '0';
      if CONFIG_EN = '1' and en_r = '0' then          -- rising edge -> ENABLED
        msg_r <= C_DEV & G_TYPE & TIMESTAMP & G_CODE_ENABLED;
        vld_r <= '1';
      elsif CONFIG_EN = '0' and en_r = '1' then        -- falling edge -> DISABLED
        msg_r <= C_DEV & G_TYPE & TIMESTAMP & G_CODE_DISABLED;
        vld_r <= '1';
      end if;
      if RST = '1' then
        en_r  <= '0';
        vld_r <= '0';
      end if;
    end if;
  end process;

  MSG_OUT <= msg_r;
  MSG_VLD <= vld_r;

end rtl;
