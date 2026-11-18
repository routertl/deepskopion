-------------------------------------------------------------------------------
-- Title      : i2c_datalogger
-- Project    : datalogger
-------------------------------------------------------------------------------
-- File       : i2c_datalogger.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    :
-- Created    : 2025-02-11
-- Last update: 2025-02-11
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: i2c raw data datalogger
-------------------------------------------------------------------------------
-- Copyright (c) Daniel J. Mazure 2025
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

entity i2c_datalogger is
    generic (
      G_I2C_ID_WIDTH   : integer                                  := 5;
      G_I2C_DEV_ID     : natural range 0 to (2**G_I2C_ID_WIDTH)-1 := 28;
      G_TIMESTAMP_WIDTH : natural range 16 to 32                  := 16;
      G_CONFIG_WIDTH    : integer                                 := 16;
      G_ERROR_WIDTH     : integer                                 := 2;
      G_ILA_EN          : integer                                 := 1;
      -- System clock (CLK) frequency in Hz — scales the SCL-stuck-low timeout
      -- (DS-P4.1: was mis-scaled off a TIMESTAMP bit assuming a 1us base).
      G_CLK_FREQ_HZ     : natural                                 := 100_000_000
      );
    port (
      CLK         : in  std_logic;
      RST         : in  std_logic;
      I2C_PINS    : in  std_logic_vector(1 downto 0); -- SDA (1) & SCL (0)
      CONFIG      : in  std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
      TIMESTAMP   : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
      -- I2C
      I2C_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
      I2C_MSG_VLD : out std_logic;
      I2C_MSG_RDY : in  std_logic;
      --
      I2C_ERRORS  : out std_logic_vector(G_ERROR_WIDTH-1 downto 0)
      );
end i2c_datalogger;


