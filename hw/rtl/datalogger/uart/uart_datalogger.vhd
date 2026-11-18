-------------------------------------------------------------------------------
-- Title      : uart_datalogger.vhd
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
-- Description: UART Datalogger. Creates a datalogger message when UART data is
--              received. The message contains the timestamp, the data and
--              status flags.
-------------------------------------------------------------------------------
-- Copyright (c) Daniel J. Mazure 2025
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2025-11-05  1.0      dmazure Created
-------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;

library tich;
use tich.datalogger_pkg.all;
use tich.uart_pkg.all;

entity uart_datalogger is
    generic (
      G_SYSTEM_CLK_FREQ : integer                                   := 100_000_000; -- real system clock (DS-P2.154: all clk-derived math follows this)
      G_TICK_PRESCALER  : integer                                   := 0; -- 0 = derive from G_SYSTEM_CLK_FREQ (DS-P2.154); >0 = explicit override (tests)
      G_UART_ID_WIDTH   : integer                                   := 5;
      G_UART_DEV_ID     : natural range 0 to (2**G_UART_ID_WIDTH)-1 := 28;
      G_TIMESTAMP_WIDTH : natural range 16 to 32                    := 16;
      G_CONFIG_WIDTH    : integer                                   := 16;
      G_ERROR_WIDTH     : integer                                   := 2;
      G_ILA_EN          : integer                                   := 1
      );
    port (
      CLK         : in  std_logic;
      RST         : in  std_logic;
      UART_IN     : in  std_logic;
      -- CONFIG format (DS-T3.12 — the bits below were previously MIS-DOCUMENTED
      -- as "[10:1] Reserved=0", which silently forced explicit 5-bit decode):
      --   [15:11] UART_DEV_ID : device id for message tagging (must match G_UART_DEV_ID)
      --   [0]     ENABLE      : datalogger enable
      --   [1]     autoSync    : 1 = auto-detect baud + byte format from the line;
      --                         0 = use the explicit [10:2] fields below.
      --                         *** Set this to 1 for auto-baud/format. ***
      --   [4:2]   dataBits    : explicit data bits, encoded as (nbits-5): 0→5 .. 4→9 (autoSync=0)
      --   [5]     parityEn    : explicit parity enable (autoSync=0)
      --   [6]     parityType  : explicit parity type  (autoSync=0)
      --   [10:7]  stopBitsDef : explicit stop-bit config (autoSync=0)
      -- (The baud is ALWAYS auto-measured from the edges; [4:2..10:7] set the byte
      --  FORMAT only, and only when autoSync=0 — see nbitsSel/paritySel/stopBitsSel.)
      CONFIG      : in  std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
      TIMESTAMP   : in  std_logic_vector(G_TIMESTAMP_WIDTH-1 downto 0);
      -- UART
      UART_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
      UART_MSG_VLD : out std_logic;
      UART_MSG_RDY : in  std_logic;
      --
      UART_ERRORS  : out std_logic_vector(G_ERROR_WIDTH-1 downto 0)
      );
end uart_datalogger;

