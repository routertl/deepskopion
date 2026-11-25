-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : tb_stimulus_gen_loop
-- Project    : deepskopion / datalogger self-validation (DS-P2.100, DS-D.38 option a)
-------------------------------------------------------------------------------
-- File       : tb_stimulus_gen_loop.vhd
-- Author     : Daniel J. Mazure  <djmazure@routertl.com>
-- Created    : 2026-08-25
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Standalone reproduction of the DE25 fabric-side stimulus loop, mirroring
--   tb_emio_loop.vhd's shape but with ds_stimulus_gen in place of a cocotb
--   PS-EMIO master. Instantiates exactly what deepskopion_de25_top.vhd wires
--   after DS-P2.100:
--
--       tich.ds_stimulus_gen  (UART TX / I2C master / SPI master / edge src)
--       tich.ds_emio_mux      (bus resolution + per-protocol PMOD/EMIO mux)
--       tich.i2c_injector     (regbank-fed FIFO -> i2c_slave responder)
--       tich.spi_injector     (regbank-fed FIFO -> spi_slave responder)
--
--   The generator's stim_*_o legs feed ds_emio_mux's PS-master-leg inputs —
--   the exact loopback deepskopion_de25_top.vhd wires — so any defect in
--   the generator's protocol timing or in its interaction with the real
--   fabric mux/responders is exercised here, standalone.
--
--   Also reproduces the deepskopion_core EDGE0 self-test tap (EMIO_MUX_SEL
--   bit[7], core:819/846-852 after DS-P2.100): CAPTURE_EDGE0 = STIM_EDGE_O
--   when STIM_EDGE_SEL='1' else PMOD_EDGE_FALLBACK, so the self-test path
--   can be proven live without pulling in the whole core/top.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;

library tich;
use tich.datalogger_pkg.all;

entity tb_stimulus_gen_loop is
  generic (
    G_FIFO_DEPTH : integer := 4
  );
  port (
    CLK : in  std_logic;
    RST : in  std_logic;

    TIMESTAMP : in std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);

    -- EMIO source-select, same bit layout as the regbank EMIO_MUX_SEL slot.
    EMIO_MUX_SEL : in std_logic_vector(2 downto 0);

    -- ── ds_stimulus_gen control plane (regbank slots, flat here) ─────────
    STIM_UART_CONTROL  : in  std_logic_vector(31 downto 0);
    STIM_UART_BAUD_DIV : in  std_logic_vector(31 downto 0);
    STIM_UART_DATA     : in  std_logic_vector(31 downto 0);
    STIM_UART_DATA_VLD : in  std_logic;

    STIM_I2C_CONTROL  : in  std_logic_vector(31 downto 0);
    STIM_I2C_CLK_DIV  : in  std_logic_vector(31 downto 0);
    STIM_I2C_DATA     : in  std_logic_vector(31 downto 0);
    STIM_I2C_DATA_VLD : in  std_logic;

    STIM_SPI_CONTROL  : in  std_logic_vector(31 downto 0);
    STIM_SPI_CLK_DIV  : in  std_logic_vector(31 downto 0);
    STIM_SPI_DATA     : in  std_logic_vector(31 downto 0);
    STIM_SPI_DATA_VLD : in  std_logic;

    STIM_EDGE_CONTROL : in  std_logic_vector(31 downto 0);
    STIM_EDGE_SEL     : in  std_logic;  -- EMIO_MUX_SEL bit[7] equivalent
    PMOD_EDGE_FALLBACK: in  std_logic;  -- jb(2) equivalent

    -- ── Observation: resolved bus + muxed datalogger sources ────────────────
    I2C0_BUS_SCL  : out std_logic;
    I2C0_BUS_SDA  : out std_logic;
    SPI0_BUS_SCLK : out std_logic;
    SPI0_BUS_MOSI : out std_logic;
    SPI0_BUS_MISO : out std_logic;
    SPI0_CS_GPIO  : out std_logic;  -- = stim SS (drives both native SS + GPIO CS, DS-T2.16/18)
    SRC_UART_TX   : out std_logic;
    CAPTURE_EDGE0   : out std_logic;

    -- ── i2c_injector / spi_injector control + observation plane ──────────
    I2C_INJ_CONTROL    : in  std_logic_vector(31 downto 0);
    I2C_INJ_DATAIN     : in  std_logic_vector(31 downto 0);
    I2C_INJ_DATAIN_VLD : in  std_logic;
    I2C_INJ_STATUS     : out std_logic_vector(31 downto 0);

    SPI_INJ_CONTROL    : in  std_logic_vector(31 downto 0);
    SPI_INJ_DATAIN     : in  std_logic_vector(31 downto 0);
    SPI_INJ_DATAIN_VLD : in  std_logic;
    SPI_INJ_STATUS     : out std_logic_vector(31 downto 0)
  );
