-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- tb_datalogger_gather_i2c_dual.vhd  —  DS-P2.107 two-independent-I2C-bus harness
-------------------------------------------------------------------------------
-- Drives TWO real, independent I2C buses simultaneously through the full
-- datalogger_gather aggregator, to prove the DE25 stereo-vision capture use-case:
-- the Zybo Z7-20 (VmodCAM) configures cam A + cam B over two separate I2C
-- buses, and the DE25 deepskopion datalogger must decode BOTH, byte-exact, with
-- NO cross-bleed. Channel map (matches DE25_BOARD_FACTS.md):
--
--   cam B  -> I2C0 datalogger, device_id 4, CAPTURE_PINS[12]=SCL [13]=SDA (ja[0]/ja[1])
--   cam A  -> I2C1 datalogger, device_id 5, CAPTURE_PINS[14]=SCL [15]=SDA (ja[2]/ja[3])
--
-- Doubled wiring of tb_datalogger_gather_i2c.vhd: each bus gets its own cocotb
-- master (open-drain via M?_SDA_OUT/IN) and a tich.i2c_slave BFM that ACKs at
-- its own address. G_SYS_CLK defaults to 50 MHz so this ALSO exercises the
-- DE25 timebase (clk_sys = CLOCK2_50) and the DS-T1.10 G_SYS_CLK_HZ fix
-- behaviourally, not just structurally.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

library tich;
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;
use tich.system_regbank_pkg.all;

entity tb_datalogger_gather_i2c_dual is
  generic (
    G_SYS_CLK : natural := 50_000_000   -- DE25 clk_sys (CLOCK2_50); DS-T1.10 timebase
  );
  port (
    CLK             : in  std_logic;
    RST             : in  std_logic;
    -- bus 0 (I2C0, device_id 4 = cam B) cocotb master
    M0_SCL          : in  std_logic;
    M0_SDA_OUT      : in  std_logic;
    M0_SDA_IN       : out std_logic;
    I2C0_SLAVE_ADDR : in  std_logic_vector(6 downto 0);
    -- bus 1 (I2C1, device_id 5 = cam A) cocotb master
    M1_SCL          : in  std_logic;
    M1_SDA_OUT      : in  std_logic;
    M1_SDA_IN       : out std_logic;
    I2C1_SLAVE_ADDR : in  std_logic_vector(6 downto 0);
    -- Datalogger control + gather burst output
    CONFIG          : in  std_logic_vector(C_DATALOGGER_CONFIG_WIDTH-1 downto 0);
    TIMESTAMP       : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
    DOUT_PAYLOAD    : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
    DOUT_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    DOUT_MSG_TYPE   : out std_logic_vector(15 downto 0);
    DOUT_VALID      : out std_logic;
    DOUT_REQ        : in  std_logic
  );
end entity tb_datalogger_gather_i2c_dual;

architecture str of tb_datalogger_gather_i2c_dual is
  signal sda0_slave_o, sda0_slave_t, sda0_resolved : std_logic;
  signal sda1_slave_o, sda1_slave_t, sda1_resolved : std_logic;
  signal capture_pins  : std_logic_vector(C_NUM_DATALOGGER_PINS-1 downto 0) := (others => '1');
  signal i2c0_ctrl, i2c1_ctrl : std_logic_vector(15 downto 0);
begin

  -- Open-drain SDA per bus: low when that bus's slave (T=0,O=0) OR master pulls it.
  sda0_resolved <= '0' when (sda0_slave_t = '0' and sda0_slave_o = '0') or M0_SDA_OUT = '0'
                   else '1';
  M0_SDA_IN <= sda0_resolved;

  sda1_resolved <= '0' when (sda1_slave_t = '0' and sda1_slave_o = '0') or M1_SDA_OUT = '0'
                   else '1';
  M1_SDA_IN <= sda1_resolved;

  -- Single driver process: all pins idle high except the two I2C buses' pins.
  p_pins : process(M0_SCL, sda0_resolved, M1_SCL, sda1_resolved)
  begin
    capture_pins              <= (others => '1');
    capture_pins(C_I2C0_SCL)  <= M0_SCL;
    capture_pins(C_I2C0_SDA)  <= sda0_resolved;
    capture_pins(C_I2C1_SCL)  <= M1_SCL;
    capture_pins(C_I2C1_SDA)  <= sda1_resolved;
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

  -- I2C slave BFMs — bit0 enable, bits[7:1] address; one per bus.
  i2c0_ctrl <= "00000000" & I2C0_SLAVE_ADDR & '1';
  i2c1_ctrl <= "00000000" & I2C1_SLAVE_ADDR & '1';

  u_slave0 : entity tich.i2c_slave
    generic map ( G_CTRL_WIDTH => 16, G_ERROR_WIDTH => 4, G_ILA_EN => 0 )
    port map (
      CLK => CLK, RST => RST,
      SCL => M0_SCL, SDA_I => sda0_resolved,
      SDA_O => sda0_slave_o, SDA_T => sda0_slave_t,
      TIMESTAMP => (others => '0'), I2C_CTRL => i2c0_ctrl,
      I2C_TX_DIN => (others => '0'), I2C_TX_VLD => '0', I2C_TX_RDY => open,
      I2C_RX_DOUT => open, I2C_RX_VLD => open, I2C_RX_RDY => '1', I2C_ERRORS => open
    );

  u_slave1 : entity tich.i2c_slave
    generic map ( G_CTRL_WIDTH => 16, G_ERROR_WIDTH => 4, G_ILA_EN => 0 )
    port map (
      CLK => CLK, RST => RST,
      SCL => M1_SCL, SDA_I => sda1_resolved,
      SDA_O => sda1_slave_o, SDA_T => sda1_slave_t,
      TIMESTAMP => (others => '0'), I2C_CTRL => i2c1_ctrl,
      I2C_TX_DIN => (others => '0'), I2C_TX_VLD => '0', I2C_TX_RDY => open,
      I2C_RX_DOUT => open, I2C_RX_VLD => open, I2C_RX_RDY => '1', I2C_ERRORS => open
    );

end architecture str;
