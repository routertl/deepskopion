-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : tb_regbank_spi_inj_seam
-- Project    : deepskopion / DS-T12 PS<->fabric seam diagnostic
-------------------------------------------------------------------------------
-- File       : tb_regbank_spi_inj_seam.vhd
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   SPI mirror of tb_regbank_i2c_inj_seam (DS-T2.11 / DS-T3.2). spi_injector
--   carried the same compound defect i2c_injector did (level-sampled
--   CONTROL[9] + RST_BUSY=>open) — DS-T12. This bench reproduces the
--   regbank-front-door race on the SPI side: AXI-Lite writes to
--   I2C_INJ_CONTROL- and I2C_INJ_DATAIN-equivalent slots (SPI_INJ_CONTROL
--   = 0x40, SPI_INJ_DATAIN = 0x44, SPI_INJ_STATUS = 0x48) drive the REAL
--   inner regbank (tich.system_regbank), whose REGS_OUT fan-out feeds the
--   REAL spi_injector. Mirrors deepskopion_top.vhd slot wiring +
--   telemetry fan-in.
--
--       AXI-Lite ──▶ system_regbank ──REGS_OUT─▶  spi_injector
--                           ▲                          │
--                           └──REGS_IN(STATUS)─────────┘ telemetry fan-in
--
--   We instantiate the INNER regbank (not system_regbank_wrapper) — the
--   wrapper adds two BRAM ROMs whose .hex files don't affect this seam.
--
--   SPI bus signals (SCLK/MOSI/CS_N) are tied to idle here — the seam
--   under test is the REGBANK -> DATAIN_VLD path, not the SPI protocol.
--   (The full protocol path is already covered by tb_emio_loop +
--   tb_spi_injector.)
--
--   PASS criterion: after CONTROL[0]=1 and four AXI writes to DATAIN
--   with bytes DE / AD / BE / EF (back-to-back, no idle), STATUS reports
--   fifo_empty='0' and FIFO count = 4. With the DS-T3.2/DS-T12 fix in
--   place (edge-detected CONTROL[9] + drain mode), the race never
--   triggers and all 4 bytes survive.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.system_regbank_pkg.all;   -- t_memArrayReg, C_INDEX_*, C_REGBANK_DEPTH

entity tb_regbank_spi_inj_seam is
  generic (
    G_AXI_ADDR_WIDTH : integer := 11;
    G_AXI_DATA_WIDTH : integer := 32;
    G_FIFO_DEPTH     : integer := 4
  );
  port (
    CLK : in std_logic;
    RST : in std_logic;

    -- ── Flat AXI4-Lite slave (driven by cocotbext.axi.AxiLiteMaster). ──
    S_AXI_AWADDR  : in  std_logic_vector(G_AXI_ADDR_WIDTH-1 downto 0);
    S_AXI_AWPROT  : in  std_logic_vector(2 downto 0) := (others => '0');
    S_AXI_AWVALID : in  std_logic;
    S_AXI_AWREADY : out std_logic;

    S_AXI_WDATA   : in  std_logic_vector(G_AXI_DATA_WIDTH-1 downto 0);
    S_AXI_WSTRB   : in  std_logic_vector(G_AXI_DATA_WIDTH/8-1 downto 0);
    S_AXI_WVALID  : in  std_logic;
    S_AXI_WREADY  : out std_logic;

    S_AXI_BRESP   : out std_logic_vector(1 downto 0);
    S_AXI_BVALID  : out std_logic;
    S_AXI_BREADY  : in  std_logic;

    S_AXI_ARADDR  : in  std_logic_vector(G_AXI_ADDR_WIDTH-1 downto 0);
    S_AXI_ARPROT  : in  std_logic_vector(2 downto 0) := (others => '0');
    S_AXI_ARVALID : in  std_logic;
    S_AXI_ARREADY : out std_logic;

    S_AXI_RDATA   : out std_logic_vector(G_AXI_DATA_WIDTH-1 downto 0);
    S_AXI_RRESP   : out std_logic_vector(1 downto 0);
    S_AXI_RVALID  : out std_logic;
    S_AXI_RREADY  : in  std_logic;

    -- ── Observation taps mirroring tb_regbank_i2c_inj_seam. ──
    OBS_SPI_INJ_CONTROL    : out std_logic_vector(31 downto 0);
    OBS_SPI_INJ_DATAIN     : out std_logic_vector(31 downto 0);
    OBS_SPI_INJ_DATAIN_VLD : out std_logic;
    OBS_SPI_INJ_STATUS     : out std_logic_vector(31 downto 0)
  );
end entity tb_regbank_spi_inj_seam;

