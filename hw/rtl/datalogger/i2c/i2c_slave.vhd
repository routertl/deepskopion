-------------------------------------------------------------------------------
-- Title      : i2c_slave
-- Project    : datalogger
-------------------------------------------------------------------------------
-- File       : i2c_slave.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    :
-- Created    : 2025-07-03
-- Last update: 2025-07-03
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: i2c  slave
-------------------------------------------------------------------------------
-- Copyright (c) 2025 Daniel J.Mazure
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2025-07-03  1.0      dmazure Created
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use IEEE.MATH_REAL.all;
use ieee.std_logic_misc.all;

library tich;
use tich.datalogger_pkg.all;

entity i2c_slave is
    generic (
      G_CTRL_WIDTH      : integer                      := 16; -- Control interface width
      G_ERROR_WIDTH     : integer                      := 4; -- Number of error bits
      G_ILA_EN          : integer                      := 1;
      -- System clock (CLK) frequency in Hz — scales the SCL-stuck-low timeout
      -- (DS-T2.34/DS-T2.36: the timeout MUST be derived from the real clock, not
      -- an assumed 1us timestamp base).
      G_CLK_FREQ_HZ     : natural                      := 100_000_000
      );
    port (
      CLK          : in  std_logic;
      RST          : in  std_logic;
      -- I2C Wire interface
      SCL          : in  std_logic;
      SDA_I        : in  std_logic;
      SDA_O        : out std_logic;
      SDA_T        : out std_logic; -- Tri-state control for SDA
      --
      TIMESTAMP    : in  std_logic_vector(31 downto 0); -- Timestamp for events, 1 us base
      I2C_CTRL     : in  std_logic_vector(G_CTRL_WIDTH-1 downto 0);
      -- Access to the data interface (reading responses)
      I2C_TX_DIN   : in  std_logic_vector(7 downto 0);
      I2C_TX_VLD   : in  std_logic;
      I2C_TX_RDY   : out std_logic;
      -- Data Out interface
      I2C_RX_DOUT  : out std_logic_vector(7 downto 0);
      I2C_RX_VLD   : out std_logic;
      I2C_RX_RDY   : in  std_logic;
      --
      I2C_ERRORS   : out std_logic_vector(G_ERROR_WIDTH-1 downto 0);
      -- DS-T2.34 REA probe: FSM-meaningful internals for cycle-accurate JTAG
      -- capture — compare the bit-count progression against the real SCL to
      -- see the premature-SDA-release / SCL-miscount on silicon.
      DBG          : out std_logic_vector(19 downto 0) := (others => '0');
      -- DS-T2.34 FIFO-handshake probe: the byte the slave latched to drive
      -- (compare against the injector's fifo_dout to see a stale read).
      DBG_SHIFT    : out std_logic_vector(7 downto 0) := (others => '0');
      -- DS-T2.34 bail-decision probe: the non-sequential abort conditions so a
      -- SEND_DATA->WAIT_START bail can be attributed to the exact trigger.
      --   [7]=stopCondition [6]=startCondition [5]=sclLowTimeout_r
      --   [4]=i2cGlitchError_r [3]=sclStableHigh [2]=sdaRisingEdge
      --   [1]=sdaFallingEdge [0]=slaveDriving
      DBG2         : out std_logic_vector(7 downto 0) := (others => '0')
      );
end i2c_slave;


architecture rtl of i2c_slave is

  --------------------------------------------------------------------------
  -- Constants
  --------------------------------------------------------------------------
  constant C_I2C_EVENT_WIDTH           : natural := 8;
  constant C_I2C_EVENT_ENABLED         : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"03";
  constant C_I2C_EVENT_START           : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"09";
  constant C_I2C_EVENT_STOP            : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"0A";
  constant C_I2C_EVENT_REPEATED_START  : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"0B";
  constant C_I2C_EVENT_STOP_ERROR      : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"05";
  constant C_I2C_EVENT_STRETCH_ERROR   : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"06";
  constant C_I2C_EVENT_REP_START_ERROR : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"07";
  constant C_I2C_EVENT_GLITCH_ERROR    : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"08";
  constant C_I2C_EVENT_SLAVE_FIFO_ERROR: std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"0D";
  constant C_I2C_SLAVE_FULL_ERROR      : std_logic_vector(C_I2C_EVENT_WIDTH - 1 downto 0) := x"0C";
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
  -- DS-T2.34/DS-T2.36 FIX: derive the timeout from the REAL system clock (a
  -- generic), counting CLK cycles while SCL is held low. The old code assumed a
  -- "1us timestamp base" and free-ran off a TIMESTAMP bit, firing a spurious
  -- STRETCH_ERROR ~every 317us regardless of SCL — aborting ~35% of healthy reads.
  constant C_SCL_TIMEOUT_CYCLES      : natural := (G_CLK_FREQ_HZ / 1000) * C_SCL_TIMEOUT_MS;
  constant C_SCL_TIMEOUT_CNT_WIDTH   : natural := integer(ceil(log2(real(C_SCL_TIMEOUT_CYCLES))));
  --
  constant C_DEBOUNCE_TIME_CYCLES    : natural := 3; -- Below this, it is considered a glitch. 30 ns/3,40 MHz I2C clock
  constant C_DEBOUNCE_TIME_CNT_WIDTH : natural := integer(ceil(log2(real(C_DEBOUNCE_TIME_CYCLES))));

  --------------------------------------------------------------------------
  -- Types
  --------------------------------------------------------------------------
  type t_fsmI2c is (I2C_DISABLED, I2C_WAIT_START, I2C_CAPTURE_ADDRESS, I2C_WAIT_ACK, I2C_CAPTURE_DATA, I2C_SEND_DATA, 
                   I2C_ASSERT_ACK, I2C_ADDRESS_ACK, I2C_WAIT_INTERNAL_DATA);

  --------------------------------------------------------------------------
  -- Signals
  --------------------------------------------------------------------------
  signal fsmI2c_r       : t_fsmI2c := I2C_DISABLED;
  signal fsmI2c_n       : t_fsmI2c;
  signal sclRisingEdge  : std_logic;
  signal sclFallingEdge : std_logic;
  signal sdaRisingEdge  : std_logic;
  signal sdaFallingEdge : std_logic;
  signal startCondition : std_logic;
  signal stopCondition  : std_logic;

  -- DS-T2.12: SCL and SDA_I are an EXTERNALLY clocked I2C bus, fully
  -- asynchronous to clk_sys. Sampling them directly into clk_sys edge
  -- detectors is a CDC violation: metastable / phase-dependent edge
  -- detection makes legitimate SCL/SDA edge-proximity at protocol boundaries
  -- look like a glitch (primary mode) and lets the slave's own async SDA
  -- transitions look like a START (secondary mode). Synchronise both lines
  -- through a 2-FF chain BEFORE any edge detection or sampling.
  signal sclSync_r      : std_logic_vector(1 downto 0) := (others => '1');
  signal sdaSync_r      : std_logic_vector(1 downto 0) := (others => '1');
  signal sclS           : std_logic;  -- synchronised SCL
  signal sdaS           : std_logic;  -- synchronised SDA_I
  attribute ASYNC_REG            : string;
  attribute ASYNC_REG of sclSync_r : signal is "TRUE";
  attribute ASYNC_REG of sdaSync_r : signal is "TRUE";

  -- DS-T2.12 (residual mode): START/STOP must be classified by whether SCL
  -- was STABLY high AT THE MOMENT SDA transitioned. Two subtleties force a
  -- delayed SCL history rather than the live level:
  --   1. The edge_detector adds one clk_sys of pipeline, so sda{Rising,
  --      Falling}Edge asserts one cycle AFTER sdaS actually changed. Gating
  --      on the live sclS would sample SCL one cycle too late and MISS a real
  --      START when SCL falls promptly after it (observed: a START with a
  --      tight SCL-low follow-on was never detected).
  --   2. The two independent SCL/SDA synchronisers can resolve a master
  --      data-bit change and the SCL rise into adjacent cycles, so an
  --      ordinary data transition (SDA changes while SCL is LOW, then SCL
  --      rises) must not be read as SDA-edge-while-SCL-high (false START/STOP)
  --      during capture.
  -- sclWasHigh_r  = sclS delayed 1 cycle  → SCL level when SDA changed.
  -- sclWasHigh2_r = sclS delayed 2 cycles → SCL the cycle before that.
  -- Requiring BOTH high means SCL was genuinely, stably high across the SDA
  -- edge (a real START/STOP), not merely rising into it (a data bit).
  signal sclWasHigh_r   : std_logic := '1';
  signal sclWasHigh2_r  : std_logic := '1';
  signal sclStableHigh  : std_logic;  -- SCL stably high across the SDA edge

  -- Slave is actively SOURCING the bus (driving SDA) in these responder
  -- states; while it owns SDA, its own SDA transitions are NOT bus
  -- conditions. START/STOP/repeated-START are generated by the MASTER only,
  -- and only the master drives SDA outside these states.
  signal slaveDriving   : std_logic;


  signal configEnable     : std_logic;
  signal configEnableFe   : std_logic;
  signal i2cDevAddress    : std_logic_vector(C_I2C_ADDRESS_WIDTH - 1 downto 0); -- I2C device address

  signal msgEvent_n       : std_logic_vector(C_I2C_DATA_WIDTH - 1 downto 0);
  signal msgEvent_r       : std_logic_vector(C_I2C_DATA_WIDTH - 1 downto 0) := (others => '0');
  signal msgPayload_n     : std_logic_vector(C_I2C_DATA_WIDTH - 1 downto 0);
  signal msgPayload_r     : std_logic_vector(C_I2C_DATA_WIDTH - 1 downto 0) := (others => '0');
  signal msgEventVld_n    : std_logic;
  signal msgEventVld_r    : std_logic := '0';
  signal msgPayloadVld_n  : std_logic;
  signal msgPayloadVld_r  : std_logic := '0';
  signal sdaShiftReg_n    : std_logic_vector(C_SHIFT_REG_WIDTH-1 downto 0);
  signal sdaShiftReg_r    : std_logic_vector(C_SHIFT_REG_WIDTH-1 downto 0) := (others => '0');
  signal cntSamples_r     : unsigned(C_SHIFT_REG_CNT_WIDTH-1 downto 0)     := (others => '0');
  signal cntSamples_n     : unsigned(C_SHIFT_REG_CNT_WIDTH-1 downto 0);
  signal rwBit_n          : std_logic;
  signal rwBit_r          : std_logic := '0';
  signal ack_n            : std_logic;
  signal ack_r            : std_logic := '0';
  signal i2cAdress_n      : std_logic_vector(C_I2C_ADDRESS_WIDTH - 1 downto 0);
  signal i2cAdress_r      : std_logic_vector(C_I2C_ADDRESS_WIDTH - 1 downto 0) := (others => '0');
  signal sdaOut_n         : std_logic;
  signal sdaOut_r         : std_logic := '1';
  --
  signal repeatedStart_n  : std_logic;
  signal repeatedStart_r  : std_logic := '0';
  --
  signal sclLowTimeout_r  : std_logic := '0'; -- SCL stuck low
  signal i2cGlitchError_r : std_logic := '0'; -- SDA/SCL glitch error
  signal sclLowCnt_r      : unsigned(C_SCL_TIMEOUT_CNT_WIDTH-1 downto 0)   := (others => '0');
  -- DS-T2.12: de-conflated, per-line debounce. The original single
  -- glitchCnt_r OR-ed sclRisingEdge + sdaRisingEdge, so a legitimate
  -- SCL-rise and SDA-rise landing within C_DEBOUNCE_TIME_CYCLES (normal at
  -- protocol boundaries on an async bus) falsely tripped a glitch. A real
  -- glitch is two edges on the SAME line in quick succession, so debounce
  -- each line independently.
  signal sclGlitchCnt_r   : unsigned(C_DEBOUNCE_TIME_CNT_WIDTH-1 downto 0) := (others => '0');
  signal sdaGlitchCnt_r   : unsigned(C_DEBOUNCE_TIME_CNT_WIDTH-1 downto 0) := (others => '0');
  --
  signal msgOut           : std_logic_vector(C_I2C_DATA_WIDTH - 1 downto 0);
  signal msgOutVld        : std_logic := '0';
  --

  signal fifoInFull       : std_logic;
  signal fifoInEmpty      : std_logic;
  signal fifoInRdEn       : std_logic := '0';

  signal rdNewData_n      : std_logic;
  signal rdNewData_r      : std_logic := '0'; -- Read new data from the TX FIFO
  --
  signal errorIntFifo_n   : std_logic;
  signal errorIntFifo_r   : std_logic := '0'; -- FIFO didn't provide the data on time
  

begin

  -- The I2C slave will monitor:
  -- a) START condition (SDA goes low while SCL is high)
  -- b) STOP condition (SDA goes high while SCL is high)
  -- Event messages:
  -- 1. START condition
  -- 2. STOP condition
  -- 3. Repeated START condition (SDA goes low while SCL is high, after a STOP condition)
  -- 4. slave Enabled/Disabled
  -- 5. Error condition (Clock stretching, SDA/SCL stuck low, etc.)

  configEnable  <= I2C_CTRL(0);
  i2cDevAddress <= I2C_CTRL(C_I2C_ADDRESS_WIDTH + C_I2C_RW_BIT_WIDTH - 1 downto C_I2C_RW_BIT_WIDTH);

  -- DS-T2.12: 2-FF synchronisers for the async SCL / SDA_I bus lines.
  -- Idle (released open-drain) is '1', so the chain resets to '1'. Edge
  -- detection and all bus sampling use the synchronised sclS / sdaS.
  PR_BUS_SYNC : process(CLK)
  begin
    if rising_edge(CLK) then
      sclSync_r    <= sclSync_r(0) & SCL;
      sdaSync_r    <= sdaSync_r(0) & SDA_I;
      sclWasHigh_r  <= sclSync_r(1);   -- = sclS delayed one cycle
      sclWasHigh2_r <= sclWasHigh_r;   -- = sclS delayed two cycles
      if RST = '1' then
        sclSync_r     <= (others => '1');
        sdaSync_r     <= (others => '1');
        sclWasHigh_r  <= '1';
        sclWasHigh2_r <= '1';
      end if;
    end if;
  end process;

  sclS <= sclSync_r(1);
  sdaS <= sdaSync_r(1);
  -- SCL stably high across the (pipeline-delayed) SDA edge.
  sclStableHigh <= sclWasHigh_r and sclWasHigh2_r;

  -- Edge detectors for SCL and SDA (on the synchronised lines)

  i_scl_rising_edge : entity tich.edge_detector
  generic map (
    G_EDGE_TYPE => C_EDGE_TYPE_RISING
    )
  port map (
    CLK => CLK,
    CE  => '1',
    D   => sclS,
    Q   => sclRisingEdge
    );

