-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : uart_frame_gen.vhd
-- Project    : deepskopion
-------------------------------------------------------------------------------
-- File       : uart_frame_gen.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    : RouteRTL
-- Created    : 2025-11-05
-- Last update: 2025-11-05
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: uart_frame_gen, generates a frame from sampling the wave
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
use tich.uart_pkg.all;

entity uart_frame_gen is
  generic (
    G_UART_DEV_ID     : natural := 26;
    G_UART_ID_WIDTH   : integer := 5;
    G_MSG_WIDTH       : integer := 32;
    G_BR_CYCLES_WIDTH : integer
    );
  port (
    CLK       : in  std_logic;
    RST       : in  std_logic;
    ENABLE    : in  std_logic;
    READY     : in  std_logic;  -- playback-FIFO data-available gate (not backpressure)
    -- DS-T2.42: external consumer accept (UART_MSG_RDY). Used to complete the
    -- post-cal CONFIG handshake EXACTLY once. Defaults '1' (back-compat): a
    -- consumer that is always ready accepts the held CONFIG on the first cycle
    -- and the FSM advances, so no duplicate frames are produced.
    OUT_RDY   : in  std_logic := '1';
    BR_CYCLES : in  std_logic_vector(G_BR_CYCLES_WIDTH-1 downto 0);
    WAVE      : in  t_wave;
    NBITS     : in  integer;
    PARITY    : in  std_logic_vector(1 downto 0); -- Check partity if '1'
    STOP_BITS : in  std_logic;  -- 1: one stop bit, 0: two stop bits (protocol stop_bit_encoding)
    MSG_OUT   : out std_logic_vector(G_MSG_WIDTH-1 downto 0);
    MSG_VLD   : out std_logic
    );
end uart_frame_gen;

