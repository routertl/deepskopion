-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : tb_spi_injector
-- Project    : deepskopion (DS-P3.46 — DS-P2.51 cocotb regression, SPI side)
-------------------------------------------------------------------------------
-- File       : tb_spi_injector.vhd
-- Author     : Daniel J. Mazure <djmazure@routertl.com>
-- Created    : 2026-05-22
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Thin wrapper that exposes flat master-side SPI pins (M_SCLK / M_MOSI
--   / M_CS_N / M_MISO) and instantiates spi_injector. SPI is push-pull,
--   so no open-drain bus resolution is needed — we just collapse the
--   slave's (MISO_O, MISO_T) pair into a single M_MISO line the cocotb
--   master can sample directly:
--
--     M_MISO <= MISO_O when MISO_T = '0' else '0'
--
--   When CS_N='1' (idle) the slave drives MISO_T='1' (Hi-Z) and the
--   wrapper presents M_MISO='0' — which matches the deepskopion_top
--   bus model (Phase 1 'no echo slave' tie + the Phase 2 contribution
--   from the slave when CS asserted).
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;

entity tb_spi_injector is
  generic (
    G_FIFO_DEPTH : integer := 4
  );
  port (
    CLK         : in  std_logic;
    RST         : in  std_logic;

    -- Master-side flat pins for the cocotb driver.
    M_SCLK      : in  std_logic;
    M_MOSI      : in  std_logic;
    M_CS_N      : in  std_logic;
    M_MISO      : out std_logic;

    -- Regbank-side control plane.
    ENABLE      : in  std_logic;
    CONTROL     : in  std_logic_vector(31 downto 0);
    DATAIN      : in  std_logic_vector(31 downto 0);
    DATAIN_VLD  : in  std_logic;
    STATUS      : out std_logic_vector(31 downto 0)
  );
end entity tb_spi_injector;

architecture str of tb_spi_injector is

  signal miso_o_w : std_logic;
  signal miso_t_w : std_logic;

begin

  ---------------------------------------------------------------------------
  -- Push-pull MISO resolution. Slave drives MISO when CS_N='0' (T='0');
  -- otherwise the line is released and we pin it to '0' so cocotb sees
  -- deterministic Hi-Z behaviour.
  ---------------------------------------------------------------------------
  M_MISO <= miso_o_w when miso_t_w = '0' else '0';

  ---------------------------------------------------------------------------
  -- Injector instance.
  ---------------------------------------------------------------------------
  i_injector : entity tich.spi_injector
    generic map (
      G_FIFO_DEPTH => G_FIFO_DEPTH,
      G_FIFO_FWFT  => 1
    )
    port map (
      CLK        => CLK,
      RST        => RST,
      ENABLE     => ENABLE,
      CONTROL    => CONTROL,
      DATAIN     => DATAIN,
      DATAIN_VLD => DATAIN_VLD,
      STATUS     => STATUS,
      SCLK       => M_SCLK,
      MOSI       => M_MOSI,
      CS_N       => M_CS_N,
      MISO_O     => miso_o_w,
      MISO_T     => miso_t_w
    );

end architecture str;
