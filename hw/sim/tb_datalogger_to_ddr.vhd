-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : tb_datalogger_to_ddr
-- Project    : deepskopion
-- Standard   : VHDL 2008
-------------------------------------------------------------------------------
-- Description: Full datalogger data-path integration wrapper for cocotb.
--
--   CAPTURE_PINS ─▶ datalogger_gather ─▶ zs_msg_framer ─▶ ddr_stream_axi ─▶ AXI4
--                  (tich)            (tich)           (rr)              master
--
--   Drives the complete burst path end to end so a cocotb test can push
--   I2C / UART / SPI traffic onto the datalogger pins and watch framed
--   zs_msg frames land in a BRAM-ish DDR sink (cocotbext.axi.AxiRam
--   attached to the flat M_AXI_* ports below — the same flattening
--   convention as rr-ddr-stream's wrapper_ddr_stream_axi).
--
--   EDGE traffic routes through the production edge_batcher_aggregator before
--   the same variable-length framer, matching deepskopion_core.
--
--   Clocks: sys and ddr are tied to one CLK for sim simplicity (the IP
--   supports independent clocks; the cocotb test drives a single domain).
--   TIMESTAMP is an internal free-running 64-bit counter — gather takes
--   the low 32 bits, the framer takes the full 64.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_config_pkg.all;     -- C_NUM_DATALOGGER_PINS
use tich.datalogger_pkg.all;            -- C_DATALOGGER_MSG_WIDTH, C_TIMESTAMP_WIDTH
use tich.datalogger_records_pkg.all;    -- t_fifo_csr, t_proto_counter_array, C_DATALOGGER_CONFIG_WIDTH
use tich.zs_msg_pkg.all;             -- C_ZS_CLASS_DATA
use tich.axi_pkg.C_ZYNQ7_S_AXI_HP_MAX_BURST;  -- DS-T2.65: single-sourced AXI3 write-burst cap (same as deepskopion_top)

-- Selective imports from rr.ddr_stream_pkg (DS-T11). The bare-`.all` form
-- re-exports C_INIT_FIFO_CSR and C_INIT_FIFO_STATUS, which also live in
-- tich.datalogger_records_pkg with different record types. Any bare reference
-- becomes a "multiple conflicting visible declarations" NVC error.
-- Importing only the symbols this wrapper actually needs removes the
-- homograph hazard. (The third historical collision, C_DATALOGGER_MSG_WIDTH,
-- was retired upstream 2026-05-28 — renamed to C_USER_MSG_WIDTH.)
library rr;
use rr.ddr_stream_pkg.axi4_h2c_bus_t;
use rr.ddr_stream_pkg.axi4_c2h_bus_t;
use rr.ddr_stream_pkg.C_AXI_DATA_WIDTH;
use rr.ddr_stream_pkg.C_AXI_ID_WIDTH;
use rr.ddr_stream_pkg.C_AXI_ADDR_WIDTH;
use rr.ddr_stream_pkg.C_AXI_LEN_WIDTH;
use rr.ddr_stream_pkg.C_AXI_SIZE_WIDTH;
use rr.ddr_stream_pkg.C_AXI_BURST_WIDTH;
use rr.ddr_stream_pkg.C_AXI_LOCK_WIDTH;
use rr.ddr_stream_pkg.C_AXI_CACHE_WIDTH;
use rr.ddr_stream_pkg.C_AXI_PROT_WIDTH;
use rr.ddr_stream_pkg.C_AXI_QOS_WIDTH;
use rr.ddr_stream_pkg.C_AXI_RESP_WIDTH;

entity tb_datalogger_to_ddr is
  generic (
    G_SYS_CLK        : natural := 100_000_000;
    G_AXI_ID         : integer := 0;
    G_FIFO_DEPTH     : integer := 9;
    G_USER_DATA_WIDTH : integer := 32;
    G_DDR_DATA_WIDTH : integer := 128;
    G_DDR_ADDR_WIDTH : integer := 32;
    -- DS-P2.110: base is a fixed-width VECTOR, mirroring the deepskopion_core →
    -- ddr_stream_axi seam that DS-T3.32 (2b565da) already put in the vector
    -- domain. As an INTEGER this generic could NOT carry the DE25 product base
    -- 0xA0000000 (= 2_684_354_560 > INTEGER'high on 32-bit-signed VHDL), so the
    -- tb was structurally incapable of exercising the production geometry and
    -- always ran at base 0 / a toy DDR width — the scaled-sim trap that let the
    -- DS-P3.153 rr_ddr_stream repin's uploader-idle wedge slip simulation.
    G_DDR_BASE_ADDR  : std_logic_vector(31 downto 0) := (others => '0');
    G_BUFFER_WIDTH   : integer := 31;
    G_FIFO_IMPL      : string  := "olo";
    -- PF-H0 (DS-P2.124): which rr-ddr-stream backend the write plane elaborates,
    -- mirroring the deepskopion_core G_DDR_BACKEND arm. "axi" (default) keeps the
    -- Zynq/DE25-proven ddr_stream_axi body byte-for-byte; "fic" selects the
    -- PolarFire SoC MSS-FIC body (ddr_stream_fic, DDR-P2.15) so the PFSoC
    -- product-geometry guard (test_datalogger_to_ddr_product_base_fic) exercises
    -- the SAME write plane the PFSoC top ships (64-bit, 64-beat cap, base
    -- 0xA0000000). Both bodies wrap the same ddr_stream_axi core, so the
    -- core-side taps this tb watches (AWVALID / WR_COMMIT_ADDR / WRITE_IDLE)
    -- behave identically — the guard's power is that a broken fic arm or a width
    -- truncation still fails the exact assertions the DE25 axi guard uses.
    G_DDR_BACKEND    : string  := "axi"
  );
  port (
    CLK : in std_logic;
    RST : in std_logic;

    -- Scalar datalogger stimulus pins — one handle per protocol line so the
    -- routertl SDK BFMs (UartSource / I2cMasterDriver / SpiMaster) can each
    -- own a signal. Assembled into the gather's flat CAPTURE_PINS bus below
    -- at the indices declared in datalogger_config_pkg (C_I2C0_SCL = 12, ...).
    I2C0_SCL  : in std_logic := '1';   -- pin C_I2C0_SCL  (idle high, open-drain)
    I2C0_SDA  : in std_logic := '1';   -- pin C_I2C0_SDA
    UART0_TX  : in std_logic := '1';   -- pin C_UART0_TX  (idle high)
    SPI0_SCLK : in std_logic := '0';   -- pin C_SPI0_SCLK
    SPI0_CS   : in std_logic := '1';   -- pin C_SPI0_CS   (idle high)
    SPI0_MOSI : in std_logic := '0';   -- pin C_SPI0_MOSI
    SPI0_MISO : in std_logic := '0';   -- pin C_SPI0_MISO
    EDGE0     : in std_logic := '0';   -- pin C_EDGE0_LINE, device id 1
    CONFIG    : in std_logic_vector(C_DATALOGGER_CONFIG_WIDTH - 1 downto 0);

    -- Status taps (handy for the cocotb test)
    WRITE_IDLE : out std_logic;
    GATHER_BURST_VALID : out std_logic;
    -- DS-P2.110: live DMA commit pointer (ddr_stream_axi.wr_commit_addr_o) — the
    -- byte offset the uploader has actually written+B-acked to DDR. On silicon
    -- (DS-T1.12) this stayed wedged at 0 while the enqueue counter climbed; the
    -- product-geometry regression watches it advance to prove the write plane
    -- really initiated and completed transfers.
    WR_COMMIT_ADDR : out std_logic_vector(G_DDR_ADDR_WIDTH - 1 downto 0);

    --------------------------------------------------------------------------
    -- FLAT AXI4 MASTER (DUT → DDR sink). Mirrors wrapper_ddr_stream_axi so
    -- cocotbext.axi.AxiBus.from_prefix("M_AXI") + AxiRam attach cleanly.
    -- Write address channel
    M_AXI_AWID    : out std_logic_vector(C_AXI_ID_WIDTH-1    downto 0);
    M_AXI_AWADDR  : out std_logic_vector(C_AXI_ADDR_WIDTH-1  downto 0);
    M_AXI_AWLEN   : out std_logic_vector(C_AXI_LEN_WIDTH-1   downto 0);
    M_AXI_AWSIZE  : out std_logic_vector(C_AXI_SIZE_WIDTH-1  downto 0);
    M_AXI_AWBURST : out std_logic_vector(C_AXI_BURST_WIDTH-1 downto 0);
    M_AXI_AWLOCK  : out std_logic_vector(C_AXI_LOCK_WIDTH-1  downto 0);
    M_AXI_AWCACHE : out std_logic_vector(C_AXI_CACHE_WIDTH-1 downto 0);
    M_AXI_AWPROT  : out std_logic_vector(C_AXI_PROT_WIDTH-1  downto 0);
    M_AXI_AWQOS   : out std_logic_vector(C_AXI_QOS_WIDTH-1   downto 0);
    M_AXI_AWVALID : out std_logic;
    -- Write data channel
    M_AXI_WDATA   : out std_logic_vector(G_DDR_DATA_WIDTH-1   downto 0);
    M_AXI_WSTRB   : out std_logic_vector(G_DDR_DATA_WIDTH/8-1 downto 0);
    M_AXI_WLAST   : out std_logic;
    M_AXI_WVALID  : out std_logic;
    -- Write response (master side)
    M_AXI_BREADY  : out std_logic;
    -- Read address channel
    M_AXI_ARID    : out std_logic_vector(C_AXI_ID_WIDTH-1    downto 0);
    M_AXI_ARADDR  : out std_logic_vector(C_AXI_ADDR_WIDTH-1  downto 0);
    M_AXI_ARLEN   : out std_logic_vector(C_AXI_LEN_WIDTH-1   downto 0);
    M_AXI_ARSIZE  : out std_logic_vector(C_AXI_SIZE_WIDTH-1  downto 0);
    M_AXI_ARBURST : out std_logic_vector(C_AXI_BURST_WIDTH-1 downto 0);
    M_AXI_ARLOCK  : out std_logic_vector(C_AXI_LOCK_WIDTH-1  downto 0);
    M_AXI_ARCACHE : out std_logic_vector(C_AXI_CACHE_WIDTH-1 downto 0);
    M_AXI_ARPROT  : out std_logic_vector(C_AXI_PROT_WIDTH-1  downto 0);
    M_AXI_ARQOS   : out std_logic_vector(C_AXI_QOS_WIDTH-1   downto 0);
    M_AXI_ARVALID : out std_logic;
    M_AXI_RREADY  : out std_logic;
    --------------------------------------------------------------------------
    -- FLAT AXI4 SLAVE RESPONSE (DDR sink → DUT)
    M_AXI_AWREADY : in  std_logic;
    M_AXI_WREADY  : in  std_logic;
    M_AXI_BID     : in  std_logic_vector(C_AXI_ID_WIDTH-1   downto 0);
    M_AXI_BRESP   : in  std_logic_vector(C_AXI_RESP_WIDTH-1 downto 0);
    M_AXI_BVALID  : in  std_logic;
    M_AXI_ARREADY : in  std_logic;
    M_AXI_RID     : in  std_logic_vector(C_AXI_ID_WIDTH-1   downto 0);
    M_AXI_RDATA   : in  std_logic_vector(G_DDR_DATA_WIDTH-1 downto 0);
    M_AXI_RRESP   : in  std_logic_vector(C_AXI_RESP_WIDTH-1 downto 0);
    M_AXI_RLAST   : in  std_logic;
    M_AXI_RVALID  : in  std_logic
  );
end entity tb_datalogger_to_ddr;

architecture str of tb_datalogger_to_ddr is

  function f_edge_ids_flat return std_logic_vector is
    variable v : std_logic_vector(C_EDGE_DATALOGGERS*8-1 downto 0) := (others => '0');
  begin
    for k in 0 to C_EDGE_DATALOGGERS-1 loop
      v((k+1)*8-1 downto k*8) := std_logic_vector(to_unsigned(C_EDGE_IDS(k), 8));
    end loop;
    return v;
  end function;
  constant C_EDGE_IDS_FLAT_W : std_logic_vector(C_EDGE_DATALOGGERS*8-1 downto 0)
                             := f_edge_ids_flat;

  -- ── board_pin bus (DS-P2.166) ───────────────────────────────────────────
  signal board_pin : std_logic_vector(C_NUM_BOARD_PINS - 1 downto 0) := (others => '0');
  -- ── Assembled datalogger pin bus (idle low except driven protocol pins) ──
  signal capture_bus : std_logic_vector(C_NUM_DATALOGGER_PINS - 1 downto 0) := (others => '0');

  -- ── Free-running timestamp ────────────────────────────────────────────
  signal ts_r : unsigned(63 downto 0) := (others => '0');

  -- ── gather → framer ───────────────────────────────────────────────────
  signal g_payload    : std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0);
  signal g_datalogger_id : std_logic_vector(7 downto 0);
  signal g_msg_type   : std_logic_vector(15 downto 0);
  signal g_valid      : std_logic;
  signal g_dout_req  : std_logic;        -- framer.IN_READY → gather back-pressure
  signal g_fifo_csr   : t_fifo_csr;
  signal g_proto_cnt  : t_proto_counter_array(0 to C_NUM_PROTO_COUNTERS - 1);

  -- ── framer → ddr_stream_axi (32-bit AXI-Stream) ───────────────────────
  signal fr_out_data  : std_logic_vector(31 downto 0);
  signal fr_out_valid : std_logic;
  signal s_axis_ready : std_logic;

  constant C_PAYLOAD_WORDS_MAX : integer := 32;
  signal gather_is_edge, gather_to_agg_v, gather_to_fr_v : std_logic;
  signal agg_ev_ready, agg_fr_valid, agg_fr_ready : std_logic;
  signal agg_fr_datalogger_id : std_logic_vector(7 downto 0);
  signal agg_fr_msg_class  : std_logic_vector(7 downto 0);
  signal agg_fr_msg_type, agg_fr_flags : std_logic_vector(15 downto 0);
  signal agg_fr_timestamp : std_logic_vector(63 downto 0);
  signal agg_fr_payload : std_logic_vector(C_PAYLOAD_WORDS_MAX*32-1 downto 0);
  signal agg_fr_words : std_logic_vector(7 downto 0);
  signal fr_in_valid, fr_in_ready : std_logic;
  signal fr_datalogger_id, fr_msg_class : std_logic_vector(7 downto 0);
  signal fr_msg_type, fr_flags : std_logic_vector(15 downto 0);
  signal fr_timestamp : std_logic_vector(63 downto 0);
  signal fr_payload : std_logic_vector(C_PAYLOAD_WORDS_MAX*32-1 downto 0);
  signal fr_words : std_logic_vector(7 downto 0);
  signal nonedge_payload : std_logic_vector(C_PAYLOAD_WORDS_MAX*32-1 downto 0);

  -- ── AXI4 record bus at the IP boundary ────────────────────────────────
  signal axi_h2c : axi4_h2c_bus_t;
  signal axi_c2h : axi4_c2h_bus_t;

begin

  GATHER_BURST_VALID <= g_valid;

  ---------------------------------------------------------------------------
  -- Route the scalar bench pins onto the board_pin vector, then assemble
  -- capture_bus via C_CAPTURE_BOARD_PIN from datalogger_config_pkg
  -- (mirroring deepskopion_core).
  ---------------------------------------------------------------------------
  board_pin(C_BOARD_PIN_JA0) <= I2C0_SCL;
  board_pin(C_BOARD_PIN_JA1) <= I2C0_SDA;
  board_pin(C_BOARD_PIN_JB0) <= UART0_TX;
  board_pin(C_BOARD_PIN_JA7) <= SPI0_SCLK;
  board_pin(C_BOARD_PIN_JA4) <= SPI0_CS;
  board_pin(C_BOARD_PIN_JA5) <= SPI0_MOSI;
  board_pin(C_BOARD_PIN_JA6) <= SPI0_MISO;
  board_pin(C_BOARD_PIN_JB2) <= EDGE0;

  gen_capture_bus : for i in capture_bus'range generate
    gen_wired : if C_CAPTURE_BOARD_PIN(i) /= C_BOARD_PIN_NONE generate
      capture_bus(i) <= board_pin(C_CAPTURE_BOARD_PIN(i));
    end generate gen_wired;
    gen_unwired : if C_CAPTURE_BOARD_PIN(i) = C_BOARD_PIN_NONE generate
      capture_bus(i) <= '0';
    end generate gen_unwired;
  end generate gen_capture_bus;

  ---------------------------------------------------------------------------
  -- Free-running 64-bit timestamp
  ---------------------------------------------------------------------------
  ts_proc : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        ts_r <= (others => '0');
      else
        ts_r <= ts_r + 1;
      end if;
    end if;
  end process ts_proc;

  ---------------------------------------------------------------------------
  -- Datalogger aggregation node
  ---------------------------------------------------------------------------
  u_gather : entity tich.datalogger_gather
    generic map (
      G_SYS_CLK => G_SYS_CLK
    )
    port map (
      CLK              => CLK,
      RST              => RST,
      CAPTURE_PINS       => capture_bus,
      CONFIG           => CONFIG,
      TIMESTAMP        => std_logic_vector(ts_r(C_TIMESTAMP_WIDTH - 1 downto 0)),
      DOUT_PAYLOAD    => g_payload,
      DOUT_DATALOGGER_ID => g_datalogger_id,
      DOUT_MSG_TYPE   => g_msg_type,
      DOUT_VALID      => g_valid,
      DOUT_REQ        => g_dout_req,
      FIFO_CSR   => g_fifo_csr,
      PROTO_COUNTERS   => g_proto_cnt
    );

  ---------------------------------------------------------------------------
  -- Production EDGE classifier + batcher, followed by the shared framer.
  ---------------------------------------------------------------------------
  -- DS-T2.84: the same classifier as deepskopion_core (lifecycle bypasses).
  gather_is_edge <= is_edge_batch_word(g_msg_type);
  gather_to_agg_v <= g_valid and gather_is_edge;
  gather_to_fr_v <= g_valid and not gather_is_edge;
  nonedge_payload(31 downto 0) <= g_payload;
  nonedge_payload(nonedge_payload'high downto 32) <= (others => '0');

  gen_edge_agg : if C_EDGE_DATALOGGERS > 0 generate
    u_edge_agg : entity tich.edge_batcher_aggregator
      generic map (G_NUM_CHANNELS => C_EDGE_DATALOGGERS,
                   G_N_MAX => C_PAYLOAD_WORDS_MAX,
                   G_T_FLUSH_CYCLES => 64,
                   G_USE_EVENT_TIMESTAMP => true,
                   G_CHANNEL_IDS => C_EDGE_IDS_FLAT_W)
      port map (
        CLK => CLK, RST => RST, EV_VALID => gather_to_agg_v,
        EV_READY => agg_ev_ready, EV_WORD => g_payload,
        TIMESTAMP_64 => std_logic_vector(ts_r), FR_VALID => agg_fr_valid,
        FR_READY => agg_fr_ready, FR_DATALOGGER_ID => agg_fr_datalogger_id,
        FR_MSG_CLASS => agg_fr_msg_class, FR_MSG_TYPE => agg_fr_msg_type,
        FR_FLAGS => agg_fr_flags, FR_TIMESTAMP => agg_fr_timestamp,
        FR_PAYLOAD => agg_fr_payload, FR_PAYLOAD_WORDS => agg_fr_words);
  else generate
    agg_ev_ready         <= '1';
    agg_fr_valid         <= '0';
    agg_fr_datalogger_id <= (others => '0');
    agg_fr_msg_class     <= (others => '0');
    agg_fr_msg_type      <= (others => '0');
    agg_fr_flags         <= (others => '0');
    agg_fr_timestamp     <= (others => '0');
    agg_fr_payload       <= (others => '0');
    agg_fr_words         <= (others => '0');
  end generate gen_edge_agg;

  fr_in_valid <= agg_fr_valid or gather_to_fr_v;
  fr_datalogger_id <= agg_fr_datalogger_id when agg_fr_valid = '1' else g_datalogger_id;
  fr_msg_class <= agg_fr_msg_class when agg_fr_valid = '1' else C_ZS_CLASS_DATA;
  fr_msg_type <= agg_fr_msg_type when agg_fr_valid = '1' else g_msg_type;
  fr_flags <= agg_fr_flags when agg_fr_valid = '1' else (others => '0');
  fr_timestamp <= agg_fr_timestamp when agg_fr_valid = '1' else std_logic_vector(ts_r);
  fr_payload <= agg_fr_payload when agg_fr_valid = '1' else nonedge_payload;
  fr_words <= agg_fr_words when agg_fr_valid = '1' else std_logic_vector(to_unsigned(1, 8));
  agg_fr_ready <= fr_in_ready when agg_fr_valid = '1' else '0';
  g_dout_req <= agg_ev_ready when gather_is_edge = '1'
                else fr_in_ready when agg_fr_valid = '0'
                else '0';

  u_framer : entity tich.zs_msg_framer
    generic map (
      G_PAYLOAD_WORDS => C_PAYLOAD_WORDS_MAX
    )
    port map (
      CLK         => CLK,
      RST         => RST,
      IN_VALID    => fr_in_valid,
      IN_READY    => fr_in_ready,
      DATALOGGER_ID  => fr_datalogger_id,
      MSG_CLASS   => fr_msg_class,
      MSG_TYPE    => fr_msg_type,
      FLAGS       => fr_flags,
      TIMESTAMP   => fr_timestamp,
      PAYLOAD     => fr_payload,
      PAYLOAD_WORDS_LIVE => fr_words,
      OUT_DATA    => fr_out_data,
      OUT_VALID   => fr_out_valid,
      OUT_READY   => s_axis_ready
    );

  ---------------------------------------------------------------------------
  -- DDR streaming DMA — write plane bursts the framed stream to the AXI
  -- master (→ AxiRam in cocotb). Read plane left idle (m_axis unused).
  ---------------------------------------------------------------------------
  -- PF-H0 (DS-P2.124): backend-select generate. The "axi" arm is the DE25/Zynq
  -- body, kept byte-identical (its label + generic map are what the DS-P2.110
  -- guards match). The "fic" arm elaborates the PolarFire SoC MSS-FIC body at
  -- the same seam so the PFSoC product-geometry guard runs the write plane the
  -- PFSoC top actually ships.
  gen_ddr_axi : if G_DDR_BACKEND = "axi" generate
  u_ddr : entity rr.ddr_stream_axi
    generic map (
      axi_id_g          => G_AXI_ID,
      fifo_depth_g      => G_FIFO_DEPTH,
      user_data_width_g => G_USER_DATA_WIDTH,
      ddr_data_width_g  => G_DDR_DATA_WIDTH,
      ddr_addr_width_g  => G_DDR_ADDR_WIDTH,
      ddr_base_addr_g   => G_DDR_BASE_ADDR,   -- DS-P2.110: vector passes through unchanged
      buffer_width_g    => G_BUFFER_WIDTH,
      -- DS-T2.60: mirror deepskopion_top's u_ddr_burst — cap the write burst to
      -- 16 beats (AWLEN<=15) for the Zynq-7 S_AXI_HP AXI3 target. Left at the
      -- AXI4 default (256) this instance emits AWLEN up to 254 under sustained
      -- back-pressure (proven by test_axi3_burst_cap_under_backpressure), which
      -- a real AXI3 slave splits/drops -> silent data loss.
      max_burst_len_g   => C_ZYNQ7_S_AXI_HP_MAX_BURST,  -- DS-T2.65: single-sourced (axi_pkg) — cannot diverge from deepskopion_top
      fifo_impl_g       => G_FIFO_IMPL,
      enable_read_plane_g => false
    )
    port map (
      clk_sys_i        => CLK,
      clk_ddr_i        => CLK,
      rst_sys_i        => RST,
      rst_ddr_i        => RST,
      s_axis_data_i    => fr_out_data,
      s_axis_valid_i   => fr_out_valid,
      s_axis_ready_o   => s_axis_ready,
      m_axis_data_o    => open,
      m_axis_valid_o   => open,
      m_axis_ready_i   => '0',
      csr_wr_base_addr_i => G_DDR_BASE_ADDR(G_DDR_ADDR_WIDTH - 1 downto 0),
      csr_wr_base_load_i => '0',
      csr_rd_base_addr_i => (others => '0'),
      csr_rd_base_load_i => '0',
      write_idle_o       => WRITE_IDLE,
      read_idle_o        => open,
      ddr_csr_o        => open,
      wr_commit_addr_o => WR_COMMIT_ADDR,
      m_axi4_h2c_bus_o => axi_h2c,
      m_axi4_c2h_bus_i => axi_c2h
    );
  end generate gen_ddr_axi;

  gen_ddr_fic : if G_DDR_BACKEND = "fic" generate
  -- PolarFire SoC MSS-FIC body. Same core-side contract; the FIC defaults clamp
  -- the write burst to C_PFSOC_FIC_MAX_BURST (64 beats). Driven at 64-bit width /
  -- base 0xA0000000 by the PFSoC product-geometry guard.
  u_ddr : entity rr.ddr_stream_fic
    generic map (
      axi_id_g          => G_AXI_ID,
      fifo_depth_g      => G_FIFO_DEPTH,
      user_data_width_g => G_USER_DATA_WIDTH,
      ddr_data_width_g  => G_DDR_DATA_WIDTH,
      ddr_addr_width_g  => G_DDR_ADDR_WIDTH,
      ddr_base_addr_g   => G_DDR_BASE_ADDR,
      buffer_width_g    => G_BUFFER_WIDTH,
      max_burst_len_g   => 64,          -- C_PFSOC_FIC_MAX_BURST (no clamp warning)
      fifo_impl_g       => G_FIFO_IMPL,
      enable_read_plane_g => false
    )
    port map (
      clk_sys_i        => CLK,
      clk_ddr_i        => CLK,
      rst_sys_i        => RST,
      rst_ddr_i        => RST,
      s_axis_data_i    => fr_out_data,
      s_axis_valid_i   => fr_out_valid,
      s_axis_ready_o   => s_axis_ready,
      m_axis_data_o    => open,
      m_axis_valid_o   => open,
      m_axis_ready_i   => '0',
      csr_wr_base_addr_i => G_DDR_BASE_ADDR(G_DDR_ADDR_WIDTH - 1 downto 0),
      csr_wr_base_load_i => '0',
      csr_rd_base_addr_i => (others => '0'),
      csr_rd_base_load_i => '0',
      write_idle_o       => WRITE_IDLE,
      read_idle_o        => open,
      ddr_csr_o        => open,
      wr_commit_addr_o => WR_COMMIT_ADDR,
      m_axi4_h2c_bus_o => axi_h2c,
      m_axi4_c2h_bus_i => axi_c2h
    );
  end generate gen_ddr_fic;

  ---------------------------------------------------------------------------
  -- RECORD → FLAT (master commands out to the AxiRam sink)
  ---------------------------------------------------------------------------
  M_AXI_AWID    <= axi_h2c.AWID;
  M_AXI_AWADDR  <= axi_h2c.AWADDR;
  M_AXI_AWLEN   <= axi_h2c.AWLEN;
  M_AXI_AWSIZE  <= axi_h2c.AWSIZE;
  M_AXI_AWBURST <= axi_h2c.AWBURST;
  M_AXI_AWLOCK  <= axi_h2c.AWLOCK;
  M_AXI_AWCACHE <= axi_h2c.AWCACHE;
  M_AXI_AWPROT  <= axi_h2c.AWPROT;
  M_AXI_AWQOS   <= axi_h2c.AWQOS;
  M_AXI_AWVALID <= axi_h2c.AWVALID;
  M_AXI_WDATA   <= axi_h2c.WDATA(G_DDR_DATA_WIDTH-1 downto 0);
  M_AXI_WSTRB   <= axi_h2c.WSTRB(G_DDR_DATA_WIDTH/8-1 downto 0);
  M_AXI_WLAST   <= axi_h2c.WLAST;
  M_AXI_WVALID  <= axi_h2c.WVALID;
  M_AXI_BREADY  <= axi_h2c.BREADY;
  M_AXI_ARID    <= axi_h2c.ARID;
  M_AXI_ARADDR  <= axi_h2c.ARADDR;
  M_AXI_ARLEN   <= axi_h2c.ARLEN;
  M_AXI_ARSIZE  <= axi_h2c.ARSIZE;
  M_AXI_ARBURST <= axi_h2c.ARBURST;
  M_AXI_ARLOCK  <= axi_h2c.ARLOCK;
  M_AXI_ARCACHE <= axi_h2c.ARCACHE;
  M_AXI_ARPROT  <= axi_h2c.ARPROT;
  M_AXI_ARQOS   <= axi_h2c.ARQOS;
  M_AXI_ARVALID <= axi_h2c.ARVALID;
  M_AXI_RREADY  <= axi_h2c.RREADY;

  ---------------------------------------------------------------------------
  -- FLAT → RECORD (slave responses back from the AxiRam sink)
  ---------------------------------------------------------------------------
  axi_c2h.AWREADY <= M_AXI_AWREADY;
  axi_c2h.WREADY  <= M_AXI_WREADY;
  axi_c2h.BID     <= M_AXI_BID;
  axi_c2h.BRESP   <= M_AXI_BRESP;
  axi_c2h.BVALID  <= M_AXI_BVALID;
  axi_c2h.ARREADY <= M_AXI_ARREADY;
  axi_c2h.RID     <= M_AXI_RID;
  axi_c2h.RDATA(G_DDR_DATA_WIDTH-1 downto 0)               <= M_AXI_RDATA;
  axi_c2h.RDATA(C_AXI_DATA_WIDTH-1 downto G_DDR_DATA_WIDTH) <= (others => '0');
  axi_c2h.RRESP   <= M_AXI_RRESP;
  axi_c2h.RLAST   <= M_AXI_RLAST;
  axi_c2h.RVALID  <= M_AXI_RVALID;

end architecture str;
