-------------------------------------------------------------------------------
-- Title      : Datalogger Gather (Auto-Generated)
-- Project    : Zybo Z7-20
-------------------------------------------------------------------------------
-- File       : datalogger_gather.vhd
-- Generated  : tools/datalogger-builder/datalogger_gather_gen.py
-- Generator  : datalogger_gather_gen.py
-------------------------------------------------------------------------------
-- Description: Auto-generated datalogger aggregation module.
--              Instantiates protocol dataloggers and feeds the single-tier
--              burst arbiter chain (D.4 — stream tier collapsed; the
--              downstream zs_msg_framer is the single stamping authority).
-- DO NOT EDIT MANUALLY - Regenerate from datalogger_config.yml
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.std_logic_misc.all;
library tich;
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;
use tich.arbiter_pkg.all;
use tich.axi_pkg.all;
use tich.system_regbank_pkg.all;
use tich.zs_msg_pkg.all;        -- DS-P3.29: severity / domain constants + make_msg_type

entity datalogger_gather is
  generic(
    G_SYS_CLK : natural := 100_000_000
    );
  port(
    RST                   : in  std_logic;
    CLK                   : in  std_logic;
    CAPTURE_PINS            : in  std_logic_vector(C_NUM_DATALOGGER_PINS-1 downto 0);
    CONFIG                : in  std_logic_vector(C_DATALOGGER_CONFIG_WIDTH-1 downto 0);
    TIMESTAMP             : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
    -- Burst output stream (DS-P3.29: per-source metadata threaded
    -- through the chain). DOUT_PAYLOAD is the original 32-bit
    -- datalogger message; DOUT_DATALOGGER_ID is the 5-bit device_id
    -- (extracted from payload[31:27]) zero-extended to 8 bits;
    -- DOUT_MSG_TYPE is (severity[15:12] | domain[11:8] |
    -- event_id[7:0]) — severity = INFO, domain per protocol, event_id
    -- from payload[26:24].
    DOUT_PAYLOAD    : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
    DOUT_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    DOUT_MSG_TYPE   : out std_logic_vector(15 downto 0);
    DOUT_VALID      : out std_logic;
    DOUT_REQ             : in  std_logic;
    FIFO_CSR        : out t_fifo_csr := C_INIT_FIFO_CSR;
    -- Per-protocol telemetry counters (DS-P3.26). One slot per active
    -- protocol, indexed by the sorted-name C_PROTO_IDX_<NAME> constants
    -- from datalogger_config_pkg. Each slot carries:
    --   .pkt_cnt — accepted-handshake count summed across instances
    --   .err_cnt — cycles-with-error count summed across instances
    PROTO_COUNTERS        : out t_proto_counter_array(0 to C_NUM_PROTO_COUNTERS-1)
                              := (others => C_INIT_PROTO_COUNTER);
    -- DS-T3.18: per-EDGE-instance running loss counters — one 32-bit slot per
    -- edge datalogger, channel k in bits [(k+1)*32-1 : k*32]. Fed into the per-
    -- channel error_event_bank in deepskopion_top so in-band loss frames carry
    -- the overflowing channel's datalogger_id (wave_stream loss kind v0.11).
    EDGE_ERR_CNTS         : out std_logic_vector(C_EDGE_DATALOGGERS*32-1 downto 0)
                              := (others => '0');
    -- DS-P3.117: per-EDGE-instance max-frequency taps. EDGE_MAX_IVALS carries the
    -- latched smallest validated edge-to-edge interval (CLK ticks) per channel,
    -- channel k in bits [(k+1)*C_EDGE_MAXFREQ_WIDTH-1 : k*C_EDGE_MAXFREQ_WIDTH]
    -- (all-ones = no edge yet / re-armed). EDGE_MAX_NEW pulses one CLK on the
    -- channel that just latched a new (smaller) min. Fed into the per-channel
    -- maxfreq_event_bank in deepskopion_top → in-band EVT_EDGE_MAXFREQ frames.
    EDGE_MAX_IVALS        : out std_logic_vector(C_EDGE_DATALOGGERS*C_EDGE_MAXFREQ_WIDTH-1 downto 0)
                              := (others => '1');
    EDGE_MAX_NEW          : out std_logic_vector(C_EDGE_DATALOGGERS-1 downto 0)
                              := (others => '0')
    );
end entity datalogger_gather;

architecture RTL of datalogger_gather is

  -----------------------------------------------------------------------------
  -- Constants
  -----------------------------------------------------------------------------
  -- DS-P3.29: tagged-message width carried through the arbiter chain.
  --   bits [55:24] — payload (C_INPUT_WIDTH = 32, the original datalogger
  --                  message word)
  --   bits [23:16] — datalogger_id (8 bits — 5 bits of device_id zero-
  --                  extended into the top of the byte for room to grow)
  --   bits [15: 0] — msg_type (severity | domain | event_id)
  -- See deepskopion D.4 + DS-P3.29 + zs_msg_pkg.
  constant C_DATALOGGER_ID_FIELD_WIDTH : integer := 8;
  constant C_MSG_TYPE_FIELD_WIDTH   : integer := 16;
  constant C_TAGGED_WIDTH : integer := C_INPUT_WIDTH
                                     + C_DATALOGGER_ID_FIELD_WIDTH
                                     + C_MSG_TYPE_FIELD_WIDTH;

  -----------------------------------------------------------------------------
  -- Types
  -----------------------------------------------------------------------------
  type t_arrayOfMsgs is array (natural range <>) of std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  type t_array_of_errors is array (natural range <>) of std_logic_vector(C_ERROR_WIDTH-1 downto 0);

  -----------------------------------------------------------------------------
  -- Signals
  -----------------------------------------------------------------------------
  -- Top-layer burst arbiter wiring at the tagged-tuple width (DS-P3.29).
  signal epArbiter_data_flat  : std_logic_vector(C_NUM_ARB_INPUTS*C_TAGGED_WIDTH - 1 downto 0) := (others => '0');
  signal epArbiter_valid_flat : std_logic_vector(C_NUM_ARB_INPUTS - 1 downto 0) := (others => '0');
  signal ep_dout_data         : std_logic_vector(C_TAGGED_WIDTH - 1 downto 0) := (others => '0');
  signal ep_dout_valid        : std_logic := '0';
  signal arb2ndLayerFullIn    : std_logic_vector(0 to C_NUM_ARB_INPUTS-1) := (others => '0');

  -- DS-T2: FIFO_CSR.{level,empty,overflow,overflowCnt} were
  -- previously undriven. Wire them from the 2nd-layer burst arbiter
  -- so PS-side reads reflect runtime state instead of init values.
  -- (Lives in the generator template — a hand-edit on the generated
  -- file is reverted by the next `rr project run` regen; see DS-T10.)
  constant C_ARB_FIFO_DEPTH : integer := 9;
  constant C_ARB_FIFO_CNT_WIDTH : integer := C_ARB_FIFO_DEPTH + 1;
  signal arb2nd_fifo_cnt   : t_fifo_buffer_cnt(0 to C_NUM_ARB_INPUTS - 1)(C_ARB_FIFO_CNT_WIDTH - 1 downto 0)
                                  := (others => (others => '0'));
  signal arb2nd_dout_empty : std_logic := '1';
  -- Overflow detection: a write attempt to a full per-input FIFO is
  -- a dropped word. One-cycle pulse OR'd across inputs.
  signal burst_overflow_pulse : std_logic := '0';
  signal burst_overflow_cnt_r : unsigned(C_FIFO_OVERFLOW_WIDTH - 1 downto 0)
                                := (others => '0');
  -- Sum of per-input FIFO occupancies, clamped to C_FIFO_LEVEL_WIDTH.
  signal burst_level_sum      : unsigned(C_FIFO_LEVEL_WIDTH - 1 downto 0)
                                := (others => '0');

  -- Per-protocol telemetry counters (DS-P3.26). Declared at architecture
  -- scope so the aggregation into PROTO_COUNTERS at the end of the file
  -- can reach them. Each is driven by exactly one process inside its
  -- protocol's `if C_X_DATALOGGERS > 0 generate` block; when a protocol
  -- block is disabled the corresponding signal stays at its init value
  -- (zero) — no driver conflict.
  signal i2c_pkt_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal i2c_err_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal i2c_drop_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal jtag_pkt_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal jtag_err_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal jtag_drop_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal spi_pkt_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal spi_err_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal spi_drop_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal uart_pkt_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal uart_err_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal uart_drop_cnt_r : unsigned(31 downto 0) := (others => '0');