architecture rtl of i2c_datalogger is

  --------------------------------------------------------------------------
  -- Constants
  --------------------------------------------------------------------------
  constant C_DEVICE_ID : std_logic_vector(G_I2C_ID_WIDTH - 1 downto 0) := std_logic_vector(to_unsigned(G_I2C_DEV_ID, G_I2C_ID_WIDTH));
  constant C_TIMESTAMP_SLICE_HIGH      : natural                      := 16;
  --
  constant C_I2C_EVENT_WIDTH           : natural := 8;
  constant C_I2C_EVENT_ENABLED         : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"03";
  constant C_I2C_EVENT_START           : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"09";
  constant C_I2C_EVENT_STOP            : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"0A";
  constant C_I2C_EVENT_REPEATED_START  : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"0B";
  constant C_I2C_EVENT_STOP_ERROR      : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"05";
  constant C_I2C_EVENT_STRETCH_ERROR   : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"06";
  constant C_I2C_EVENT_REP_START_ERROR : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"07";
  constant C_I2C_EVENT_GLITCH_ERROR    : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"08";
  constant C_I2C_EVENT_DISABLED        : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"00";
  constant C_I2C_TYPE_EVENT            : std_logic_vector(2 downto 0) := "100";
  constant C_I2C_TYPE_PAYLOAD          : std_logic_vector(1 downto 0) := "00"; -- The midataloggerng bit is the ACK/NACK
  --
  constant C_I2C_ADDRESS_WIDTH         : natural := 7;
  constant C_I2C_RW_BIT_WIDTH          : natural := 1; -- The R/W bit is the last bit of the address
  constant C_I2C_DATA_WIDTH            : natural := 8;
  constant C_SHIFT_REG_WIDTH           : natural := 8;
  constant C_SHIFT_REG_CNT_WIDTH       : natural := integer(ceil(log2(real(C_SHIFT_REG_WIDTH)))) + 1;
  --
  constant C_SCL_TIMEOUT_MS          : natural := 25; -- SCL-stuck-low timeout (ms)
  -- DS-P4.1 FIX: derive the threshold from the REAL system clock (counting CLK
  -- cycles while SCL is held low) instead of free-running off a TIMESTAMP bit
  -- under a wrong "1us base" assumption (which made it ~317us, not 25ms).
  constant C_SCL_TIMEOUT_CYCLES      : natural := (G_CLK_FREQ_HZ / 1000) * C_SCL_TIMEOUT_MS;
  constant C_SCL_TIMEOUT_CNT_WIDTH   : natural := integer(ceil(log2(real(C_SCL_TIMEOUT_CYCLES))));
  --
  constant C_DEBOUNCE_TIME_CYCLES    : natural := 3; -- Below this, it is considered a glitch. 30 ns/3,40 MHz I2C clock
  constant C_DEBOUNCE_TIME_CNT_WIDTH : natural := integer(ceil(log2(real(C_DEBOUNCE_TIME_CYCLES))));

  --------------------------------------------------------------------------
  -- Types
  --------------------------------------------------------------------------
  type t_fsmI2c is (I2C_DISABLED, I2C_WAIT_START, I2C_CAPTURE_ADDRESS, I2C_WAIT_ACK, I2C_CAPTURE_DATA);

  --------------------------------------------------------------------------
  -- Signals
  --------------------------------------------------------------------------
  signal fsmI2c_r       : t_fsmI2c := I2C_DISABLED;
  signal fsmI2c_n       : t_fsmI2c;
  signal scl            : std_logic;
  signal sda            : std_logic;
  signal sclRisingEdge  : std_logic;
  signal sclFallingEdge : std_logic;
  signal sdaRisingEdge  : std_logic;
  signal sdaFallingEdge : std_logic;
  signal startCondition : std_logic;
  signal stopCondition  : std_logic;

  signal deviceLoadConfig : std_logic_vector(G_CONFIG_WIDTH - G_I2C_ID_WIDTH - 1 downto 0);
  signal configEnable     : std_logic;
  -- DS-P3.90 Phase 2: per-datalogger reset (global RST OR this device disabled).
  signal dataloggerRst       : std_logic;

  signal msgEvent_n       : std_logic_vector(C_DATALOGGER_MSG_WIDTH- G_I2C_ID_WIDTH - 1 downto 0);
  signal msgEvent_r       : std_logic_vector(C_DATALOGGER_MSG_WIDTH- G_I2C_ID_WIDTH - 1 downto 0) := (others => '0');
  signal msgPayload_n     : std_logic_vector(C_DATALOGGER_MSG_WIDTH- G_I2C_ID_WIDTH - 1 downto 0);
  signal msgPayload_r     : std_logic_vector(C_DATALOGGER_MSG_WIDTH- G_I2C_ID_WIDTH - 1 downto 0) := (others => '0');
  signal msgEventVld_n    : std_logic;
  signal msgEventVld_r    : std_logic := '0';
  signal msgPayloadVld_n  : std_logic;
  signal msgPayloadVld_r  : std_logic := '0';
  signal sdaShiftReg_n    : std_logic_vector(C_SHIFT_REG_WIDTH-1 downto 0);
  signal sdaShiftReg_r    : std_logic_vector(C_SHIFT_REG_WIDTH-1 downto 0) := (others => '0');
  signal cntSamples_r     : unsigned(C_SHIFT_REG_CNT_WIDTH-1 downto 0)         := (others => '0');
  signal cntSamples_n     : unsigned(C_SHIFT_REG_CNT_WIDTH-1 downto 0);
  signal rwBit_n          : std_logic;
  signal rwBit_r          : std_logic := '0';
  signal ack_n            : std_logic;
  signal ack_r            : std_logic := '0';
  --
  signal repeatedStart_n  : std_logic;
  signal repeatedStart_r  : std_logic := '0';
  --
  signal sclLowTimeout_r  : std_logic := '0'; -- SCL stuck low
  signal i2cGlitchError_r : std_logic := '0'; -- SDA/SCL glitch error
  signal sclLowCnt_r      : unsigned(C_SCL_TIMEOUT_CNT_WIDTH-1 downto 0) := (others => '0');
  signal glitchCnt_r      : unsigned(C_DEBOUNCE_TIME_CNT_WIDTH-1 downto 0) := (others => '0');
  --
  signal lifecycleMsg   : std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0);
  signal lifecycleVld   : std_logic;
  --
  signal msgOut         : std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0);
  signal msgOutVld      : std_logic := '0';
  --
  signal timeStampSlice : std_logic_vector(C_TIMESTAMP_SLICE_HIGH-1 downto 0) := (others => '0');