architecture rtl of uart_datalogger is

  ------------------------------------------------------------
  -- Constants
  ------------------------------------------------------------
  constant C_DEBUG_TRACES : integer := 1;

  -- DS-P2.154: uart_pkg's C_TICK_PRESCALER / C_MAX_BITWIDTH_UART_CYCLES are
  -- 100 MHz-sized package constants; using them here silently halved the
  -- sampler resolution on the 50 MHz DE25 (prescaler 4 -> 12.5 ticks/bit at
  -- 1 Mbaud, the calibration-never-locked regime). Everything clock-derived
  -- is computed from G_SYSTEM_CLK_FREQ instead; G_TICK_PRESCALER = 0 means
  -- "derive" (100 MHz -> 4, 50 MHz -> 2), > 0 is an explicit test override.
  constant C_PRESCALER          : integer := f_resolve_prescaler(G_TICK_PRESCALER, G_SYSTEM_CLK_FREQ);
  constant C_MAX_BW_UART_CYCLES : integer := f_max_bitwidth_uart_cycles(G_SYSTEM_CLK_FREQ);

  -- Tick-based width constants (computed from the real clock + prescaler)
  constant C_DIST_WIDTH       : integer := f_dist_width(G_SYSTEM_CLK_FREQ, C_PRESCALER);
  constant C_LOCAL_FIFO_WIDTH : integer := f_fifo_width(G_SYSTEM_CLK_FREQ, C_PRESCALER);
  constant C_LOCAL_BRAM_WIDTH : integer := f_bram_width(G_SYSTEM_CLK_FREQ, C_PRESCALER);

  -- DS-T2.30: relativeDistCnt_r counts PRESCALED ticks (one per G_TICK_PRESCALER
  -- clks). The old saturation cap compared it against C_MAX_RELATIVE_DISTANCE_
  -- CYCLES (a RAW-cycle count, 124991) which is > 2**C_DIST_WIDTH, so the 15-bit
  -- counter never reached it and WRAPPED at 2**C_DIST_WIDTH during long idle gaps
  -- — storing a corrupted distance that wrecked the playback reconstruction →
  -- constant-byte garble on real silicon streams (which always have a long
  -- initial idle + inter-byte gaps; synthetic back-to-back testbenches never
  -- wrap, so it stayed hidden). Saturate at the counter's natural max instead:
  -- all-ones (2**C_DIST_WIDTH-1) is > the 16-bit-time idle timeout
  -- (baudRateMin<<4) and the max frame distance, so the idle/flush timeouts
  -- still fire AND the counter never rolls over.
  constant C_MAX_DIST_TICKS : integer := 2**C_DIST_WIDTH - 1;

  type t_one_hot_status is record
    cnt_hots   : integer;
    index      : integer;
  end record;

  function where_is_one_hot(v : std_logic_vector) return t_one_hot_status is
    variable result : t_one_hot_status;
  begin
    result.index       :=  0;
    result.cnt_hots    :=  0;
    for i in v'range loop
      if v(i) = '1' then
        result.cnt_hots := result.cnt_hots + 1;
        result.index := i;
      end if;
    end loop;
    return result;
  end function;

  -- DS-P2.242: the format the calibration replay supports. An n-bit frame with
  -- its top data bit 1 is the same waveform as an (n-1)-bit frame with two stop
  -- bits, and a gapped n-bit frame the same as an (n+1)-bit frame with its top
  -- data bit 1, so several stop-finder instances can match every frame of a
  -- buffer. Of the instances that reached G_MIN_MATCHES and that no frame
  -- refuted, the shortest word is taken: a buffer that fits both readings
  -- resolves to the shorter word with two stop bits.
  function pick_format(match : std_logic_vector) return t_one_hot_status is
    variable result : t_one_hot_status;
  begin
    result.index    := 0;
    result.cnt_hots := 0;            -- number of matching instances
    for i in match'high downto match'low loop
      if match(i) = '1' then
        result.cnt_hots := result.cnt_hots + 1;
        result.index    := i;        -- ends on the lowest index: the shortest word
      end if;
    end loop;
    return result;
  end function;

  function sl_to_integer(b : std_logic) return integer is
    variable result : unsigned(0 downto 0);
    begin
      result(0) := b;
    return to_integer(result);
  end function;

  ------------------------------------------------------------
  -- Types
  ------------------------------------------------------------
  type t_fsm_analyze_uart is   (DISABLED, IDLE, WRITE_BUFFER, BUFFER_END);
  type t_fsm_calibrate_uart is (DISABLED, IDLE, DETECT_BAUDRATE, DETECT_STOP_BIT, WAIT_FOR_DETECTION, CALIBRATION_DONE);
  type t_fsm_uart_stream is    (IDLE, WAIT_FOR_BUFFER, STREAM);

  ------------------------------------------------------------
  -- Signals
  ------------------------------------------------------------
  signal fsmCalibrate_r    : t_fsm_calibrate_uart := DISABLED;
  signal fsmCalibrate_n    : t_fsm_calibrate_uart;
  signal fsmBufferUart_r   : t_fsm_analyze_uart   := DISABLED;
  signal fsmBufferUart_n   : t_fsm_analyze_uart;
  signal fsmUartStream_r   : t_fsm_uart_stream    := IDLE;

  signal uart_data         : std_logic_vector(7 downto 0) := (others => '0');
  signal uart_data_vld     : std_logic := '0';
  signal uart_data_rd      : std_logic := '0';
  signal uart_error        : std_logic_vector(G_ERROR_WIDTH-1 downto 0) := (others => '0');

  -- Configuration signals
  signal deviceLoadConfig  : std_logic_vector(G_CONFIG_WIDTH-G_UART_ID_WIDTH-1 downto 0);
  signal configEnable      : std_logic;
  -- Per-datalogger reset from config_control (DS-T2.31): global RST OR this device
  -- disabled. Used as the synchronous reset for the whole datalogger body so a
  -- SW ENABLE=0 flushes all calibration/FIFO/BRAM state — no stale traces
  -- across reconfiguration (e.g. switching format or into autoSync).
  signal dataloggerRst        : std_logic;
  signal autoSync          : std_logic;
  signal dataBits          : std_logic_vector(2 downto 0);
  signal parityEn          : std_logic;
  signal parityTypeDef     : std_logic;
  signal stopBitsDef       : std_logic_vector(3 downto 0);
  -- Edge detection
  signal edgeDetected      : std_logic;
  -- Timebase generation
  signal timeStampTicker_r    : std_logic := '0';
  signal timeStampTickerCnt_r : unsigned(C_DIST_WIDTH-1 downto 0)    := (others => '0');
  signal relativeDistCnt_r    : unsigned(C_DIST_WIDTH-1 downto 0) := (others => '0');
  -- FIFO signals
  signal fifoUartDin        : std_logic_vector(C_LOCAL_FIFO_WIDTH-1 downto 0);
  signal fifoUartDin_r      : std_logic_vector(C_LOCAL_FIFO_WIDTH-1 downto 0) := (others => '0');
  signal fifoUartDout       : std_logic_vector(C_LOCAL_FIFO_WIDTH-1 downto 0);
  signal fifoUartWrEn       : std_logic;
  signal fifoUartWrEn_r     : std_logic := '0';
  signal fifoUartRdEn_r     : std_logic := '0';
  signal fifoUartEmpty      : std_logic;
  signal fifoUartAlmostFull : std_logic;
  signal fifoUartRstBusy    : std_logic;
  signal fifoUartFull       : std_logic;
  signal fifoUartDataCnt    : std_logic_vector(C_FIFO_DEPTH downto 0);
  --
  signal uartPin_r          : std_logic := '1';

  -- FSM to store edges in BRAM
  signal bramWrEn_r         : std_logic := '0';
  signal bramWrEn_n         : std_logic;
  signal bramWrAddr_r       : unsigned(C_FIFO_DEPTH-1 downto 0)              := C_BRAM_ADDR_INIT;
  signal bramWrAddr_n       : unsigned(C_FIFO_DEPTH-1 downto 0);
  signal bramRdAddr_r       : unsigned(C_FIFO_DEPTH-1 downto 0)              := C_BRAM_ADDR_INIT;
  signal bramRdAddr_n       : unsigned(C_FIFO_DEPTH-1 downto 0);
  signal bramDin_r          : std_logic_vector(C_LOCAL_BRAM_WIDTH-1 downto 0) := (others => '0');
  signal bramDout           : std_logic_vector(C_LOCAL_BRAM_WIDTH-1 downto 0) := (others => '0');
  signal bramRdEn_r         : std_logic := '0';
  signal bramRdEn_n         : std_logic;
  signal bufferFinish_r     : std_logic := '0';
  signal bufferFinish_n     : std_logic;

  -- Baurdrate detection
  signal baudRateMin_r      : unsigned(C_DIST_WIDTH-1 downto 0)    := (others => '1');
  signal baudRateMin_n      : unsigned(C_DIST_WIDTH-1 downto 0);
  signal stopFinderEn_r     : std_logic := '0';
  signal stopFinderEn_n     : std_logic;
  signal baudRateGenCnt_r   : unsigned(C_DIST_WIDTH-1 downto 0)    := (others => '0');
  signal baudRateGenCnt_n   : unsigned(C_DIST_WIDTH-1 downto 0);
  signal baudratetick_r     : std_logic := '0';
  signal calibrationDone_r  : std_logic := '0';
  signal calibrationDone_n  : std_logic;

  --
  signal error_r            : std_logic_vector(G_ERROR_WIDTH-1 downto 0) := (others => '0');
  signal error_n            : std_logic_vector(G_ERROR_WIDTH-1 downto 0);
  --
  signal levelDout              : std_logic;
  signal relativeDistDout       : unsigned(C_DIST_WIDTH-1 downto 0);
  signal relativeDistanceIn_r   : unsigned(C_DIST_WIDTH-1 downto 0) := (others => '1');
  signal relativeDistDoutPrev_r : unsigned(C_DIST_WIDTH-1 downto 0) := (others => '0');
  signal relativeDistDoutPrev_n : unsigned(C_DIST_WIDTH-1 downto 0);

  signal stopMatch           : std_logic_vector(C_POSSIBLE_UART_CONFS-1 downto 0);
  signal parityMatch         : std_logic_vector(C_POSSIBLE_UART_CONFS-1 downto 0);
  signal parityType          : std_logic_vector(C_POSSIBLE_UART_CONFS-1 downto 0);
  signal stopBitsN           : std_logic_vector(C_POSSIBLE_UART_CONFS-1 downto 0);
  -- DS-P2.242: the format is decided once the replay has drained, plus two
  -- bit-times for the stop finders to finish the last frame.
  signal settleCnt_r         : unsigned(C_DIST_WIDTH downto 0) := (others => '0');
  signal settleCnt_n         : unsigned(C_DIST_WIDTH downto 0);
  signal formatFound_r       : std_logic := '0';
  signal stopFinderJudge     : std_logic;       -- DS-P2.242: the replay still plays recorded edges
  signal formatFound_n       : std_logic;
  signal stopBitFound_r      : std_logic := '0';
  signal stopBitFound_n      : std_logic;
  signal stopIndex_r         : integer := 0;
  signal stopIndex_n         : integer;
  signal parityBitFound_r    : std_logic := '0';
  signal parityBitFound_n    : std_logic;
  signal stopBitsConf_r      : std_logic := '0';
  signal stopBitsConf_n      : std_logic;
  signal nbitsConf_r         : integer := 0;
  signal nbitsConf_n         : integer;
  signal parityConf_r        : std_logic_vector(1 downto 0) := (others => '0');
  signal parityConf_n        : std_logic_vector(1 downto 0);
  -- Format presented to the frame generator: the auto-calibrated *Conf_r when
  -- autoSync='1', or the host-supplied explicit config when autoSync='0'
  -- (DS-T3.9 — latency path: skip the stop-finder replay, decode immediately).
  signal nbitsSel            : integer;
  signal paritySel           : std_logic_vector(1 downto 0);
  signal stopBitsSel         : std_logic;
  --
  signal initDone_r          : std_logic := '0';
  signal initDone_n          : std_logic;
  signal pbEmpty             : std_logic;
  signal pbFull              : std_logic;
  signal pbWrEn_r            : std_logic := '0';
  signal streamFull          : std_logic;
  signal streamEmpty         : std_logic;
  signal streamPbValid       : std_logic;
  signal wavePlayback        : t_wave;
  signal uartSreamWave       : t_wave;
  signal startStreaming      : std_logic;
  signal calibrationFail_r   : std_logic := '0';
  signal calibrationFail_n   : std_logic;
  signal timeOutDist_r       : std_logic := '0';
  signal timeOutEdgeDetected : std_logic;

  -- DS-P3.90 Phase 1: uniform symmetric ENABLED/DISABLED lifecycle frames,
  -- emitted by datalogger_lifecycle in the ALWAYS-ON (global RST) domain so they
  -- survive the per-datalogger dataloggerRst (the whole point — H.96). Type "100" is
  -- the event type, distinct from the UART payload types (0b010/0b011).
  constant C_UART_EVENT_TYPE     : std_logic_vector(2 downto 0) := "100";
  -- DS-P2.245: the UART STATUS code space is uart_frame_gen's (0x00..0x04,
  -- uart_datalogger_protocol.yml status_codes). The lifecycle frames used 0x03
  -- and 0x00, which decoded as START_ERROR and CALIBRATION_DONE.
  constant C_UART_EVENT_ENABLED  : std_logic_vector(7 downto 0) := x"05";
  constant C_UART_EVENT_DISABLED : std_logic_vector(7 downto 0) := x"06";
  signal lifecycleMsg : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal lifecycleVld : std_logic;
  signal frameGenMsg  : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal frameGenVld  : std_logic;
  -- DS-P2.239: a failed autoSync calibration holds the frame generator in IDLE
  -- (startStreaming), so without this frame the data logger emits nothing after
  -- its ENABLED frame. STATUS 0x01 CALIBRATION_TIMEOUT ("no valid frames
  -- detected", uart_datalogger_protocol.yml) is sent once per failed
  -- calibration, held until UART_MSG_RDY takes it.
  constant C_UART_CALIBRATION_TIMEOUT : std_logic_vector(7 downto 0) := x"01";
  constant C_UART_DEV_ID_SLV : std_logic_vector(G_UART_ID_WIDTH-1 downto 0)
                             := std_logic_vector(to_unsigned(G_UART_DEV_ID, G_UART_ID_WIDTH));
  signal calFailMsg_r  : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
  signal calFailVld_r  : std_logic := '0';
  signal calFailSent_r : std_logic := '0';

begin

  -- DS-P2.154: a derived prescaler of 0 means the clock is below
  -- 25 * C_UART_MAX_BAUDRATE and the sampler cannot reach the validated
  -- resolution — refuse to elaborate rather than divide by zero downstream.
  assert C_PRESCALER >= 1
    report "uart_datalogger: G_SYSTEM_CLK_FREQ = " & integer'image(G_SYSTEM_CLK_FREQ)
         & " is too slow for C_UART_MAX_BAUDRATE = " & integer'image(C_UART_MAX_BAUDRATE)
         & " (derived prescaler 0)"
    severity FAILURE;

    -- Debug traces
  GEN_DEBUG_TRACES : if C_DEBUG_TRACES = 1 generate
    assert FALSE report "C_UART_MAX_BAUDRATE : " & integer'image(C_UART_MAX_BAUDRATE) severity NOTE;
    assert FALSE report "C_UART_MIN_BAUDRATE = " & integer'image(C_UART_MIN_BAUDRATE) severity NOTE;
    assert FALSE report "G_SYSTEM_CLK_FREQ = " & integer'image(G_SYSTEM_CLK_FREQ) severity NOTE;
    assert FALSE report "C_PRESCALER = " & integer'image(C_PRESCALER) severity NOTE;
    assert FALSE report "TICKS_PER_BIT_AT_MAX_BAUD = " & integer'image(G_SYSTEM_CLK_FREQ / C_PRESCALER / C_UART_MAX_BAUDRATE) severity NOTE;
    assert FALSE report "C_MAX_RELATIVE_DISTANCE_CYCLES = " & integer'image(f_max_relative_distance_cycles(G_SYSTEM_CLK_FREQ)) severity NOTE;
    assert FALSE report "C_DIST_WIDTH = " & integer'image(C_DIST_WIDTH) severity NOTE;
    assert FALSE report "C_FIFO_DEPTH = " & integer'image(C_FIFO_DEPTH) severity NOTE;
    assert FALSE report "C_LOCAL_FIFO_WIDTH = " & integer'image(C_LOCAL_FIFO_WIDTH) severity NOTE;
    assert FALSE report "C_LOCAL_BRAM_WIDTH = " & integer'image(C_LOCAL_BRAM_WIDTH) severity NOTE;
    assert FALSE report "C_BUFFER_TIMEOUT_LIMIT = " & integer'image(C_BUFFER_TIMEOUT_LIMIT) severity NOTE;
  end generate GEN_DEBUG_TRACES;

  -- First stage, sample with the system clock the UART pins
  -- until it discovers the start bit - stop bit sequence
  -- In parallel, it stores the minimum bit width to determine the baudrate

  -- The minimum bit width is used to create the sampling clock. There will be
  -- 16 samples always, regardless the stop bit position, as it is not known
  -- and the last bit may be a parity bit, or even the data bit itself.

  -- UART Frame Format:
  -- UART transmits data asynchronously using a framed serial format composed of:
  -- 1. START BIT  (1 bit): Logic '0' to mark the beginning of a frame.
  -- 2. DATA BITS  (5–9 bits): Payload bits transmitted LSB first.
  -- 3. PARITY BIT (1 bit, optional): Provides basic error detection (even or odd parity).
  -- 4. STOP BIT(S) (1, 1.5, or 2 bits): Logic '1' marking the end of the frame.
  --
  -- Frame length: 7–13 bits depending on configuration.
  -- Idle state: Logic '1' (mark condition).
  -- Baud rate: Defines the duration of one bit period and thus the overall transmission speed.

  --   Idle     Start                Data (LSB first)            Parity   Stop
  --    ────┐    ┌───────────────────────────────────────────────┐   ┌───────
  --        └────┘                                                └───┘
  -- Level    1    0   d0   d1   d2   d3   d4   d5   d6   d7      P     1..1
  --                ┌───┬───┬───┬───┬───┬───┬───┬───┬───┐
  -- Bit idx        [S] [0] [1] [2] [3] [4] [5] [6] [7]  (5–9 data bits)
  --
  --               <--------- each cell is one bit period (Tbit) --------->

  -- With enough UART frames, by accumulating edge timing statistics,
  -- we can infer:
  -- 1. Baud rate → from repeated edge spacing
  -- 2. Stop region → persistent ‘1’s near end
  -- 3. Data + parity width → distance from start to stop
  -- 4. Parity presence/type → statistical check across frames

  -- Implementation:
  -- 1. Edge detection and baud rate estimation
  -- 1.a Store in a BRAM a number of super sampled UART frames
  -- The number of samples per bit is dependant on the maximux supported baudrate
  -- e.g: 16 samples in a 2Mbps → 125ns per sample. In 9600bps, this is 10416ns per bit
  -- 1.b While looking for the STOP bit, some baud rates can be discarded. E.g, 2Mbps would
  -- require a STOP bit after 13bits x 125ns = 1625ns. But how to determine if it is an STOP
  -- or actual data?
  -- There is only 1 garaneed 1-0-1 transition: START + STOP. The problem is that that can be
  -- confused with data bits.
  -- Idea: Don't store the oversampling, but the moment of the togglings. Then 
  -- reproduce the waveform and toggle when sampled line with a counter.
  -- Then, give time or a max number of toggles to determine both the baudate and the frame size.
  --  Store in a FIFO as the normal way of storing the data. Use a BRAM for the analysis when 
  -- determining the characteristics. The FIFO is the actual buffer, while the BRAM

  -- From the BRAM, the data needs to be extracted, and sampled with the selected baudrate.
  -- As it is sampled, we need to create 13-7+1 = 7 tables of statistics. This tables are histograms
  -- so parity and fundamentally the stop bit position can be determined.
  -- The stop bit is always '1', and the parity bit needs to checked against the data.
  -- There is an elimination process where tables are ruled out when they don't satisfy
  -- the stop condition. Again, '1' needs to be persistent. If it can't be determined with only
  -- one max frame (13 bits), then multiple frames are needed.
  -- Eventually, there needs to be another START BIT. After the STOP bit position, the next
  -- thing to determine is the presence of parity, and eventually, the number of STOP bits.

  uartPin_r <= UART_IN when rising_edge(CLK); -- it costs nothing and helps in standalone simulation

    -- Register the configuration values
  i_config_control : entity tich.config_control
    generic map (
      G_DEV_ID_WIDTH => G_UART_ID_WIDTH,
      G_DEV_ID       => G_UART_DEV_ID,
      G_CONFIG_WIDTH => G_CONFIG_WIDTH
      )
    port map (
      CLK        => CLK,
      RST        => RST,
      CONFIG     => CONFIG,
      CONFIG_OUT => deviceLoadConfig,
      CAPTURE_RST  => dataloggerRst
      );

  configEnable  <= deviceLoadConfig(0);
  autoSync      <= deviceLoadConfig(1);
  dataBits      <= deviceLoadConfig(4 downto 2);
  parityEn      <= deviceLoadConfig(5);
  parityTypeDef <= deviceLoadConfig(6);
  stopBitsDef   <= deviceLoadConfig(10 downto 7);


  -- Detect the edge on the input signal
  i_edge_event : entity tich.edge_detector
    generic map(
      G_EDGE_TYPE => C_EDGE_TYPE_BOTH
    )
    port map
    (
      CLK => CLK,
      CE  => '1',
      D   => uartPin_r,
      Q   => edgeDetected
    );

  ------------------------------------------------
  -- Timebase generation
  ------------------------------------------------
  pr_timebase: process (CLK)
  begin
    if rising_edge(CLK) then
      -- To be implemented: Timebase generation based on edge timing
      -- to target max 3Mbps, we need to sample at leas as fast as 2x320ns
      -- which is 3.125MHz. With a 100MHz clock, that is 2*32 clock cycles.
      -- What is saved is the distance, in this time unit, of each edge relative to the previous one.
      -- From there, we can reconstruct the waveform by toggling a signal every time the counter
      -- reaches the stored value.
      timeStampTicker_r    <= '0';
      timeStampTickerCnt_r <= timeStampTickerCnt_r + 1;
      if timeStampTickerCnt_r = C_PRESCALER - 1 then
        timeStampTicker_r    <= '1';
        timeStampTickerCnt_r <= (others => '0');
      end if;
      -- Relative distance counter
      if timeStampTicker_r = '1' then
        -- Stop counting at max value (do not roll over). DS-T2.30: cap in
        -- prescaled TICKS (C_MAX_DIST_TICKS), not raw cycles — see constant.
        if relativeDistCnt_r < C_MAX_DIST_TICKS - 1 then
          relativeDistCnt_r <= relativeDistCnt_r + 1;
        end if;
      end if;
      if edgeDetected = '1' then
        -- DS-P2.236: a tick in the edge cycle belongs to the new interval.
        -- Resetting to 0 dropped it, so at the prescaler phase where edges
        -- land on the tick every interval recorded one tick short.
        relativeDistCnt_r    <= (0 => timeStampTicker_r, others => '0');
        relativeDistanceIn_r <= relativeDistCnt_r;
      end if;
      if dataloggerRst = '1' then
        relativeDistCnt_r    <= to_unsigned(C_MAX_BW_UART_CYCLES, relativeDistCnt_r'length);
        relativeDistanceIn_r <= (others => '1');
      end if;
    end if;
  end process;


  pr_timeout_idle: process(CLK)
  begin
    if rising_edge(CLK) then
      timeOutDist_r <= '0';
      if calibrationDone_r = '1' then
        -- Only time out when calibrated
        if relativeDistCnt_r >= baudRateMin_r & "0000" then
          timeOutDist_r <= '1';
        end if;
      end if;
    end if;
  end process;

  -- Detect the edge on the input signal
  i_timeout_dist_event : entity tich.edge_detector
    generic map(
      G_EDGE_TYPE => C_EDGE_TYPE_RISING
    )
    port map
    (
      CLK => CLK,
      CE  => '1',
      D   => timeOutDist_r,
      Q   => timeOutEdgeDetected
    );

  ----------------------------------------------
  -- FIFO to store the edges
  ----------------------------------------------
  -- Store the waveform in the FIFO to be reproduced later with the determined baudrate
  -- Slice TIMESTAMP to uart_pkg.C_TIMESTAMP_WIDTH (16 bits) — the FIFO
  -- width `C_LOCAL_FIFO_WIDTH = f_fifo_width(G_SYSTEM_CLK_FREQ, C_PRESCALER)` is sized
  -- against uart_pkg's local 16-bit timestamp constant, not the global
  -- 32-bit one in datalogger_pkg. Without this slice, datalogger_gather
  -- (which passes G_TIMESTAMP_WIDTH=32) trips a width-mismatch at synth.
  fifoUartDin <= TIMESTAMP(tich.uart_pkg.C_TIMESTAMP_WIDTH-1 downto 0) & (not uartPin_r) & std_logic_vector(relativeDistCnt_r);

  pr_store_edges: process (CLK)
  begin
    if rising_edge(CLK) then
      fifoUartDin_r  <= fifoUartDin;
      fifoUartWrEn_r <= '0';
      if configEnable = '1' then
        fifoUartWrEn_r <= edgeDetected;
        if timeOutEdgeDetected = '1' then
          fifoUartDin_r(C_DIST_WIDTH)  <= '1';
          fifoUartWrEn_r            <= '1';
        end if;
      end if;
    end if;
  end process;


  i_fifo_buffer : entity tich.fifo_sync
    generic map
    (
      G_FIFO_DEPTH  => C_FIFO_DEPTH,
      G_FIFO_WIDTH  => C_LOCAL_FIFO_WIDTH,
      G_FIFO_FWFT   => 1,
      G_FULL_SLACK  => 16, -- Allows time to process what is already buffered
      G_CALLER_NAME => "UART Datalogger"
    )
    port map
    (
      RST       => dataloggerRst,
      CLK       => CLK,
      DIN       => fifoUartDin_r,
      WR_EN     => fifoUartWrEn_r,
      RD_EN     => fifoUartRdEn_r,
      DOUT      => fifoUartDout,
      FULL      => fifoUartFull,
      PROG_FULL => fifoUartAlmostFull,
      EMPTY     => fifoUartEmpty,
      DATA_CNT  => fifoUartDataCnt,
      RST_BUSY  => fifoUartRstBusy
    );

  --------------------------------------------------------------
  -- FSM to buffer the edges for later analysis, store in BRAM.
  --------------------------------------------------------------
  pr_fsm_buffer_statistics : process(all)
  begin
    fsmBufferUart_n <= fsmBufferUart_r;
    bramWrAddr_n     <= bramWrAddr_r;
    bufferFinish_n   <= bufferFinish_r;
    bramWrEn_n       <= '0';
    baudRateMin_n    <= baudRateMin_r;
    error_n          <= uart_error;
    case fsmBufferUart_r is
      when DISABLED =>
        bufferFinish_n <= '0';
        baudRateMin_n  <= (others => '1');
        if configEnable = '1' then
          fsmBufferUart_n <= IDLE;
          if uartPin_r = '0' then -- If line is not IDLE, that's an error, as we can't detect the start bit.
            error_n(0) <= '1';
          end if;
        end if;
      when IDLE =>
        if bufferFinish_r = '0' then -- Stay here when done
          fsmBufferUart_n <= WRITE_BUFFER;
          bramWrAddr_n    <= (others => '1'); -- prepare for address 0
        end if;
      when WRITE_BUFFER =>
        if edgeDetected = '1' then
          bramWrAddr_n <= bramWrAddr_r + 1;
          bramWrEn_n   <= '1';
        end if;
        if relativeDistanceIn_r < baudRateMin_r then
          baudRateMin_n <= relativeDistanceIn_r;
        end if;
        -- Timeout condition to end buffering, no more data coming in.
        -- FIFO flags can be used here as BRAM and FIFO are written at the same time.
        -- Threshold = 16 idle bit-times (baudRateMin_r sll 4), the same overflow-safe
        -- idiom as the post-calibration timeout (pr_timeout_idle). The old
        -- C_BUFFER_TIMEOUT_LIMIT (= C_UART_MIN_BAUDRATE*16 = 153600) was dead code:
        -- relativeDistCnt_r is only C_DIST_WIDTH (15) bits (max 32767 ticks), so the
        -- compare was never true and short bursts (<~25 bytes, below the FIFO-fill
        -- path below) never flushed to the decoder. (DS-T2.27)
        if relativeDistCnt_r >= baudRateMin_r & "0000" then
          if fifoUartEmpty = '0' then -- only if something was stored
            fsmBufferUart_n <= IDLE;
            bufferFinish_n  <= '1';
          else
            fsmBufferUart_n <= IDLE; -- Like nothing happened
            bufferFinish_n  <= '0';
          end if;
        elsif unsigned(fifoUartDataCnt) >= 2**(C_FIFO_DEPTH-3)-1 then -- TODO: make it configurable, so the buffering can be much smaller
          fsmBufferUart_n <= IDLE;
          bufferFinish_n  <= '1';
        end if;
      when others =>
        fsmBufferUart_n <= IDLE;
    end case;
  end process;

  pr_reg_fsm_buffer_statistics : process(CLK)
  begin
    if rising_edge(CLK) then
      fsmBufferUart_r <= fsmBufferUart_n;
      bramWrAddr_r     <= bramWrAddr_n;
      bramDin_r        <= fifoUartDin(C_LOCAL_BRAM_WIDTH-1 downto 0); -- Take only edge type + relative distance
      bramWrEn_r       <= bramWrEn_n;
      bufferFinish_r   <= bufferFinish_n;
      baudRateMin_r    <= baudRateMin_n;
      if dataloggerRst = '1' then
        fsmBufferUart_r <= DISABLED;
        bramWrAddr_r    <= C_BRAM_ADDR_INIT;
        baudRateMin_r   <= (others => '1');
        bramWrEn_r      <= '0';
        bufferFinish_r  <= '0';
      end if;
    end if;
  end process;

  i_ram_dual_port : entity tich.ram_dual_port
    generic map (
      G_WIDTH     => C_LOCAL_BRAM_WIDTH,             -- data width
      G_DEPTH     => C_FIFO_DEPTH,              -- address width (2^G_DEPTH locations)
      G_REG_OUT   => "false",                      -- register read outputs
      G_RAM_STYLE => "auto"                        -- "distributed" | "block" | "ultra"
    )
    port map (
      CLKA  => CLK,                               -- clock for port A
      CLKB  => CLK,                               -- clock for port B
      WEA   => bramWrEn_r,                        -- write enable for port A
      ADDRA => std_logic_vector(bramWrAddr_r),    -- address for port A
      ADDRB => std_logic_vector(bramRdAddr_r),    -- address for port B
      DIA   => bramDin_r,                         -- data input for port A
      DOA   => open,                              -- data output from port A
      DOB   => bramDout                           -- data output from port B
    );


  ------------------------------------------------------
  -- Calibration FSM
  ------------------------------------------------------
  relativeDistDout <= unsigned(bramDout(C_DIST_WIDTH-1 downto 0));
  levelDout        <= bramDout(bramDout'left);

  pr_baudrate_generation: process(CLK)
  begin
    if rising_edge(CLK) then
      if stopFinderEn_r = '1' then -- Enabled by the FSM below
        baudRateGenCnt_r <= baudRateGenCnt_r + 1;
        -- Toggle the tick at half period for better sampling
        if baudRateGenCnt_r = baudRateMin_r/2 - 1 then
          baudRateTick_r   <= not baudRateTick_r;
        elsif baudRateGenCnt_r = baudRateMin_r - 1 then
          baudRateGenCnt_r <= (others => '0');
        end if;
      end if;
      if dataloggerRst = '1' then
        baudRateGenCnt_r <= (others => '0');
        baudRateTick_r   <= '0';
      end if;
    end if;
  end process;


  -- TODO: If calibration fails, allow buffering more data and re-run the calibration
  pr_calibration_fsm : process(all)
  begin
    fsmCalibrate_n         <= fsmCalibrate_r;
    bramRdAddr_n           <= bramRdAddr_r;
    bramRdEn_n             <= '0';
    uart_data_rd           <= '0';
    calibrationDone_n      <= calibrationDone_r;
    relativeDistDoutPrev_n <= relativeDistDoutPrev_r;
    stopBitFound_n         <= stopBitFound_r;
    parityBitFound_n       <= parityBitFound_r;
    nbitsConf_n            <= nbitsConf_r;
    calibrationFail_n      <= calibrationFail_r;
    parityConf_n           <= parityConf_r;
    stopFinderEn_n         <= stopFinderEn_r;
    stopIndex_n            <= stopIndex_r;
    stopBitsConf_n         <= stopBitsConf_r;
    settleCnt_n            <= settleCnt_r;
    formatFound_n          <= formatFound_r;
    --
    case fsmCalibrate_r is
      when DISABLED => -- Triggered by end of buffer
        if bufferFinish_r = '1' and calibrationDone_r = '0' then
          if autoSync = '0' then
            -- DS-T3.9 latency path: explicit host config — skip the stop-finder
            -- baud/format replay and go straight to done. baudRateMin_r is
            -- already measured from the buffered edges; the byte FORMAT comes
            -- from the muxed config (nbitsSel/paritySel/stopBitsSel). This lets
            -- a short burst (even a few chars) decode immediately, without the
            -- stop-finder needing enough frames to lock (which fails on ~6 bytes).
            fsmCalibrate_n <= CALIBRATION_DONE;
          else
            fsmCalibrate_n         <= DETECT_STOP_BIT;
            settleCnt_n            <= (others => '0');
            formatFound_n          <= '0';
            bramRdAddr_n           <= C_BRAM_ADDR_INIT;
            relativeDistDoutPrev_n <= relativeDistDout;
          end if;
        end if;
      when DETECT_STOP_BIT =>
        stopFinderEn_n <= '1';
        if pbFull = '0' then
          bramRdAddr_n <= bramRdAddr_r + 1;
          bramRdEn_n   <= '1';
        end if;
      -- This implies reproducing the waveform with the detected baudrate
      -- considering up to 13-7 = 6 possible frames
        if bramRdAddr_r = bramWrAddr_r-1 then
          fsmCalibrate_n <= WAIT_FOR_DETECTION;
          bramRdEn_n     <= '0';
        end if;
      when WAIT_FOR_DETECTION =>
        -- DS-P2.242: decide on the whole buffer, not on the first instance to
        -- reach G_MIN_MATCHES: a shorter instance can match the first frames
        -- only because the line's top data bit happened to be 1 in each.
        if pbEmpty = '1' then -- the replay has consumed every buffered edge
          settleCnt_n <= settleCnt_r + 1;
          if settleCnt_r >= (baudRateMin_r & '0') then -- two bit-times
            fsmCalibrate_n <= CALIBRATION_DONE;
            if pick_format(stopMatch).cnt_hots > 0 then
              stopIndex_n   <= pick_format(stopMatch).index;
              formatFound_n <= '1';
            else
              calibrationFail_n <= '1'; -- no instance matched the buffer
            end if;
          end if;
        else
          settleCnt_n <= (others => '0');
        end if;
      when CALIBRATION_DONE =>
        calibrationDone_n <= '1';
        fsmCalibrate_n    <= DISABLED;
        stopBitFound_n    <= '0';
        parityBitFound_n  <= '0';
        if formatFound_r = '1' then
          stopBitFound_n   <= '1';
          parityBitFound_n <= parityMatch(stopIndex_r);
          nbitsConf_n      <= stopIndex_r + C_POSSIBLE_UART_CONFS - sl_to_integer(parityMatch(stopIndex_r));
          parityConf_n     <= parityType(stopIndex_r) & parityMatch(stopIndex_r);
          stopBitsConf_n   <= stopBitsN(stopIndex_r);
        end if;
      when others =>
        fsmCalibrate_n <= DISABLED;
    end case;
  end process;

  process(CLK)
  begin
    if rising_edge(CLK) then
      fsmCalibrate_r         <= fsmCalibrate_n;
      bramRdAddr_r           <= bramRdAddr_n;
      bramRdEn_r             <= bramRdEn_n;
      calibrationDone_r      <= calibrationDone_n;
      relativeDistDoutPrev_r <= relativeDistDoutPrev_n;
      stopBitFound_r         <= stopBitFound_n;
      parityBitFound_r       <= parityBitFound_n;
      nbitsConf_r            <= nbitsConf_n;
      parityConf_r           <= parityConf_n;
      calibrationFail_r      <= calibrationFail_n;
      stopFinderEn_r         <= stopFinderEn_n;
      stopIndex_r            <= stopIndex_n;
      stopBitsConf_r         <= stopBitsConf_n;
      settleCnt_r            <= settleCnt_n;
      formatFound_r          <= formatFound_n;
      --
      -- DS-T1.16: the per-datalogger reset clears the WHOLE calibration result,
      -- not just the FSM. Synplify ignores VHDL initial values on PolarFire
      -- (FX1172), so an unreset flop powers up however the silicon likes:
      -- calibrationFail_r read '1' on the kit in explicit 8N1 mode, where
      -- nothing sets it, and startStreaming (calibrationDone and not
      -- calibrationFail) held the frame generator in IDLE forever — no CONFIG,
      -- no bytes, and a disable/enable could not recover it because this reset
      -- never touched it. The same stickiness bit an autoSync calibration that
      -- failed once. A re-arm must re-lock from nothing.
      -- (baudRateMin_r is owned by pr_reg_fsm_buffer_statistics alone.)
      if dataloggerRst = '1' then
        fsmCalibrate_r    <= DISABLED;
        bramRdAddr_r      <= (others => '0');
        bramRdEn_r        <= '0';
        calibrationDone_r <= '0';
        calibrationFail_r <= '0';
        stopFinderEn_r    <= '0';
        stopBitFound_r    <= '0';
        parityBitFound_r  <= '0';
        nbitsConf_r       <= 0;
        parityConf_r      <= (others => '0');
        stopIndex_r       <= 0;
        stopBitsConf_r    <= '0';
        settleCnt_r       <= (others => '0');
        formatFound_r     <= '0';
      end if;
    end if;
  end process;

  -- Instance
  i_uart_playback : entity tich.uart_playback
    generic map (
      G_DIST_WIDTH => C_DIST_WIDTH,
      G_FIFO_DEPTH => 8,              -- tbd
      G_TIME_EN    => 0 -- Not used
    )
    port map (
      CLK       => CLK,
      RST       => dataloggerRst,
      ENABLE    => stopFinderEn_r,
      PB_CLK    => '1', -- WtimeStampTicker_r,
      LEVEL     => levelDout,
      TIMESTAMP => (others => '0'), -- Not used
      DISTANCE  => std_logic_vector(relativeDistDout),
      VLD       => pbWrEn_r,
      EMPTY     => pbEmpty,
      FULL      => pbFull,
      WAVE      => wavePlayback
    );

    pbWrEn_r <= bramRdEn_r when rising_edge(CLK); -- Synchronize with data production

  stopFinderJudge <= not pbEmpty;

  -- Instance
  GEN_STOP_FINDER : for i in 0 to C_POSSIBLE_UART_CONFS-1 generate
  begin
    i_uart_stop_finder : entity tich.uart_stop_finder
      generic map (
        G_CONF_NBITS => 5 + i, -- From min 5 to max 9 data bits
        G_DIST_WIDTH => C_DIST_WIDTH,
        G_MIN_MATCHES => 7 -- Number of consecutive '1's to consider a valid STOP bit. TODO: Needs to be related with the FIFO/BRAM buffer depth
      )
      port map (
        CLK       => CLK,
        RST       => dataloggerRst,
        ENABLE    => stopFinderEn_r,
        BR_CYCLES => std_logic_vector(baudRateMin_r),
        WAVE      => wavePlayback,
        JUDGE     => stopFinderJudge,    -- DS-P2.242: refute only on recorded edges
        MATCH     => stopMatch(i),       -- STOP bit found
        PARITY(0) => parityMatch(i),      -- Parity bit found
        PARITY(1) => parityType(i),
        STOP_BITS => stopBitsN(i)
      );

  end generate GEN_STOP_FINDER;

  ------------------------------------------------------
  -- Operation FSM: Reproduces the waveform stored in the FIFO,
  -- and generates the messages for the SW.
  ------------------------------------------------------

    i_uart_streamer : entity tich.uart_playback
    generic map (
      G_DIST_WIDTH => C_DIST_WIDTH,
      G_FIFO_DEPTH => 8,              -- tbd
      G_TIME_EN    => 1
    )
    port map (
      CLK      => CLK,
      RST      => dataloggerRst,
      ENABLE   => '1',
      PB_CLK   => '1',
      TIMESTAMP=> fifoUartDout(C_LOCAL_BRAM_WIDTH + tich.uart_pkg.C_TIMESTAMP_WIDTH-1 downto C_LOCAL_BRAM_WIDTH),
      LEVEL    => fifoUartDout(C_DIST_WIDTH),
      DISTANCE => fifoUartDout(C_DIST_WIDTH-1 downto 0),
      VLD      => streamPbValid,
      EMPTY    => streamEmpty,
      FULL     => streamFull,
      WAVE     => uartSreamWave
    );


    -- Frame-format select (DS-T3.9). autoSync='1' uses the auto-calibrated
    -- format; autoSync='0' uses the host-supplied explicit config and the
    -- calibration FSM skips the stop-finder replay (see pr_calibration_fsm).
    -- The baud (BR_CYCLES => baudRateMin_r) is ALWAYS auto-measured from the
    -- buffered edges — explicit config sets the byte FORMAT only.
    --   nbits = data bits (5..9) = dataBits + 5
    --   parity = [parityType, parityEn]  (matches parityConf encoding)
    --   stopBits = 0:1 stop, 1:1+ stop
    nbitsSel    <= (to_integer(unsigned(dataBits)) + 5) when autoSync = '0' else nbitsConf_r;
    paritySel   <= (parityTypeDef & parityEn)           when autoSync = '0' else parityConf_r;
    stopBitsSel <= stopBitsDef(0)                       when autoSync = '0' else stopBitsConf_r;

    -- TODO: The UART final calibration conf would be elegantly be presented inside a record
    i_uart_frame_gen : entity tich.uart_frame_gen
    generic map (
      G_UART_DEV_ID     => G_UART_DEV_ID,     -- default 26
      G_UART_ID_WIDTH   => G_UART_ID_WIDTH,   -- default 5
      G_MSG_WIDTH       => C_DATALOGGER_MSG_WIDTH,  -- e.g. 8..16
      G_BR_CYCLES_WIDTH => C_DIST_WIDTH
    )
    port map (
      CLK       => CLK,
      RST       => dataloggerRst,
      ENABLE    => startStreaming,
      READY     => not streamEmpty,
      -- DS-T2.42: feed the external consumer accept so the post-cal CONFIG frame
      -- completes EXACTLY one handshake. Previously UART_MSG_RDY was an unused
      -- input and the frame gen leaned on 'not streamEmpty' (internal playback
      -- FIFO state) to decide when the CONFIG was "accepted" — holding valid for
      -- several cycles at calibration-done and emitting ~7 duplicate CONFIG
      -- frames to any ready-every-cycle consumer (gather arbiter, DDR ring).
      OUT_RDY   => UART_MSG_RDY,
      BR_CYCLES => std_logic_vector(baudRateMin_r),
      WAVE      => uartSreamWave,
      NBITS     => nbitsSel,
      PARITY    => paritySel,
      STOP_BITS => stopBitsSel,
      MSG_OUT   => frameGenMsg,
      MSG_VLD   => frameGenVld
    );

    -- DS-P3.90 Phase 1: uniform lifecycle emitter, on GLOBAL RST so an
    -- ENABLE=0 (dataloggerRst) still produces the DISABLED frame (H.96).
    i_lifecycle : entity tich.datalogger_lifecycle
      generic map (
        G_DEV_ID        => G_UART_DEV_ID,
        G_DEV_ID_WIDTH  => G_UART_ID_WIDTH,
        G_TYPE          => C_UART_EVENT_TYPE,
        G_TS_WIDTH      => tich.uart_pkg.C_TIMESTAMP_WIDTH,
        G_CODE_ENABLED  => C_UART_EVENT_ENABLED,
        G_CODE_DISABLED => C_UART_EVENT_DISABLED
        )
      port map (
        CLK       => CLK,
        RST       => RST,   -- GLOBAL reset, NOT dataloggerRst
        CONFIG_EN => configEnable,
        TIMESTAMP => TIMESTAMP(tich.uart_pkg.C_TIMESTAMP_WIDTH-1 downto 0),
        MSG_OUT   => lifecycleMsg,
        MSG_VLD   => lifecycleVld
        );

    -- Output mux — lifecycle frame (ENABLED/DISABLED) takes top priority so an
    -- enable/disable marker is never masked by a coincident payload frame.
    -- DS-P2.239: the calibration-failure STATUS is next; the frame generator
    -- is idle while it is pending (startStreaming is 0 on a failure).
    UART_MSG_OUT <= lifecycleMsg when lifecycleVld = '1' else
                    calFailMsg_r when calFailVld_r = '1' else
                    frameGenMsg;
    UART_MSG_VLD <= lifecycleVld or calFailVld_r or frameGenVld;

    assert (G_UART_ID_WIDTH + C_UART_EVENT_TYPE'length + tich.uart_pkg.C_TIMESTAMP_WIDTH
            + C_UART_CALIBRATION_TIMEOUT'length) = C_DATALOGGER_MSG_WIDTH
      report "uart_datalogger: DEV_ID+TYPE+TS+CODE width /= C_DATALOGGER_MSG_WIDTH"
      severity failure;

    pr_calibration_fail_status : process(CLK)
    begin
      if rising_edge(CLK) then
        if calFailVld_r = '1' and UART_MSG_RDY = '1' and lifecycleVld = '0' then
          calFailVld_r <= '0';                        -- taken by the consumer
        end if;
        if calibrationFail_r = '1' and calFailSent_r = '0' then
          calFailMsg_r  <= C_UART_DEV_ID_SLV & C_UART_EVENT_TYPE
                           & TIMESTAMP(tich.uart_pkg.C_TIMESTAMP_WIDTH-1 downto 0)
                           & C_UART_CALIBRATION_TIMEOUT;
          calFailVld_r  <= '1';
          calFailSent_r <= '1';
        end if;
        if dataloggerRst = '1' then                   -- a re-arm re-calibrates
          calFailVld_r  <= '0';
          calFailSent_r <= '0';
        end if;
      end if;
    end process;


    startStreaming <= calibrationDone_r and not calibrationFail_r;
    streamPbValid  <= fifoUartRdEn_r and not fifoUartEmpty;

    pr_fsm_read_fifo_uart : process(CLK)
    begin
      if rising_edge(CLK) then
        fifoUartRdEn_r <= '0';
        case fsmUartStream_r is
          when IDLE =>
            if calibrationDone_r = '1' then
              fsmUartStream_r <= WAIT_FOR_BUFFER;
            end if;
          when WAIT_FOR_BUFFER =>
            -- Wait until a complete UART frame is available
            if fifoUartEmpty = '0' and streamFull = '0' then
              fsmUartStream_r <= STREAM;
            end if;
          when STREAM =>
            fifoUartRdEn_r <= '1';
            -- Either the stream is full of requests, or the input FIFO is empty
            -- In that case, no more readings can be consumed from the receiving FIFO
            if streamFull = '1' or fifoUartEmpty = '1' then
              fsmUartStream_r <= IDLE;
            end if;
          when others =>
            fsmUartStream_r <= IDLE;
        end case;
        -- DS-T2.31: the read-side FSM was previously unreset. A per-datalogger
        -- reset must return it to IDLE so a reconfiguration does not resume a
        -- mid-STREAM read against freshly-flushed buffers.
        if dataloggerRst = '1' then
          fsmUartStream_r <= IDLE;
          fifoUartRdEn_r  <= '0';
        end if;
      end if;
    end process;

    UART_ERRORS <= error_r;

end rtl;
