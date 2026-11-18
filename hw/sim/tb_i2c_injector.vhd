-------------------------------------------------------------------------------
-- Title      : tb_i2c_injector
-- Project    : deepskopion (DS-P3.43 — DS-P2.51 cocotb regression)
-------------------------------------------------------------------------------
-- File       : tb_i2c_injector.vhd
-- Author     : Daniel J. Mazure <djmazure@routertl.com>
-- Created    : 2026-05-22
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Wrapper that exposes flat master-side I2C pins (M_SCL / M_SDA_OUT /
--   M_SDA_IN) for a cocotb master driver and instantiates i2c_injector.
--   The bus model resolves master + slave open-drain pulls onto a single
--   sda_resolved wire that both sides observe via their _I inputs.
--
--   Mirrors the bus model embedded in deepskopion_top so the cocotb test
--   exercises exactly the resolution the synthesised design uses.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_pkg.all;

entity tb_i2c_injector is
  generic (
    G_FIFO_DEPTH  : integer := 4;
    -- Scaled DOWN for sim so the inactivity-watchdog timeout is reachable in a
    -- reasonable number of cycles (real silicon uses 100 MHz → 25 ms). At
    -- 100 kHz the timeout is (100000/1000)*25 = 2500 CLK cycles — well above the
    -- ~125-cycle SCL half-period so healthy reads never trip it.
    G_CLK_FREQ_HZ : natural := 100_000
  );
  port (
    CLK         : in  std_logic;
    RST         : in  std_logic;

    -- Master-side flat pins for the cocotb driver. M_SDA_OUT='0' means
    -- the master pulls SDA low; M_SDA_OUT='1' means the master releases
    -- SDA (Hi-Z). M_SDA_IN reflects the resolved bus level.
    M_SCL       : in  std_logic;
    M_SDA_OUT   : in  std_logic;
    M_SDA_IN    : out std_logic;

    -- Timestamp slice fed straight through to the slave (drives the SCL
    -- timeout / glitch detector). Defaults to 0 so existing tests that don't
    -- drive it keep the old behaviour (timeout never fires); the DS-T2.21
    -- wedge-recovery test toggles bit 9 (C_BIT_TIMESYNC-1) to fire the timeout.
    TIMESTAMP   : in  std_logic_vector(31 downto 0) := (others => '0');

    -- Regbank-side control plane.
    ENABLE      : in  std_logic;
    CONTROL     : in  std_logic_vector(31 downto 0);
    DATAIN      : in  std_logic_vector(31 downto 0);
    DATAIN_VLD  : in  std_logic;
    STATUS      : out std_logic_vector(31 downto 0);

    -- Resolved bus exposed for direct probing.
    BUS_SDA     : out std_logic;
    BUS_SCL     : out std_logic
  );
end entity tb_i2c_injector;

architecture str of tb_i2c_injector is

  signal sda_slave_o  : std_logic;
  signal sda_slave_t  : std_logic;
  signal sda_resolved : std_logic;

begin

  ---------------------------------------------------------------------------
  -- Open-drain bus model. SDA is pulled low if either master or slave
  -- asserts; released to '1' (pull-up) otherwise. SCL is master-only —
  -- the slave doesn't drive clock stretch in v1.
  ---------------------------------------------------------------------------
  sda_resolved <= '0' when (sda_slave_t = '0' and sda_slave_o = '0')
                        or  M_SDA_OUT = '0'
                  else '1';
  M_SDA_IN <= sda_resolved;
  BUS_SDA  <= sda_resolved;
  BUS_SCL  <= M_SCL;

  ---------------------------------------------------------------------------
  -- Injector instance.
  ---------------------------------------------------------------------------
  i_injector : entity tich.i2c_injector
    generic map (
      G_FIFO_DEPTH  => G_FIFO_DEPTH,
      G_FIFO_FWFT   => 1,
      G_CLK_FREQ_HZ => G_CLK_FREQ_HZ
    )
    port map (
      CLK        => CLK,
      RST        => RST,
      TIMESTAMP  => TIMESTAMP,
      ENABLE     => ENABLE,
      CONTROL    => CONTROL,
      DATAIN     => DATAIN,
      DATAIN_VLD => DATAIN_VLD,
      STATUS     => STATUS,
      SCL        => M_SCL,
      SDA_I      => sda_resolved,
      SDA_O      => sda_slave_o,
      SDA_T      => sda_slave_t
    );

end architecture str;
