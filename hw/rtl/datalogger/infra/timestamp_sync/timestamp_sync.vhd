-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : timestamp_sync
-- Project    : datalogger
-------------------------------------------------------------------------------
-- File       : timestamp_sync.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    :
-- Created    : 2025-06-18
-- Last update: 2025-06-18
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: Sends messages with the upper part of the timestamp, every 16ms
-------------------------------------------------------------------------------
-- Copyright (c) 2025 RouteRTL
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2025-09-25  1.0      dmazure Created
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use IEEE.MATH_REAL.all;
use ieee.std_logic_misc.all;

library tich;
use tich.datalogger_pkg.all;


entity timestamp_sync is
  generic (
    G_TS_ID_WIDTH      : integer                                     := 5;
    G_TS_DEV_ID        : natural range 0 to (2 ** G_TS_ID_WIDTH) - 1 := 28;
    G_TIMESTAMP_WIDTH  : natural range 16 to 32                      := 32;
    G_CONFIG_WIDTH     : integer                                     := 16;
    G_TIMESTAMP_BIT    : integer                                     := 14; -- 16mstick
    G_TIMESTAMP_SLICE  : integer                                     := 18;
    G_TS_ERROR_WIDTH   : integer                                     := 1
  );
  port (
    CLK        : in std_logic;
    RST        : in std_logic;
    CONFIG     : in std_logic_vector(G_CONFIG_WIDTH - 1 downto 0);
    TIMESTAMP  : in std_logic_vector(G_TIMESTAMP_WIDTH - 1 downto 0);
    -- TS
    TS_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0) := (others => '0');
    TS_MSG_VLD : out std_logic;
    TS_MSG_RDY : in std_logic;
    TS_ERRORS  : out std_logic_vector(G_TS_ERROR_WIDTH-1 downto 0)
    --
  );
end timestamp_sync;


architecture behavioral of timestamp_sync is

  ---------------------------------------------------------------------------
  -- Constants
  ---------------------------------------------------------------------------
  constant C_ID_EXTENSION         : std_logic_vector(1 downto 0) := "00"; -- DJM: 02/09/2025, ID 31 is now shared
  constant C_PAYLOAD_CODE         : std_logic_vector(2 downto 0) := "100";
  constant C_INTERNAL_DEVID_WIDTH : natural := G_TS_ID_WIDTH + C_ID_EXTENSION'length;
  constant C_INTERNAL_DEVID       : natural := to_integer(to_unsigned(G_TS_DEV_ID, G_TS_ID_WIDTH) & unsigned(C_ID_EXTENSION));
  constant C_RESERVED_BITS        : std_logic_vector(C_DATALOGGER_MSG_WIDTH - G_TIMESTAMP_SLICE - 8 - 1 downto 0) := (others => '0');

  ----------------------------------------------------------------------------
  -- Signals
  ----------------------------------------------------------------------------
  signal deviceLoadConfig : std_logic_vector(G_CONFIG_WIDTH-C_INTERNAL_DEVID_WIDTH-1 downto 0);
  signal configEnable     : std_logic;
  signal timesyncEdge     : std_logic;
  --
  signal msgHeader      : std_logic_vector(G_TS_ID_WIDTH + C_PAYLOAD_CODE'length - 1 downto 0);
  signal tsMsgVld_r     : std_logic := '0';
  signal tsMsg_r        : std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0) := (others => '0');
  signal tsMsgPending_r : std_logic := '0';
  signal tsMsgError_r   : std_logic_vector(G_TS_ERROR_WIDTH-1 downto 0) := (others => '0');

begin


  -- Register the configuration values
  i_config_control : entity tich.config_control
  generic map (
    G_DEV_ID_WIDTH => C_INTERNAL_DEVID_WIDTH,
    G_DEV_ID       => C_INTERNAL_DEVID,
    G_CONFIG_WIDTH => G_CONFIG_WIDTH
    )
  port map (
    CLK        => CLK,
    RST        => RST,
    CONFIG     => CONFIG,
    CONFIG_OUT => deviceLoadConfig,
      CAPTURE_RST  => open  -- DS-T2.31: per-datalogger reset not yet adopted here
    );

    --
    configEnable <= deviceLoadConfig(0);

  i_timebit_rising_edge : entity tich.edge_detector
  generic map (
    G_EDGE_TYPE => C_EDGE_TYPE_BOTH
    )
  port map (
    CLK => CLK,
    CE  => '1',
    D   => TIMESTAMP(G_TIMESTAMP_BIT),
    Q   => timesyncEdge
    );

    -- alias
    msgHeader <= std_logic_vector(to_unsigned(G_TS_DEV_ID, G_TS_ID_WIDTH)) & C_PAYLOAD_CODE;

    process(CLK)
    begin
      if rising_edge(CLK) then
        tsMsgVld_r   <= '0';
        tsMsgError_r <= (others => '0');
        if configEnable = '1' then
          if timesyncEdge = '1' then
            tsMsg_r    <= msgHeader & TIMESTAMP(G_TIMESTAMP_WIDTH - 1 downto G_TIMESTAMP_WIDTH - G_TIMESTAMP_SLICE) & C_RESERVED_BITS;
            tsMsgPending_r <= '1';
            if tsMsgPending_r = '1' then
              tsMsgError_r(0) <= '1';
            end if;
          end if;
          -- holds the value until ready
          if tsMsgPending_r = '1' and TS_MSG_RDY = '1' then
            tsMsgVld_r     <= '1';
            tsMsgPending_r <= '0';
          end if;
        end if;
        if RST = '1' then
          tsMsgPending_r <= '0';
        end if;
      end if;
    end process;

    TS_MSG_OUT <= tsMsg_r;
    TS_MSG_VLD <= tsMsgVld_r;

    TS_ERRORS <= tsMsgError_r;

end behavioral;