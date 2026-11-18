-------------------------------------------------------------------------------
-- tb_datalogger_gather_i2c.vhd  —  DS-P3.32 protocol-level I2C harness
-------------------------------------------------------------------------------
-- Drives REAL I2C bus traffic through the full datalogger_gather aggregator so the
-- adversarial cocotb suite can attack SGA-REQ-3 (single-source routing) at the
-- protocol layer, not with synthetic edges. Wiring mirrors tb_i2c_datalogger.vhd
-- but the datalogger-under-test is the whole gather:
--
--   cocotb I2C master ── M_SCL / M_SDA_OUT ──┐
--                                            ├─ open-drain SDA resolution
--   tich.i2c_slave (BFM, ACKs @ addr) ───────┘   (slave OR master pulls low)
--                         │
--   datalogger_gather.CAPTURE_PINS[12]=SCL, [13]=SDA (the I2C0 pin map); every other
--   pin idles '1' so only the (CONFIG-enabled) I2C0 datalogger produces messages.
--   The gather demuxes the I2C frames onto DOUT_PAYLOAD / DOUT_DATALOGGER_ID(=4).
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

library tich;
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;
use tich.system_regbank_pkg.all;

entity tb_datalogger_gather_i2c is
  generic (
    G_SYS_CLK : natural := 100_000_000
  );
  port (
    CLK             : in  std_logic;
    RST             : in  std_logic;
    -- cocotb I2C master interface (open-drain: '1' = released, '0' = pulled low)
    M_SCL           : in  std_logic;
    M_SDA_OUT       : in  std_logic;
    M_SDA_IN        : out std_logic;
    I2C_SLAVE_ADDR  : in  std_logic_vector(6 downto 0);
    -- Datalogger control + gather burst output
    CONFIG          : in  std_logic_vector(C_DATALOGGER_CONFIG_WIDTH-1 downto 0);
    TIMESTAMP       : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
    DOUT_PAYLOAD    : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
    DOUT_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    DOUT_MSG_TYPE   : out std_logic_vector(15 downto 0);
    DOUT_VALID      : out std_logic;
    DOUT_REQ        : in  std_logic
  );
end entity tb_datalogger_gather_i2c;

architecture str of tb_datalogger_gather_i2c is
  signal sda_slave_o : std_logic;
  signal sda_slave_t : std_logic;
  signal sda_resolved : std_logic;
  signal capture_pins  : std_logic_vector(C_NUM_DATALOGGER_PINS-1 downto 0) := (others => '1');
  signal i2c_ctrl    : std_logic_vector(15 downto 0);
begin

  -- Open-drain SDA: low when the slave (T=0,O=0) OR the master pulls it.
  sda_resolved <= '0' when (sda_slave_t = '0' and sda_slave_o = '0') or M_SDA_OUT = '0'
                  else '1';
  M_SDA_IN <= sda_resolved;

  -- Map SCL/SDA onto the I2C0 datalogger's pins; all other pins idle high so only
  -- the enabled I2C0 instance can emit. SINGLE driver (a process) — a parallel
  -- `(others=>'1')` plus a per-bit assignment would multiply-drive the I2C pins
  -- to 'X'.
  p_pins : process(M_SCL, sda_resolved)
  begin
    capture_pins              <= (others => '1');
    capture_pins(C_I2C0_SCL)  <= M_SCL;
    capture_pins(C_I2C0_SDA)  <= sda_resolved;
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

  -- I2C slave BFM — bit0 enable, bits[7:1] address.
  i2c_ctrl <= "00000000" & I2C_SLAVE_ADDR & '1';

  u_slave : entity tich.i2c_slave
    generic map (
      G_CTRL_WIDTH  => 16,
      G_ERROR_WIDTH => 4,
      G_ILA_EN      => 0
    )
    port map (
      CLK         => CLK,
      RST         => RST,
      SCL         => M_SCL,
      SDA_I       => sda_resolved,
      SDA_O       => sda_slave_o,
      SDA_T       => sda_slave_t,
      TIMESTAMP   => (others => '0'),
      I2C_CTRL    => i2c_ctrl,
      I2C_TX_DIN  => (others => '0'),
      I2C_TX_VLD  => '0',
      I2C_TX_RDY  => open,
      I2C_RX_DOUT => open,
      I2C_RX_VLD  => open,
      I2C_RX_RDY  => '1',
      I2C_ERRORS  => open
    );

end architecture str;
