-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : qspi_datalogger
-- Project    : datalogger
-------------------------------------------------------------------------------
-- File       : qspi_datalogger.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    :
-- Created    : 2025-02-11
-- Last update: 2025-02-11
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: QSPI raw data datalogger
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

entity qspi_datalogger is
    generic (
      G_QSPI_ID_WIDTH   : integer                                    := 5;
      G_QSPI_DEV_ID     : natural range 0 to (2**G_QSPI_ID_WIDTH)-1  := 28;
      G_QSPI_PINS       : natural range 3 to 6                       := 6; -- 4 data pins + SCLK + CSn
      G_TIMESTAMP_WIDTH : natural range 16 to 32                     := 16;
      G_CONFIG_WIDTH    : integer                                    := 16;
      G_ERROR_WIDTH     : integer                                    := 2;
      G_ENABLE_INFILTER : integer                                    := 0;
      G_ILA_EN          : integer                                    := 1;
      -- DS-P3.92: sample-clock frequency, used to scale the SCLK-stuck timeout.
      -- Silicon = 100 MHz; testbenches pass a scaled-down value so the EV_TIMEOUT
      -- path is reachable in a handful of sim cycles. (Same pattern as the i2c
      -- DS-T2.34 G_CLK_FREQ_HZ fix — see feedback_sim_hw_mismatch_scale_timeouts.)
      G_CLK_FREQ_HZ     : natural                                    := 100_000_000
      );
    port (
      CLK         : in  std_logic;
      RST         : in  std_logic;
      QSPI_PINS   : in  std_logic_vector(G_QSPI_PINS-1 downto 0);
      CONFIG      : in  std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
      TIMESTAMP   : in  std_logic_vector(G_TIMESTAMP_WIDTH-1 downto 0);
      -- QSPI
      QSPI_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
      QSPI_MSG_VLD : out std_logic;
      QSPI_MSG_RDY : in  std_logic;
      --
      QSPI_ERRORS : out std_logic_vector(G_ERROR_WIDTH-1 downto 0)
      );
end qspi_datalogger;


