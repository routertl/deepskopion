-------------------------------------------------------------------------------
-- Title      : uart_playback.vhd
-- Project    : deepskopion
-------------------------------------------------------------------------------
-- File       : uart_datalogger.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    : RouteRTL
-- Created    : 2025-11-05
-- Last update: 2025-11-05
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: UART Datalogger. Reproduces a stored
--              sequence of UART frames based on distance input.
-------------------------------------------------------------------------------
-- Copyright (c) Daniel J. Mazure 2025
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2025-11-11  1.0      dmazure Created
-------------------------------------------------------------------------------


library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;

library tich;
use tich.uart_pkg.all;

entity uart_playback is
  generic (
    G_DIST_WIDTH : integer := 16;
    G_FIFO_DEPTH : integer := 8;
    G_TIME_EN    : integer := 0
    );
  port (
    CLK       : in  std_logic;
    RST       : in  std_logic;
    ENABLE    : in  std_logic;
    PB_CLK    : in  std_logic; -- Sample tick used to pack the distances
    TIMESTAMP : in  std_logic_vector(15 downto 0);
    LEVEL     : in  std_logic;
    DISTANCE  : in  std_logic_vector(G_DIST_WIDTH-1 downto 0);
    VLD       : in  std_logic;
    EMPTY     : out std_logic;
    FULL      : out std_logic;
    WAVE      : out t_wave
    );
end uart_playback;

architecture rtl of uart_playback is

  ------------------------------------------------------
  -- Constants
  ------------------------------------------------------

  ------------------------------------------------------
  -- types
  ------------------------------------------------------
  type t_fsm_playback is (IDLE, LOAD_0, LOAD_1, PLAYBACK);

  ------------------------------------------------------
  -- Signals
  ------------------------------------------------------
  signal fsmPlayBack_r    : t_fsm_playback := IDLE;
  signal fifoPbWrEn       : std_logic;
  signal fifoPbRdEn_r     : std_logic      := '0';
  signal fifoPbDin        : std_logic_vector(G_TIME_EN * C_TIMESTAMP_WIDTH + 1 + G_DIST_WIDTH-1 downto 0);
  signal fifoPbDout       : std_logic_vector(G_TIME_EN * C_TIMESTAMP_WIDTH + 1 + G_DIST_WIDTH-1 downto 0);
  signal fifoPbFull       : std_logic;
  signal fifoPbAlmostFull : std_logic;
  signal fifoPbEmpty      : std_logic;
  signal fifoPbRstBusy    : std_logic;
  -- signal fifoPbDataCnt    : std_logic_vector(G_FIFO_DEPTH-1 downto 0); -- Not used

  signal level_r          : std_logic := '0';
  signal distance_r       : std_logic_vector(G_DIST_WIDTH-1 downto 0) := (others => '0');

  signal timeStampOut     : std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
  signal distanceOut      : unsigned(G_DIST_WIDTH-1 downto 0);
  signal levelOut         : std_logic;
  signal cntDistance_r    : unsigned(G_DIST_WIDTH-1 downto 0) := (others => '0');
  --
  signal pbTick           : std_logic;
  signal idle_r           : std_logic := '1';

begin

  fifoPbWrEn <= VLD;

  GEN_FIFO_IN : if G_TIME_EN > 0 generate
    fifoPbDin    <= TIMESTAMP & LEVEL & DISTANCE;
    timeStampOut <= fifoPbDout(G_TIME_EN * C_TIMESTAMP_WIDTH + G_DIST_WIDTH downto G_DIST_WIDTH + 1);
  else generate
    fifoPbDin    <= LEVEL & DISTANCE;
    timeStampOut <= (others => '0');
  end generate GEN_FIFO_IN;

  -- Use the distances that are issued as requests. Load them in a FIFO
  i_fifo_buffer : entity tich.fifo_sync
  generic map
  (
    G_FIFO_DEPTH  => C_FIFO_DEPTH,
    G_FIFO_WIDTH  => G_TIME_EN * C_TIMESTAMP_WIDTH + 1 + G_DIST_WIDTH,
    G_FIFO_FWFT   => 0,
    G_CALLER_NAME => "UART Datalogger Playback"
  )
  port map
  (
    RST       => RST,
    CLK       => CLK,
    DIN       => fifoPbDin,
    WR_EN     => fifoPbWrEn,
    RD_EN     => fifoPbRdEn_r,
    DOUT      => fifoPbDout,
    FULL      => fifoPbFull,
    PROG_FULL => fifoPbAlmostFull,
    EMPTY     => fifoPbEmpty,
    DATA_CNT  => open, -- fifoPbDataCnt,
    RST_BUSY  => fifoPbRstBusy
  );

  distanceOut <= unsigned(fifoPbDout(G_DIST_WIDTH-1 downto 0));
  levelOut    <= fifoPbDout(G_DIST_WIDTH);
  pbTick      <= PB_CLK;

  pr_playback : process(CLK)
  begin
    if rising_edge(CLK) then
      fifoPbRdEn_r  <= '0'; -- default
      cntDistance_r <= cntDistance_r;
      idle_r        <= '0';
      case fsmPlayBack_r is
        when IDLE =>
          idle_r  <= '1';
          if fifoPbEmpty = '0' and ENABLE = '1' then
            fsmPlayBack_r <= LOAD_0;
          end if;
        when LOAD_0 =>
          fifoPbRdEn_r  <= '1';
          fsmPlayBack_r <= LOAD_1;
          cntDistance_r <= (others => '0');
          if fifoPbEmpty = '1' or ENABLE = '0' then
            fsmPlayBack_r <= IDLE;
            fifoPbRdEn_r  <= '0';
          end if;
        when LOAD_1 =>
          fsmPlayBack_r <= PLAYBACK;
        when PLAYBACK =>
          level_r <= levelOut;
          if pbTick = '1' then
            cntDistance_r <= cntDistance_r + 1;
            -- DS-P2.236: an interval of distance d lasts exactly d clocks:
            -- LOAD_0 + LOAD_1 + (d - 2) PLAYBACK clocks. Leaving at
            -- cntDistance_r = d - 2 replayed d + 1, so a rebuilt frame grew by
            -- one clock per edge and an edge-dense frame lost its stop bit at
            -- 1 Mbaud. The compare is widened so d <= 3 (a glitch) replays the
            -- 3-clock minimum instead of wrapping.
            if resize(cntDistance_r, G_DIST_WIDTH + 1) + 3 >= resize(distanceOut, G_DIST_WIDTH + 1) then
              fsmPlayBack_r <= LOAD_0;
            end if;
          end if;
        when others =>
          fsmPlayBack_r <= IDLE;
      end case;
      if RST = '1' then
        fsmPlayBack_r <= IDLE;
        level_r       <= '1';
      end if;
    end if;
  end process;

  WAVE.level     <= level_r;
  WAVE.timestamp <= timeStampOut;
  EMPTY          <= idle_r;
  FULL           <= fifoPbAlmostFull;

end rtl;