architecture rtl of uart_frame_gen is

  constant C_UART_DEV_ID_UNS     : std_logic_vector(G_UART_ID_WIDTH-1 downto 0) := std_logic_vector(to_unsigned(G_UART_DEV_ID, G_UART_ID_WIDTH));
  constant C_MAX_UART_BITS_WIDTH : integer := integer(ceil(log2(real(C_MAX_UART_BITS))));
  constant C_MAX_UART_MSG_CYCLES : integer := C_MAX_UART_BITS * 2**G_BR_CYCLES_WIDTH;
  -- constant C_MAX_UART_MSG_WIDTH  : integer := integer(ceil(log2(real(C_MAX_UART_MSG_CYCLES))));
  constant C_MAX_UART_MSG_WIDTH  : integer := G_BR_CYCLES_WIDTH + 4;
  constant C_EDGE_TYPE_FALLING   : integer := 1;


  constant C_UART_CALIBRATION_DONE    : std_logic_vector(7 downto 0) := x"00";
  constant C_UART_CALIBRATION_TIMEOUT : std_logic_vector(7 downto 0) := x"01";
  constant C_UART_PARITY_ERROR        : std_logic_vector(7 downto 0) := x"02";
  constant C_UART_START_ERROR         : std_logic_vector(7 downto 0) := x"03";
  constant C_UART_STOP_ERROR          : std_logic_vector(7 downto 0) := x"04";
  -- The addition is below
  constant C_NUM_UART_STATUS          : integer := 5;

  constant C_UART_PACKET_STATUS_TYPE     : std_logic_vector(2 downto 0) := "100";
  constant C_UART_PACKET_PAYLOAD_TYPE    : std_logic_vector(2 downto 0) := "010";  -- 8-bit or 9-bit MSB=0
  constant C_UART_PACKET_PAYLOAD_9B_TYPE : std_logic_vector(2 downto 0) := "011";  -- 9-bit MSB=1
  constant C_UART_PACKET_CONFIG_TYPE     : std_logic_vector(2 downto 0) := "001";


  constant C_MSG_STATUS_INDEX    : natural := 0;
  constant C_MSG_PAYLOAD_A_INDEX : natural := 1;
  constant C_MSG_PAYLOAD_B_INDEX : natural := 2;
  constant C_MSG_CONFIG_INDEX    : natural := 3;

  constant C_MSG_MUX_SIZE : integer := 4;

  constant C_PAYLOAD_A_WIDTH     : integer := 8;
  constant C_PAYLOAD_B_WIDTH     : integer := C_MAX_UART_BITS - C_PAYLOAD_A_WIDTH;
  constant C_PAYLOAD_B_PAD_WIDTH : integer := 8 - C_PAYLOAD_B_WIDTH;
  constant C_PAYLOAD_B_ZERO_PAD  : std_logic_vector(C_PAYLOAD_B_PAD_WIDTH-1 downto 0) := (others => '0');

  constant C_CONFIG_MSG_WIDTH     : integer := C_MAX_UART_BITS_WIDTH + 1 + 1 + 1 + G_BR_CYCLES_WIDTH-3;


  type t_fsm_uart_frame_gen is (IDLE, WAIT_START, SAMPLE_START_BIT, CHECK_START, SAMPLE_BITS, WAIT_STOP, CHECK_STOP, SAMPLE_PARITY, RESOLVE_PARITY, CHECK_PARITY, GENERATE_MSG, SEND_MSG_A, SEND_MSG_B, PAUSE);
  type t_uart_status is array (0 to C_NUM_UART_STATUS-1) of std_logic_vector(7 downto 0);
  type t_msg_out_mux is array (0 to C_MSG_MUX_SIZE-1) of std_logic_vector(G_MSG_WIDTH-1 downto 0);

  constant C_UART_STATUS : t_uart_status := (
    C_UART_CALIBRATION_DONE,
    C_UART_CALIBRATION_TIMEOUT,
    C_UART_PARITY_ERROR,
    C_UART_START_ERROR,
    C_UART_STOP_ERROR
  );



  signal fsmUartFrameGen_r    : t_fsm_uart_frame_gen := IDLE;
  signal fsmUartFrameGen_n    : t_fsm_uart_frame_gen;
  signal fsmSavedState_r      : t_fsm_uart_frame_gen := IDLE;
  signal fsmSavedState_n      : t_fsm_uart_frame_gen := IDLE;

  signal baudRateSamplingEdge : std_logic;
  signal uartSamples_r        : std_logic_vector(C_MAX_UART_BITS-1 downto 0) := (others => '0');
  signal uartSamples_n        : std_logic_vector(C_MAX_UART_BITS-1 downto 0);
  signal waitStart_r          : std_logic := '1';
  signal waitStart_n          : std_logic;
  signal startDetected        : std_logic;
  signal bitCounter_r         : unsigned(C_MAX_UART_BITS_WIDTH-1 downto 0) := (others => '0');
  signal bitCounter_n         : unsigned(C_MAX_UART_BITS_WIDTH-1 downto 0);
  signal onesCounter_r        : unsigned(C_MAX_UART_BITS_WIDTH-1 downto 0) := (others => '0');
  signal onesCounter_n        : unsigned(C_MAX_UART_BITS_WIDTH-1 downto 0);
  signal parityBit_r          : std_logic := '0';
  signal parityBit_n          : std_logic;
  signal stopBit_r            : std_logic := '0';
  signal stopBit_n            : std_logic;
  signal isEvenParity_r       : std_logic := '0';
  signal isEvenParity_n       : std_logic;
  signal isEvenParityPrev_r   : std_logic := '0';
  signal isEvenParityPrev_n   : std_logic;
  signal firstParityCheck_r   : std_logic := '1';
  signal firstParityCheck_n   : std_logic;
  signal parityFail_r         : std_logic := '0';
  signal parityFail_n         : std_logic;
  signal match_r              : std_logic := '0';
  signal payloadA_r           : std_logic_vector(C_PAYLOAD_A_WIDTH-1 downto 0)                   := (others => '0');
  signal payloadA_n           : std_logic_vector(C_PAYLOAD_A_WIDTH-1 downto 0);
  signal payloadB_r           : std_logic_vector(C_PAYLOAD_B_WIDTH-1 downto 0) := (others => '0');
  signal payloadB_n           : std_logic_vector(C_MAX_UART_BITS-8-1 downto 0);
  signal statusIndex_r        : integer := 0;
  signal statusIndex_n        : integer;
  signal msgIndex_r           : natural := 0;
  signal msgIndex_n           : natural;
  signal msgVld_r             : std_logic := '0';
  signal msgVld_n             : std_logic;
  signal payloadType_r        : std_logic_vector(2 downto 0) := C_UART_PACKET_PAYLOAD_TYPE;  -- Dynamic type for 9-bit
  signal payloadType_n        : std_logic_vector(2 downto 0);
  signal uartConfig_n         : std_logic_vector(C_CONFIG_MSG_WIDTH-1 downto 0);
  signal uartConfig_r         : std_logic_vector(C_CONFIG_MSG_WIDTH-1 downto 0) := (others => '0');
  signal msgOut               : t_msg_out_mux := (others => (others => '0'));
  signal hasParity            : std_logic;
  signal parityType           : std_logic; -- 0: even, 1: odd
  signal startBit_r           : std_logic := '1';
  signal startBit_n           : std_logic;
  signal timeOutCnt_r         : unsigned(G_BR_CYCLES_WIDTH+4-1 downto 0) := (others => '0');
  signal timeOutCnt_n         : unsigned(G_BR_CYCLES_WIDTH+4-1 downto 0);
  signal timeoutThreshold     : unsigned(G_BR_CYCLES_WIDTH+4  -1 downto 0);
  signal intReady             : std_logic;
  signal timeStamp_n          : std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
  signal timeStamp_r          : std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0) := (others => '0');


