-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- tb_datalogger_gather_uart.vhd  —  DS-P3.97 protocol-level UART harness
-------------------------------------------------------------------------------
-- Drives a REAL serial UART line through the full datalogger_gather aggregator so
-- the adversarial cocotb suite can attack SGA-REQ-3 (single-source routing) at
-- the protocol layer for UART, not with synthetic edges. UART is a one-way line
-- (the datalogger passively monitors TX) — no slave/bus-resolution, unlike I2C:
--
--   cocotb UartSource ── UART_IN ──> datalogger_gather.CAPTURE_PINS[36] (= C_UART0_TX)
--   every other pin idles '1' so only the (CONFIG-enabled) UART0 datalogger emits.
--   The gather demuxes the decoded UART payload onto DOUT_PAYLOAD /
--   DOUT_DATALOGGER_ID(=12).
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

library tich;
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;
use tich.system_regbank_pkg.all;

entity tb_datalogger_gather_uart is
  generic (
    G_SYS_CLK : natural := 100_000_000
  );
  port (
    CLK             : in  std_logic;
    RST             : in  std_logic;
    -- cocotb UART serial line (idle '1'); the UART0 datalogger monitors it.
    UART_IN         : in  std_logic;
    CONFIG          : in  std_logic_vector(C_DATALOGGER_CONFIG_WIDTH-1 downto 0);
    TIMESTAMP       : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
    DOUT_PAYLOAD    : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
    DOUT_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    DOUT_MSG_TYPE   : out std_logic_vector(15 downto 0);
    DOUT_VALID      : out std_logic;
    DOUT_REQ        : in  std_logic
  );
end entity tb_datalogger_gather_uart;

architecture str of tb_datalogger_gather_uart is
  signal capture_pins : std_logic_vector(C_NUM_DATALOGGER_PINS-1 downto 0) := (others => '1');
begin

  -- Map the serial line onto the UART0 datalogger's TX pin; all other pins idle
  -- high so only the enabled UART0 instance can emit. SINGLE driver (process).
  p_pins : process(UART_IN)
  begin
    capture_pins                <= (others => '1');
    capture_pins(C_UART0_TX)    <= UART_IN;
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
