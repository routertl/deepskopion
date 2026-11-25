-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : tb_emio_loop
-- Project    : deepskopion / datalogger self-validation (DS-T2.11)
-------------------------------------------------------------------------------
-- File       : tb_emio_loop.vhd
-- Author     : Daniel J. Mazure <djmazure@routertl.com>
-- Created    : 2026-05-24
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Standalone reproduction of the EMIO self-validation loop (REQ-5 UART /
--   REQ-6 I2C / REQ-7 SPI) against the REAL fabric RTL. Instantiates exactly
--   the three blocks deepskopion_top wires into the loop —
--
--       tich.ds_emio_mux  (bus resolution + per-protocol PMOD/EMIO source mux)
--       tich.i2c_injector (regbank-fed FIFO → i2c_slave responder)
--       tich.spi_injector (regbank-fed FIFO → spi_slave responder)
--
--   — and reproduces the top's internal wiring (deepskopion_top.vhd ~700-755
--   + 1124-1166): the PS master legs and injector responder drives feed
--   ds_emio_mux; the resolved *_BUS_* levels feed BOTH the PS master `_I`
--   legs AND the injector bus inputs; the SRC_* muxed sources feed the
--   datalogger pin assembly. By carving the loop out of the top, the cocotb
--   driver can drive the PS master legs directly and observe the resolved
--   read-back, isolating the fabric loop from the PS↔fabric boundary.
--
--   Flat cocotb ports expose:
--     * the PS I2C0 master legs (I2C0_SCL_O/T, I2C0_SDA_O/T) as the cocotb
--       I2cMasterDriver's open-drain drive, plus I2C0_BUS_SDA / I2C0_BUS_SCL
--       resolved read-back (the same level the PS `_I` legs would observe);
--     * the PS SPI0 master legs (SPI0_SCLK_O/MOSI_O/SS_O) plus the resolved
--       SPI0_BUS_MISO read-back the cocotb SpiMaster samples;
--     * the PS UART0_TX leg plus the muxed SRC_UART_TX datalogger source;
--     * each injector's CONTROL / DATAIN / DATAIN_VLD / STATUS plane so the
--       cocotb test preloads the FIFO responses (mirrors regs_out_local
--       delivery in the top).
--
--   The bus resolution is performed inside the REAL ds_emio_mux — not the
--   simplified inline model of tb_i2c_injector / tb_spi_injector — so any
--   fabric defect in the mux's open-drain / push-pull resolution or the
--   2:1 source select is exercised here.
--
--   NOTE on combinational safety: I2C0_BUS_SDA depends on the i2c_injector's
--   SDA drive and the injector's SDA_I is fed from I2C0_BUS_SDA, but the
--   injector's SDA_O/SDA_T are REGISTERED (clocked i2c_slave), so no async
--   comb loop exists. Same for the SPI MISO path. This matches the safety
--   argument in the ds_emio_mux header.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_pkg.all;

