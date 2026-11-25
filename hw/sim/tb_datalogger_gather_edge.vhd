-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- tb_datalogger_gather_edge.vhd  —  DS-P3.32 protocol-level EDGE pulse-gen harness
-------------------------------------------------------------------------------
-- Drives REAL multi-channel edge pulse trains through the full datalogger_gather
-- aggregator so the adversarial cocotb suite can attack SGA-REQ-3 (single-source
-- routing) at the protocol layer with ALL FOUR edge channels live at once — the
-- multi-source stress the single-instance I2C/UART/SPI harnesses cannot exert.
--
--   cocotb pulse-gen ── EDGE0_IN ─→ CAPTURE_PINS[42]  (EDGE0, dev_id 1)
--                       EDGE1_IN ─→ CAPTURE_PINS[43]  (EDGE1, dev_id 2)
--                       EDGE2_IN ─→ CAPTURE_PINS[44]  (EDGE2, dev_id 3)
--   every other pin idles '1' so only the (CONFIG-enabled) EDGE instances emit.
--   The gather demuxes each channel's transitions onto DOUT_PAYLOAD with the
--   channel's DOUT_DATALOGGER_ID — the test asserts each pulse train routes to its
--   own device id with the exact rising/falling polarity sequence.
--
-- EDGE is a passive single-line monitor: no slave/responder BFM (unlike I2C).
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

library tich;
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;
use tich.system_regbank_pkg.all;

entity tb_datalogger_gather_edge is
  generic (
    G_SYS_CLK : natural := 100_000_000
  );
  port (
    CLK             : in  std_logic;
    RST             : in  std_logic;
    -- cocotb pulse-gen edge lines (one per EDGE channel)
    EDGE0_IN        : in  std_logic;
    EDGE1_IN        : in  std_logic;
    EDGE2_IN        : in  std_logic;
    -- Datalogger control + gather burst output
    CONFIG          : in  std_logic_vector(C_DATALOGGER_CONFIG_WIDTH-1 downto 0);
    TIMESTAMP       : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
    DOUT_PAYLOAD    : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
    DOUT_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    DOUT_MSG_TYPE   : out std_logic_vector(15 downto 0);
    DOUT_VALID      : out std_logic;
    DOUT_REQ        : in  std_logic
  );
end entity tb_datalogger_gather_edge;

architecture str of tb_datalogger_gather_edge is
  signal capture_pins : std_logic_vector(C_NUM_DATALOGGER_PINS-1 downto 0) := (others => '1');
begin

  -- Map the three pulse-gen lines onto the EDGE0..2 datalogger pins; all other pins
  -- idle high so only the enabled EDGE instances can emit. SINGLE driver (a
  -- process) — a parallel `(others=>'1')` plus a per-bit assignment would
  -- multiply-drive the edge pins to 'X'.
  p_pins : process(EDGE0_IN, EDGE1_IN, EDGE2_IN)
  begin
    capture_pins                <= (others => '1');
    capture_pins(C_EDGE0_LINE)  <= EDGE0_IN;
    capture_pins(C_EDGE1_LINE)  <= EDGE1_IN;
    capture_pins(C_EDGE2_LINE)  <= EDGE2_IN;
  end process p_pins;

  u_gather : entity tich.datalogger_gather
    generic map (
      G_SYS_CLK => G_SYS_CLK
    )
    port map (
      RST             => RST,
      CLK             => CLK,
      CAPTURE_PINS      => capture_pins,
      CONFIG          => CONFIG,
      TIMESTAMP       => TIMESTAMP,
      DOUT_PAYLOAD    => DOUT_PAYLOAD,
      DOUT_DATALOGGER_ID => DOUT_DATALOGGER_ID,
      DOUT_MSG_TYPE   => DOUT_MSG_TYPE,
      DOUT_VALID      => DOUT_VALID,
      DOUT_REQ        => DOUT_REQ,
      FIFO_CSR        => open,
      PROTO_COUNTERS  => open,
      EDGE_ERR_CNTS   => open
    );

end architecture str;
