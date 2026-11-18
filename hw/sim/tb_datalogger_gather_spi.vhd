-------------------------------------------------------------------------------
-- tb_datalogger_gather_spi.vhd  —  DS-P3.98 protocol-level SPI harness
-------------------------------------------------------------------------------
-- Drives a REAL SPI bus through the full datalogger_gather aggregator so the
-- adversarial cocotb suite can attack SGA-REQ-3 (single-source routing) at the
-- protocol layer for SPI, with a true BFM (cocotb SpiMaster) rather than
-- bit-banged or synthetic edges. SPI is master-driven (SCLK/CS/MOSI from the
-- master, MISO from a slave); the qspi_datalogger in 2-wire mode captures it.
--
--   cocotb SpiMaster ── SCLK/CS/MOSI ──┐
--   (MISO held idle; no slave for a    ├─> datalogger_gather.CAPTURE_PINS:
--    MOSI-capture SGA-REQ-3 test)       │     [C_SPI0_SCLK=23] [C_SPI0_CS=20]
--                                       │     [C_SPI0_MOSI=21] [C_SPI0_MISO=22]
--   every other pin idles '1' so only the (CONFIG-enabled) SPI0 datalogger emits.
--   The gather routes the decoded MOSI bytes onto DOUT_DATALOGGER_ID(=8).
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

library tich;
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;
use tich.system_regbank_pkg.all;

entity tb_datalogger_gather_spi is
  generic (
    G_SYS_CLK : natural := 100_000_000
  );
  port (
    CLK             : in  std_logic;
    RST             : in  std_logic;
    -- cocotb SPI master lines (CS active-low, SCLK idle per CPOL).
    SCLK            : in  std_logic;
    CS              : in  std_logic;
    MOSI            : in  std_logic;
    MISO            : in  std_logic;
    CONFIG          : in  std_logic_vector(C_DATALOGGER_CONFIG_WIDTH-1 downto 0);
    TIMESTAMP       : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
    DOUT_PAYLOAD    : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
    DOUT_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    DOUT_MSG_TYPE   : out std_logic_vector(15 downto 0);
    DOUT_VALID      : out std_logic;
    DOUT_REQ        : in  std_logic
  );
end entity tb_datalogger_gather_spi;

architecture str of tb_datalogger_gather_spi is
  signal capture_pins : std_logic_vector(C_NUM_DATALOGGER_PINS-1 downto 0) := (others => '1');
begin

  -- Map the 4 SPI lines onto the SPI0 datalogger's pins; all other pins idle high
  -- so only the enabled SPI0 instance can emit. SINGLE driver (process).
  p_pins : process(SCLK, CS, MOSI, MISO)
  begin
    capture_pins                 <= (others => '1');
    capture_pins(C_SPI0_SCLK)    <= SCLK;
    capture_pins(C_SPI0_CS)      <= CS;
    capture_pins(C_SPI0_MOSI)    <= MOSI;
    capture_pins(C_SPI0_MISO)    <= MISO;
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