entity tb_emio_loop is
  generic (
    G_FIFO_DEPTH : integer := 4
  );
  port (
    CLK         : in  std_logic;
    RST         : in  std_logic;

    -- Timestamp slice fed to the i2c_injector (SCL-stuck / glitch detector).
    TIMESTAMP   : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);

    -- EMIO source-select (low 3 bits of the regbank slot):
    --   bit[0]=I2C, bit[1]=UART, bit[2]=SPI  (0=PMOD, 1=EMIO bus).
    EMIO_MUX_SEL : in  std_logic_vector(2 downto 0);

    -- ── PS I2C0 master legs (open-drain). _O='0' & _T='0' pulls low. ──────
    I2C0_SCL_O   : in  std_logic;
    I2C0_SCL_T   : in  std_logic;
    I2C0_SDA_O   : in  std_logic;
    I2C0_SDA_T   : in  std_logic;
    -- Resolved I2C bus read-back (= the level the PS `_I` legs would see).
    I2C0_BUS_SCL : out std_logic;
    I2C0_BUS_SDA : out std_logic;

    -- ── PS SPI0 master legs (push-pull). ─────────────────────────────────
    SPI0_SCLK_O  : in  std_logic;
    SPI0_MOSI_O  : in  std_logic;
    SPI0_SS_O    : in  std_logic;   -- native PS SPI SS — DEAD at the PL seam (Haz.62), kept idle
    -- DS-T2.29 / Haz.62: chip-select is driven as a MANUAL EMIO GPIO (the Zynq-7
    -- native EMIO SS never reaches the PL seam). deepskopion_top wires spi0_cs_gpio_r
    -- here; the cocotb SpiMaster drives its CS onto THIS leg, not SPI0_SS_O.
    SPI0_CS_GPIO : in  std_logic;
    -- Resolved SPI MISO read-back (driven by the spi_injector when CS active).
    SPI0_BUS_MISO : out std_logic;

    -- ── PS UART0 TX leg + muxed datalogger source. ──────────────────────────
    UART0_TX     : in  std_logic;
    PMOD_UART_TX : in  std_logic;
    SRC_UART_TX  : out std_logic;

    -- ── i2c_injector control plane (regbank slots). ──────────────────────
    I2C_CONTROL    : in  std_logic_vector(31 downto 0);
    I2C_DATAIN     : in  std_logic_vector(31 downto 0);
    I2C_DATAIN_VLD : in  std_logic;
    I2C_STATUS     : out std_logic_vector(31 downto 0);

    -- ── spi_injector control plane (regbank slots). ──────────────────────
    SPI_CONTROL    : in  std_logic_vector(31 downto 0);
    SPI_DATAIN     : in  std_logic_vector(31 downto 0);
    SPI_DATAIN_VLD : in  std_logic;
    SPI_STATUS     : out std_logic_vector(31 downto 0)
  );
end entity tb_emio_loop;

architecture str of tb_emio_loop is

  -- Resolved EMIO bus levels (ds_emio_mux outputs).
  signal i2c0_bus_scl_w  : std_logic;
  signal i2c0_bus_sda_w  : std_logic;
  signal spi0_bus_sclk_w : std_logic;
  signal spi0_bus_mosi_w : std_logic;
  signal spi0_bus_ss_w   : std_logic;
  signal spi0_bus_miso_w : std_logic;
  signal uart0_rx_w      : std_logic;

  -- Injector responder drives (registered outputs).
  signal i2c_inj_sda_o_w  : std_logic;
  signal i2c_inj_sda_t_w  : std_logic;
  signal spi_inj_miso_o_w : std_logic;
  signal spi_inj_miso_t_w : std_logic;

  -- Muxed datalogger sources (only UART is probed in this cut; the rest are
  -- left open — they exercise the same 2:1 select logic but aren't needed
  -- for the loop read-back asserts).
  signal src_i2c_scl_w  : std_logic;
  signal src_i2c_sda_w  : std_logic;
  signal src_spi_ss_w   : std_logic;
  signal src_spi_mosi_w : std_logic;
  signal src_spi_miso_w : std_logic;
  signal src_spi_sclk_w : std_logic;

