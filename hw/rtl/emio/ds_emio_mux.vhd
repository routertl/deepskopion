-------------------------------------------------------------------------------
-- Title      : ds_emio_mux
-- Project    : deepskopion / datalogger self-validation (DS-P2.51)
-------------------------------------------------------------------------------
-- File       : ds_emio_mux.vhd
-- Author     : Daniel J. Mazure  <djmazure@routertl.com>
-- Created    : 2026-05-24
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Pure-combinational EMIO source select + bus resolution for the
--   self-validation loop. Carved out of deepskopion_top (DS-T2.11) so the
--   PS-EMIO <-> datalogger/injector loopback can be simulated standalone — the
--   inline glue in the top was untestable, and all three protocols failing
--   together on hardware pointed the finger straight here.
--
--   Two jobs:
--
--   1. BUS RESOLUTION — combine the PS master legs with the injector
--      responders into the physical bus level each protocol would see:
--        * I2C0  open-drain: SCL/SDA = '0' iff the master (or, for SDA, the
--          i2c_injector) is actively pulling low (_T='0' AND _O='0'); else
--          '1' (pulled up). SCL has no slave clock-stretch in v1.
--        * SPI0  push-pull master: SCLK/MOSI/SS come straight from the
--          master; MISO is driven by the spi_injector when it asserts
--          MISO_T='0' (CS active), else parks at '0'.
--        * UART0 has no PL responder yet, so the PS RX leg idles high.
--      The resolved *_BUS_* outputs feed BOTH the PS master `_I` legs (so
--      START/ACK/NAK/read-back timing is real) AND the injector bus inputs.
--
--   2. SOURCE SELECT — per protocol, a 2:1 mux picks what the datalogger sees:
--      the external PMOD pin (default) or the resolved EMIO bus, chosen by
--      EMIO_MUX_SEL bit 0 (I2C) / 1 (UART) / 2 (SPI). The SRC_* outputs feed
--      the top's capture_pin index assembly.
--
--   NOTE on combinational safety: I2C0_BUS_SDA depends on the injector's
--   SDA drive, and the injector's SDA_I is fed from I2C0_BUS_SDA — but the
--   injector's SDA_O/SDA_T are REGISTERED outputs (clocked i2c_slave), so
--   there is no asynchronous comb loop through this block. Same for SPI MISO.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;