begin

  -- The I2C datalogger will monitor:
  -- a) START condition (SDA goes low while SCL is high)
  -- b) STOP condition (SDA goes high while SCL is high)
  -- Event messages:
  -- 1. START condition
  -- 2. STOP condition
  -- 3. Repeated START condition (SDA goes low while SCL is high, after a STOP condition)
  -- 4. Datalogger Enabled/Disabled
  -- 5. Error condition (Clock stretching, SDA/SCL stuck low, etc.)

  scl <= I2C_PINS(0);
  sda <= I2C_PINS(1);

  timeStampSlice <= TIMESTAMP(C_TIMESTAMP_SLICE_HIGH-1 downto 0);

  -- Register the configuration values
  i_config_control : entity tich.config_control
    generic map (
      G_DEV_ID_WIDTH => G_I2C_ID_WIDTH,
      G_DEV_ID       => G_I2C_DEV_ID,
      G_CONFIG_WIDTH => G_CONFIG_WIDTH
      )
    port map (
      CLK        => CLK,
      RST        => RST,
      CONFIG     => CONFIG,
      CONFIG_OUT => deviceLoadConfig,
      CAPTURE_RST  => dataloggerRst  -- DS-P3.90 Phase 2: resets the decode FSM on disable
      );

  configEnable <= deviceLoadConfig(0);

  -- DS-P3.90 Phase 1: uniform symmetric ENABLED/DISABLED lifecycle frames are
  -- emitted by datalogger_lifecycle in the ALWAYS-ON (global RST) domain, NOT by
  -- the decode FSM. This decouples the lifecycle markers from the decode core
  -- so the core can later adopt a per-datalogger CAPTURE_RST without losing the
  -- DISABLED frame (DS-T2.31 / H.96). Replaces the former in-core ENABLED
  -- emission + i_disable_event/DISABLED emission.
  i_lifecycle : entity tich.datalogger_lifecycle
    generic map (
      G_DEV_ID        => G_I2C_DEV_ID,
      G_DEV_ID_WIDTH  => G_I2C_ID_WIDTH,
      G_TYPE          => C_I2C_TYPE_EVENT,
      G_TS_WIDTH      => C_TIMESTAMP_SLICE_HIGH,
      G_CODE_ENABLED  => C_I2C_EVENT_ENABLED,
      G_CODE_DISABLED => C_I2C_EVENT_DISABLED
      )
    port map (
      CLK       => CLK,
      RST       => RST,
      CONFIG_EN => configEnable,
      TIMESTAMP => timeStampSlice,
      MSG_OUT   => lifecycleMsg,
      MSG_VLD   => lifecycleVld
      );

  -- Edge detectors for SCL and SDA

  i_scl_rising_edge : entity tich.edge_detector
  generic map (
    G_EDGE_TYPE => C_EDGE_TYPE_RISING
    )
  port map (
    CLK => CLK,
    CE  => '1',
    D   => scl,
    Q   => sclRisingEdge
    );

i_scl_falling_edge : entity tich.edge_detector
  generic map (
    G_EDGE_TYPE => C_EDGE_TYPE_FALLING
    )
  port map (
    CLK => CLK,
    CE  => '1',
    D   => scl,
    Q   => sclFallingEdge
    );

i_sda_rising_edge : entity tich.edge_detector
  generic map (
    G_EDGE_TYPE => C_EDGE_TYPE_RISING
    )
  port map (
    CLK => CLK,
    CE  => '1',
    D   => sda,
    Q   => sdaRisingEdge
    );

