-------------------------------------------------------------------------------
-- tb_datalogger_gather_jtag.vhd  —  DS-P3.99 protocol-level JTAG harness
-------------------------------------------------------------------------------
-- Drives a REAL IEEE-1149.1 TAP sequence (cocotb TapMaster: TCK/TMS/TDI/TDO)
-- through the full datalogger_gather aggregator so the adversarial cocotb suite can
-- attack SGA-REQ-3 (single-source routing) at the protocol layer for the JTAG
-- datalogger — the last member of the I2C/UART/SPI/EDGE BFM-harness family.
--
--   cocotb TapMaster ── JTAG_PINS[0]=TCK ─→ CAPTURE_PINS[24]
--                       JTAG_PINS[1]=TMS ─→ CAPTURE_PINS[25]
--                       JTAG_PINS[2]=TDI ─→ CAPTURE_PINS[26]
--                       JTAG_PINS[3]=TDO ─→ CAPTURE_PINS[27]   (JTAG0, dev_id 16)
--   every other pin idles '1' so only the (CONFIG-enabled) JTAG0 instance emits.
--   The gather demuxes the IR/DR shift bytes + begin/end events onto
--   DOUT_PAYLOAD with DOUT_DATALOGGER_ID = 16.
--
-- The JTAG_PINS bit order matches the unit-test TapMaster's packing
-- ((tdo<<3)|(tdi<<2)|(tms<<1)|tck), so that BFM is reused verbatim. JTAG is a
-- passive observer: no slave/responder (the TapMaster plays both host TDI and
-- device TDO).
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

library tich;
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;
use tich.system_regbank_pkg.all;

entity tb_datalogger_gather_jtag is
  generic (
    G_SYS_CLK : natural := 100_000_000
  );
  port (
    CLK             : in  std_logic;
    RST             : in  std_logic;
    -- cocotb TapMaster bus: [0]TCK [1]TMS [2]TDI [3]TDO
    JTAG_PINS       : in  std_logic_vector(3 downto 0);
    -- Datalogger control + gather burst output
    CONFIG          : in  std_logic_vector(C_DATALOGGER_CONFIG_WIDTH-1 downto 0);
    TIMESTAMP       : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
    DOUT_PAYLOAD    : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
    DOUT_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    DOUT_MSG_TYPE   : out std_logic_vector(15 downto 0);
    DOUT_VALID      : out std_logic;
    DOUT_REQ        : in  std_logic
  );
end entity tb_datalogger_gather_jtag;

architecture str of tb_datalogger_gather_jtag is
  signal capture_pins : std_logic_vector(C_NUM_DATALOGGER_PINS-1 downto 0) := (others => '1');
begin

  -- Map TapMaster's TCK/TMS/TDI/TDO onto the JTAG0 datalogger pins; all other pins
  -- idle high so only the enabled JTAG0 instance can emit. SINGLE driver (a
  -- process) — a parallel `(others=>'1')` plus per-bit assignment would
  -- multiply-drive the JTAG pins to 'X'.
  p_pins : process(JTAG_PINS)
  begin
    capture_pins               <= (others => '1');
    capture_pins(C_JTAG0_TCK)  <= JTAG_PINS(0);
    capture_pins(C_JTAG0_TMS)  <= JTAG_PINS(1);
    capture_pins(C_JTAG0_TDI)  <= JTAG_PINS(2);
    capture_pins(C_JTAG0_TDO)  <= JTAG_PINS(3);
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