begin

  -----------------------------------------------------------------------------
  -- I2C Block (Burst)
  -----------------------------------------------------------------------------
  GEN_I2C_BLOCK : if C_I2C_DATALOGGERS > 0 generate
    signal i2cMessages       : t_arrayOfMsgs(0 to C_I2C_DATALOGGERS-1);
    signal i2cReq            : std_logic_vector(0 to C_I2C_DATALOGGERS-1);
    signal i2cAck            : std_logic_vector(0 to C_I2C_DATALOGGERS-1);
    signal i2cErrors         : t_array_of_errors(0 to C_I2C_DATALOGGERS-1);
    -- Per-instance tagged tuple (payload + datalogger_id + msg_type),
    -- packed into the C_TAGGED_WIDTH bus the arbiter carries (DS-P3.29).
    signal i2cArb_data_flat  : std_logic_vector(C_I2C_DATALOGGERS*C_TAGGED_WIDTH - 1 downto 0);
    signal i2cArb_valid_flat : std_logic_vector(C_I2C_DATALOGGERS - 1 downto 0);
    signal i2cArb_dout_data  : std_logic_vector(C_TAGGED_WIDTH - 1 downto 0);
    signal i2cArb_dout_valid : std_logic;
    signal i2cArbFullIn      : std_logic_vector(0 to C_I2C_DATALOGGERS-1);
    signal i2c2ndLayerReady  : std_logic;
  begin

    GEN_I2C_DATALOGGERS : for i in 0 to C_I2C_DATALOGGERS-1 generate
      signal mappedI2C : std_logic_vector(C_I2C_PINS-1 downto 0);
    begin

      -- Pin Mapping + system-wide async input conditioning (DS-T3.5). Every
      -- captured line passes through a 2-FF synchroniser + glitch filter before
      -- it reaches the datalogger. The rejection window self-sizes from this
      -- protocol's max frequency (C_I2C_MAX_FREQ_HZ) vs G_SYS_CLK:
      -- slow lines (UART/I2C) get real glitch rejection; fast lines (SPI/QSPI/
      -- JTAG clocks) floor the window at 2 — a pure synchroniser that never eats
      -- a real edge. Fixes the uart_datalogger auto-baud collapse on a spurious
      -- 1-clk EMIO edge, and hardens every other datalogger's CDC the same way.
      GEN_I2C_PIN_MAP : for j in 0 to C_I2C_PINS-1 generate
        i_cond : entity tich.glitch_filter
          generic map (
            G_CLK_HZ      => G_SYS_CLK,
            G_MAX_FREQ_HZ => C_I2C_MAX_FREQ_HZ
          )
          port map (
            CLK     => CLK,
            RST     => RST,
            D_ASYNC => CAPTURE_PINS(C_I2C_PIN_ARRAY(i)(j)),
            Q       => mappedI2C(j)
          );
      end generate;

      -- Datalogger Instance
      i_i2c_datalogger : entity tich.i2c_datalogger
        generic map (
          G_I2C_ID_WIDTH => C_DATALOGGER_ID_WIDTH,
          G_I2C_DEV_ID => C_I2C_IDS(i),
          G_TIMESTAMP_WIDTH => C_TIMESTAMP_WIDTH,
          G_CONFIG_WIDTH => C_DATALOGGER_CONFIG_WIDTH,
          G_ERROR_WIDTH => C_ERROR_WIDTH
        )
        port map (
          CLK => CLK,
          RST => RST,
          I2C_PINS => mappedI2C,
          CONFIG => CONFIG,
          TIMESTAMP => TIMESTAMP,
          I2C_MSG_OUT => i2cMessages(i),
          I2C_MSG_VLD => i2cReq(i),
          I2C_MSG_RDY => i2cAck(i),
          I2C_ERRORS => i2cErrors(i)
        );

      -- DS-P3.29 — pack the tagged tuple at source. Layout (high→low):
      --   [55:24] payload (the 32-bit datalogger message)
      --   [23:16] datalogger_id — 5-bit device_id from payload[31:27]
      --                        zero-extended to 8 bits
      --   [15: 0] msg_type   — make_msg_type(SEV_INFO,
      --                                       C_DOMAIN_I2C,
      --                                       payload[26:24] zero-ext)
      i2cArb_data_flat((i + 1) * C_TAGGED_WIDTH - 1 downto i * C_TAGGED_WIDTH) <=
        i2cMessages(i)
        & "000" & i2cMessages(i)(31 downto 27)
        & make_msg_type(C_ZS_SEV_INFO,
                        C_DOMAIN_I2C,
                        "00000" & i2cMessages(i)(26 downto 24));
      i2cArb_valid_flat(i) <= i2cReq(i);
      i2cAck(i) <= not i2cArbFullIn(i);

    end generate GEN_I2C_DATALOGGERS;

    -- Per-protocol telemetry accumulators (DS-P3.26 / DS-T2.22). pkt_cnt counts
    -- accepted handshakes (req && ack) summed across instances per cycle;
    -- err_cnt counts cycles where any instance asserts a non-zero errors bus;
    -- drop_cnt counts BACKPRESSURE cycles (req && !ack — the datalogger has a
    -- message but the arbiter FIFO is full), summed across instances. A
    -- climbing drop_cnt is the out-of-band signal that this protocol's gather
    -- path is backpressured and real-time fidelity is at risk (DS-T2.22; the
    -- deeper ring-full datalogger-input loss is tracked separately). Counters are
    -- 32-bit, free-running, wrap on overflow. PS reads them via the regbank's
    -- PKT_CNT / ERR_CNT / DROP_CNT_<PROTO> slots.
    p_count_i2c : process(CLK)
      variable v_pkt_events  : natural range 0 to C_I2C_DATALOGGERS;
      variable v_err_events  : natural range 0 to C_I2C_DATALOGGERS;
      variable v_drop_events : natural range 0 to C_I2C_DATALOGGERS;
    begin
      if rising_edge(CLK) then
        if RST = '1' then
          i2c_pkt_cnt_r <= (others => '0');
          i2c_err_cnt_r <= (others => '0');
          i2c_drop_cnt_r <= (others => '0');
        else
          v_pkt_events  := 0;
          v_err_events  := 0;
          v_drop_events := 0;
          for i in 0 to C_I2C_DATALOGGERS-1 loop
            if i2cReq(i) = '1' and i2cAck(i) = '1' then
              v_pkt_events := v_pkt_events + 1;
            end if;
            if i2cReq(i) = '1' and i2cAck(i) = '0' then
              v_drop_events := v_drop_events + 1;
            end if;
            if or_reduce(i2cErrors(i)) = '1' then
              v_err_events := v_err_events + 1;
            end if;
          end loop;
          i2c_pkt_cnt_r <= i2c_pkt_cnt_r + to_unsigned(v_pkt_events, 32);
          i2c_err_cnt_r <= i2c_err_cnt_r + to_unsigned(v_err_events, 32);
          i2c_drop_cnt_r <= i2c_drop_cnt_r + to_unsigned(v_drop_events, 32);
        end if;
      end if;
    end process;

    -- 1st Layer Arbiter (if multiple instances). DS-P3.29: arbiter
    -- widened to C_TAGGED_WIDTH so the chain carries (payload,
    -- datalogger_id, msg_type) instead of bare 32-bit message.
    GEN_I2C_ARB : if C_I2C_DATALOGGERS > 1 generate
      i_i2c_arbiter : entity tich.arbiter
        generic map (
          G_FIFO_DEPTH => 8,
          G_NUM_INPUTS => C_I2C_DATALOGGERS,
          G_DATA_WIDTH => C_TAGGED_WIDTH
        )
        port map (
          CLK        => CLK,
          RST        => RST,
          DIN_DATA   => i2cArb_data_flat,
          DIN_VALID  => i2cArb_valid_flat,
          DOUT_DATA  => i2cArb_dout_data,
          DOUT_VALID => i2cArb_dout_valid,
          DOUT_EMPTY => open,
          DOUT_FULL  => open,
          FULL       => i2cArbFullIn,
          READY      => i2c2ndLayerReady
        );

      epArbiter_data_flat((C_ARB_I2C_INDEX + 1) * C_TAGGED_WIDTH - 1 downto C_ARB_I2C_INDEX * C_TAGGED_WIDTH) <= i2cArb_dout_data;
      epArbiter_valid_flat(C_ARB_I2C_INDEX) <= i2cArb_dout_valid;
      i2c2ndLayerReady <= not arb2ndLayerFullIn(C_ARB_I2C_INDEX);

    else generate
      -- Single-instance pass-through (no per-protocol arbiter). The
      -- single datalogger's tagged tuple goes straight into the top
      -- arbiter's input slot.
      epArbiter_data_flat((C_ARB_I2C_INDEX + 1) * C_TAGGED_WIDTH - 1 downto C_ARB_I2C_INDEX * C_TAGGED_WIDTH) <= i2cArb_data_flat(C_TAGGED_WIDTH - 1 downto 0);
      epArbiter_valid_flat(C_ARB_I2C_INDEX) <= i2cArb_valid_flat(0);
      i2cArbFullIn(0) <= arb2ndLayerFullIn(C_ARB_I2C_INDEX);
    end generate GEN_I2C_ARB;

  end generate GEN_I2C_BLOCK;

  -----------------------------------------------------------------------------
  -- JTAG Block (Burst)
  -----------------------------------------------------------------------------
  GEN_JTAG_BLOCK : if C_JTAG_DATALOGGERS > 0 generate
    signal jtagMessages       : t_arrayOfMsgs(0 to C_JTAG_DATALOGGERS-1);
    signal jtagReq            : std_logic_vector(0 to C_JTAG_DATALOGGERS-1);
    signal jtagAck            : std_logic_vector(0 to C_JTAG_DATALOGGERS-1);
    signal jtagErrors         : t_array_of_errors(0 to C_JTAG_DATALOGGERS-1);
    -- Per-instance tagged tuple (payload + datalogger_id + msg_type),
    -- packed into the C_TAGGED_WIDTH bus the arbiter carries (DS-P3.29).
    signal jtagArb_data_flat  : std_logic_vector(C_JTAG_DATALOGGERS*C_TAGGED_WIDTH - 1 downto 0);
    signal jtagArb_valid_flat : std_logic_vector(C_JTAG_DATALOGGERS - 1 downto 0);
    signal jtagArb_dout_data  : std_logic_vector(C_TAGGED_WIDTH - 1 downto 0);
    signal jtagArb_dout_valid : std_logic;
    signal jtagArbFullIn      : std_logic_vector(0 to C_JTAG_DATALOGGERS-1);
    signal jtag2ndLayerReady  : std_logic;
  begin

    GEN_JTAG_DATALOGGERS : for i in 0 to C_JTAG_DATALOGGERS-1 generate
      signal mappedJTAG : std_logic_vector(C_JTAG_PINS-1 downto 0);
    begin

      -- Pin Mapping + system-wide async input conditioning (DS-T3.5). Every
      -- captured line passes through a 2-FF synchroniser + glitch filter before
      -- it reaches the datalogger. The rejection window self-sizes from this
      -- protocol's max frequency (C_JTAG_MAX_FREQ_HZ) vs G_SYS_CLK:
      -- slow lines (UART/I2C) get real glitch rejection; fast lines (SPI/QSPI/
      -- JTAG clocks) floor the window at 2 — a pure synchroniser that never eats
      -- a real edge. Fixes the uart_datalogger auto-baud collapse on a spurious
      -- 1-clk EMIO edge, and hardens every other datalogger's CDC the same way.
      GEN_JTAG_PIN_MAP : for j in 0 to C_JTAG_PINS-1 generate
        i_cond : entity tich.glitch_filter
          generic map (
            G_CLK_HZ      => G_SYS_CLK,
            G_MAX_FREQ_HZ => C_JTAG_MAX_FREQ_HZ
          )
          port map (
            CLK     => CLK,
            RST     => RST,
            D_ASYNC => CAPTURE_PINS(C_JTAG_PIN_ARRAY(i)(j)),
            Q       => mappedJTAG(j)
          );
      end generate;

      -- Datalogger Instance
      i_jtag_datalogger : entity tich.jtag_datalogger
        generic map (
          G_JTAG_ID_WIDTH => C_DATALOGGER_ID_WIDTH,
          G_JTAG_DEV_ID => C_JTAG_IDS(i),
          G_JTAG_PINS => C_JTAG_PINS,
          G_TIMESTAMP_WIDTH => C_TIMESTAMP_WIDTH,
          G_CONFIG_WIDTH => C_DATALOGGER_CONFIG_WIDTH,
          G_ERROR_WIDTH => C_ERROR_WIDTH
        )
        port map (
          CLK => CLK,
          RST => RST,
          JTAG_PINS => mappedJTAG,
          CONFIG => CONFIG,
          TIMESTAMP => TIMESTAMP,
          JTAG_MSG_OUT => jtagMessages(i),
          JTAG_MSG_VLD => jtagReq(i),
          JTAG_MSG_RDY => jtagAck(i),
          JTAG_ERRORS => jtagErrors(i)
        );

      -- DS-P3.29 — pack the tagged tuple at source. Layout (high→low):
      --   [55:24] payload (the 32-bit datalogger message)
      --   [23:16] datalogger_id — 5-bit device_id from payload[31:27]
      --                        zero-extended to 8 bits
      --   [15: 0] msg_type   — make_msg_type(SEV_INFO,
      --                                       C_DOMAIN_JTAG,
      --                                       payload[26:24] zero-ext)
      jtagArb_data_flat((i + 1) * C_TAGGED_WIDTH - 1 downto i * C_TAGGED_WIDTH) <=
        jtagMessages(i)
        & "000" & jtagMessages(i)(31 downto 27)
        & make_msg_type(C_ZS_SEV_INFO,
                        C_DOMAIN_JTAG,
                        "00000" & jtagMessages(i)(26 downto 24));
      jtagArb_valid_flat(i) <= jtagReq(i);
      jtagAck(i) <= not jtagArbFullIn(i);

    end generate GEN_JTAG_DATALOGGERS;

    -- Per-protocol telemetry accumulators (DS-P3.26 / DS-T2.22). pkt_cnt counts
    -- accepted handshakes (req && ack) summed across instances per cycle;
    -- err_cnt counts cycles where any instance asserts a non-zero errors bus;
    -- drop_cnt counts BACKPRESSURE cycles (req && !ack — the datalogger has a
    -- message but the arbiter FIFO is full), summed across instances. A
    -- climbing drop_cnt is the out-of-band signal that this protocol's gather
    -- path is backpressured and real-time fidelity is at risk (DS-T2.22; the
    -- deeper ring-full datalogger-input loss is tracked separately). Counters are
    -- 32-bit, free-running, wrap on overflow. PS reads them via the regbank's
    -- PKT_CNT / ERR_CNT / DROP_CNT_<PROTO> slots.
    p_count_jtag : process(CLK)
      variable v_pkt_events  : natural range 0 to C_JTAG_DATALOGGERS;
      variable v_err_events  : natural range 0 to C_JTAG_DATALOGGERS;
      variable v_drop_events : natural range 0 to C_JTAG_DATALOGGERS;
    begin
      if rising_edge(CLK) then
        if RST = '1' then
          jtag_pkt_cnt_r <= (others => '0');
          jtag_err_cnt_r <= (others => '0');
          jtag_drop_cnt_r <= (others => '0');
        else
          v_pkt_events  := 0;
          v_err_events  := 0;
          v_drop_events := 0;
          for i in 0 to C_JTAG_DATALOGGERS-1 loop
            if jtagReq(i) = '1' and jtagAck(i) = '1' then
              v_pkt_events := v_pkt_events + 1;
            end if;
            if jtagReq(i) = '1' and jtagAck(i) = '0' then
              v_drop_events := v_drop_events + 1;
            end if;
            if or_reduce(jtagErrors(i)) = '1' then
              v_err_events := v_err_events + 1;
            end if;
          end loop;
          jtag_pkt_cnt_r <= jtag_pkt_cnt_r + to_unsigned(v_pkt_events, 32);
          jtag_err_cnt_r <= jtag_err_cnt_r + to_unsigned(v_err_events, 32);
          jtag_drop_cnt_r <= jtag_drop_cnt_r + to_unsigned(v_drop_events, 32);
        end if;
      end if;
    end process;

    -- 1st Layer Arbiter (if multiple instances). DS-P3.29: arbiter
    -- widened to C_TAGGED_WIDTH so the chain carries (payload,
    -- datalogger_id, msg_type) instead of bare 32-bit message.
    GEN_JTAG_ARB : if C_JTAG_DATALOGGERS > 1 generate
      i_jtag_arbiter : entity tich.arbiter
        generic map (
          G_FIFO_DEPTH => 8,
          G_NUM_INPUTS => C_JTAG_DATALOGGERS,
          G_DATA_WIDTH => C_TAGGED_WIDTH
        )
        port map (
          CLK        => CLK,
          RST        => RST,
          DIN_DATA   => jtagArb_data_flat,
          DIN_VALID  => jtagArb_valid_flat,
          DOUT_DATA  => jtagArb_dout_data,
          DOUT_VALID => jtagArb_dout_valid,
          DOUT_EMPTY => open,
          DOUT_FULL  => open,
          FULL       => jtagArbFullIn,
          READY      => jtag2ndLayerReady
        );

      epArbiter_data_flat((C_ARB_JTAG_INDEX + 1) * C_TAGGED_WIDTH - 1 downto C_ARB_JTAG_INDEX * C_TAGGED_WIDTH) <= jtagArb_dout_data;
      epArbiter_valid_flat(C_ARB_JTAG_INDEX) <= jtagArb_dout_valid;
      jtag2ndLayerReady <= not arb2ndLayerFullIn(C_ARB_JTAG_INDEX);

    else generate
      -- Single-instance pass-through (no per-protocol arbiter). The
      -- single datalogger's tagged tuple goes straight into the top
      -- arbiter's input slot.
      epArbiter_data_flat((C_ARB_JTAG_INDEX + 1) * C_TAGGED_WIDTH - 1 downto C_ARB_JTAG_INDEX * C_TAGGED_WIDTH) <= jtagArb_data_flat(C_TAGGED_WIDTH - 1 downto 0);
      epArbiter_valid_flat(C_ARB_JTAG_INDEX) <= jtagArb_valid_flat(0);
      jtagArbFullIn(0) <= arb2ndLayerFullIn(C_ARB_JTAG_INDEX);
    end generate GEN_JTAG_ARB;

  end generate GEN_JTAG_BLOCK;

  -----------------------------------------------------------------------------
  -- SPI Block (Burst)
  -----------------------------------------------------------------------------
  GEN_SPI_BLOCK : if C_SPI_DATALOGGERS > 0 generate
    signal spiMessages       : t_arrayOfMsgs(0 to C_SPI_DATALOGGERS-1);
    signal spiReq            : std_logic_vector(0 to C_SPI_DATALOGGERS-1);
    signal spiAck            : std_logic_vector(0 to C_SPI_DATALOGGERS-1);
    signal spiErrors         : t_array_of_errors(0 to C_SPI_DATALOGGERS-1);
    -- Per-instance tagged tuple (payload + datalogger_id + msg_type),
    -- packed into the C_TAGGED_WIDTH bus the arbiter carries (DS-P3.29).
    signal spiArb_data_flat  : std_logic_vector(C_SPI_DATALOGGERS*C_TAGGED_WIDTH - 1 downto 0);
    signal spiArb_valid_flat : std_logic_vector(C_SPI_DATALOGGERS - 1 downto 0);
    signal spiArb_dout_data  : std_logic_vector(C_TAGGED_WIDTH - 1 downto 0);
    signal spiArb_dout_valid : std_logic;
    signal spiArbFullIn      : std_logic_vector(0 to C_SPI_DATALOGGERS-1);
    signal spi2ndLayerReady  : std_logic;
  begin

    GEN_SPI_DATALOGGERS : for i in 0 to C_SPI_DATALOGGERS-1 generate
      signal mappedSPI : std_logic_vector(C_SPI_PINS-1 downto 0);
    begin

      -- Pin Mapping + system-wide async input conditioning (DS-T3.5). Every
      -- captured line passes through a 2-FF synchroniser + glitch filter before
      -- it reaches the datalogger. The rejection window self-sizes from this
      -- protocol's max frequency (C_SPI_MAX_FREQ_HZ) vs G_SYS_CLK:
      -- slow lines (UART/I2C) get real glitch rejection; fast lines (SPI/QSPI/
      -- JTAG clocks) floor the window at 2 — a pure synchroniser that never eats
      -- a real edge. Fixes the uart_datalogger auto-baud collapse on a spurious
      -- 1-clk EMIO edge, and hardens every other datalogger's CDC the same way.
      GEN_SPI_PIN_MAP : for j in 0 to C_SPI_PINS-1 generate
        i_cond : entity tich.glitch_filter
          generic map (
            G_CLK_HZ      => G_SYS_CLK,
            G_MAX_FREQ_HZ => C_SPI_MAX_FREQ_HZ
          )
          port map (
            CLK     => CLK,
            RST     => RST,
            D_ASYNC => CAPTURE_PINS(C_SPI_PIN_ARRAY(i)(j)),
            Q       => mappedSPI(j)
          );
      end generate;

      -- Datalogger Instance
      i_spi_datalogger : entity tich.qspi_datalogger
        generic map (
          G_QSPI_ID_WIDTH => C_DATALOGGER_ID_WIDTH,
          G_QSPI_DEV_ID => C_SPI_IDS(i),
          G_QSPI_PINS => 4,
          G_TIMESTAMP_WIDTH => C_TIMESTAMP_WIDTH,
          G_CONFIG_WIDTH => C_DATALOGGER_CONFIG_WIDTH,
          G_ERROR_WIDTH => C_ERROR_WIDTH
        )
        port map (
          CLK => CLK,
          RST => RST,
          QSPI_PINS => mappedSPI,
          CONFIG => CONFIG,
          TIMESTAMP => TIMESTAMP,
          QSPI_MSG_OUT => spiMessages(i),
          QSPI_MSG_VLD => spiReq(i),
          QSPI_MSG_RDY => spiAck(i),
          QSPI_ERRORS => spiErrors(i)
        );

      -- DS-P3.29 — pack the tagged tuple at source. Layout (high→low):
      --   [55:24] payload (the 32-bit datalogger message)
      --   [23:16] datalogger_id — 5-bit device_id from payload[31:27]
      --                        zero-extended to 8 bits
      --   [15: 0] msg_type   — make_msg_type(SEV_INFO,
      --                                       C_DOMAIN_SPI,
      --                                       payload[26:24] zero-ext)
      spiArb_data_flat((i + 1) * C_TAGGED_WIDTH - 1 downto i * C_TAGGED_WIDTH) <=
        spiMessages(i)
        & "000" & spiMessages(i)(31 downto 27)
        & make_msg_type(C_ZS_SEV_INFO,
                        C_DOMAIN_SPI,
                        "00000" & spiMessages(i)(26 downto 24));
      spiArb_valid_flat(i) <= spiReq(i);
      spiAck(i) <= not spiArbFullIn(i);

    end generate GEN_SPI_DATALOGGERS;

    -- Per-protocol telemetry accumulators (DS-P3.26 / DS-T2.22). pkt_cnt counts
    -- accepted handshakes (req && ack) summed across instances per cycle;
    -- err_cnt counts cycles where any instance asserts a non-zero errors bus;
    -- drop_cnt counts BACKPRESSURE cycles (req && !ack — the datalogger has a
    -- message but the arbiter FIFO is full), summed across instances. A
    -- climbing drop_cnt is the out-of-band signal that this protocol's gather
    -- path is backpressured and real-time fidelity is at risk (DS-T2.22; the
    -- deeper ring-full datalogger-input loss is tracked separately). Counters are
    -- 32-bit, free-running, wrap on overflow. PS reads them via the regbank's
    -- PKT_CNT / ERR_CNT / DROP_CNT_<PROTO> slots.
    p_count_spi : process(CLK)
      variable v_pkt_events  : natural range 0 to C_SPI_DATALOGGERS;
      variable v_err_events  : natural range 0 to C_SPI_DATALOGGERS;
      variable v_drop_events : natural range 0 to C_SPI_DATALOGGERS;
    begin
      if rising_edge(CLK) then
        if RST = '1' then
          spi_pkt_cnt_r <= (others => '0');
          spi_err_cnt_r <= (others => '0');
          spi_drop_cnt_r <= (others => '0');
        else
          v_pkt_events  := 0;
          v_err_events  := 0;
          v_drop_events := 0;
          for i in 0 to C_SPI_DATALOGGERS-1 loop
            if spiReq(i) = '1' and spiAck(i) = '1' then
              v_pkt_events := v_pkt_events + 1;
            end if;
            if spiReq(i) = '1' and spiAck(i) = '0' then
              v_drop_events := v_drop_events + 1;
            end if;
            if or_reduce(spiErrors(i)) = '1' then
              v_err_events := v_err_events + 1;
            end if;
          end loop;
          spi_pkt_cnt_r <= spi_pkt_cnt_r + to_unsigned(v_pkt_events, 32);
          spi_err_cnt_r <= spi_err_cnt_r + to_unsigned(v_err_events, 32);
          spi_drop_cnt_r <= spi_drop_cnt_r + to_unsigned(v_drop_events, 32);
        end if;
      end if;
    end process;

    -- 1st Layer Arbiter (if multiple instances). DS-P3.29: arbiter
    -- widened to C_TAGGED_WIDTH so the chain carries (payload,
    -- datalogger_id, msg_type) instead of bare 32-bit message.
    GEN_SPI_ARB : if C_SPI_DATALOGGERS > 1 generate
      i_spi_arbiter : entity tich.arbiter
        generic map (
          G_FIFO_DEPTH => 8,
          G_NUM_INPUTS => C_SPI_DATALOGGERS,
          G_DATA_WIDTH => C_TAGGED_WIDTH
        )
        port map (
          CLK        => CLK,
          RST        => RST,
          DIN_DATA   => spiArb_data_flat,
          DIN_VALID  => spiArb_valid_flat,
          DOUT_DATA  => spiArb_dout_data,
          DOUT_VALID => spiArb_dout_valid,
          DOUT_EMPTY => open,
          DOUT_FULL  => open,
          FULL       => spiArbFullIn,
          READY      => spi2ndLayerReady
        );

      epArbiter_data_flat((C_ARB_SPI_INDEX + 1) * C_TAGGED_WIDTH - 1 downto C_ARB_SPI_INDEX * C_TAGGED_WIDTH) <= spiArb_dout_data;
      epArbiter_valid_flat(C_ARB_SPI_INDEX) <= spiArb_dout_valid;
      spi2ndLayerReady <= not arb2ndLayerFullIn(C_ARB_SPI_INDEX);

    else generate
      -- Single-instance pass-through (no per-protocol arbiter). The
      -- single datalogger's tagged tuple goes straight into the top
      -- arbiter's input slot.
      epArbiter_data_flat((C_ARB_SPI_INDEX + 1) * C_TAGGED_WIDTH - 1 downto C_ARB_SPI_INDEX * C_TAGGED_WIDTH) <= spiArb_data_flat(C_TAGGED_WIDTH - 1 downto 0);
      epArbiter_valid_flat(C_ARB_SPI_INDEX) <= spiArb_valid_flat(0);
      spiArbFullIn(0) <= arb2ndLayerFullIn(C_ARB_SPI_INDEX);
    end generate GEN_SPI_ARB;

  end generate GEN_SPI_BLOCK;

  -----------------------------------------------------------------------------
  -- UART Block (Burst)
  -----------------------------------------------------------------------------
  GEN_UART_BLOCK : if C_UART_DATALOGGERS > 0 generate
    signal uartMessages       : t_arrayOfMsgs(0 to C_UART_DATALOGGERS-1);
    signal uartReq            : std_logic_vector(0 to C_UART_DATALOGGERS-1);
    signal uartAck            : std_logic_vector(0 to C_UART_DATALOGGERS-1);
    signal uartErrors         : t_array_of_errors(0 to C_UART_DATALOGGERS-1);
    -- Per-instance tagged tuple (payload + datalogger_id + msg_type),
    -- packed into the C_TAGGED_WIDTH bus the arbiter carries (DS-P3.29).
    signal uartArb_data_flat  : std_logic_vector(C_UART_DATALOGGERS*C_TAGGED_WIDTH - 1 downto 0);
    signal uartArb_valid_flat : std_logic_vector(C_UART_DATALOGGERS - 1 downto 0);
    signal uartArb_dout_data  : std_logic_vector(C_TAGGED_WIDTH - 1 downto 0);
    signal uartArb_dout_valid : std_logic;
    signal uartArbFullIn      : std_logic_vector(0 to C_UART_DATALOGGERS-1);
    signal uart2ndLayerReady  : std_logic;
  begin

    GEN_UART_DATALOGGERS : for i in 0 to C_UART_DATALOGGERS-1 generate
      signal mappedUART : std_logic_vector(C_UART_PINS-1 downto 0);
    begin

      -- Pin Mapping + system-wide async input conditioning (DS-T3.5). Every
      -- captured line passes through a 2-FF synchroniser + glitch filter before
      -- it reaches the datalogger. The rejection window self-sizes from this
      -- protocol's max frequency (C_UART_MAX_FREQ_HZ) vs G_SYS_CLK:
      -- slow lines (UART/I2C) get real glitch rejection; fast lines (SPI/QSPI/
      -- JTAG clocks) floor the window at 2 — a pure synchroniser that never eats
      -- a real edge. Fixes the uart_datalogger auto-baud collapse on a spurious
      -- 1-clk EMIO edge, and hardens every other datalogger's CDC the same way.
      GEN_UART_PIN_MAP : for j in 0 to C_UART_PINS-1 generate
        i_cond : entity tich.glitch_filter
          generic map (
            G_CLK_HZ      => G_SYS_CLK,
            G_MAX_FREQ_HZ => C_UART_MAX_FREQ_HZ
          )
          port map (
            CLK     => CLK,
            RST     => RST,
            D_ASYNC => CAPTURE_PINS(C_UART_PIN_ARRAY(i)(j)),
            Q       => mappedUART(j)
          );
      end generate;

      -- Datalogger Instance
      i_uart_datalogger : entity tich.uart_datalogger
        generic map (
          G_SYSTEM_CLK_FREQ => G_SYS_CLK,
          G_UART_ID_WIDTH => C_DATALOGGER_ID_WIDTH,
          G_UART_DEV_ID => C_UART_IDS(i),
          G_TIMESTAMP_WIDTH => C_TIMESTAMP_WIDTH,
          G_CONFIG_WIDTH => C_DATALOGGER_CONFIG_WIDTH,
          G_ERROR_WIDTH => C_ERROR_WIDTH
        )
        port map (
          CLK => CLK,
          RST => RST,
          UART_IN => mappedUART(0),
          CONFIG => CONFIG,
          TIMESTAMP => TIMESTAMP,
          UART_MSG_OUT => uartMessages(i),
          UART_MSG_VLD => uartReq(i),
          UART_MSG_RDY => uartAck(i),
          UART_ERRORS => uartErrors(i)
        );

      -- DS-P3.29 — pack the tagged tuple at source. Layout (high→low):
      --   [55:24] payload (the 32-bit datalogger message)
      --   [23:16] datalogger_id — 5-bit device_id from payload[31:27]
      --                        zero-extended to 8 bits
      --   [15: 0] msg_type   — make_msg_type(SEV_INFO,
      --                                       C_DOMAIN_UART,
      --                                       payload[26:24] zero-ext)
      uartArb_data_flat((i + 1) * C_TAGGED_WIDTH - 1 downto i * C_TAGGED_WIDTH) <=
        uartMessages(i)
        & "000" & uartMessages(i)(31 downto 27)
        & make_msg_type(C_ZS_SEV_INFO,
                        C_DOMAIN_UART,
                        "00000" & uartMessages(i)(26 downto 24));
      uartArb_valid_flat(i) <= uartReq(i);
      uartAck(i) <= not uartArbFullIn(i);

    end generate GEN_UART_DATALOGGERS;

    -- Per-protocol telemetry accumulators (DS-P3.26 / DS-T2.22). pkt_cnt counts
    -- accepted handshakes (req && ack) summed across instances per cycle;
    -- err_cnt counts cycles where any instance asserts a non-zero errors bus;
    -- drop_cnt counts BACKPRESSURE cycles (req && !ack — the datalogger has a
    -- message but the arbiter FIFO is full), summed across instances. A
    -- climbing drop_cnt is the out-of-band signal that this protocol's gather
    -- path is backpressured and real-time fidelity is at risk (DS-T2.22; the
    -- deeper ring-full datalogger-input loss is tracked separately). Counters are
    -- 32-bit, free-running, wrap on overflow. PS reads them via the regbank's
    -- PKT_CNT / ERR_CNT / DROP_CNT_<PROTO> slots.
    p_count_uart : process(CLK)
      variable v_pkt_events  : natural range 0 to C_UART_DATALOGGERS;
      variable v_err_events  : natural range 0 to C_UART_DATALOGGERS;
      variable v_drop_events : natural range 0 to C_UART_DATALOGGERS;
    begin
      if rising_edge(CLK) then
        if RST = '1' then
          uart_pkt_cnt_r <= (others => '0');
          uart_err_cnt_r <= (others => '0');
          uart_drop_cnt_r <= (others => '0');
        else
          v_pkt_events  := 0;
          v_err_events  := 0;
          v_drop_events := 0;
          for i in 0 to C_UART_DATALOGGERS-1 loop
            if uartReq(i) = '1' and uartAck(i) = '1' then
              v_pkt_events := v_pkt_events + 1;
            end if;
            if uartReq(i) = '1' and uartAck(i) = '0' then
              v_drop_events := v_drop_events + 1;
            end if;
            if or_reduce(uartErrors(i)) = '1' then
              v_err_events := v_err_events + 1;
            end if;
          end loop;
          uart_pkt_cnt_r <= uart_pkt_cnt_r + to_unsigned(v_pkt_events, 32);
          uart_err_cnt_r <= uart_err_cnt_r + to_unsigned(v_err_events, 32);
          uart_drop_cnt_r <= uart_drop_cnt_r + to_unsigned(v_drop_events, 32);
        end if;
      end if;
    end process;

    -- 1st Layer Arbiter (if multiple instances). DS-P3.29: arbiter
    -- widened to C_TAGGED_WIDTH so the chain carries (payload,
    -- datalogger_id, msg_type) instead of bare 32-bit message.
    GEN_UART_ARB : if C_UART_DATALOGGERS > 1 generate
      i_uart_arbiter : entity tich.arbiter
        generic map (
          G_FIFO_DEPTH => 8,
          G_NUM_INPUTS => C_UART_DATALOGGERS,
          G_DATA_WIDTH => C_TAGGED_WIDTH
        )
        port map (
          CLK        => CLK,
          RST        => RST,
          DIN_DATA   => uartArb_data_flat,
          DIN_VALID  => uartArb_valid_flat,
          DOUT_DATA  => uartArb_dout_data,
          DOUT_VALID => uartArb_dout_valid,
          DOUT_EMPTY => open,
          DOUT_FULL  => open,
          FULL       => uartArbFullIn,
          READY      => uart2ndLayerReady
        );

      epArbiter_data_flat((C_ARB_UART_INDEX + 1) * C_TAGGED_WIDTH - 1 downto C_ARB_UART_INDEX * C_TAGGED_WIDTH) <= uartArb_dout_data;
      epArbiter_valid_flat(C_ARB_UART_INDEX) <= uartArb_dout_valid;
      uart2ndLayerReady <= not arb2ndLayerFullIn(C_ARB_UART_INDEX);

    else generate
      -- Single-instance pass-through (no per-protocol arbiter). The
      -- single datalogger's tagged tuple goes straight into the top
      -- arbiter's input slot.
      epArbiter_data_flat((C_ARB_UART_INDEX + 1) * C_TAGGED_WIDTH - 1 downto C_ARB_UART_INDEX * C_TAGGED_WIDTH) <= uartArb_data_flat(C_TAGGED_WIDTH - 1 downto 0);
      epArbiter_valid_flat(C_ARB_UART_INDEX) <= uartArb_valid_flat(0);
      uartArbFullIn(0) <= arb2ndLayerFullIn(C_ARB_UART_INDEX);
    end generate GEN_UART_ARB;

  end generate GEN_UART_BLOCK;

  -----------------------------------------------------------------------------
  -- 2nd-layer burst arbiter — single tier post-D.4. Gated behind a generate
  -- guard so an empty-burst config (C_NUM_ARB_INPUTS=0) doesn't instantiate the
  -- arbiter with a null-range input array (Vivado 2024.1 elaborator crash —
  -- see DS-P2.35).
  -----------------------------------------------------------------------------
  GEN_ARB : if C_NUM_ARB_INPUTS > 0 generate
    i_2nd_layer_burst_arbiter : entity tich.arbiter
      generic map (
        G_FIFO_DEPTH => C_ARB_FIFO_DEPTH,
        G_NUM_INPUTS => C_NUM_ARB_INPUTS,
        G_DATA_WIDTH => C_TAGGED_WIDTH
      )
      port map (
        CLK        => CLK,
        RST        => RST,
        DIN_DATA   => epArbiter_data_flat,
        DIN_VALID  => epArbiter_valid_flat,
        DOUT_DATA  => ep_dout_data,
        DOUT_VALID => ep_dout_valid,
        DOUT_EMPTY => arb2nd_dout_empty,
        DOUT_FULL  => open,
        EMPTY      => open,
        FULL       => arb2ndLayerFullIn,
        FIFO_CNT   => arb2nd_fifo_cnt,
        READY      => DOUT_REQ
      );

    -- DS-T2: overflow pulse — any input attempted a write while its
    -- per-input FIFO was full. The write is dropped by the arbiter
    -- (the input FIFO's wr_en is gated on `not full`). One cycle wide.
    burst_overflow_pulse <= or_reduce(epArbiter_valid_flat
                                      and arb2ndLayerFullIn);

    -- DS-T2: total occupancy across all per-input FIFOs, clamped to
    -- C_FIFO_LEVEL_WIDTH. C_NUM_ARB_INPUTS * 2^G_FIFO_DEPTH = 4 * 512 =
    -- 2048 max, fits comfortably in 16 bits.
    SUM_LEVEL : process(arb2nd_fifo_cnt)
      variable acc : unsigned(C_FIFO_LEVEL_WIDTH - 1 downto 0);
    begin
      acc := (others => '0');
      for i in 0 to C_NUM_ARB_INPUTS - 1 loop
        acc := acc + resize(arb2nd_fifo_cnt(i), C_FIFO_LEVEL_WIDTH);
      end loop;
      burst_level_sum <= acc;
    end process SUM_LEVEL;

    -- DS-T2: saturating overflow event counter. Increments on each
    -- overflow pulse; clamps at all-ones (no wraparound — drops are
    -- forever-visible to the PS once they occur).
    OVERFLOW_CNT : process(CLK)
    begin
      if rising_edge(CLK) then
        if RST = '1' then
          burst_overflow_cnt_r <= (others => '0');
        elsif burst_overflow_pulse = '1'
              and burst_overflow_cnt_r /= unsigned'(C_FIFO_OVERFLOW_WIDTH - 1 downto 0 => '1') then
          burst_overflow_cnt_r <= burst_overflow_cnt_r + 1;
        end if;
      end if;
    end process OVERFLOW_CNT;
  end generate GEN_ARB;

  -----------------------------------------------------------------------------
  -- Outputs — DS-P3.29 splits the tagged tuple back into payload +
  -- datalogger_id + msg_type for the downstream framer.
  --   bits [55:24] payload  → DOUT_PAYLOAD
  --   bits [23:16] datalogger  → DOUT_DATALOGGER_ID
  --   bits [15: 0] msg_type → DOUT_MSG_TYPE
  -----------------------------------------------------------------------------
  DOUT_PAYLOAD    <= ep_dout_data(C_TAGGED_WIDTH - 1
                                              downto C_DATALOGGER_ID_FIELD_WIDTH
                                                    + C_MSG_TYPE_FIELD_WIDTH);
  DOUT_DATALOGGER_ID <= ep_dout_data(C_DATALOGGER_ID_FIELD_WIDTH
                                              + C_MSG_TYPE_FIELD_WIDTH - 1
                                              downto C_MSG_TYPE_FIELD_WIDTH);
  DOUT_MSG_TYPE   <= ep_dout_data(C_MSG_TYPE_FIELD_WIDTH - 1 downto 0);
  DOUT_VALID      <= ep_dout_valid;

  -- DS-T2: drive all CSR fields (previously only fifoStatus.full was wired).
  FIFO_CSR.fifoStatus.full     <= or_reduce(arb2ndLayerFullIn);
  FIFO_CSR.fifoStatus.empty    <= arb2nd_dout_empty;
  FIFO_CSR.fifoStatus.overflow <= burst_overflow_pulse;
  FIFO_CSR.level               <= std_logic_vector(burst_level_sum);
  FIFO_CSR.overflowCnt         <= std_logic_vector(burst_overflow_cnt_r);

  -----------------------------------------------------------------------------
  -- Per-protocol counter aggregation (DS-P3.26)
  --
  -- Drives the PROTO_COUNTERS output array from the architecture-scope
  -- per-protocol counter signals. Index per protocol comes from the
  -- generator-emitted C_PROTO_IDX_<NAME> constants. Disabled protocols
  -- naturally read zero (their counter signal stays at init value
  -- because no process inside a `generate` drives it).
  -----------------------------------------------------------------------------
  PROTO_COUNTERS(C_PROTO_IDX_I2C).pkt_cnt <= std_logic_vector(i2c_pkt_cnt_r);
  PROTO_COUNTERS(C_PROTO_IDX_I2C).err_cnt <= std_logic_vector(i2c_err_cnt_r);
  PROTO_COUNTERS(C_PROTO_IDX_I2C).drop_cnt <= std_logic_vector(i2c_drop_cnt_r);
  PROTO_COUNTERS(C_PROTO_IDX_JTAG).pkt_cnt <= std_logic_vector(jtag_pkt_cnt_r);
  PROTO_COUNTERS(C_PROTO_IDX_JTAG).err_cnt <= std_logic_vector(jtag_err_cnt_r);
  PROTO_COUNTERS(C_PROTO_IDX_JTAG).drop_cnt <= std_logic_vector(jtag_drop_cnt_r);
  PROTO_COUNTERS(C_PROTO_IDX_SPI).pkt_cnt <= std_logic_vector(spi_pkt_cnt_r);
  PROTO_COUNTERS(C_PROTO_IDX_SPI).err_cnt <= std_logic_vector(spi_err_cnt_r);
  PROTO_COUNTERS(C_PROTO_IDX_SPI).drop_cnt <= std_logic_vector(spi_drop_cnt_r);
  PROTO_COUNTERS(C_PROTO_IDX_UART).pkt_cnt <= std_logic_vector(uart_pkt_cnt_r);
  PROTO_COUNTERS(C_PROTO_IDX_UART).err_cnt <= std_logic_vector(uart_err_cnt_r);
  PROTO_COUNTERS(C_PROTO_IDX_UART).drop_cnt <= std_logic_vector(uart_drop_cnt_r);

end architecture RTL;