architecture rtl of qspi_datalogger is

  -----------------------------------------------------------------------------
  -- Constants
  -----------------------------------------------------------------------------
  constant C_QSPI_DEV_ID_UNS           : std_logic_vector(G_QSPI_ID_WIDTH-1 downto 0) := std_logic_vector(to_unsigned(G_QSPI_DEV_ID, G_QSPI_ID_WIDTH));
  constant C_SHIFT_REG_WIDTH           : integer := 8;
  constant C_SHIFT_REG_CNT_WIDTH       : integer := integer(ceil(log2(real(C_SHIFT_REG_WIDTH))));
  -- DS-P3.92: derive the SCLK-stuck timeout from the sample-clock frequency so
  -- it is (a) correctly scaled and (b) sim-reachable via a scaled-down
  -- G_CLK_FREQ_HZ. C_TIMEOUT_MS is the stuck-bus detection window: ~1 ms is
  -- ~1000 bit-periods at 1 MHz (the slowest expected QSPI) — long enough never
  -- to false-fire on live traffic (the counter resets on every sclkRisingEdge),
  -- far faster than the previous hard-coded 1-second (100_000_000-cycle) value.
  constant C_TIMEOUT_MS                : natural := 1;
  constant C_TIMEOUT_CNT               : integer := (G_CLK_FREQ_HZ/1000) * C_TIMEOUT_MS;
  constant C_TIMEOUT_CNT_WIDTH         : integer := integer(ceil(log2(real(C_TIMEOUT_CNT))));
  constant C_EVENT_CS_DOWN             : std_logic_vector(7 downto 0) := x"09";
  constant C_EVENT_CS_UP               : std_logic_vector(7 downto 0) := x"0A";
  constant C_EVENT_START               : std_logic_vector(7 downto 0) := x"03";
  constant C_EVENT_STOP                : std_logic_vector(7 downto 0) := x"00";
  constant C_EVENT_TIMEOUT             : std_logic_vector(7 downto 0) := x"04";
  constant C_EVENT_CS_UP_INCOMPLETE    : std_logic_vector(7 downto 0) := x"0B";
  constant C_EVENT_CS_DOWN_NEW_POL_H   : std_logic_vector(7 downto 0) := x"10";
  constant C_EVENT_CS_DOWN_NEW_POL_L   : std_logic_vector(7 downto 0) := x"11";
  constant C_RESERVED_BYTE             : std_logic_vector(7 downto 0) := (others => '0');
  constant C_QSPI_PACKET_PAYLOAD_TYPE  : std_logic_vector(2 downto 0) := "001";
  constant C_QSPI_PACKET_EVENT_TYPE    : std_logic_vector(2 downto 0) := "100";
  constant C_QSPI_PACKET_ERROR_TYPE    : std_logic_vector(2 downto 0) := "010";
  constant C_SPI_PACKET_PAYLOAD_TYPE_A : std_logic_vector(2 downto 0) := "001";
  constant C_SPI_PACKET_PAYLOAD_TYPE_B : std_logic_vector(2 downto 0) := "010";
  constant C_TIMESTAMP_SLICE_HIGH      : integer                      := 16;
  constant C_QSPI_DATA_PINS            : integer                      := G_QSPI_PINS-2;


  -----------------------------------------------------------------------------
  -- Types
  -----------------------------------------------------------------------------
  type t_fsmQspi is (STOP, WAIT_START, IDLE, SAMPLING, PAD_PACKET);
  type t_fsmSeq is (IDLE, GENERATE_PACKET_A, GENERATE_PACKET_B, SEND_EVENT);

  type t_qspiByte is array(0 to C_QSPI_DATA_PINS-1) of std_logic_vector(C_SHIFT_REG_WIDTH-1 downto 0);

  -----------------------------------------------------------------------------
  -- Signals
  -----------------------------------------------------------------------------

  signal fsmQspi_r     : t_fsmQspi := STOP;
  signal fsmQspi_n     : t_fsmQspi;

  signal fsmSeq_r      : t_fsmSeq := IDLE;

  signal qspiPinsFilter : std_logic_vector(G_QSPI_PINS-1 downto 0);
  signal qspiCSn        : std_logic;
  signal qspiSclk       : std_logic;
  signal qspiQx         : std_logic_vector(C_QSPI_DATA_PINS-1 downto 0);

  signal sclkRisingEdge  : std_logic;
  signal sclkFallingEdge : std_logic;
  signal qspiCE          : std_logic;
  signal csRisingEdge    : std_logic;
  signal csFallingEdge   : std_logic;
  signal csFallingEdge_r : std_logic := '0';
  --
  signal qspiSrQx_r    : t_qspiByte := (others => (others => '0'));
  --
  signal bufferPayload_r : std_logic := '0';
  signal bufferPayload_n : std_logic;
  signal cntSamples_r    : unsigned(C_SHIFT_REG_CNT_WIDTH-1 downto 0)         := (others => '0');
  signal cntSamples_n    : unsigned(C_SHIFT_REG_CNT_WIDTH-1 downto 0);
  signal payloadA        : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal payloadB        : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal eventPkt        : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal defaultPkt      : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal msgVld_r        : std_logic := '0';
  signal msg2Send_r      : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal newEvent_r      : std_logic := '0';
  signal newEvent_n      : std_logic;
  signal eventPending_r  : std_logic := '0';
  signal payloadPendint_r: std_logic := '0';
  signal eventMsg_r      : std_logic_vector(7 downto 0) := C_EVENT_CS_DOWN;
  signal eventMsg_n      : std_logic_vector(7 downto 0);
  signal eventPktBuff_r  : std_logic_vector(7 downto 0) := (others => '0');
  -- DS-T3.10: timestamps latched at CAPTURE time (not read combinationally at
  -- delivery — which, under QSPI_MSG_RDY backpressure, lags by the whole stall).
  signal tsPayload_r     : std_logic_vector(C_TIMESTAMP_SLICE_HIGH-1 downto 0) := (others => '0');
  signal tsEvent_r       : std_logic_vector(C_TIMESTAMP_SLICE_HIGH-1 downto 0) := (others => '0');
  signal timeOutError_r  : std_logic := '0';
  signal timeOutCnt_r    : unsigned(C_TIMEOUT_CNT_WIDTH-1 downto 0) := (others => '0');
  signal padMode_r       : std_logic := '0';
  signal padMode_n       : std_logic;

  signal deviceLoadConfig   : std_logic_vector(G_CONFIG_WIDTH-G_QSPI_ID_WIDTH-1 downto 0);
  signal configEnable       : std_logic;
  signal clockPolarity_r    : std_logic := '0';
  signal clockPhase         : std_logic;
  -- SPI data-line topology (vs default 4-wire full-duplex MOSI+MISO → A+B payloads):
  signal halfDuplexMode     : std_logic;  -- 3-wire: shared bidir SI/SO, CS-framed → A only
  signal noChipSelectMode   : std_logic;  -- 2-wire: clock+data, no CS → A only, no timeout
  signal prevPolarity_r     : std_logic := '0';
  signal prevPolarity_n     : std_logic;


  begin

    -- The QSPI datalogger samples the QSPI signals on the rising edge of the SCLK
    -- Then, it shifts the bits into a 8-bit buffer, preparing a packet out of it.
    -- The process starts when the CSn signal goes low and ends when it goes high.
    -- If CS goes high before the 8-bit buffer is completed, the buffer is padded. 
    -- There are three types of events: CSn down, CSn up and start/stop of the datalogger (CONFIG)

    GEN_PIN_WIRES : for i in 0 to C_QSPI_DATA_PINS-1 generate
      qspiQx(i) <= qspiPinsFilter(i+2);
    end generate GEN_PIN_WIRES;
    qspiCSn   <= qspiPinsFilter(1);
    qspiSclk  <= qspiPinsFilter(0);

    -- SCLKxSI <= DataToCapturexDI(0);
    -- SSxSI   <= DataToCapturexDI(5);
    -- SIOxDI  <= DataToCapturexDI(4 downto 1);  -- does this tich.like this?

    ENABLE_FILTER : if G_ENABLE_INFILTER = 1 generate
        GEN_FILTER : for i in 0 to G_QSPI_PINS-1 generate
        i_qspi_infilter : entity tich.infilter
            generic map (
            G_DEB_CYCLES => 2
            )
            port map (
            CLK       => CLK,
            CE        => '1',
            PULSE_IN  => QSPI_PINS(i),
            PULSE_OUT => qspiPinsFilter(i)
            );
        end generate GEN_FILTER;

    else generate -- No filter
        qspiPinsFilter <= QSPI_PINS;
    end generate ENABLE_FILTER;


  -- ============================================================================
  -- SPI Mode Reference Table
  -- ============================================================================
  --  Mode | CPOL | CPHA | SCLK Idle  | First Edge   | Data Sampled On
  -- ------+------+------+------------+-------------+----------------------------
  --   0   |  0   |  0   |   Low      |   Rising     | Rising edge (first edge)
  --   1   |  0   |  1   |   Low      |   Rising     | Falling edge (second edge)
  --   2   |  1   |  0   |   High     |   Falling    | Falling edge (first edge)
  --   3   |  1   |  1   |   High     |   Falling    | Rising edge (second edge)
  --
  -- Notes:
  -- - CPOL (Clock Polarity): Defines the idle level of SCLK
  --     - 0 = idle low
  --     - 1 = idle high
  -- - CPHA (Clock Phase): Defines which edge is used to sample data
  --     - 0 = sample on first edge after CSn goes low
  --     - 1 = sample on second edge (i.e., first edge is setup)
  -- ============================================================================

    -- Clock polarity controls the idle level of the SCLK signal
    pr_detect_cpol : process(CLK)
    begin
        if rising_edge(CLK) then
          if csFallingEdge = '1' then
            clockPolarity_r <= qspiSclk;
          end if;
          if RST = '1' then
            clockPolarity_r <= '0';
          end if;
        end if;
    end process;

    -- Support for the 4 SPI modes
      qspiCE <= '1' when (qspiCSn = '0') and (
        ((clockPhase xor clockPolarity_r) = '0' and sclkRisingEdge = '1') or
        ((clockPhase xor clockPolarity_r) = '1' and sclkFallingEdge = '1')) else '0';

    i_sclk_rising_edge : entity tich.edge_detector
    generic map (
      G_EDGE_TYPE => C_EDGE_TYPE_RISING
      )
    port map (
      CLK => CLK,
      CE  => not qspiCSn,
      D   => qspiSclk,
      Q   => sclkRisingEdge
      );

    i_sclk_falling_edge : entity tich.edge_detector
      generic map (
        G_EDGE_TYPE => C_EDGE_TYPE_FALLING
        )
      port map (
        CLK => CLK,
        CE  => not qspiCSn,
        D   => qspiSclk,
        Q   => sclkFallingEdge
        );

    i_cs_rising_edges : entity tich.edge_detector
      generic map (
        G_EDGE_TYPE => C_EDGE_TYPE_RISING
        )
      port map (
        CLK => CLK,
        CE  => '1',
        D   => qspiCSn,
        Q   => csRisingEdge
        );

    i_cs_falling_edges : entity tich.edge_detector
      generic map (
        G_EDGE_TYPE => C_EDGE_TYPE_FALLING
        )
      port map (
        CLK => CLK,
        CE  => '1',
        D   => qspiCSn,
        Q   => csFallingEdge
        );

    csFallingEdge_r <= csFallingEdge when rising_edge(CLK);


    i_config_control : entity tich.config_control
      generic map (
        G_DEV_ID_WIDTH => G_QSPI_ID_WIDTH,
        G_DEV_ID       => G_QSPI_DEV_ID,
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
    configEnable      <= deviceLoadConfig(0);
    clockPhase        <= deviceLoadConfig(1);  -- CPHA
    halfDuplexMode    <= deviceLoadConfig(2);  -- 3-wire SPI
    noChipSelectMode  <= deviceLoadConfig(3);  -- 2-wire SPI

    pr_reg_qx : process(CLK)
    begin
        if rising_edge(CLK) then
          if qspiCE = '1' then
            for i in 0 to C_QSPI_DATA_PINS-1 loop
              qspiSrQx_r(i) <= qspiSrQx_r(i)(C_SHIFT_REG_WIDTH-2 downto 0) & qspiQx(i);
            end loop;
          end if; -- CE
          -- DS-T2.37: pad an incomplete byte by LEFT-justifying the captured
          -- bits — continue shifting the SAME shift register (qspiCE is 0 here,
          -- CS is high), inserting '0' at the LSB. PAD_PACKET runs for exactly
          -- (8 - cntSamples) cycles, so the N captured bits end up MSB-aligned
          -- in qspiSrQx_r, which is what payloadA/B emit. (The old qspiSrQxPad_r
          -- register did this shift but was never read — dead logic.)
          if padMode_r = '1' then
            for i in 0 to C_QSPI_DATA_PINS-1 loop
              qspiSrQx_r(i) <= qspiSrQx_r(i)(C_SHIFT_REG_WIDTH-2 downto 0) & '0';
            end loop;
          end if; -- padMode
          if RST = '1' then
            for i in 0 to C_QSPI_DATA_PINS-1 loop
              qspiSrQx_r(i) <= (others => '0');
            end loop;
          end if; -- RST
        end if; -- CLK
    end process;

    pr_timeout : process(CLK)
    begin
        if rising_edge(CLK) then
          if timeOutCnt_r < C_TIMEOUT_CNT-1 then
            timeOutCnt_r <= timeOutCnt_r + 1;
          else
            timeOutError_r <= '1';
            if G_QSPI_PINS < 6 then
              timeOutError_r <= not noChipSelectMode; -- No timeout error in 2-wire mode
            end if;
          end if;
          if csFallingEdge = '1' or sclkRisingEdge = '1' or qspiCSn = '1' then
            timeOutCnt_r   <= (others => '0');
            timeOutError_r <= '0';
          end if;
        end if;
    end process;


    --------------------------------------------------------------------
    -- FSM to sequence the transfers
    --------------------------------------------------------------------
    -- If there are more than 2 data pins, generate a second payload
    GEN_PAYLOAD_B : if C_QSPI_DATA_PINS > 2 generate
      payloadA <= C_QSPI_DEV_ID_UNS & C_QSPI_PACKET_PAYLOAD_TYPE & C_RESERVED_BYTE & qspiSrQx_r(1) & qspiSrQx_r(0);
      payloadB <= C_QSPI_DEV_ID_UNS & C_QSPI_PACKET_PAYLOAD_TYPE & C_RESERVED_BYTE & qspiSrQx_r(3) & qspiSrQx_r(2);
    else generate
      -- DS-T3.10: use the timestamp latched at byte-capture, not combinational TIMESTAMP
      payloadA <= C_QSPI_DEV_ID_UNS & C_SPI_PACKET_PAYLOAD_TYPE_A & tsPayload_r & qspiSrQx_r(0);
      payloadB <= C_QSPI_DEV_ID_UNS & C_SPI_PACKET_PAYLOAD_TYPE_B & tsPayload_r & qspiSrQx_r(1);
    end generate GEN_PAYLOAD_B;

    -- DS-T3.10: use the timestamp latched at the event instant, not combinational TIMESTAMP
    eventPkt   <= C_QSPI_DEV_ID_UNS & C_QSPI_PACKET_EVENT_TYPE & tsEvent_r & eventPktBuff_r;
    defaultPkt <= C_QSPI_DEV_ID_UNS & C_QSPI_PACKET_ERROR_TYPE & C_RESERVED_BYTE & C_RESERVED_BYTE & C_RESERVED_BYTE;

    nsl_fsm : process(all)
    begin
        fsmQspi_n       <= fsmQspi_r;
        cntSamples_n    <= cntSamples_r;
        bufferPayload_n <= '0';
        newEvent_n      <= '0';
        eventMsg_n      <= eventMsg_r;
        padMode_n       <= padMode_r;
        prevPolarity_n  <= prevPolarity_r;
        --
        case fsmQspi_r is
          when STOP =>
          -- The Datalogger needs to be enabled through the CONFIG register
            cntSamples_n <= (others => '0');
            if configEnable = '1' then
              eventMsg_n <= C_EVENT_START;
              newEvent_n <= '1';
              fsmQspi_n  <= WAIT_START;
            end if;
          when WAIT_START =>
            -- Is it podataloggerble for the start event and CS down event to overlap. Either
            -- we use a FIFO to store event or we just wait for the first event to be delivered
            -- as it is a rare event and buffering the events is an overkill.
            if msgVld_r = '1' then -- first event message has beein delivered
                fsmQspi_n      <= IDLE;
                prevPolarity_n <= not clockPolarity_r; -- Force a polarity change detection at first CSn down
            end if;
          when IDLE =>
          -- Waiting for CS to be pulled down, but use the delayed version to allow capturing the clock polarity
            if csFallingEdge_r = '1' then -- falling edge
              fsmQspi_n      <= SAMPLING;
              eventMsg_n     <= C_EVENT_CS_DOWN;
              newEvent_n     <= '1';
              prevPolarity_n <= clockPolarity_r;
              if prevPolarity_r /= clockPolarity_r then
                if clockPolarity_r = '1' then
                  eventMsg_n <= C_EVENT_CS_DOWN_NEW_POL_H;
                else
                  eventMsg_n <= C_EVENT_CS_DOWN_NEW_POL_L;
                end if;
              end if;
            elsif noChipSelectMode = '1' then
              -- DS-T2.38: a 2-wire bus has no CS edge to frame on, so the FSM
              -- would otherwise wait in IDLE forever. Self-start sampling on the
              -- continuous clock+data stream (byte alignment is free-running;
              -- the consumer resyncs). No CS_DOWN event — there is no CS.
              fsmQspi_n <= SAMPLING;
            end if;
            if configEnable = '0' then
                fsmQspi_n  <= STOP;
                eventMsg_n <= C_EVENT_STOP;
                newEvent_n <= '1';
            end if;
          when SAMPLING =>
            -- Wait 8 cycles of SCLK to load a complete byte out of all the 4 data lines.
            -- Load Into the Padding buffer
            if qspiCE = '1' then
                cntSamples_n <= cntSamples_r + 1;
                -- Generate the DATALOGGER packet
                if cntSamples_r = C_SHIFT_REG_WIDTH-1 then
                    bufferPayload_n <= '1';
                    cntSamples_n    <= (others => '0');
                end if;
            end if;
            -- SCLK stopped while sampling
            if timeOutError_r = '1' then
                newEvent_n   <= '1';
                cntSamples_n <= (others => '0');
                eventMsg_n   <= C_EVENT_TIMEOUT;
                fsmQspi_n    <= IDLE;
            end if;
            -- If CSn up before completing the buffer, it needs to be padded before sending the packet
            if csRisingEdge = '1' then
                if cntSamples_r = 0 then
                    fsmQspi_n  <= IDLE;
                    eventMsg_n <= C_EVENT_CS_UP;
                    newEvent_n <= '1';
                else
                -- Load into a padding buffer
                    padMode_n <= '1';
                    fsmQspi_n  <= PAD_PACKET;
                    eventMsg_n <= C_EVENT_CS_UP_INCOMPLETE;
                    newEvent_n <= '0'; -- delay the event until the padding is done
                end if;
            end if;
          when PAD_PACKET =>
            cntSamples_n <= cntSamples_r + 1; -- no wait for CE!
            if cntSamples_r = C_SHIFT_REG_WIDTH-1 then
                padMode_n       <= '0';
                fsmQspi_n       <= IDLE;
                cntSamples_n    <= (others => '0');
                bufferPayload_n <= '1';
                newEvent_n      <= '1';
            end if;
          when others =>
            fsmQspi_n <= IDLE;
        end case;
    end process;

    -- Register NSL

    nsl_reg : process(CLK)
    begin
      if rising_edge(CLK) then
        fsmQspi_r       <= fsmQspi_n;
        cntSamples_r    <= cntSamples_n;
        newEvent_r      <= newEvent_n;
        eventMsg_r      <= eventMsg_n;
        bufferPayload_r <= bufferPayload_n;
        padMode_r       <= padMode_n;
        prevPolarity_r  <= prevPolarity_n;
        if RST = '1' then
          padMode_r     <= '0';
          fsmQspi_r     <= STOP;
        end if;
      end if;
    end process;

    --------------------------------------------------------------------
    -- Sequence the delivery of the packets
    --------------------------------------------------------------------

    seq_deliver : process(CLK)
    begin
      if rising_edge(CLK) then
        msgVld_r   <= '0';
        msg2Send_r <= defaultPkt;
        -- Capture the events for further procedataloggerng
        if newEvent_r = '1' then
          eventPending_r <= '1';
          eventPktBuff_r <= eventMsg_r;
          tsEvent_r      <= TIMESTAMP(C_TIMESTAMP_SLICE_HIGH-1 downto 0);  -- DS-T3.10
        end if;
        if bufferPayload_r = '1' then
          payloadPendint_r <= '1';
          tsPayload_r      <= TIMESTAMP(C_TIMESTAMP_SLICE_HIGH-1 downto 0);  -- DS-T3.10
        end if;
        case fsmSeq_r is
          when IDLE =>
            if eventPending_r = '1' then
              fsmSeq_r <= SEND_EVENT;
            end if;
            -- Payload sending has priority, as both events happening at the same time
            -- only occur when the payload needed padding. Send the payload first, then the CS event
            if payloadPendint_r = '1' then
              fsmSeq_r <= GENERATE_PACKET_A;
            end if;
          when GENERATE_PACKET_A =>
          -- The generation 
            payloadPendint_r <= '0';
            if QSPI_MSG_RDY = '1' then
              msg2Send_r <= payloadA;
              msgVld_r   <= '1';
              fsmSeq_r   <= GENERATE_PACKET_B;
              if halfDuplexMode = '1' or noChipSelectMode = '1' then
                fsmSeq_r <= IDLE; -- no B payload in 2/3-wire mode
              end if;
            end if;
          when GENERATE_PACKET_B =>
            if QSPI_MSG_RDY = '1' then
              msg2Send_r <= payloadB;
              msgVld_r   <= '1';
              fsmSeq_r   <= IDLE;
            end if;
          when SEND_EVENT =>
            eventPending_r <= '0';
            if QSPI_MSG_RDY = '1' then
              msg2Send_r <= eventPkt;
              msgVld_r   <= '1';
              fsmSeq_r   <= IDLE;
            end if;
          when others =>
            fsmSeq_r <= IDLE;
        end case;
        if RST = '1' then
          fsmSeq_r         <= IDLE;
          eventPending_r   <= '0';
          payloadPendint_r <= '0';
        end if;
      end if; -- CLK
    end process;


    -- Output message
    QSPI_MSG_VLD <= msgVld_r;
    QSPI_MSG_OUT <= msg2Send_r;

    QSPI_ERRORS <= (0 => timeOutError_r, others => '0');


    -- Debug
    GEN_ILA: if G_ILA_EN = 1 generate
        signal msgVld_d   : std_logic;
        signal msg2Send_d : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
        signal config_d   : std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
        signal newEvent_d : std_logic;
        signal eventPending_d : std_logic;
        signal payloadPending_d : std_logic;
        signal bufferPayload_d : std_logic;
        signal cntSamples_d    : unsigned(C_SHIFT_REG_CNT_WIDTH-1 downto 0);
        signal qspiPinsFilter_d : std_logic_vector(G_QSPI_PINS-1 downto 0);
        signal fsmQspi_d : integer range 0 to 4;
        signal fsmSeq_d  : integer range 0 to 3;
        --
        signal sclkRisingEdge_d  : std_logic;
        signal sclkFallingEdge_d : std_logic;
        signal qspiCE_d          : std_logic;
        signal csRisingEdge_d    : std_logic;
        signal csFallingEdge_d   : std_logic;
        --
        signal timeOutError_d   : std_logic;
        signal timeOutCnt_d     : unsigned(C_TIMEOUT_CNT_WIDTH-1 downto 0);

        attribute mark_debug : string;
        attribute mark_debug of msgVld_d : signal is "true";
        attribute mark_debug of msg2Send_d : signal is "true";
        attribute mark_debug of config_d : signal is "true";
        attribute mark_debug of newEvent_d : signal is "true";
        attribute mark_debug of eventPending_d : signal is "true";
        attribute mark_debug of payloadPending_d : signal is "true";
        attribute mark_debug of bufferPayload_d : signal is "true";
        attribute mark_debug of cntSamples_d : signal is "true";
        attribute mark_debug of qspiPinsFilter_d : signal is "true";
        attribute mark_debug of fsmQspi_d : signal is "true";
        attribute mark_debug of fsmSeq_d : signal is "true";
        attribute mark_debug of sclkRisingEdge_d : signal is "true";
        attribute mark_debug of sclkFallingEdge_d : signal is "true";
        attribute mark_debug of qspiCE_d : signal is "true";
        attribute mark_debug of csRisingEdge_d : signal is "true";
        attribute mark_debug of csFallingEdge_d : signal is "true";
        attribute mark_debug of timeOutError_d : signal is "true";




    begin

        process(CLK)
        begin
            if rising_edge(CLK) then
                msgVld_d          <= msgVld_r;
                msg2Send_d        <= msg2Send_r;
                config_d          <= CONFIG;
                newEvent_d        <= newEvent_r;
                eventPending_d    <= eventPending_r;
                payloadPending_d  <= payloadPendint_r;
                bufferPayload_d   <= bufferPayload_r;
                cntSamples_d      <= cntSamples_r;
                qspiPinsFilter_d  <= qspiPinsFilter;
                sclkRisingEdge_d  <= sclkRisingEdge;
                sclkFallingEdge_d <= sclkFallingEdge;
                qspiCE_d          <= qspiCE;
                csRisingEdge_d    <= csRisingEdge;
                csFallingEdge_d   <= csFallingEdge;
                timeOutError_d    <= timeOutError_r;
                timeOutCnt_d      <= timeOutCnt_r;

                --
                case fsmQspi_r is
                    when STOP =>
                        fsmQspi_d <= 0;
                    when WAIT_START =>
                        fsmQspi_d <= 1;
                    when IDLE =>
                        fsmQspi_d <= 2;
                    when SAMPLING =>
                        fsmQspi_d <= 3;
                    when PAD_PACKET =>
                        fsmQspi_d <= 4;
                    when others =>
                        fsmQspi_d <= 0;
                end case;
                --
                case fsmSeq_r is
                    when IDLE =>
                        fsmSeq_d <= 0;
                    when GENERATE_PACKET_A =>
                        fsmSeq_d <= 1;
                    when GENERATE_PACKET_B =>
                        fsmSeq_d <= 2;
                    when SEND_EVENT =>
                        fsmSeq_d <= 3;
                    when others =>
                        fsmSeq_d <= 0;
                end case;
            end if;
        end process;

    end generate GEN_ILA;

end architecture;