end entity tb_stimulus_gen_loop;

architecture str of tb_stimulus_gen_loop is

  -- ds_stimulus_gen -> ds_emio_mux PS-master-leg loopback.
  signal stim_uart_tx_w : std_logic;
  signal stim_i2c_scl_o_w, stim_i2c_scl_t_w : std_logic;
  signal stim_i2c_sda_o_w, stim_i2c_sda_t_w : std_logic;
  signal stim_spi_sclk_w : std_logic;
  signal stim_spi_mosi_w : std_logic;
  signal stim_spi_ss_w   : std_logic;
  signal stim_edge_w     : std_logic;

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

  signal src_i2c_scl_w  : std_logic;
  signal src_i2c_sda_w  : std_logic;
  signal src_spi_ss_w   : std_logic;
  signal src_spi_mosi_w : std_logic;
  signal src_spi_miso_w : std_logic;
  signal src_spi_sclk_w : std_logic;

  signal i2c_slave_dbg_w  : std_logic_vector(19 downto 0);
  signal i2c_dbg_fifo_w   : std_logic_vector(15 downto 0);
  signal i2c_dbg_shift_w  : std_logic_vector(7 downto 0);
  signal i2c_dbg2_w       : std_logic_vector(7 downto 0);

begin

  ---------------------------------------------------------------------------
  -- ds_stimulus_gen — the block under test (DS-P2.100).
  ---------------------------------------------------------------------------
  u_stim_gen : entity tich.ds_stimulus_gen
    port map (
      CLK           => CLK,
      RST           => RST,
      UART_CONTROL  => STIM_UART_CONTROL,
      UART_BAUD_DIV => STIM_UART_BAUD_DIV,
      UART_DATA     => STIM_UART_DATA,
      UART_DATA_VLD => STIM_UART_DATA_VLD,
      UART_TX_O     => stim_uart_tx_w,
      I2C_CONTROL   => STIM_I2C_CONTROL,
      I2C_CLK_DIV   => STIM_I2C_CLK_DIV,
      I2C_DATA      => STIM_I2C_DATA,
      I2C_DATA_VLD  => STIM_I2C_DATA_VLD,
      I2C_SCL_O     => stim_i2c_scl_o_w,
      I2C_SCL_T     => stim_i2c_scl_t_w,
      I2C_SDA_O     => stim_i2c_sda_o_w,
      I2C_SDA_T     => stim_i2c_sda_t_w,
      SPI_CONTROL   => STIM_SPI_CONTROL,
      SPI_CLK_DIV   => STIM_SPI_CLK_DIV,
      SPI_DATA      => STIM_SPI_DATA,
      SPI_DATA_VLD  => STIM_SPI_DATA_VLD,
      SPI_SCLK_O    => stim_spi_sclk_w,
      SPI_MOSI_O    => stim_spi_mosi_w,
      SPI_SS_O      => stim_spi_ss_w,
      EDGE_CONTROL  => STIM_EDGE_CONTROL,
      EDGE_O        => stim_edge_w
    );

  SPI0_CS_GPIO <= stim_spi_ss_w;
  CAPTURE_EDGE0  <= stim_edge_w when STIM_EDGE_SEL = '1' else PMOD_EDGE_FALLBACK;

  ---------------------------------------------------------------------------
  -- ds_emio_mux — real fabric bus resolution, wired exactly as
  -- deepskopion_core after DS-P2.100.
  ---------------------------------------------------------------------------
  u_emio_mux : entity tich.ds_emio_mux
    port map (
      EMIO_MUX_SEL   => EMIO_MUX_SEL,
      PMOD_I2C_SCL   => '1',
      PMOD_I2C_SDA   => '1',
      PMOD_UART_TX   => '1',
      PMOD_SPI_SS    => '1',
      PMOD_SPI_MOSI  => '1',
      PMOD_SPI_MISO  => '1',
      PMOD_SPI_SCLK  => '1',
      UART0_TX       => stim_uart_tx_w,
      I2C0_SCL_O     => stim_i2c_scl_o_w,
      I2C0_SCL_T     => stim_i2c_scl_t_w,
      I2C0_SDA_O     => stim_i2c_sda_o_w,
      I2C0_SDA_T     => stim_i2c_sda_t_w,
      SPI0_SCLK_O    => stim_spi_sclk_w,
      SPI0_MOSI_O    => stim_spi_mosi_w,
      SPI0_SS_O      => stim_spi_ss_w,
      SPI0_CS_GPIO   => stim_spi_ss_w,
      I2C_INJ_SDA_O  => i2c_inj_sda_o_w,
      I2C_INJ_SDA_T  => i2c_inj_sda_t_w,
      SPI_INJ_MISO_O => spi_inj_miso_o_w,
      SPI_INJ_MISO_T => spi_inj_miso_t_w,
      I2C0_BUS_SCL   => i2c0_bus_scl_w,
      I2C0_BUS_SDA   => i2c0_bus_sda_w,
      SPI0_BUS_SCLK  => spi0_bus_sclk_w,
      SPI0_BUS_MOSI  => spi0_bus_mosi_w,
      SPI0_BUS_MISO  => spi0_bus_miso_w,
      SPI0_BUS_SS    => spi0_bus_ss_w,
      UART0_RX       => uart0_rx_w,
      SRC_I2C_SCL    => src_i2c_scl_w,
      SRC_I2C_SDA    => src_i2c_sda_w,
      SRC_UART_TX    => SRC_UART_TX,
      SRC_SPI_SS     => src_spi_ss_w,
      SRC_SPI_MOSI   => src_spi_mosi_w,
      SRC_SPI_MISO   => src_spi_miso_w,
      SRC_SPI_SCLK   => src_spi_sclk_w
    );

  I2C0_BUS_SCL  <= i2c0_bus_scl_w;
  I2C0_BUS_SDA  <= i2c0_bus_sda_w;
  SPI0_BUS_SCLK <= spi0_bus_sclk_w;
  SPI0_BUS_MOSI <= spi0_bus_mosi_w;
  SPI0_BUS_MISO <= spi0_bus_miso_w;

  ---------------------------------------------------------------------------
  -- i2c_injector / spi_injector — the same responders the real top uses.
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
      CONTROL    => I2C_INJ_CONTROL,
      DATAIN     => I2C_INJ_DATAIN,
      DATAIN_VLD => I2C_INJ_DATAIN_VLD,
      STATUS     => I2C_INJ_STATUS,
      SCL        => i2c0_bus_scl_w,
      SDA_I      => i2c0_bus_sda_w,
      SDA_O      => i2c_inj_sda_o_w,
      SDA_T      => i2c_inj_sda_t_w,
      DBG        => i2c_slave_dbg_w,
      DBG_FIFO   => i2c_dbg_fifo_w,
      DBG_SHIFT  => i2c_dbg_shift_w,
      DBG2       => i2c_dbg2_w
    );

  u_spi_injector : entity tich.spi_injector
    port map (
      CLK        => CLK,
      RST        => RST,
      ENABLE     => '1',
      CONTROL    => SPI_INJ_CONTROL,
      DATAIN     => SPI_INJ_DATAIN,
      DATAIN_VLD => SPI_INJ_DATAIN_VLD,
      STATUS     => SPI_INJ_STATUS,
      SCLK       => spi0_bus_sclk_w,
      MOSI       => spi0_bus_mosi_w,
      CS_N       => stim_spi_ss_w,
      MISO_O     => spi_inj_miso_o_w,
      MISO_T     => spi_inj_miso_t_w
    );

end architecture str;
