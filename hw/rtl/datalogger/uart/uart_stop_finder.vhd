-------------------------------------------------------------------------------
-- Title      : uart_stop_finder.vhd
-- Project    : deepskopion
-------------------------------------------------------------------------------
-- File       : uart_stop_finder.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    : RouteRTL
-- Created    : 2025-11-05
-- Last update: 2025-11-05
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: uart_stop_finder, generates a frame from sampling the wave
--              with the input baudrate
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
use tich.datalogger_pkg.all;
use tich.uart_pkg.all;


entity uart_stop_finder is
  generic (
    G_CONF_NBITS  : integer  := 8;
    G_DIST_WIDTH  : integer  := 16;
    G_MIN_MATCHES : integer := 16
    );
  port (
    CLK       : in  std_logic;
    RST       : in  std_logic;
    ENABLE    : in  std_logic;
    BR_CYCLES : in  std_logic_vector(G_DIST_WIDTH-1 downto 0);
    WAVE      : in  t_wave;
    -- DS-P2.242: '1' while the replayed level is recorded data. After the
    -- playback has drained, WAVE holds its last level, which can match a
    -- format but must not refute one.
    JUDGE     : in  std_logic;
    MATCH     : out std_logic; -- STOP bit is found for the given G_CONF_BITS, and no frame refuted it since (DS-P2.242)
    PARITY    : out std_logic_vector(1 downto 0);
    STOP_BITS : out std_logic
    );
end uart_stop_finder;


architecture rtl of uart_stop_finder is

  constant C_CNT_MATCHES_WIDTH : integer := integer(ceil(log2(real(G_MIN_MATCHES ))))+1;

  type t_fsm_uart_frame_gen is (WAIT_START, SAMPLE_START_BIT, CHECK_START, SAMPLE_BITS, WAIT_STOP, CHECK_STOP, RESOLVE_PARITY, CHECK_PARITY, NOT_A_MATCH);

  signal fsmUartFrameGen_r : t_fsm_uart_frame_gen := WAIT_START;

  signal baudRateSamplingEdge : std_logic;
  signal uartSamples_r        : std_logic_vector(G_CONF_NBITS-1 downto 0) := (others => '1');
  signal waitStart_r          : std_logic := '1';
  signal startDetected        : std_logic;
  signal bitCounter_r         : unsigned(G_CONF_NBITS-1 downto 0) := (others => '0');
  signal onesCounter_r        : unsigned(G_CONF_NBITS-1 downto 0) := (others => '0');
  signal parityBit_r          : std_logic := '0';
  signal stopBit_r            : std_logic := '0';
  signal stopMatch_r          : std_logic := '0';
  signal isEvenParity_r       : std_logic := '0';
  signal isEvenParityPrev_r   : std_logic := '0';
  signal firstParityCheck_r   : std_logic := '1';
  signal parityFail_r         : std_logic := '0';
  signal matchStop            : std_logic;
  signal startBit_r           : std_logic := '1';
  signal cntMatches_r         : unsigned(C_CNT_MATCHES_WIDTH-1 downto 0) := (others => '0');
  signal stopBitsConf_r       : std_logic := '0';
  signal cntStopCycles_r      : unsigned(G_DIST_WIDTH-1 downto 0) := (others => '0');
  signal refuted_r            : std_logic := '0'; -- DS-P2.242: a frame contradicted this format