begin

  ---------------------------------------------------------------------------
  -- EMIO bus resolution + per-protocol source select — the REAL fabric block.
  -- Wired exactly as deepskopion_top u_emio_mux. PMOD I2C/SPI sources are
  -- tied to idle ('1' open-drain release for I2C, '1' for SPI) since this
  -- bench drives the EMIO side; PMOD_UART_TX is exposed for the REQ-5 bit1=0
  -- branch test.
  ---------------------------------------------------------------------------
  u_emio_mux : entity tich.ds_emio_mux
    port map (
      EMIO_MUX_SEL   => EMIO_MUX_SEL,
      -- External PMOD sources (idle except UART, which is exposed).
      PMOD_I2C_SCL   => '1',
      PMOD_I2C_SDA   => '1',
      PMOD_UART_TX   => PMOD_UART_TX,
      PMOD_SPI_SS    => '1',
      PMOD_SPI_MOSI  => '1',
      PMOD_SPI_MISO  => '1',
      PMOD_SPI_SCLK  => '1',
      -- PS EMIO master legs (driven by the cocotb master drivers).
      UART0_TX       => UART0_TX,
      I2C0_SCL_O     => I2C0_SCL_O,
      I2C0_SCL_T     => I2C0_SCL_T,
      I2C0_SDA_O     => I2C0_SDA_O,
      I2C0_SDA_T     => I2C0_SDA_T,
      SPI0_SCLK_O    => SPI0_SCLK_O,
      SPI0_MOSI_O    => SPI0_MOSI_O,
      SPI0_SS_O      => SPI0_SS_O,
      SPI0_CS_GPIO   => SPI0_CS_GPIO,   -- DS-T2.29: manual GPIO chip-select (Haz.62 mitigation)
      -- Injector responder drives.
      I2C_INJ_SDA_O  => i2c_inj_sda_o_w,
      I2C_INJ_SDA_T  => i2c_inj_sda_t_w,
      SPI_INJ_MISO_O => spi_inj_miso_o_w,
      SPI_INJ_MISO_T => spi_inj_miso_t_w,
      -- Resolved EMIO bus levels.
      I2C0_BUS_SCL   => i2c0_bus_scl_w,
      I2C0_BUS_SDA   => i2c0_bus_sda_w,
      SPI0_BUS_SCLK  => spi0_bus_sclk_w,
      SPI0_BUS_MOSI  => spi0_bus_mosi_w,
      SPI0_BUS_MISO  => spi0_bus_miso_w,
      SPI0_BUS_SS    => spi0_bus_ss_w,
      UART0_RX       => uart0_rx_w,
      -- Muxed datalogger sources.
      SRC_I2C_SCL    => src_i2c_scl_w,
      SRC_I2C_SDA    => src_i2c_sda_w,
      SRC_UART_TX    => SRC_UART_TX,
      SRC_SPI_SS     => src_spi_ss_w,
      SRC_SPI_MOSI   => src_spi_mosi_w,
      SRC_SPI_MISO   => src_spi_miso_w,
      SRC_SPI_SCLK   => src_spi_sclk_w
    );

  ---------------------------------------------------------------------------
  -- i2c_injector — fed by the resolved I2C bus (deepskopion_top u_i2c_injector).
  ---------------------------------------------------------------------------
  u_i2c_injector : entity tich.i2c_injector
    generic map (
      G_FIFO_DEPTH => G_FIFO_DEPTH,
      G_FIFO_FWFT  => 1
    )
    port map (
      CLK        => CLK,
      RST        => RST,
      TIMESTAMP  => TIMESTAMP,
      ENABLE     => '1',
      CONTROL    => I2C_CONTROL,
      DATAIN     => I2C_DATAIN,
      DATAIN_VLD => I2C_DATAIN_VLD,
      STATUS     => I2C_STATUS,
      SCL        => i2c0_bus_scl_w,
      SDA_I      => i2c0_bus_sda_w,
      SDA_O      => i2c_inj_sda_o_w,
      SDA_T      => i2c_inj_sda_t_w
    );

  ---------------------------------------------------------------------------
  -- spi_injector — fed by the resolved SPI bus (deepskopion_top u_spi_injector).
  ---------------------------------------------------------------------------
  u_spi_injector : entity tich.spi_injector
    generic map (
      G_FIFO_DEPTH => G_FIFO_DEPTH,
      G_FIFO_FWFT  => 1
    )
    port map (
      CLK        => CLK,
      RST        => RST,
      ENABLE     => '1',
      CONTROL    => SPI_CONTROL,
      DATAIN     => SPI_DATAIN,
      DATAIN_VLD => SPI_DATAIN_VLD,
      STATUS     => SPI_STATUS,
      SCLK       => spi0_bus_sclk_w,
      MOSI       => spi0_bus_mosi_w,
      -- DS-T2.29 / DS-T2.16: CS_N is the EMIO-GPIO chip-select, NOT the dead
      -- native SS (spi0_bus_ss_w never asserts at the seam, Haz.62). Matches
      -- deepskopion_top u_spi_injector CS_N => spi0_cs_gpio_w exactly.
      CS_N       => SPI0_CS_GPIO,
      MISO_O     => spi_inj_miso_o_w,
      MISO_T     => spi_inj_miso_t_w
    );

  ---------------------------------------------------------------------------
  -- Resolved bus read-back to the cocotb master drivers (= PS `_I` legs).
  ---------------------------------------------------------------------------
  I2C0_BUS_SCL  <= i2c0_bus_scl_w;
  I2C0_BUS_SDA  <= i2c0_bus_sda_w;
  SPI0_BUS_MISO <= spi0_bus_miso_w;

end architecture str;
