-------------------------------------------------------------------------------
-- tb_edge_agg_sparse.vhd  —  DS-P3.50 sparse→dense remap harness
-------------------------------------------------------------------------------
-- Instantiates edge_batcher_aggregator with the REAL deepskopion edge id set
-- (global ids 1,2,3,6 → 4 DENSE batchers), so the cocotb test can prove that an
-- event with global device_id=6 routes to the dense batcher that owns id 6 and
-- flushes with FR_DATALOGGER_ID=6 (the global id, not the dense index 3) — and
-- that NO dead batcher exists for ids 0/4/5/7.
--
-- G_CHANNEL_IDS packing (low byte = dense batcher 0):
--   batcher 0 → id 0x01,  batcher 1 → id 0x02,
--   batcher 2 → id 0x03,  batcher 3 → id 0x06   ⇒  x"06030201"
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

library tich;

entity tb_edge_agg_sparse is
  generic (
    G_N_MAX          : natural := 32;
    G_T_FLUSH_CYCLES : natural := 10_000
  );
  port (
    CLK              : in  std_logic;
    RST              : in  std_logic;
    EV_VALID         : in  std_logic;
    EV_READY         : out std_logic;
    EV_WORD          : in  std_logic_vector(31 downto 0);
    TIMESTAMP_64     : in  std_logic_vector(63 downto 0);
    FR_VALID         : out std_logic;
    FR_READY         : in  std_logic;
    FR_DATALOGGER_ID    : out std_logic_vector(7  downto 0);
    FR_MSG_CLASS     : out std_logic_vector(7  downto 0);
    FR_MSG_TYPE      : out std_logic_vector(15 downto 0);
    FR_FLAGS         : out std_logic_vector(15 downto 0);
    FR_TIMESTAMP     : out std_logic_vector(63 downto 0);
    FR_PAYLOAD       : out std_logic_vector(G_N_MAX*32 - 1 downto 0);
    FR_PAYLOAD_WORDS : out std_logic_vector(7 downto 0)
  );
end entity tb_edge_agg_sparse;

architecture tb of tb_edge_agg_sparse is
begin
  dut : entity tich.edge_batcher_aggregator
    generic map (
      G_NUM_CHANNELS   => 4,
      G_N_MAX          => G_N_MAX,
      G_T_FLUSH_CYCLES => G_T_FLUSH_CYCLES,
      G_CHANNEL_IDS    => x"06030201"   -- dense 0,1,2,3 ⇒ global ids 1,2,3,6
    )
    port map (
      CLK              => CLK,
      RST              => RST,
      EV_VALID         => EV_VALID,
      EV_READY         => EV_READY,
      EV_WORD          => EV_WORD,
      TIMESTAMP_64     => TIMESTAMP_64,
      FR_VALID         => FR_VALID,
      FR_READY         => FR_READY,
      FR_DATALOGGER_ID    => FR_DATALOGGER_ID,
      FR_MSG_CLASS     => FR_MSG_CLASS,
      FR_MSG_TYPE      => FR_MSG_TYPE,
      FR_FLAGS         => FR_FLAGS,
      FR_TIMESTAMP     => FR_TIMESTAMP,
      FR_PAYLOAD       => FR_PAYLOAD,
      FR_PAYLOAD_WORDS => FR_PAYLOAD_WORDS
    );
end architecture tb;
