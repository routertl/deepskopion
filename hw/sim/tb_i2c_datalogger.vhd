-------------------------------------------------------------------------------
-- Title      : tb_i2c_datalogger
-- Project    : deepskopion
-------------------------------------------------------------------------------
-- File       : tb_i2c_datalogger.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    :
-- Created    : 2026-02-02
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: Wrapper TB for i2c_datalogger and i2c_slave
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_pkg.all;

entity tb_i2c_datalogger is
    generic (
      G_I2C_ID_WIDTH   : integer                                  := 5;
      G_I2C_DEV_ID     : natural range 0 to (2**G_I2C_ID_WIDTH)-1 := 28;
      G_CONFIG_WIDTH    : integer                                 := 16;
      G_ERROR_WIDTH     : integer                                 := 2;
      G_ILA_EN          : integer                                 := 1
      );
    port (
      CLK         : in  std_logic;
      RST         : in  std_logic;
      -- Master Interface
      M_SCL       : in  std_logic;
      M_SDA_OUT   : in  std_logic;
      M_SDA_IN    : out std_logic;
      -- Datalogger Control/Status
      CONFIG      : in  std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
      TIMESTAMP   : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
      -- Datalogger Output
      I2C_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
      I2C_MSG_VLD : out std_logic;
      I2C_MSG_RDY : in  std_logic;
      -- Slave Control
      I2C_SLAVE_ADDR : in std_logic_vector(6 downto 0)
      );
end tb_i2c_datalogger;

architecture str of tb_i2c_datalogger is

    signal sda_slave_o : std_logic;
    signal sda_slave_t : std_logic;
    signal sda_resolved : std_logic;
    signal i2c_pins    : std_logic_vector(1 downto 0);
    signal i2c_ctrl    : std_logic_vector(15 downto 0);

begin

    -- Resolved SDA (Master and Slave combined)
    -- Slave drives low if T=0 and O=0.
    sda_resolved <= '0' when (sda_slave_t = '0' and sda_slave_o = '0') or M_SDA_OUT = '0' else '1';
    
    -- Master Feedback
    M_SDA_IN <= sda_resolved;

    -- Datalogger Pins Input
    i2c_pins(0) <= M_SCL;
    i2c_pins(1) <= sda_resolved;

    -- Datalogger Instance
    i_datalogger : entity tich.i2c_datalogger
        generic map (
            G_I2C_ID_WIDTH    => G_I2C_ID_WIDTH,
            G_I2C_DEV_ID      => G_I2C_DEV_ID,
            G_CONFIG_WIDTH    => G_CONFIG_WIDTH,
            G_ERROR_WIDTH     => G_ERROR_WIDTH,
            G_ILA_EN          => G_ILA_EN
        )
        port map (
            CLK         => CLK,
            RST         => RST,
            I2C_PINS    => i2c_pins,
            CONFIG      => CONFIG,
            TIMESTAMP   => TIMESTAMP,
            I2C_MSG_OUT => I2C_MSG_OUT,
            I2C_MSG_VLD => I2C_MSG_VLD,
            I2C_MSG_RDY => I2C_MSG_RDY,
            I2C_ERRORS  => open
        );

    -- Slave Control
    -- Bit 0: Enable, Bits 7:1: Address
    i2c_ctrl <= "00000000" & I2C_SLAVE_ADDR & '1';

    -- Slave Instance
    i_slave : entity tich.i2c_slave
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

end str;