i_scl_falling_edge : entity tich.edge_detector
  generic map (
    G_EDGE_TYPE => C_EDGE_TYPE_FALLING
    )
  port map (
    CLK => CLK,
    CE  => '1',
    D   => sclS,
    Q   => sclFallingEdge
    );

i_sda_rising_edge : entity tich.edge_detector
  generic map (
    G_EDGE_TYPE => C_EDGE_TYPE_RISING
    )
  port map (
    CLK => CLK,
    CE  => '1',
    D   => sdaS,
    Q   => sdaRisingEdge
    );

i_sda_falling_edge : entity tich.edge_detector
  generic map (
    G_EDGE_TYPE => C_EDGE_TYPE_FALLING
    )
  port map (
    CLK => CLK,
    CE  => '1',
    D   => sdaS,
    Q   => sdaFallingEdge
    );


    -- DS-T2.12 (secondary mode): START/STOP are MASTER-generated bus
    -- conditions. While the slave is sourcing SDA (responder states below),
    -- its OWN async SDA transitions — sampled near an SCL-high phase — must
    -- not be mistaken for a START (SDA fall) or STOP (SDA rise). Gate the
    -- condition detectors off whenever the slave drives the bus.
    slaveDriving <= '1' when (fsmI2c_r = I2C_ADDRESS_ACK or
                              fsmI2c_r = I2C_SEND_DATA  or
                              fsmI2c_r = I2C_ASSERT_ACK or
                              (fsmI2c_r = I2C_WAIT_INTERNAL_DATA and sdaOut_r = '0'))
                        else '0';
    -- DS-P2.217: I2C_WAIT_INTERNAL_DATA releases SDA on the first SCL fall
    -- when the TX FIFO is empty. Once released the slave drives nothing, so
    -- the master's STOP / repeated START must be seen there; masked, the
    -- slave stayed in this state, deaf to every later address, until 25 ms
    -- of bus silence (silicon: 1 of 200 back-to-back i2cget reads answered).

    startCondition <= '1' when sclStableHigh = '1' and sdaFallingEdge = '1' and slaveDriving = '0' else '0';
    stopCondition  <= '1' when sclStableHigh = '1' and sdaRisingEdge  = '1' and slaveDriving = '0' else '0';


    nsl_fsm : process(all)
    begin
        -- Default
        fsmI2c_n        <= fsmI2c_r;
        msgEventVld_n   <= '0';
        msgPayloadVld_n <= '0';
        msgPayload_n    <= msgPayload_r;
        msgEvent_n      <= (others => '0');
        rwBit_n         <= rwBit_r;
        ack_n           <= ack_r;
        repeatedStart_n <= repeatedStart_r;
        cntSamples_n    <= cntSamples_r;
        sdaShiftReg_n   <= sdaShiftReg_r;
        i2cAdress_n     <= i2cAdress_r;
        sdaOut_n        <= sdaOut_r; -- Default SDA output is high
        rdNewData_n     <= '0'; -- Read new data from the TX FIFO
        errorIntFifo_n  <= '0'; -- FIFO doesn't provide the data on time
        --
        case fsmI2c_r is
          when I2C_DISABLED =>
            sdaOut_n <= '1'; -- SDA is high when disabled
            if configEnable = '1' then
              fsmI2c_n       <= I2C_WAIT_START;
              msgEventVld_n  <= '1';
              msgEvent_n     <= C_I2C_EVENT_ENABLED;
            end if;
          when I2C_WAIT_START =>
            -- DS-T2.21 / Haz.61: a slave waiting for a START must NEVER drive
            -- SDA. Without this, a master that aborts mid-byte (no clean STOP)
            -- leaves sdaOut_r='0' from I2C_SEND_DATA/I2C_*_ACK; the free-running
            -- 25 ms timeout (PR_TIMEOUT) kicks the FSM here, but the default
            -- `sdaOut_n <= sdaOut_r` HOLDS SDA low — so SDA stays pulled low, the
            -- master can never re-issue a START (START needs SDA to FALL while
            -- SCL high, impossible when SDA is already low), no SCL edges arrive,
            -- and the EMIO-I2C bus wedges into permanent EAGAIN. Releasing SDA on
            -- entry to the idle/wait state lets the bus self-free.
            sdaOut_n        <= '1'; -- release SDA — never drive while idle
            repeatedStart_n <= '0';
            cntSamples_n    <= (others => '0'); -- Reset the sample counter
            if startCondition = '1' then
              -- START condition detected
              fsmI2c_n        <= I2C_CAPTURE_ADDRESS;
              msgEventVld_n   <= '1';
              msgEvent_n      <= C_I2C_EVENT_START;
              repeatedStart_n <= '1';
            end if;
          when I2C_CAPTURE_ADDRESS =>
            if sclRisingEdge = '1' then
              sdaShiftReg_n <= sdaShiftReg_r(sdaShiftReg_r'length-2 downto 0) & sdaS;
              cntSamples_n  <= cntSamples_r + 1;
            end if;
            if cntSamples_r = C_I2C_ADDRESS_WIDTH + C_I2C_RW_BIT_WIDTH  then
              fsmI2c_n    <= I2C_ADDRESS_ACK;
              rwBit_n     <= sdaShiftReg_r(0);
              -- Store the address
              i2cAdress_n <= sdaShiftReg_r(C_I2C_RW_BIT_WIDTH + C_I2C_ADDRESS_WIDTH - 1 downto C_I2C_RW_BIT_WIDTH);
              cntSamples_n <= (others => '0'); -- Reset the sample counter
            end if;
          when I2C_ADDRESS_ACK =>
            -- First, there is a falling edge that we use to pull down SDA
            if sclFallingEdge = '1' then
              sdaOut_n <= '0' when i2cAdress_r = i2cDevAddress else '1';
            end if;
            -- Wait for the rising edge to move to the next state.
            -- DS-T2.5: gate the FIFO drain + state transition on address-match —
            -- otherwise a wrong-address master read still pops a TX byte.
            if sclRisingEdge = '1' then
              if i2cAdress_r = i2cDevAddress then
                if rwBit_r = '1' then --read
                  fsmI2c_n    <= I2C_WAIT_INTERNAL_DATA;
                  rdNewData_n <= '1'; -- Read new data from the TX FIFO
                else
                  fsmI2c_n <= I2C_CAPTURE_DATA;
                end if;
              else
                -- Wrong address: NAK already driven on the falling edge; wait for STOP/repeated START.
                fsmI2c_n <= I2C_WAIT_START;
              end if;
            end if;
          when I2C_WAIT_ACK =>
            -- The first thing that happens here is that there is a falling edge of SCL,
            -- Then a new rising edge of SCL, that transitions the FSM
            if sclRisingEdge = '1' then
              ack_n     <= not sdaS; -- register ACK from master
              if sdaS = '0' then
                -- ACK received, continue reading until Stop condition
                rdNewData_n <= '1'; -- Read new data from the TX FIFO
                fsmI2c_n    <= I2C_WAIT_INTERNAL_DATA;
              else
                -- NACK received
                fsmI2c_n  <= I2C_WAIT_START;
              end if;
            end if;
          when I2C_CAPTURE_DATA =>
            -- First, release SDA after ACK/NACK
            if sclFallingEdge = '1' then
              sdaOut_n <= '1';
              ack_n    <= '0'; -- Default ACK
            end if;
            if sclRisingEdge = '1' then
              sdaShiftReg_n <= sdaShiftReg_r(sdaShiftReg_r'length-2 downto 0) & sdaS;
              cntSamples_n  <= cntSamples_r + 1;
            end if;
            if cntSamples_r = C_I2C_DATA_WIDTH  then
              fsmI2c_n        <= I2C_ASSERT_ACK;
              msgPayload_n    <= sdaShiftReg_r(C_I2C_DATA_WIDTH-1 downto 0);
              msgPayloadVld_n <= '1';
              cntSamples_n    <= (others => '0'); -- Reset the sample counter
            end if;
          when I2C_ASSERT_ACK =>
            if sclFallingEdge = '1' then
              sdaOut_n <= '0';
            end if;
            if sclRisingEdge = '1' then
              fsmI2c_n        <= I2C_CAPTURE_DATA;
            end if;
            -- Backpressure mechanism. If the Upstream FIFO is full, we delay the valid
            msgPayloadVld_n <= '1';
            if I2C_RX_RDY = '1' then
              -- The master is ready to read the data, so we can send it
              msgPayloadVld_n <= '0';
            end if;            
          when I2C_WAIT_INTERNAL_DATA =>
            if I2C_TX_VLD = '1' then
              fsmI2c_n      <= I2C_SEND_DATA;
              sdaShiftReg_n <= I2C_TX_DIN;
            end if;
            if sclFallingEdge = '1' then
              sdaOut_n       <= '1'; -- Release SDA
              errorIntFifo_n <= '1'; -- FIFO didn't provide the data on time
              msgEventVld_n  <= '1';
              msgEvent_n     <= C_I2C_EVENT_SLAVE_FIFO_ERROR;
            end if;            
          when I2C_SEND_DATA =>
            -- DS-T2.6: drive on falling edge, hold through the whole high phase,
            -- only release on the NEXT falling edge after cntSamples = 8.
            -- Master samples at end of SCL-high (after half_period stretch);
            -- releasing inside the high phase races the master's sample and the
            -- LSB reads as '1' (released bus) instead of the real bit.
            if sclFallingEdge = '1' then
              if cntSamples_r = C_I2C_DATA_WIDTH then -- All 8 bits driven + sampled; release for ACK
                cntSamples_n <= (others => '0');
                sdaOut_n     <= '1'; -- Release SDA
                fsmI2c_n     <= I2C_WAIT_ACK;
              else
                sdaOut_n      <= sdaShiftReg_r(sdaShiftReg_r'length-1);
                sdaShiftReg_n <= sdaShiftReg_r(sdaShiftReg_r'length-2 downto 0) & '0'; -- Shift out the data
              end if;
            end if;
            if sclRisingEdge = '1' then -- Keeps track of the samples, effective transfers happen on the rising edge
              cntSamples_n  <= cntSamples_r + 1;
            end if;
            -- TODO: Would this be chepaer sharing an elseif branching than parallel cases?
        end case;
        -- non-sequential events
        -- Repeated START condition detected
        if configEnable = '1' then
          if startCondition = '1' and repeatedStart_r = '1' then
            fsmI2c_n        <= I2C_CAPTURE_ADDRESS;
            msgEventVld_n   <= '1';
            msgEvent_n      <= C_I2C_EVENT_REPEATED_START;
            cntSamples_n    <= (others => '0'); -- Reset the sample counter
            if ack_r = '1' then
              -- repeated START after NACK
              msgEvent_n      <= C_I2C_EVENT_REP_START_ERROR;
            end if;
          end if;
          if stopCondition = '1' then
            -- STOP condition detected
            fsmI2c_n        <= I2C_WAIT_START;
            msgEventVld_n   <= '1';
            msgEvent_n      <= C_I2C_EVENT_STOP;
            if cntSamples_r /= 0 then -- This should be 0 after the ACK/NACK
              msgEvent_n      <= C_I2C_EVENT_STOP_ERROR;
            end if;
          end if;
          -- Timing error conditions
          if sclLowTimeout_r = '1' then
            -- SCL stuck low
            fsmI2c_n        <= I2C_WAIT_START;
            msgEventVld_n   <= '1';
            msgEvent_n      <= C_I2C_EVENT_STRETCH_ERROR;
          end if;
          -- Glitches are errors.
          -- DS-T2.12 (primary mode): a glitch still raises the event, but is
          -- NON-FATAL while a transfer is in progress — aborting an
          -- in-flight byte on a single debounce-window hit destroyed the read
          -- on the async bus. Only force the FSM back to WAIT_START when idle
          -- (waiting for a START). The debounce is now per-line (PR_GLITCH),
          -- so this fires only on a true same-line double-edge.
          if i2cGlitchError_r = '1' then
            msgEventVld_n   <= '1';
            msgEvent_n      <= C_I2C_EVENT_GLITCH_ERROR;
            if fsmI2c_r = I2C_WAIT_START then
              fsmI2c_n      <= I2C_WAIT_START;
            end if;
          end if;
        elsif configEnableFe = '1' then -- disabled is an asynchronous event
          -- If the I2C slave is disabled, we reset the FSM
          fsmI2c_n        <= I2C_DISABLED;
          msgEventVld_n   <= '1';
          msgEvent_n      <= C_I2C_EVENT_DISABLED;
        end if;
    end process;


  i_config_enable_falling_edge : entity tich.edge_detector
  generic map (
    G_EDGE_TYPE => C_EDGE_TYPE_FALLING
    )
  port map (
    CLK => CLK,
    CE  => '1',
    D   => configEnable,
    Q   => configEnableFe
    );    


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
      i2cAdress_r     <= i2cAdress_n;
      sdaOut_r        <= sdaOut_n; -- Default SDA output is high
      rdNewData_r     <= rdNewData_n;
      errorIntFifo_r  <= errorIntFifo_n;
      -- Reset
      if RST = '1' then
        fsmI2c_r     <= I2C_DISABLED;
        msgEventVld_r   <= '0';
        msgPayloadVld_r <= '0';
        sdaOut_r        <= '1'; -- Default 
      end if;
    end if;
  end process;

  ----------------------------------------------------------------------------
  -- Timeouts and counters
  ----------------------------------------------------------------------------
  -- DS-T2.34/DS-T2.36 FIX: a transaction-INACTIVITY watchdog (CLK-cycle based,
  -- scaled from G_CLK_FREQ_HZ). It must satisfy BOTH:
  --   * DS-T2.21/Haz.61: a master that ABANDONS a read (SCL frozen HIGH *or* LOW,
  --     slave still driving SDA low) must eventually release SDA so the EMIO bus
  --     doesn't wedge into permanent EAGAIN.
  --   * DS-T2.34: it must NOT fire during a HEALTHY in-flight read.
  -- So RESET the watchdog on ANY SCL EDGE (rising or falling = real bus activity)
  -- and fire only after C_SCL_TIMEOUT_MS with NO SCL activity. A live ~400 kHz
  -- read toggles SCL every ~1.25 us (<< the multi-million-cycle threshold) so it
  -- never trips; a frozen/abandoned bus has no edges so it fires. (The old code
  -- free-ran off a TIMESTAMP bit, ungated and never reset, firing ~every 317 us
  -- regardless of SCL and aborting ~35% of reads mid-SEND_DATA — the DS-T2.34
  -- root cause; gating on SCL-low alone would have re-wedged the SCL-HIGH case.)
    PR_TIMEOUT : process(CLK)
    begin
      if rising_edge(CLK) then
        sclLowTimeout_r <= '0';
        if sclRisingEdge = '1' or sclFallingEdge = '1' then
          sclLowCnt_r <= (others => '0');               -- SCL activity: transaction alive
        elsif sclLowCnt_r = C_SCL_TIMEOUT_CYCLES - 1 then
          sclLowTimeout_r <= '1';                       -- no activity for the full timeout
          sclLowCnt_r     <= (others => '0');
        else
          sclLowCnt_r <= sclLowCnt_r + 1;               -- counting inactivity
        end if;
        if RST = '1' then
          sclLowCnt_r     <= (others => '0');
        end if;
      end if;
    end process;

    -- DS-T2.12 (primary mode): de-conflated, per-line glitch debounce.
    -- Previously a single counter was zeroed by an SCL-rise OR an SDA-rise
    -- and flagged a glitch when EITHER line saw a rise within the window —
    -- so a legitimate SCL-rise and SDA-rise on DIFFERENT lines, landing close
    -- together (normal at I2C protocol boundaries, and unavoidable once the
    -- async bus is sampled on clk_sys), falsely tripped a glitch and reset
    -- the FSM mid-read. A real glitch is two edges on the SAME line in quick
    -- succession; debounce each line independently so cross-line proximity is
    -- no longer mistaken for one. Operates on the SYNCHRONISED edges.
    PR_GLITCH : process(CLK)
    begin
      if rising_edge(CLK) then
        i2cGlitchError_r <= '0';
        -- SCL line debounce
        if sclGlitchCnt_r < C_DEBOUNCE_TIME_CYCLES-1 then
          sclGlitchCnt_r <= sclGlitchCnt_r + 1;
        end if;
        if sclRisingEdge = '1' then
          sclGlitchCnt_r <= (others => '0');
          if sclGlitchCnt_r < C_DEBOUNCE_TIME_CYCLES-1 then
            i2cGlitchError_r <= '1'; -- two SCL rises inside the window
          end if;
        end if;
        -- SDA line debounce
        if sdaGlitchCnt_r < C_DEBOUNCE_TIME_CYCLES-1 then
          sdaGlitchCnt_r <= sdaGlitchCnt_r + 1;
        end if;
        if sdaRisingEdge = '1' then
          sdaGlitchCnt_r <= (others => '0');
          if sdaGlitchCnt_r < C_DEBOUNCE_TIME_CYCLES-1 then
            i2cGlitchError_r <= '1'; -- two SDA rises inside the window
          end if;
        end if;
      end if;
    end process;

  ----------------------------------------------------------------------------
  -- Outputs
  ----------------------------------------------------------------------------
  I2C_ERRORS <= (0 => errorIntFifo_r, others => '0');
  --Data interface
  I2C_RX_DOUT <= msgPayload_r;
  I2C_RX_VLD  <= msgPayloadVld_r;

  I2C_TX_RDY <= rdNewData_r;

  -- Open drain behavior
  SDA_O <= '0';
  SDA_T <= sdaOut_r;

  -- DS-T2.34 REA debug bus — FSM-meaningful internals for the i2c REA probe.
  -- Lets the JTAG capture line up the slave's bit counter + detected SCL edges
  -- + release point (sdaOut_r) against the real bus SCL/SDA, to localise the
  -- premature-SDA-release / SCL-miscount that fails ~27% of multi-byte reads.
  DBG <= std_logic_vector(to_unsigned(t_fsmI2c'pos(fsmI2c_r), 4))   -- [19:16] FSM state
       & std_logic_vector(resize(cntSamples_r, 4))                  -- [15:12] bit counter
       & std_logic_vector(resize(sclGlitchCnt_r, 2))                -- [11:10] SCL glitch cnt
       & std_logic_vector(resize(sdaGlitchCnt_r, 2))                -- [9:8]   SDA glitch cnt
       & sclS & sdaS & slaveDriving & ack_r                         -- [7:4]
       & sclRisingEdge & sclFallingEdge & sdaOut_r & rdNewData_r;   -- [3:0] sdaOut_r=SDA_T(release)

  -- DS-T2.34: the byte currently latched in the shift register (= the loaded
  -- TX byte at the WAIT_INTERNAL_DATA->SEND_DATA edge, then shifted out).
  DBG_SHIFT <= sdaShiftReg_r(7 downto 0);

  -- DS-T2.34 bail-decision probe (the non-sequential abort conditions).
  DBG2 <= stopCondition & startCondition & sclLowTimeout_r & i2cGlitchError_r
        & sclStableHigh & sdaRisingEdge & sdaFallingEdge & slaveDriving;

end rtl;