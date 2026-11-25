-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : tb_datalogger_gather
-- Project    : deepskopion
-- Standard   : VHDL 2008
-------------------------------------------------------------------------------
-- Description: Cocotb wrapper for datalogger_gather — flattens the FIFO_CSR
--              and PROTO_COUNTERS record / array-of-record outputs into simple
--              std_logic_vector ports cocotb can sample directly. Inputs
--              (CAPTURE_PINS, CONFIG, TIMESTAMP, DOUT_REQ) pass through 1:1.
--              Outputs DOUT_DATA/VALID also pass through.
--
--              For the PoC datalogger_config.yml: C_NUM_PROTO_COUNTERS=4 (I2C,
--              UART, SPI, EDGE). Sorted-alphabetic index per
--              datalogger_config_pkg.C_PROTO_IDX_*:
--                idx 0 → EDGE
--                idx 1 → I2C
--                idx 2 → SPI
--                idx 3 → UART
--              Wrapper exposes them as
--                proto_pkt_cnt_<idx> + proto_err_cnt_<idx>.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;
use tich.arbiter_pkg.all;

entity tb_datalogger_gather is
  generic (
    G_SYS_CLK : natural := 100_000_000
  );
  port (
    CLK        : in  std_logic;
    RST        : in  std_logic;
    -- Pass-through inputs
    CAPTURE_PINS : in  std_logic_vector(C_NUM_DATALOGGER_PINS - 1 downto 0);
    CONFIG     : in  std_logic_vector(C_DATALOGGER_CONFIG_WIDTH - 1 downto 0);
    TIMESTAMP  : in  std_logic_vector(C_TIMESTAMP_WIDTH - 1 downto 0);
    DOUT_REQ  : in  std_logic;
    -- DS-P3.29: tagged burst output (payload + datalogger_id + msg_type)
    DOUT_PAYLOAD    : out std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0);
    DOUT_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    DOUT_MSG_TYPE   : out std_logic_vector(15 downto 0);
    DOUT_VALID      : out std_logic;
    -- Flattened FIFO_CSR
    FIFO_BURST_FULL      : out std_logic;
    FIFO_BURST_EMPTY     : out std_logic;
    FIFO_BURST_OVERFLOW  : out std_logic;
    FIFO_BURST_LEVEL     : out std_logic_vector(C_FIFO_LEVEL_WIDTH - 1 downto 0);
    FIFO_BURST_OVF_CNT   : out std_logic_vector(C_FIFO_OVERFLOW_WIDTH - 1 downto 0);
    -- Flattened PROTO_COUNTERS — 4 protocols (sorted alphabetical) ×
    -- {pkt_cnt, err_cnt, drop_cnt}. EDGE=0, I2C=1, SPI=2, UART=3.
    PROTO_PKT_CNT_FLAT  : out std_logic_vector(C_NUM_PROTO_COUNTERS*32 - 1 downto 0);
    PROTO_ERR_CNT_FLAT  : out std_logic_vector(C_NUM_PROTO_COUNTERS*32 - 1 downto 0);
    PROTO_DROP_CNT_FLAT : out std_logic_vector(C_NUM_PROTO_COUNTERS*32 - 1 downto 0)
  );
end entity tb_datalogger_gather;

architecture str of tb_datalogger_gather is

  signal fifo_csr : t_fifo_csr;
  signal proto_counters : t_proto_counter_array(0 to C_NUM_PROTO_COUNTERS - 1);

begin

  ---------------------------------------------------------------------------
  -- DUT
  ---------------------------------------------------------------------------
  u_dut : entity tich.datalogger_gather
    generic map (
      G_SYS_CLK => G_SYS_CLK
    )
    port map (
      RST              => RST,
      CLK              => CLK,
      CAPTURE_PINS       => CAPTURE_PINS,
      CONFIG           => CONFIG,
      TIMESTAMP        => TIMESTAMP,
      DOUT_PAYLOAD    => DOUT_PAYLOAD,
      DOUT_DATALOGGER_ID => DOUT_DATALOGGER_ID,
      DOUT_MSG_TYPE   => DOUT_MSG_TYPE,
      DOUT_VALID      => DOUT_VALID,
      DOUT_REQ        => DOUT_REQ,
      FIFO_CSR   => fifo_csr,
      PROTO_COUNTERS   => proto_counters
    );

  ---------------------------------------------------------------------------
  -- Flatten FIFO_CSR
  ---------------------------------------------------------------------------
  FIFO_BURST_FULL     <= fifo_csr.fifoStatus.full;
  FIFO_BURST_EMPTY    <= fifo_csr.fifoStatus.empty;
  FIFO_BURST_OVERFLOW <= fifo_csr.fifoStatus.overflow;
  FIFO_BURST_LEVEL    <= fifo_csr.level;
  FIFO_BURST_OVF_CNT  <= fifo_csr.overflowCnt;

  ---------------------------------------------------------------------------
  -- Flatten PROTO_COUNTERS into one concatenated vector per field
  ---------------------------------------------------------------------------
  gen_flat : for i in 0 to C_NUM_PROTO_COUNTERS - 1 generate
    PROTO_PKT_CNT_FLAT((i + 1) * 32 - 1 downto i * 32) <= proto_counters(i).pkt_cnt;
    PROTO_ERR_CNT_FLAT((i + 1) * 32 - 1 downto i * 32) <= proto_counters(i).err_cnt;
    PROTO_DROP_CNT_FLAT((i + 1) * 32 - 1 downto i * 32) <= proto_counters(i).drop_cnt;
  end generate gen_flat;

end architecture str;