begin

  hasParity  <= PARITY(0);
  parityType <= PARITY(1);


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
      G_DIST_WIDTH => G_BR_CYCLES_WIDTH
    )
    port map (
      CLK       => CLK,
      RST       => RST,
      ENABLE    => not waitStart_r,
      PAUSE     => not intReady,
      BR_CYCLES => BR_CYCLES,
      TICK      => baudRateSamplingEdge
    );

  timeoutThreshold <= unsigned(BR_CYCLES) & "0000"; -- 16 times the BR cycles
  -- Flow-control alias only: READY pauses the baud generator (PAUSE => not intReady);
  -- there is no data register behind it, so a skid buffer has nothing to hold.
  intReady <= READY; -- lint-ok: axis-skid

  pr_nsl_fsm: process(all)
  begin
    fsmUartFrameGen_n  <= fsmUartFrameGen_r;
    payloadA_n         <= payloadA_r;
    payloadB_n         <= payloadB_r;
    uartConfig_n       <= uartConfig_r;
    msgIndex_n         <= C_MSG_STATUS_INDEX;
    msgVld_n           <= '0';
    statusIndex_n      <= 0;
    isEvenParity_n     <= isEvenParity_r;
    isEvenParityPrev_n <= isEvenParityPrev_r;
    firstParityCheck_n <= firstParityCheck_r;
    parityFail_n       <= parityFail_r;
    waitStart_n        <= waitStart_r;
    bitCounter_n       <= bitCounter_r;
    onesCounter_n      <= onesCounter_r;
    parityBit_n        <= parityBit_r;
    stopBit_n          <= stopBit_r;
    startBit_n         <= startBit_r;
    uartSamples_n      <= uartSamples_r;
    fsmSavedState_n    <= fsmSavedState_r;
    timeOutCnt_n       <= (others => '0');
    timeStamp_n        <= timeStamp_r;
    payloadType_n      <= payloadType_r;  -- Default: hold current type
    --
    case fsmUartFrameGen_r is
      when IDLE =>
        if ENABLE = '1' then
          -- DS-T3.5: the post-cal CONFIG report (detected NBITS / parity / baud)
          -- was emitted here as a ONE-CYCLE msgVld pulse and then jumped straight
          -- to WAIT_START with NO backpressure — unlike the payload path, which
          -- PAUSEs when the gather isn't ready. Being a SINGLE frame, one unlucky
          -- not-ready cycle dropped it permanently, so SW never saw the detected
          -- config (0 config frames in the ring). Hold msgVld in IDLE until the
          -- gather ACCEPTS it, THEN start streaming.
          --
          -- DS-T2.42 fix: the genuine "gather accepted it" signal is the external
          -- OUT_RDY (UART_MSG_RDY / arbiter uartAck), NOT intReady. intReady is
          -- READY <= not streamEmpty (the INTERNAL playback-FIFO data-available
          -- flag), which is momentarily low right at calibration-done; gating the
          -- leave on it held msgVld high for several cycles while a ready-every-
          -- cycle consumer captured the SAME CONFIG frame each cycle (~7 byte-
          -- identical duplicates → whole stream shifted). Gating on OUT_RDY makes
          -- it exactly one accepted transfer: held until the consumer is ready
          -- (no drop), advances the same cycle it is taken (no duplicate).
          statusIndex_n <= to_integer(unsigned(C_UART_CALIBRATION_DONE));
          msgIndex_n    <= C_MSG_CONFIG_INDEX;
          uartConfig_n(C_CONFIG_MSG_WIDTH-1 downto 0)  <= BR_CYCLES(BR_CYCLES'left-4 downto 0) & '0' & STOP_BITS & parityType & hasParity & std_logic_vector(to_unsigned(NBITS, C_MAX_UART_BITS_WIDTH));
          -- Standard valid/ready producer: hold msgVld until the transfer is
          -- actually taken (registered msgVld_r='1' AND OUT_RDY='1'), then drop
          -- valid the SAME cycle and advance. msgVld is registered, so leaving on
          -- OUT_RDY alone would re-present the frame one extra cycle in WAIT_START
          -- (a double transfer under backpressure); gating on msgVld_r AND OUT_RDY
          -- guarantees EXACTLY one accepted CONFIG transfer in all ready cadences.
          if msgVld_r = '1' and OUT_RDY = '1' then
            msgVld_n          <= '0';
            fsmUartFrameGen_n <= WAIT_START;
          else
            msgVld_n          <= '1';
          end if;
        end if;
      -- Use this state to hold the FSM until the calibration is done. Then,
      -- inform of the NBITS, baudrate and parity bit configuration
      when WAIT_START =>
        waitStart_n   <= '1';
        onesCounter_n  <= (others => '0');
        if startDetected = '1' then
          fsmUartFrameGen_n <= SAMPLE_START_BIT;
          waitStart_n       <= '0';
          timeStamp_n       <= WAVE.timestamp;
        end if;
      when SAMPLE_START_BIT =>
          if intReady = '0' and ENABLE = '1' then
            fsmUartFrameGen_n <= PAUSE;
            fsmSavedState_n   <= fsmUartFrameGen_r;
          elsif baudRateSamplingEdge = '1' then
            fsmUartFrameGen_n <= CHECK_START;
            bitCounter_n      <= (others => '0');
            startBit_n        <= WAVE.level; -- Sample start bit
          end if;
      when CHECK_START =>
          if startBit_r = '0' then
            fsmUartFrameGen_n <= SAMPLE_BITS;
          else
            fsmUartFrameGen_n <= WAIT_START; -- report Error
            -- TODO: Use records for status messages?
            statusIndex_n     <= to_integer(unsigned(C_UART_START_ERROR));
            msgIndex_n        <= C_MSG_STATUS_INDEX;
            msgVld_n          <= '1';
          end if;
      when SAMPLE_BITS =>
        if intReady = '0' and ENABLE = '1' then
          fsmUartFrameGen_n <= PAUSE;
          fsmSavedState_n   <= fsmUartFrameGen_r;
        elsif baudRateSamplingEdge = '1' then
          -- Indexed write: place each sampled bit at position bitCounter_r
          -- First sampled bit (UART LSB) goes to bit 0, last (UART MSB) to bit NBITS-1
          uartSamples_n                               <= uartSamples_r;
          uartSamples_n(to_integer(bitCounter_r))     <= WAVE.level;
          bitCounter_n  <= bitCounter_r + 1;
          if bitCounter_r = NBITS - 1 then
            bitCounter_n      <= (others => '0');
            fsmUartFrameGen_n <= WAIT_STOP;
            if hasParity = '1' then
              fsmUartFrameGen_n <= SAMPLE_PARITY;
            end if;
          end if;
          if WAVE.level = '1' then
            onesCounter_n <= onesCounter_r + 1;
          end if;
        end if;
      when SAMPLE_PARITY =>
        if intReady = '0' and ENABLE = '1' then
          fsmUartFrameGen_n <= PAUSE;
          fsmSavedState_n   <= fsmUartFrameGen_r;
        elsif baudRateSamplingEdge = '1' then
          parityBit_n        <= WAVE.level;
          fsmUartFrameGen_n  <= WAIT_STOP;
        end if;
      when WAIT_STOP =>
        if intReady = '0' and ENABLE = '1' then
          fsmUartFrameGen_n <= PAUSE;
          fsmSavedState_n   <= fsmUartFrameGen_r;
        elsif baudRateSamplingEdge = '1' then
          stopBit_n         <= WAVE.level;
          fsmUartFrameGen_n <= CHECK_STOP;
        end if;
      when CHECK_STOP =>
        if stopBit_r = '1' then
          if hasParity = '1' then
            fsmUartFrameGen_n <= RESOLVE_PARITY;
          else
            fsmUartFrameGen_n <= GENERATE_MSG;
          end if;
        else
          statusIndex_n     <= to_integer(unsigned(C_UART_STOP_ERROR));
          msgIndex_n        <= C_MSG_STATUS_INDEX;
          msgVld_n          <= '1';
          fsmUartFrameGen_n <= WAIT_START;
        end if;
      when RESOLVE_PARITY =>
        fsmUartFrameGen_n  <= CHECK_PARITY;
        if onesCounter_r(0) = '1' then -- Odd number of ones
          if parityBit_r = '1' then
            isEvenParity_n <= '1';
          else
            isEvenParity_n <= '0';
          end if;
        else
          if parityBit_r = '0' then
            isEvenParity_n <= '1';
          else
            isEvenParity_n <= '0';
          end if;
        end if;
        isEvenParityPrev_n <= isEvenParity_r;
      when CHECK_PARITY =>
        -- The important aspect of the parity check it is not the value, but rather if it changes.
        -- It is assumed that for the calibration process, the parity bit will be consistent
        fsmUartFrameGen_n  <= GENERATE_MSG;
        if firstParityCheck_r = '1' then -- Avoid checking on first time
          firstParityCheck_n <= '0';
        else
          parityFail_n <= isEvenParity_r xor isEvenParityPrev_r; -- check if parity changed
        end if;
      when GENERATE_MSG =>
        fsmUartFrameGen_n <= SEND_MSG_A;
        if parityFail_r = '1' then
          statusIndex_n <= to_integer(unsigned(C_UART_PARITY_ERROR));
          msgIndex_n    <= C_MSG_STATUS_INDEX;
          msgVld_n      <= '1';
        end if;
        -- With indexed write, sampled bits are at positions [NBITS-1:0]
        -- First sampled bit (UART bit 0) at position 0, last at position NBITS-1
        if NBITS <= 8 then
          -- Standard 5-8 bit mode: use type 0b010
          payloadA_n    <= uartSamples_r(7 downto 0);
          payloadType_n <= C_UART_PACKET_PAYLOAD_TYPE;
        else
          -- 9-bit mode: bit 8 (9th bit, MSB) is at position 8
          -- Encode this bit in the message type
          if uartSamples_r(8) = '1' then
            payloadType_n <= C_UART_PACKET_PAYLOAD_9B_TYPE;  -- 0b011
          else
            payloadType_n <= C_UART_PACKET_PAYLOAD_TYPE;     -- 0b010
          end if;
          -- Extract lower 8 bits from [7:0]
          payloadA_n <= uartSamples_r(7 downto 0);
        end if;
      when SEND_MSG_A =>
        msgIndex_n        <= C_MSG_PAYLOAD_A_INDEX;
        msgVld_n          <= '1';
        fsmUartFrameGen_n <= WAIT_START;  -- Always go back, no fragment B
      when SEND_MSG_B =>
        fsmUartFrameGen_n <= WAIT_START;
        msgIndex_n        <= C_MSG_PAYLOAD_B_INDEX;
        msgVld_n          <= '1';
      when PAUSE =>
        -- Readiness is based on the presence of distance data in the PlayBack FIFO. It can get
        -- empty because UART samples are much slower that the processing, or because the UART msg is actually
        -- over. The former is detected by waiting for enough cycles to accept the UART msg is over.
        if intReady = '1' then
          fsmUartFrameGen_n <= fsmSavedState_r;
        elsif timeOutCnt_r = timeoutThreshold-1 then
          fsmUartFrameGen_n <= fsmSavedState_r;
        else
          timeOutCnt_n <= timeOutCnt_r + 1;
        end if;
      when others =>
        fsmUartFrameGen_n <= WAIT_START;
    end case;
  end process;

  pr_reg_fsm : process(CLK)
  begin
    if rising_edge(CLK) then
      fsmUartFrameGen_r  <= fsmUartFrameGen_n;
      payloadA_r         <= payloadA_n;
      payloadB_r         <= payloadB_n;
      uartConfig_r       <= uartConfig_n;
      msgIndex_r         <= msgIndex_n;
      msgVld_r           <= msgVld_n;
      statusIndex_r      <= statusIndex_n;
      isEvenParity_r     <= isEvenParity_n;
      isEvenParityPrev_r <= isEvenParityPrev_n;
      firstParityCheck_r <= firstParityCheck_n;
      parityFail_r       <= parityFail_n;
      waitStart_r        <= waitStart_n;
      bitCounter_r       <= bitCounter_n;
      onesCounter_r      <= onesCounter_n;
      parityBit_r        <= parityBit_n;
      stopBit_r          <= stopBit_n;
      startBit_r         <= startBit_n;
      uartSamples_r      <= uartSamples_n;
      fsmSavedState_r    <= fsmSavedState_n;
      timeOutCnt_r       <= timeOutCnt_n;
      timeStamp_r        <= timeStamp_n;
      payloadType_r      <= payloadType_n;  -- Register dynamic payload type
      --
      if RST = '1' then
        fsmUartFrameGen_r  <= IDLE;
        msgIndex_r         <= 0;
        msgVld_r           <= '0';
        statusIndex_r      <= 0;
        firstParityCheck_r <= '1';
        -- DS-T1.16: GENERATE_MSG reads parityFail_r even with parity off, and
        -- only CHECK_PARITY ever writes it, so an unreset power-up '1' (VHDL
        -- initial values are ignored on PolarFire) emitted a PARITY_ERROR with
        -- every byte and survived every re-arm.
        parityFail_r       <= '0';
        isEvenParity_r     <= '0';
        isEvenParityPrev_r <= '0';
        waitStart_r        <= '1';
        bitCounter_r       <= (others => '0');
        onesCounter_r      <= (others => '0');
      end if;
    end if;
  end process;

  msgOut(C_MSG_STATUS_INDEX)    <= C_UART_DEV_ID_UNS & C_UART_PACKET_STATUS_TYPE & timeStamp_r & C_UART_STATUS(statusIndex_r);
  msgOut(C_MSG_PAYLOAD_A_INDEX) <= C_UART_DEV_ID_UNS & payloadType_r & timeStamp_r & payloadA_r;  -- Dynamic type for 9-bit
  msgOut(C_MSG_PAYLOAD_B_INDEX) <= C_UART_DEV_ID_UNS & payloadType_r & timeStamp_r & C_PAYLOAD_B_ZERO_PAD & payloadB_r;  -- Kept for compatibility


  msgOut(C_MSG_CONFIG_INDEX)(31 downto 24)                   <= C_UART_DEV_ID_UNS & C_UART_PACKET_CONFIG_TYPE;
  msgOut(C_MSG_CONFIG_INDEX)(C_CONFIG_MSG_WIDTH-1 downto 0)  <= uartConfig_r;

  MSG_VLD <= msgVld_r;
  MSG_OUT <= msgOut(msgIndex_r);

      -- Debug traces
  GEN_DEBUG_TRACES : if 1 = 1 generate
    assert FAlSE report "G_BR_CYCLES_WIDTH : " & integer'image(G_BR_CYCLES_WIDTH) severity NOTE;
    assert FALSE report "C_CONFIG_MSG_WIDTH : " & integer'image(C_CONFIG_MSG_WIDTH) severity NOTE;
  end generate GEN_DEBUG_TRACES;

end rtl;