architecture str of tb_regbank_spi_inj_seam is

  signal regs_in_w  : t_memArrayReg := (others => (data => (others => '0'),
                                                   valid => '0'));
  signal regs_out_w : t_memArrayReg;

  -- Slot-decoded signals fed to spi_injector — mirrors deepskopion_top wiring.
  signal spi_inj_control_w  : std_logic_vector(31 downto 0);
  signal spi_inj_datain_w   : std_logic_vector(31 downto 0);
  signal spi_inj_datain_vld : std_logic;
  signal spi_inj_status_w   : std_logic_vector(31 downto 0);

  -- Quiescent SPI bus — no master driving SCLK/MOSI/CS_N. The injector's
  -- TX path is what's under test, not the bit-shift protocol.
  signal spi_sclk_idle : std_logic := '0';
  signal spi_mosi_idle : std_logic := '0';
  signal spi_cs_n_idle : std_logic := '1';   -- CS deasserted

begin

  ---------------------------------------------------------------------------
  -- Inner system_regbank (same instance the wrapper drives).
  ---------------------------------------------------------------------------
  u_regbank : entity tich.system_regbank
    generic map (
      G_ADDR_WIDTH => G_AXI_ADDR_WIDTH,
      G_DATA_WIDTH => G_AXI_DATA_WIDTH
    )
    port map (
      CLK           => CLK,
      RST           => RST,
      S_AXI_AWADDR  => S_AXI_AWADDR,
      S_AXI_AWPROT  => S_AXI_AWPROT,
      S_AXI_AWVALID => S_AXI_AWVALID,
      S_AXI_AWREADY => S_AXI_AWREADY,
      S_AXI_WDATA   => S_AXI_WDATA,
      S_AXI_WSTRB   => S_AXI_WSTRB,
      S_AXI_WVALID  => S_AXI_WVALID,
      S_AXI_WREADY  => S_AXI_WREADY,
      S_AXI_BRESP   => S_AXI_BRESP,
      S_AXI_BVALID  => S_AXI_BVALID,
      S_AXI_BREADY  => S_AXI_BREADY,
      S_AXI_ARADDR  => S_AXI_ARADDR,
      S_AXI_ARPROT  => S_AXI_ARPROT,
      S_AXI_ARVALID => S_AXI_ARVALID,
      S_AXI_ARREADY => S_AXI_ARREADY,
      S_AXI_RDATA   => S_AXI_RDATA,
      S_AXI_RRESP   => S_AXI_RRESP,
      S_AXI_RVALID  => S_AXI_RVALID,
      S_AXI_RREADY  => S_AXI_RREADY,
      REGS_IN       => regs_in_w,
      REGS_OUT      => regs_out_w
    );

  ---------------------------------------------------------------------------
  -- Slot-decoded fan-out — mirrors deepskopion_top's spi_inj_* wiring.
  ---------------------------------------------------------------------------
  spi_inj_control_w  <= regs_out_w(C_INDEX_SPI_INJ_CONTROL / 4).data;
  spi_inj_datain_w   <= regs_out_w(C_INDEX_SPI_INJ_DATAIN  / 4).data;
  spi_inj_datain_vld <= regs_out_w(C_INDEX_SPI_INJ_DATAIN  / 4).valid;

  ---------------------------------------------------------------------------
  -- Telemetry fan-in — STATUS continuously published back into the regbank.
  ---------------------------------------------------------------------------
  regs_in_w(C_INDEX_SPI_INJ_STATUS / 4).data  <= spi_inj_status_w;
  regs_in_w(C_INDEX_SPI_INJ_STATUS / 4).valid <= '1';

  ---------------------------------------------------------------------------
  -- spi_injector — REAL block, same generics as the top instance.
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
      CONTROL    => spi_inj_control_w,
      DATAIN     => spi_inj_datain_w,
      DATAIN_VLD => spi_inj_datain_vld,
      STATUS     => spi_inj_status_w,
      SCLK       => spi_sclk_idle,
      MOSI       => spi_mosi_idle,
      CS_N       => spi_cs_n_idle,
      MISO_O     => open,
      MISO_T     => open
    );

  ---------------------------------------------------------------------------
  -- Observation taps to the cocotb test.
  ---------------------------------------------------------------------------
  OBS_SPI_INJ_CONTROL    <= spi_inj_control_w;
  OBS_SPI_INJ_DATAIN     <= spi_inj_datain_w;
  OBS_SPI_INJ_DATAIN_VLD <= spi_inj_datain_vld;
  OBS_SPI_INJ_STATUS     <= spi_inj_status_w;

end architecture str;