entity ds_emio_mux is
  port (
    -- Source select: low 3 bits of the EMIO_MUX_SEL regbank slot.
    --   bit[0] = I2C  source (0 = PMOD, 1 = EMIO bus)
    --   bit[1] = UART source (0 = PMOD, 1 = EMIO bus)
    --   bit[2] = SPI  source (0 = PMOD, 1 = EMIO bus)
    EMIO_MUX_SEL   : in  std_logic_vector(2 downto 0);

    -- External PMOD sources (default, when the matching select bit is '0').
    PMOD_I2C_SCL   : in  std_logic;   -- ja[0]
    PMOD_I2C_SDA   : in  std_logic;   -- ja[1]
    PMOD_UART_TX   : in  std_logic;   -- jb[0]
    PMOD_SPI_SS    : in  std_logic;   -- ja[4]
    PMOD_SPI_MOSI  : in  std_logic;   -- ja[5]
    PMOD_SPI_MISO  : in  std_logic;   -- ja[6]
    PMOD_SPI_SCLK  : in  std_logic;   -- ja[7]

    -- PS EMIO master legs (from the BD's processing_system7 cell).
    UART0_TX       : in  std_logic;
    I2C0_SCL_O     : in  std_logic;
    I2C0_SCL_T     : in  std_logic;
    I2C0_SDA_O     : in  std_logic;
    I2C0_SDA_T     : in  std_logic;
    SPI0_SCLK_O    : in  std_logic;
    SPI0_MOSI_O    : in  std_logic;
    SPI0_SS_O      : in  std_logic;
    -- DS-T2.18: the EMIO-GPIO fabric chip-select (the one that actually
    -- asserts; the native SPI0_SS_O above never does — DS-T2.16). Used as the
    -- SPI datalogger's CS source so the qspi_datalogger can frame the transaction.
    SPI0_CS_GPIO   : in  std_logic;

    -- Injector responder drives (registered outputs of i2c/spi injectors).
    I2C_INJ_SDA_O  : in  std_logic;
    I2C_INJ_SDA_T  : in  std_logic;
    SPI_INJ_MISO_O : in  std_logic;
    SPI_INJ_MISO_T : in  std_logic;

    -- Resolved EMIO bus levels — feed the PS master `_I` legs AND the
    -- injector bus inputs (SCL / SDA_I / SCLK / MOSI / CS_N).
    I2C0_BUS_SCL   : out std_logic;
    I2C0_BUS_SDA   : out std_logic;
    SPI0_BUS_SCLK  : out std_logic;
    SPI0_BUS_MOSI  : out std_logic;
    SPI0_BUS_MISO  : out std_logic;
    SPI0_BUS_SS    : out std_logic;
    UART0_RX       : out std_logic;

    -- Muxed datalogger sources (PMOD vs resolved EMIO bus) → capture_pin assembly.
    SRC_I2C_SCL    : out std_logic;
    SRC_I2C_SDA    : out std_logic;
    SRC_UART_TX    : out std_logic;
    SRC_SPI_SS     : out std_logic;
    SRC_SPI_MOSI   : out std_logic;
    SRC_SPI_MISO   : out std_logic;
    SRC_SPI_SCLK   : out std_logic
  );
end entity ds_emio_mux;

architecture rtl of ds_emio_mux is

  -- Resolved bus levels (also exported on the *_BUS_* ports).
  signal i2c_bus_scl : std_logic;
  signal i2c_bus_sda : std_logic;
  signal spi_bus_sclk : std_logic;
  signal spi_bus_mosi : std_logic;
  signal spi_bus_ss   : std_logic;
  signal spi_bus_miso : std_logic;
  signal uart_rx      : std_logic;

begin

  -----------------------------------------------------------------------------
  -- 1. Bus resolution
  -----------------------------------------------------------------------------
  -- I2C0 open-drain: low iff a driver actively pulls low; else pulled up.
  i2c_bus_scl <= '0' when (I2C0_SCL_T = '0' and I2C0_SCL_O = '0')
                 else '1';
  i2c_bus_sda <= '0' when (I2C0_SDA_T   = '0' and I2C0_SDA_O   = '0')
                       or (I2C_INJ_SDA_T = '0' and I2C_INJ_SDA_O = '0')
                 else '1';

  -- SPI0 push-pull master; injector drives MISO when it asserts MISO_T='0'.
  spi_bus_sclk <= SPI0_SCLK_O;
  spi_bus_mosi <= SPI0_MOSI_O;
  spi_bus_ss   <= SPI0_SS_O;
  spi_bus_miso <= SPI_INJ_MISO_O when SPI_INJ_MISO_T = '0' else '0';

  -- UART0 PS RX idles high (no PL UART responder yet).
  uart_rx <= '1';

  I2C0_BUS_SCL  <= i2c_bus_scl;
  I2C0_BUS_SDA  <= i2c_bus_sda;
  SPI0_BUS_SCLK <= spi_bus_sclk;
  SPI0_BUS_MOSI <= spi_bus_mosi;
  SPI0_BUS_SS   <= spi_bus_ss;
  SPI0_BUS_MISO <= spi_bus_miso;
  UART0_RX      <= uart_rx;

  -----------------------------------------------------------------------------
  -- 2. Per-protocol 2:1 source select (PMOD vs EMIO bus)
  -----------------------------------------------------------------------------
  SRC_I2C_SCL  <= i2c_bus_scl  when EMIO_MUX_SEL(0) = '1' else PMOD_I2C_SCL;
  SRC_I2C_SDA  <= i2c_bus_sda  when EMIO_MUX_SEL(0) = '1' else PMOD_I2C_SDA;

  SRC_UART_TX  <= UART0_TX     when EMIO_MUX_SEL(1) = '1' else PMOD_UART_TX;

  -- DS-T2.18: the SPI datalogger's chip-select must be the EMIO-GPIO CS that
  -- actually asserts (spi0_cs_gpio_w), NOT the native spi_bus_ss/SPI0_SS_O —
  -- which never asserts at the EMIO seam (DS-T2.16). Keying the qspi_datalogger
  -- off the dead native SS captured no frames (REQ-7). SPI0_BUS_SS (the bus
  -- output above) stays the native SS as the read-in/dead-witness.
  SRC_SPI_SS   <= SPI0_CS_GPIO  when EMIO_MUX_SEL(2) = '1' else PMOD_SPI_SS;
  SRC_SPI_MOSI <= spi_bus_mosi when EMIO_MUX_SEL(2) = '1' else PMOD_SPI_MOSI;
  SRC_SPI_MISO <= spi_bus_miso when EMIO_MUX_SEL(2) = '1' else PMOD_SPI_MISO;
  SRC_SPI_SCLK <= spi_bus_sclk when EMIO_MUX_SEL(2) = '1' else PMOD_SPI_SCLK;

end architecture rtl;