i_sda_falling_edge : entity tich.edge_detector
  generic map (
    G_EDGE_TYPE => C_EDGE_TYPE_FALLING
    )
  port map (
    CLK => CLK,
    CE  => '1',
    D   => sda,
    Q   => sdaFallingEdge
    );


    startCondition <= '1' when scl = '1' and sdaFallingEdge = '1' else '0';
    stopCondition  <= '1' when scl = '1' and sdaRisingEdge = '1' else '0';


    nsl_fsm : process(all)
      variable payloadAccepted : std_logic;
      variable eventAccepted   : std_logic;
    begin
        payloadAccepted := '0';
        eventAccepted   := '0';
        if lifecycleVld = '0' and I2C_MSG_RDY = '1' then
          if msgPayloadVld_r = '1' then
            payloadAccepted := '1';
          elsif msgEventVld_r = '1' then
            eventAccepted := '1';
          end if;
        end if;

        -- Default
        fsmI2c_n        <= fsmI2c_r;
        msgEventVld_n   <= msgEventVld_r;
        msgPayloadVld_n <= msgPayloadVld_r;
        msgPayload_n    <= msgPayload_r;
        msgEvent_n      <= msgEvent_r;
        rwBit_n         <= rwBit_r;
        ack_n           <= ack_r;
        repeatedStart_n <= repeatedStart_r;
        cntSamples_n    <= cntSamples_r;
        sdaShiftReg_n   <= sdaShiftReg_r;
        if payloadAccepted = '1' then
          msgPayloadVld_n <= '0';
        end if;
        if eventAccepted = '1' then
          msgEventVld_n <= '0';
        end if;
        --
        case fsmI2c_r is
          when I2C_DISABLED =>
            -- ENABLED frame now emitted by i_lifecycle (DS-P3.90 Phase 1);
            -- the FSM only performs the state transition here.
            if configEnable = '1' then
              fsmI2c_n       <= I2C_WAIT_START;
            end if;
          when I2C_WAIT_START =>
            repeatedStart_n <= '0';
            cntSamples_n    <= (others => '0');
            if startCondition = '1' then
              -- START condition detected
              fsmI2c_n        <= I2C_CAPTURE_ADDRESS;
              msgEventVld_n   <= '1';
              msgEvent_n      <= C_I2C_TYPE_EVENT & timeStampSlice & C_I2C_EVENT_START;
              repeatedStart_n <= '1';
            end if;
          when I2C_CAPTURE_ADDRESS =>
            if sclRisingEdge = '1' then
              sdaShiftReg_n <= sdaShiftReg_r(sdaShiftReg_r'length-2 downto 0) & sda;
              cntSamples_n  <= cntSamples_r + 1;
            end if;
            if cntSamples_r = C_I2C_ADDRESS_WIDTH + C_I2C_RW_BIT_WIDTH  then
              fsmI2c_n <= I2C_WAIT_ACK;
              rwBit_n  <= sdaShiftReg_r(0);
            end if;
          when I2C_WAIT_ACK =>
            -- DS-P3.128: hold the byte PAYLOAD valid until I2C_MSG_RDY accepts
            -- it (the same valid/ready hold the START/STOP events rely on).
            -- Previously msgPayloadVld pulsed for ONE cycle on the ACK
            -- sclRisingEdge and the process default cleared it next cycle, while
            -- the FSM only advanced when I2C_MSG_RDY was high in that exact
            -- cycle. Under back-pressure (RDY low at the ACK edge) the frame was
            -- dropped — ~94% byte loss on silicon. Now: latch the payload on the
            -- ACK edge, KEEP valid asserted (msgPayload_r held stable by the
            -- process default), and leave I2C_WAIT_ACK only once the byte has
            -- been accepted (msgPayloadVld_r='1' and I2C_MSG_RDY='1'). cntSamples
            -- is cleared on the ACK edge regardless, so the STOP-phase
            -- cntSamples_r check stays correct (no spurious STOP_ERROR).
            -- The byte PAYLOAD is presented to the downstream via the REGISTERED
            -- msgPayloadVld_r; the handshake completes when msgPayloadVld_r='1'
            -- AND I2C_MSG_RDY='1'. So the accept/advance decision is taken on
            -- msgPayloadVld_r (the valid actually seen on the bus), never on the
            -- combinational ACK-edge pulse — that was the original drop.
            if msgPayloadVld_r = '1' then
              -- A payload valid is presented this cycle — HOLD it asserted (and
              -- hold msgPayload_r stable via the process default) until accepted.
              if sclRisingEdge = '1' then
                cntSamples_n <= cntSamples_r + 1;
              end if;
              if payloadAccepted = '1' then
                -- handshake done: drop valid and advance to the data byte.
                msgPayloadVld_n <= '0';
                fsmI2c_n        <= I2C_CAPTURE_DATA;
              end if;
            elsif sclRisingEdge = '1' then
              -- ACK bit sampled: latch the payload and raise valid (registers it
              -- for the next cycle). cntSamples cleared so the STOP-phase check
              -- stays correct.
              ack_n            <= sda;
              msgPayloadVld_n  <= '1';
              msgPayload_n     <= C_I2C_TYPE_PAYLOAD & sda & timeStampSlice & sdaShiftReg_r;
              cntSamples_n     <= (others => '0');
            end if;
          when I2C_CAPTURE_DATA =>
            if sclRisingEdge = '1' then
              sdaShiftReg_n <= sdaShiftReg_r(sdaShiftReg_r'length-2 downto 0) & sda;
              cntSamples_n  <= cntSamples_r + 1;
            end if;
            if cntSamples_r = C_I2C_DATA_WIDTH  then
              ack_n      <= '1'; -- Default ACK
              fsmI2c_n   <= I2C_WAIT_ACK;
            end if;
        end case;
        -- non-sequential events
        -- Repeated START condition detected
        if configEnable = '1' then
          if startCondition = '1' and repeatedStart_r = '1' then -- Start detected when the previous state was not waiting for a START condition
            fsmI2c_n        <= I2C_CAPTURE_ADDRESS;
            msgEventVld_n   <= '1';
            msgEvent_n      <= C_I2C_TYPE_EVENT & timeStampSlice & C_I2C_EVENT_REPEATED_START;
            cntSamples_n    <= (others => '0');
            if ack_r = '1' then
              -- repeated START after NACK
              msgEvent_n      <= C_I2C_TYPE_EVENT & timeStampSlice & C_I2C_EVENT_REP_START_ERROR;
            end if;
          end if;
          if stopCondition = '1' then
            -- STOP condition detected
            fsmI2c_n        <= I2C_WAIT_START;
            msgEventVld_n   <= '1';
            msgEvent_n      <= C_I2C_TYPE_EVENT & timeStampSlice & C_I2C_EVENT_STOP;
            if cntSamples_r /= 1 then -- This should be 0 after the ACK/NACK, + 1 after the first rising edge of SCL (necesary for the STOP condition)
              msgEvent_n      <= C_I2C_TYPE_EVENT & timeStampSlice & C_I2C_EVENT_STOP_ERROR;
            end if;
          end if;
          -- Timing error conditions
          if sclLowTimeout_r = '1' then
            -- SCL stuck low
            fsmI2c_n        <= I2C_WAIT_START;
            msgEventVld_n   <= '1';
            msgEvent_n      <= C_I2C_TYPE_EVENT & timeStampSlice & C_I2C_EVENT_STRETCH_ERROR;
          end if;
          -- Glitches are errors
          if i2cGlitchError_r = '1' then
            fsmI2c_n        <= I2C_WAIT_START;
            msgEventVld_n   <= '1';
            msgEvent_n      <= C_I2C_TYPE_EVENT & timeStampSlice & C_I2C_EVENT_GLITCH_ERROR;
          end if;
        end if;
        -- DISABLED frame now emitted by i_lifecycle (DS-P3.90 Phase 1). The FSM
        -- only returns to I2C_DISABLED while config is de-asserted (level, so it
        -- is held disabled — the former falling-edge pulse is no longer needed).
        if configEnable = '0' then
          fsmI2c_n        <= I2C_DISABLED;
        end if;
    end process;


    -- Register NSL
  nsl_reg : process(CLK)
  begin
    if rising_edge(CLK) then
      fsmI2c_r        <= fsmI2c_n;
      msgEventVld_r   <= msgEventVld_n;
      msgPayloadVld_r <= msgPayloadVld_n;
      msgEvent_r      <= msgEvent_n;
      msgPayload_r    <= msgPayload_n;
      sdaShiftReg_r   <= sdaShiftReg_n;
      cntSamples_r    <= cntSamples_n;
      rwBit_r         <= rwBit_n;
      ack_r           <= ack_n;
      repeatedStart_r <= repeatedStart_n;
      -- DS-P3.90 Phase 2: per-datalogger decode reset. dataloggerRst = global RST OR
      -- this device disabled (ENABLE=0), so ENABLE=0 flushes the decode core —
      -- the FSM back to I2C_DISABLED, the output-valids, and the persistent
      -- accumulators (shift reg, sample count, ack, repeated-start) — so a
      -- re-enable starts a clean transaction with no stale ack/shift state.
      -- The lifecycle emitter (i_lifecycle) and config_control stay on global
      -- RST, so the DISABLED frame is never suppressed (H.96).
      if dataloggerRst = '1' then
        fsmI2c_r        <= I2C_DISABLED;
        msgEventVld_r   <= '0';
        msgPayloadVld_r <= '0';
        sdaShiftReg_r   <= (others => '0');
        cntSamples_r    <= (others => '0');
        ack_r           <= '0';
        repeatedStart_r <= '0';
      end if;
    end if;
  end process;

  ----------------------------------------------------------------------------
  -- Timeouts and counters
  ----------------------------------------------------------------------------
    -- DS-P4.1 FIX: a correctly-scaled SCL-stuck-low detector. Count CLK cycles
    -- while SCL is held LOW; RESET on SCL high (any clocking); fire only after
    -- the full C_SCL_TIMEOUT_MS (= C_SCL_TIMEOUT_CYCLES CLK cycles, from
    -- G_CLK_FREQ_HZ). The passive datalogger never drives SDA, so this is a true
    -- stuck-low/stalled-bus reporter — the reset-on-SCL-high semantic is correct
    -- (it does NOT need the slave's edge-reset inactivity watchdog). Was
    -- counting TIMESTAMP-bit edges → fired ~80x too eagerly (~317us not 25ms).
    PR_TIMEOUT : process(CLK)
    begin
      if rising_edge(CLK) then
        sclLowTimeout_r <= '0';
        if scl = '1' then
          sclLowCnt_r <= (others => '0');               -- SCL high: not stuck
        elsif sclLowCnt_r = C_SCL_TIMEOUT_CYCLES - 1 then
          sclLowTimeout_r <= '1';                       -- SCL stuck low for the full timeout
          sclLowCnt_r     <= (others => '0');
        else
          sclLowCnt_r <= sclLowCnt_r + 1;               -- SCL low: count CLK cycles
        end if;
        if RST = '1' then
          sclLowCnt_r     <= (others => '0');
        end if;
      end if;
    end process;

    PR_GLITCH : process(CLK)
    begin
      if rising_edge(CLK) then
        i2cGlitchError_r <= '0';
        if glitchCnt_r < C_DEBOUNCE_TIME_CYCLES-1 then
          glitchCnt_r <= glitchCnt_r + 1;
        end if;
        if sclRisingEdge = '1' or sdaRisingEdge = '1' then
          glitchCnt_r <= (others => '0');
          if glitchCnt_r < C_DEBOUNCE_TIME_CYCLES-1 then
            -- Glitch detected
            i2cGlitchError_r <= '1';
          end if;
        end if;
      end if;
    end process;

    -- Mux Out — lifecycle frame (ENABLED/DISABLED) takes top priority so an
    -- enable/disable marker is never masked by a coincident decode frame
    -- (DS-P3.90 Phase 1).
    msgOut <= lifecycleMsg              when lifecycleVld = '1' else
                C_DEVICE_ID & msgPayload_r when msgPayloadVld_r = '1' else
                C_DEVICE_ID & msgEvent_r   when msgEventVld_r = '1' else
                (others => '0');

    msgOutVld <= lifecycleVld or msgPayloadVld_r or msgEventVld_r;

  -------------------------------------------------------------------------
  -- Outputs
  -------------------------------------------------------------------------
  -- DS-P3.128: drive the output handshake COMBINATIONALLY from the already
  -- registered msgOut / msgOutVld (which only change on a clock edge, so this is
  -- a clean, glitch-free, timing-met valid). The former extra output register
  -- put I2C_MSG_VLD one cycle behind msgPayloadVld_r while the FSM samples
  -- I2C_MSG_RDY in the msgPayloadVld_r stage — a one-cycle valid/ready skew that
  -- made a held byte handshake TWICE (a duplicate FIFO write in datalogger_gather).
  -- With the output un-skewed, msgOutVld is the bus valid and the FSM's
  -- de-assert (below) aligns to the bus handshake => EXACTLY one handshake.
  -- This mirrors the UART datalogger's combinational MSG_OUT/MSG_VLD (DS-T2.42).
  I2C_MSG_OUT <= msgOut;
  I2C_MSG_VLD <= msgOutVld;


end rtl;
