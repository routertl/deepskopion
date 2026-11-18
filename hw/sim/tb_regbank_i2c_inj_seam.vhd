-------------------------------------------------------------------------------
-- Title      : tb_regbank_i2c_inj_seam
-- Project    : deepskopion / DS-T2.11 PS<->fabric seam diagnostic
-------------------------------------------------------------------------------
-- File       : tb_regbank_i2c_inj_seam.vhd
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   D.11 prime suspect for the EMIO-loop REQ-6 failure: the regbank-to-
--   i2c_injector DATAIN_VLD pulse may never reach the injector on a PS
--   AXI-Lite write to slot C_INDEX_I2C_INJ_DATAIN. Existing tb_emio_loop
--   drives DATAIN/DATAIN_VLD as TB ports directly — that bypasses the
--   regbank and so proves only "given a correct VLD pulse, injector
--   works". This bench wires the REAL inner regbank (tich.system_regbank)
--   to the REAL i2c_injector exactly as deepskopion_top does, then exposes
--   flat AXI-Lite ports so a cocotb AxiLiteMaster BFM can drive PS-style
--   writes to I2C_INJ_CONTROL / I2C_INJ_DATAIN and read I2C_INJ_STATUS.
--
--   Wiring mirrors deepskopion_top.vhd (regbank slot indices, the
--   "telemetry_in(STATUS).valid <= '1'" continuous fan-in, and the
--   regs_out_local fan-out to i2c_injector.CONTROL/DATAIN/DATAIN_VLD):
--
--       AXI-Lite ──▶ system_regbank ──REGS_OUT─▶  i2c_injector
--                           ▲                          │
--                           └──REGS_IN(STATUS)─────────┘ telemetry fan-in
--
--   We instantiate the INNER regbank (not system_regbank_wrapper) — the
--   wrapper adds two BRAM ROMs whose .hex files don't affect this seam.
--
--   PASS criterion: after enabling CONTROL[0] and four AXI writes to
--   DATAIN with bytes DE / AD / BE / EF, an AXI read of STATUS reports
--   fifo_empty='0' and the FIFO count equals 4.
--   FAIL signal: count stays at 0, OR partial counts (1/2/3) — that's
--   the seam defect D.11 calls out.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_pkg.all;          -- C_TIMESTAMP_WIDTH
use tich.system_regbank_pkg.all;   -- t_memArrayReg, C_INDEX_*, C_REGBANK_DEPTH

entity tb_regbank_i2c_inj_seam is
  generic (
    G_AXI_ADDR_WIDTH : integer := 11;
    G_AXI_DATA_WIDTH : integer := 32;
    G_FIFO_DEPTH     : integer := 4
  );
  port (
    CLK : in std_logic;
    RST : in std_logic;

    -- Timestamp slice for the i2c_injector (stuck/glitch monitor input).
    TIMESTAMP : in std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);

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

    -- ── Observation taps: post-regbank values feeding the injector. ──
    -- These are NOT the seam — they're the regbank's fan-out exactly as
    -- deepskopion_top consumes it. Exposed so the cocotb test can
    -- correlate AXI events with the resulting CONTROL/DATAIN/DATAIN_VLD
    -- waveform (helps localise failure if STATUS lies).
    OBS_I2C_INJ_CONTROL    : out std_logic_vector(31 downto 0);
    OBS_I2C_INJ_DATAIN     : out std_logic_vector(31 downto 0);
    OBS_I2C_INJ_DATAIN_VLD : out std_logic;
    OBS_I2C_INJ_STATUS     : out std_logic_vector(31 downto 0)
  );
end entity tb_regbank_i2c_inj_seam;

architecture str of tb_regbank_i2c_inj_seam is

  -- Regbank record buses.
  signal regs_in_w  : t_memArrayReg := (others => (data => (others => '0'),
                                                   valid => '0'));
  signal regs_out_w : t_memArrayReg;

  -- Slot-decoded signals fed to the i2c_injector — wired exactly as
  -- deepskopion_top.vhd does (see top:1128-1130).
  signal i2c_inj_control_w  : std_logic_vector(31 downto 0);
  signal i2c_inj_datain_w   : std_logic_vector(31 downto 0);
  signal i2c_inj_datain_vld : std_logic;
  signal i2c_inj_status_w   : std_logic_vector(31 downto 0);

  -- Quiescent I2C bus signals — no master / no other slave drives the
  -- physical lines in this bench. The injector's SCL/SDA_I inputs idle
  -- high (open-drain release). Its SDA_O/T outputs are left open since
  -- the seam under test is the REGBANK -> DATAIN_VLD path, not the I2C
  -- protocol. (The full I2C protocol path is already covered by
  -- tb_emio_loop + tb_i2c_injector.)
  signal i2c_bus_scl_idle : std_logic := '1';
  signal i2c_bus_sda_idle : std_logic := '1';

begin

  ---------------------------------------------------------------------------
  -- Inner system_regbank — same instance the wrapper uses (we skip the
  -- streaming-ROM wrapper since its BRAM .hex inputs are unrelated to
  -- the i2c_injector seam).
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
  -- Slot-decoded fan-out — mirrors deepskopion_top.vhd:1128-1130.
  ---------------------------------------------------------------------------
  i2c_inj_control_w  <= regs_out_w(C_INDEX_I2C_INJ_CONTROL / 4).data;
  i2c_inj_datain_w   <= regs_out_w(C_INDEX_I2C_INJ_DATAIN  / 4).data;
  i2c_inj_datain_vld <= regs_out_w(C_INDEX_I2C_INJ_DATAIN  / 4).valid;

  ---------------------------------------------------------------------------
  -- Telemetry fan-in — STATUS continuously published back into the
  -- regbank's REGS_IN so AXI reads of slot 56 return the live injector
  -- STATUS word. Mirrors deepskopion_top.vhd:1244-1245.
  ---------------------------------------------------------------------------
  regs_in_w(C_INDEX_I2C_INJ_STATUS / 4).data  <= i2c_inj_status_w;
  regs_in_w(C_INDEX_I2C_INJ_STATUS / 4).valid <= '1';

  ---------------------------------------------------------------------------
  -- i2c_injector — REAL block, same generics as the top instance.
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
      CONTROL    => i2c_inj_control_w,
      DATAIN     => i2c_inj_datain_w,
      DATAIN_VLD => i2c_inj_datain_vld,
      STATUS     => i2c_inj_status_w,
      SCL        => i2c_bus_scl_idle,
      SDA_I      => i2c_bus_sda_idle,
      SDA_O      => open,
      SDA_T      => open
    );

  ---------------------------------------------------------------------------
  -- Observation taps to the cocotb test.
  ---------------------------------------------------------------------------
  OBS_I2C_INJ_CONTROL    <= i2c_inj_control_w;
  OBS_I2C_INJ_DATAIN     <= i2c_inj_datain_w;
  OBS_I2C_INJ_DATAIN_VLD <= i2c_inj_datain_vld;
  OBS_I2C_INJ_STATUS     <= i2c_inj_status_w;

end architecture str;