begin

  -- Detect the START edge on the UART samples
  i_start_event : entity tich.edge_detector
    generic map(
      G_EDGE_TYPE => C_EDGE_TYPE_FALLING
    )
    port map
    (
      CLK => CLK,
      CE  => waitStart_r, -- Turn off when start detected
      D   => WAVE.level,
      Q   => StartDetected
    );

  i_uart_baudrate_gen : entity tich.uart_baudrate_gen
    generic map (
      G_DIST_WIDTH => G_DIST_WIDTH
    )
    port map (
      CLK       => CLK,
      RST       => RST,
      ENABLE    => not waitStart_r,
      PAUSE     => '0',
      BR_CYCLES => BR_CYCLES,
      TICK      => baudRateSamplingEdge
    );

  process(CLK)
  begin
    if rising_edge(CLK) then
      case fsmUartFrameGen_r is
        when WAIT_START =>
          waitStart_r     <= '1';
          onesCounter_r   <= (others => '0');
          cntStopCycles_r <= cntStopCycles_r + 1;
          if startDetected = '1' then
            fsmUartFrameGen_r <= SAMPLE_START_BIT;
            waitStart_r       <= '0';
            cntStopCycles_r   <= (others => '0');
            if cntStopCycles_r < unsigned(BR_CYCLES) + unsigned(BR_CYCLES(BR_CYCLES'length-1 downto 1)) then -- less than 1.5 bit times
              stopBitsConf_r <= '1';
            end if;
          end if;
        when SAMPLE_START_BIT =>
          if baudRateSamplingEdge = '1' then
            fsmUartFrameGen_r <= CHECK_START;
            bitCounter_r      <= (others => '0');
            startBit_r        <= WAVE.level; -- Sample start bit
          end if;
        when CHECK_START =>
          if startBit_r = '0' then
            fsmUartFrameGen_r <= SAMPLE_BITS;
          else
            fsmUartFrameGen_r <= NOT_A_MATCH;
            refuted_r         <= refuted_r or JUDGE;
          end if;
        when SAMPLE_BITS =>
          if baudRateSamplingEdge = '1' then
            uartSamples_r <= WAVE.level & uartSamples_r(G_CONF_NBITS-1 downto 1);
            bitCounter_r  <= bitCounter_r + 1;
            if bitCounter_r = G_CONF_NBITS - 1 then
              parityBit_r       <= WAVE.level;
              bitCounter_r      <= (others => '0');
              fsmUartFrameGen_r <= WAIT_STOP;
            else -- check parity
              if WAVE.level = '1' then
                onesCounter_r <= onesCounter_r + 1;
              end if;
            end if;
          end if;
        when WAIT_STOP =>
          if baudRateSamplingEdge = '1' then
            stopBit_r         <= WAVE.level;
            fsmUartFrameGen_r <= CHECK_STOP;
          end if;
        when CHECK_STOP =>
          if stopBit_r = '1' then
            stopMatch_r       <= '1';
            if cntMatches_r < to_unsigned(G_MIN_MATCHES, cntMatches_r'length) then
              cntMatches_r <= cntMatches_r + 1;
            end if;
            fsmUartFrameGen_r <= RESOLVE_PARITY;
            if parityFail_r = '1' then
              fsmUartFrameGen_r <= WAIT_START;
            end if;
          else
            stopMatch_r       <= '0';
            fsmUartFrameGen_r <= NOT_A_MATCH; -- NOT_A_MATCH does nothing until reset
            refuted_r         <= refuted_r or JUDGE;
          end if;
        when RESOLVE_PARITY =>
          fsmUartFrameGen_r <= CHECK_PARITY;
          if onesCounter_r(0) = '1' then -- Odd number of ones
            if parityBit_r = '1' then
              isEvenParity_r <= '1';
            else
              isEvenParity_r <= '0';
            end if;
          else
            if parityBit_r = '0' then
              isEvenParity_r <= '1';
            else
              isEvenParity_r <= '0';
            end if;
          end if;
          isEvenParityPrev_r <= isEvenParity_r;
        when CHECK_PARITY =>
          -- The important aspect of the parity check it is not the value, but rather if it changes.
          -- It is assumed that for the calibration process, the parity bit will be consistent
          fsmUartFrameGen_r  <= WAIT_START;
          if firstParityCheck_r = '1' then -- Avoid checking on first time
            firstParityCheck_r <= '0';
          else
            parityFail_r <= isEvenParity_r xor isEvenParityPrev_r; -- check if parity changed
          end if;
        when NOT_A_MATCH => -- Stays here until reset
          waitStart_r    <= '1';
          -- A mismatch on the playback hold after the drain (JUDGE = '0') is
          -- not a refutation and keeps MATCH, so it keeps the stop-bit result
          -- too; only a refutation on recorded data clears it.
          if refuted_r = '1' then
            stopBitsConf_r <= '0';
          end if;
      end case;
      if ENABLE = '0' then
        fsmUartFrameGen_r  <= WAIT_START;
      end if;
      if RST = '1' then
        fsmUartFrameGen_r  <= WAIT_START;
        bitCounter_r       <= (others => '0');
        uartSamples_r      <= (others => '1');
        cntMatches_r       <= (others => '0');
        stopBit_r          <= '0';
        startBit_r         <= '0';
        firstParityCheck_r <= '1';
        parityFail_r       <= '0';
        stopBitsConf_r     <= '0';
        refuted_r          <= '0';
      end if;
    end if;
  end process;

  -- DS-P2.242: a refuted instance no longer matches. NOT_A_MATCH keeps
  -- cntMatches_r, so without refuted_r a frame that contradicts the format
  -- after G_MIN_MATCHES could not withdraw it. Only a refutation on recorded
  -- data (JUDGE = '1') counts.
  matchStop <= '1' when cntMatches_r = G_MIN_MATCHES and refuted_r = '0' else '0';

  MATCH     <= matchStop when rising_edge(CLK); -- Consistent match on stop bit
  PARITY(0) <= not parityFail_r;    -- has parity if no fail
  PARITY(1) <= not isEvenParity_r;  -- even parity = 0, odd parity = 1
  STOP_BITS <= stopBitsConf_r;


end architecture;