-- rr unknown-llm (AI-generated)
--------------------------------------------------------------------------------
-- File       : eye_loopback_top.vhd
-- Project    : deepskopion — PMOD eye-scan / BERT instrument (DS-P2.66, Phase 5b)
-- Status     : NEW, SEPARATE build target `eye_loopback` (Zybo Z7-20,
--              xc7z020clg400-1). Does NOT touch the canfd_demo product top
--              (deepskopion_top.vhd) — this is a stripped sibling that REUSES
--              the SAME, UNMODIFIED deepskopion_bd component (PS7 + M_AXI_GP0 +
--              FCLK_CLK0/1 + DDR/FIXED_IO + the EMIO/CAN/APB/IRQ ports the BD
--              exposes). The eye instrument is polled via the AXI-Lite regbank
--              only — the datalogger fabric, zs_msg_framer and S_AXI_HP0/ddr_stream
--              datapath are NOT instantiated here.
--
-- ARCHITECTURE
--
--   fclk_clk0 (100 MHz, from BD)
--        │
--        ├─► clk_sys (system/AXI-Lite/DRP/eye-control clock)
--        │
--        ├─► MMCME2_ADV (×10 /5)  ─► refclk200 (200 MHz, BUFG) ─► IDELAYCTRL
--        │
--        └─► clk_mmcm_xilinx (DRP-reconfigurable, FREQ_SEL axis)
--                 CLK_OUT = eye_clk (swept TX bit-clock) ─► eye_engine.CLK
--
--   regbank (AXI-Lite, M_AXI_GP0):
--     EYE_CONTROL  [0]=start [1]=aggressor_en [9:4]=phase_sel(6b) [15:12]=freq_sel(4b)  (RW)
--     EYE_BIT_TARGET  (RW)
--     EYE_STATUS   [0]=busy [1]=done [2]=locked  (RO)
--     EYE_ERR_COUNT_LO/HI, EYE_BIT_COUNT_LO/HI   (RO)
--
--   PMOD JC loopback (free header — physical jumper jc[0]→jc[1]):
--     eye_engine.TX_O   → jc[0]  (V15)  -- drive
--     jc[1] (W15)       → idelay_scan.DIN → DOUT → eye_engine.RX_I
--     eye_engine.AGGR_O → jc[2]  (T11)  -- aggressor
--
-- The idelay_scan `xilinx` architecture (IDELAYE2 + IDELAYCTRL, IODELAY_GROUP
-- "eye_iodelay") is bound by DIRECT-ENTITY instantiation with explicit arch:
--   u_idelay : entity tich.idelay_scan(xilinx)
-- so synth pulls the real primitive (NOT the sim `model` arch).
--
-- Standard   : VHDL-2019 (IEEE 1076-2019)
-- Licence    : CERN-OHL-W-2.0
--------------------------------------------------------------------------------

library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

library tich;
  use tich.system_regbank_pkg.all;   -- t_memArrayReg, C_INDEX_EYE_*

library unisim;
  use unisim.vcomponents.all;        -- MMCME2_ADV, BUFG (200 MHz REFCLK gen)

entity eye_loopback_top is
  generic (
    -- Eye engine config (kept identical to the eye_engine defaults).
    G_PRBS_ORDER      : positive := 7;
    G_AGGR_ORDER      : positive := 15;
    G_LOCK_OK         : positive := 32;
    G_CNT_W           : positive := 48
  );
  port (
    -- ── PS DDR (dedicated bank) ──────────────────────────────────────────
    DDR_addr          : inout std_logic_vector(14 downto 0);
    DDR_ba            : inout std_logic_vector(2 downto 0);
    DDR_cas_n         : inout std_logic;
    DDR_ck_n          : inout std_logic;
    DDR_ck_p          : inout std_logic;
    DDR_cke           : inout std_logic;
    DDR_cs_n          : inout std_logic;
    DDR_dm            : inout std_logic_vector(3 downto 0);
    DDR_dq            : inout std_logic_vector(31 downto 0);
    DDR_dqs_n         : inout std_logic_vector(3 downto 0);
    DDR_dqs_p         : inout std_logic_vector(3 downto 0);
    DDR_odt           : inout std_logic;
    DDR_ras_n         : inout std_logic;
    DDR_reset_n       : inout std_logic;
    DDR_we_n          : inout std_logic;

    -- ── PS dedicated FIXED_IO ────────────────────────────────────────────
    FIXED_IO_ddr_vrn  : inout std_logic;
    FIXED_IO_ddr_vrp  : inout std_logic;
    FIXED_IO_mio      : inout std_logic_vector(53 downto 0);
    FIXED_IO_ps_clk   : inout std_logic;
    FIXED_IO_ps_porb  : inout std_logic;
    FIXED_IO_ps_srstb : inout std_logic;

    -- ── User-facing PL IO ────────────────────────────────────────────────
    led : out std_logic_vector(3 downto 0);  -- M14/M15/G14/D18 (liveness)
    -- PMOD JC loopback: jc[0]=TX, jc[1]=RX, jc[2]=AGGR. The remaining JC
    -- pins are declared as inputs (parked) so the header is fully defined.
    jc  : inout std_logic_vector(7 downto 0)
  );
end entity eye_loopback_top;

architecture rtl of eye_loopback_top is

  ---------------------------------------------------------------------------
  -- deepskopion_bd — SAME unmodified component as deepskopion_top. ALL ports
  -- declared; the eye target drives M_AXI_GP0 + clocks/reset and ties off the
  -- datalogger-product ports (EMIO/CAN/APB/GPIO/HP0/IRQ).
  ---------------------------------------------------------------------------
  component deepskopion_bd is
    port (
      DDR_addr          : inout std_logic_vector(14 downto 0);
      DDR_ba            : inout std_logic_vector(2 downto 0);
      DDR_cas_n         : inout std_logic;
      DDR_ck_n          : inout std_logic;
      DDR_ck_p          : inout std_logic;
      DDR_cke           : inout std_logic;
      DDR_cs_n          : inout std_logic;
      DDR_dm            : inout std_logic_vector(3 downto 0);
      DDR_dq            : inout std_logic_vector(31 downto 0);
      DDR_dqs_n         : inout std_logic_vector(3 downto 0);
      DDR_dqs_p         : inout std_logic_vector(3 downto 0);
      DDR_odt           : inout std_logic;
      DDR_ras_n         : inout std_logic;
      DDR_reset_n       : inout std_logic;
      DDR_we_n          : inout std_logic;
      FIXED_IO_ddr_vrn  : inout std_logic;
      FIXED_IO_ddr_vrp  : inout std_logic;
      FIXED_IO_mio      : inout std_logic_vector(53 downto 0);
      FIXED_IO_ps_clk   : inout std_logic;
      FIXED_IO_ps_porb  : inout std_logic;
      FIXED_IO_ps_srstb : inout std_logic;
      M_AXI_GP0_araddr  : out   std_logic_vector(31 downto 0);
      M_AXI_GP0_arprot  : out   std_logic_vector(2 downto 0);
      M_AXI_GP0_arready : in    std_logic;
      M_AXI_GP0_arvalid : out   std_logic;
      M_AXI_GP0_awaddr  : out   std_logic_vector(31 downto 0);
      M_AXI_GP0_awprot  : out   std_logic_vector(2 downto 0);
      M_AXI_GP0_awready : in    std_logic;
      M_AXI_GP0_awvalid : out   std_logic;
      M_AXI_GP0_bready  : out   std_logic;
      M_AXI_GP0_bresp   : in    std_logic_vector(1 downto 0);
      M_AXI_GP0_bvalid  : in    std_logic;
      M_AXI_GP0_rdata   : in    std_logic_vector(31 downto 0);
      M_AXI_GP0_rready  : out   std_logic;
      M_AXI_GP0_rresp   : in    std_logic_vector(1 downto 0);
      M_AXI_GP0_rvalid  : in    std_logic;
      M_AXI_GP0_wdata   : out   std_logic_vector(31 downto 0);
      M_AXI_GP0_wready  : in    std_logic;
      M_AXI_GP0_wstrb   : out   std_logic_vector(3 downto 0);
      M_AXI_GP0_wvalid  : out   std_logic;
      CANFD0_APB_paddr   : out std_logic_vector(31 downto 0);
      CANFD0_APB_penable : out std_logic;
      CANFD0_APB_prdata  : in  std_logic_vector(31 downto 0);
      CANFD0_APB_pready  : in  std_logic_vector(0 to 0);
      CANFD0_APB_psel    : out std_logic_vector(0 to 0);
      CANFD0_APB_pslverr : in  std_logic_vector(0 to 0);
      CANFD0_APB_pwdata  : out std_logic_vector(31 downto 0);
      CANFD0_APB_pwrite  : out std_logic;
      CANFD1_APB_paddr   : out std_logic_vector(31 downto 0);
      CANFD1_APB_penable : out std_logic;
      CANFD1_APB_prdata  : in  std_logic_vector(31 downto 0);
      CANFD1_APB_pready  : in  std_logic_vector(0 to 0);
      CANFD1_APB_psel    : out std_logic_vector(0 to 0);
      CANFD1_APB_pslverr : in  std_logic_vector(0 to 0);
      CANFD1_APB_pwdata  : out std_logic_vector(31 downto 0);
      CANFD1_APB_pwrite  : out std_logic;
      S_AXI_HP0_araddr  : in    std_logic_vector(31 downto 0);
      S_AXI_HP0_arburst : in    std_logic_vector(1 downto 0);
      S_AXI_HP0_arcache : in    std_logic_vector(3 downto 0);
      S_AXI_HP0_arid    : in    std_logic_vector(1 downto 0);
      S_AXI_HP0_arlen   : in    std_logic_vector(7 downto 0);
      S_AXI_HP0_arlock  : in    std_logic_vector(0 downto 0);
      S_AXI_HP0_arprot  : in    std_logic_vector(2 downto 0);
      S_AXI_HP0_arqos   : in    std_logic_vector(3 downto 0);
      S_AXI_HP0_arready : out   std_logic;
      S_AXI_HP0_arsize  : in    std_logic_vector(2 downto 0);
      S_AXI_HP0_arvalid : in    std_logic;
      S_AXI_HP0_awaddr  : in    std_logic_vector(31 downto 0);
      S_AXI_HP0_awburst : in    std_logic_vector(1 downto 0);
      S_AXI_HP0_awcache : in    std_logic_vector(3 downto 0);
      S_AXI_HP0_awid    : in    std_logic_vector(1 downto 0);
      S_AXI_HP0_awlen   : in    std_logic_vector(7 downto 0);
      S_AXI_HP0_awlock  : in    std_logic_vector(0 downto 0);
      S_AXI_HP0_awprot  : in    std_logic_vector(2 downto 0);
      S_AXI_HP0_awqos   : in    std_logic_vector(3 downto 0);
      S_AXI_HP0_awready : out   std_logic;
      S_AXI_HP0_awsize  : in    std_logic_vector(2 downto 0);
      S_AXI_HP0_awvalid : in    std_logic;
      S_AXI_HP0_bid     : out   std_logic_vector(1 downto 0);
      S_AXI_HP0_bready  : in    std_logic;
      S_AXI_HP0_bresp   : out   std_logic_vector(1 downto 0);
      S_AXI_HP0_bvalid  : out   std_logic;
      S_AXI_HP0_rdata   : out   std_logic_vector(127 downto 0);
      S_AXI_HP0_rid     : out   std_logic_vector(1 downto 0);
      S_AXI_HP0_rlast   : out   std_logic;
      S_AXI_HP0_rready  : in    std_logic;
      S_AXI_HP0_rresp   : out   std_logic_vector(1 downto 0);
      S_AXI_HP0_rvalid  : out   std_logic;
      S_AXI_HP0_wdata   : in    std_logic_vector(127 downto 0);
      S_AXI_HP0_wlast   : in    std_logic;
      S_AXI_HP0_wready  : out   std_logic;
      S_AXI_HP0_wstrb   : in    std_logic_vector(15 downto 0);
      S_AXI_HP0_wvalid  : in    std_logic;
      fclk_clk0           : out std_logic;
      fclk_aresetn        : out std_logic_vector(0 to 0);
      fclk_clk1           : out std_logic;
      fclk_clk1_aresetn   : out std_logic_vector(0 to 0);
      UART0_TX            : out std_logic;
      UART0_RX            : in  std_logic;
      I2C0_SCL_I          : in  std_logic;
      I2C0_SCL_O          : out std_logic;
      I2C0_SCL_T          : out std_logic;
      I2C0_SDA_I          : in  std_logic;
      I2C0_SDA_O          : out std_logic;
      I2C0_SDA_T          : out std_logic;
      SPI0_SCLK_I         : in  std_logic;
      SPI0_SCLK_O         : out std_logic;
      SPI0_SCLK_T         : out std_logic;
      SPI0_MOSI_I         : in  std_logic;
      SPI0_MOSI_O         : out std_logic;
      SPI0_MOSI_T         : out std_logic;
      SPI0_MISO_I         : in  std_logic;
      SPI0_MISO_O         : out std_logic;
      SPI0_MISO_T         : out std_logic;
      SPI0_SS_I           : in  std_logic;
      SPI0_SS_O           : out std_logic;
      SPI0_SS_T           : out std_logic;
      GPIO_O              : out std_logic_vector(0 downto 0);
      GPIO_I              : in  std_logic_vector(0 downto 0);
      GPIO_T              : out std_logic_vector(0 downto 0);
      CAN0_PHY_TX         : out std_logic;
      CAN0_PHY_RX         : in  std_logic;
      CAN1_PHY_TX         : out std_logic;
      CAN1_PHY_RX         : in  std_logic;
      IRQ_F2P             : in  std_logic_vector(2 downto 0)
    );
  end component deepskopion_bd;

  ---------------------------------------------------------------------------
  -- Clock + reset
  ---------------------------------------------------------------------------
  signal clk_sys      : std_logic;                 -- 100 MHz fclk_clk0
  signal rst_sys      : std_logic;
  signal aresetn      : std_logic_vector(0 to 0);
  signal clk_ddr      : std_logic;                 -- 125 MHz fclk_clk1 (parked)
  signal ddr_aresetn  : std_logic_vector(0 to 0);

  -- 200 MHz IDELAYCTRL reference (MMCME2_ADV ×10 /5)
  signal refclk200      : std_logic;
  signal refclk200_unbuf: std_logic;
  signal ref_clkfb      : std_logic;
  signal ref_clkfb_buf  : std_logic;
  signal ref_locked     : std_logic;
  signal idelay_rdy     : std_logic;

  -- Swept TX bit-clock from clk_mmcm_xilinx (frequency axis)
  signal eye_clk        : std_logic;
  signal eye_mmcm_locked: std_logic;
  signal eye_mmcm_busy  : std_logic;

  ---------------------------------------------------------------------------
  -- AXI-Lite slave from BD's M_AXI_GP0 (flat).
  ---------------------------------------------------------------------------
  signal gp0_awaddr  : std_logic_vector(31 downto 0);
  signal gp0_awprot  : std_logic_vector(2 downto 0);
  signal gp0_awvalid : std_logic;
  signal gp0_awready : std_logic;
  signal gp0_wdata   : std_logic_vector(31 downto 0);
  signal gp0_wstrb   : std_logic_vector(3 downto 0);
  signal gp0_wvalid  : std_logic;
  signal gp0_wready  : std_logic;
  signal gp0_bresp   : std_logic_vector(1 downto 0);
  signal gp0_bvalid  : std_logic;
  signal gp0_bready  : std_logic;
  signal gp0_araddr  : std_logic_vector(31 downto 0);
  signal gp0_arprot  : std_logic_vector(2 downto 0);
  signal gp0_arvalid : std_logic;
  signal gp0_arready : std_logic;
  signal gp0_rdata   : std_logic_vector(31 downto 0);
  signal gp0_rresp   : std_logic_vector(1 downto 0);
  signal gp0_rvalid  : std_logic;
  signal gp0_rready  : std_logic;

  ---------------------------------------------------------------------------
  -- Regbank fan-in/out.
  ---------------------------------------------------------------------------
  signal regs_out_local : t_memArrayReg;
  signal telemetry_in   : t_memArrayReg := (others => (data => (others => '0'), valid => '0'));

  ---------------------------------------------------------------------------
  -- EYE_CONTROL decode (clk_sys domain).
  ---------------------------------------------------------------------------
  signal eye_control_w  : std_logic_vector(31 downto 0);
  signal eye_bit_target : std_logic_vector(31 downto 0);
  signal eye_start      : std_logic;
  signal eye_aggr_en    : std_logic;
  signal eye_phase_sel  : std_logic_vector(5 downto 0);   -- EYE_CONTROL[9:4]
  signal eye_freq_sel   : std_logic_vector(3 downto 0);   -- EYE_CONTROL[15:12]

  -- IDELAY tap = low 5 bits of phase_sel (idelay_scan G_TAP_W=5)
  signal idelay_tap     : std_logic_vector(4 downto 0);

  -- LD/RECONFIG pulse generation on the rising edge of EYE_CONTROL[0] (start).
  signal start_d_r      : std_logic := '0';
  signal eye_ld_pulse   : std_logic;          -- clk_sys-domain LD/reconfig pulse

  ---------------------------------------------------------------------------
  -- eye_engine status + counters.
  ---------------------------------------------------------------------------
  signal eye_busy       : std_logic;
  signal eye_done       : std_logic;
  signal eye_locked     : std_logic;
  signal eye_err_count  : std_logic_vector(G_CNT_W-1 downto 0);
  signal eye_bit_count  : std_logic_vector(G_CNT_W-1 downto 0);

  -- PMOD loopback nets (post-IBUF / pre-OBUF).
  signal tx_bit_w       : std_logic;
  signal rx_bit_w       : std_logic;
  signal aggr_bit_w     : std_logic;
  signal rx_delayed_w   : std_logic;

  ---------------------------------------------------------------------------
  -- Liveness heartbeat (clk_sys).
  ---------------------------------------------------------------------------
  signal hb_div : unsigned(26 downto 0) := (others => '0');

begin

  ---------------------------------------------------------------------------
  -- Clock / reset distribution.
  ---------------------------------------------------------------------------
  rst_sys <= not aresetn(0);

  ---------------------------------------------------------------------------
  -- 200 MHz REFCLK for IDELAYCTRL.  100 MHz × CLKFBOUT_MULT_F(10) = 1000 MHz
  -- VCO, ÷ CLKOUT0_DIVIDE_F(5) = 200 MHz.  Feedback + output through BUFGs.
  ---------------------------------------------------------------------------
  u_refclk_mmcm : MMCME2_ADV
    generic map (
      BANDWIDTH         => "OPTIMIZED",
      CLKIN1_PERIOD     => 10.0,        -- 100 MHz in
      CLKFBOUT_MULT_F   => 10.0,        -- VCO = 1000 MHz
      DIVCLK_DIVIDE     => 1,
      CLKOUT0_DIVIDE_F  => 5.0          -- 200 MHz out
    )
    port map (
      CLKIN1        => clk_sys,
      CLKIN2        => '0',
      CLKINSEL      => '1',
      CLKFBOUT      => ref_clkfb,
      CLKFBIN       => ref_clkfb_buf,
      CLKOUT0       => refclk200_unbuf,
      CLKOUT1       => open,
      CLKOUT2       => open,
      CLKOUT3       => open,
      CLKOUT4       => open,
      CLKOUT5       => open,
      CLKOUT6       => open,
      CLKOUT0B      => open,
      CLKOUT1B      => open,
      CLKOUT2B      => open,
      CLKOUT3B      => open,
      CLKFBOUTB     => open,
      LOCKED        => ref_locked,
      RST           => rst_sys,
      PWRDWN        => '0',
      DADDR         => (others => '0'),
      DCLK          => '0',
      DEN           => '0',
      DWE           => '0',
      DI            => (others => '0'),
      DO            => open,
      DRDY          => open,
      PSCLK         => '0',
      PSEN          => '0',
      PSINCDEC      => '0',
      PSDONE        => open,
      CLKINSTOPPED  => open,
      CLKFBSTOPPED  => open
    );

  u_refclk_fb_bufg  : BUFG port map (I => ref_clkfb,        O => ref_clkfb_buf);
  u_refclk_out_bufg : BUFG port map (I => refclk200_unbuf,  O => refclk200);

  ---------------------------------------------------------------------------
  -- Frequency axis: clk_mmcm_xilinx (DRP-reconfigurable). CLK_OUT = eye_clk.
  -- FREQ_SEL = EYE_CONTROL[15:12]; RECONFIG pulses on the START rising edge.
  ---------------------------------------------------------------------------
  u_eye_mmcm : entity tich.clk_mmcm_xilinx
    generic map (
      G_LOCK_CYCLES     => 8,
      G_CLKIN_PERIOD_NS => 10.0,        -- clk_sys = 100 MHz
      G_CLKFBOUT_MULT   => 10.0,
      G_CLKOUT0_DIVIDE  => 10.0         -- base profile = 100 MHz TX clock
    )
    port map (
      CLK_IN         => clk_sys,
      DCLK           => clk_sys,
      RST            => rst_sys,
      FREQ_SEL       => eye_freq_sel,
      RECONFIG       => eye_ld_pulse,
      CLK_OUT        => eye_clk,
      -- DS-P3.118 dual-output MMCM: this Z7-20 target does not use the
      -- phase-shifted sample clock yet, so leave SAMPLE_CLK_OUT/PS_DONE open and
      -- tie the PS command inputs off (CLKOUT1 still tracks eye_clk's frequency,
      -- it just is not phase-swept here — eye_engine.CLK_RX shares eye_clk above).
      SAMPLE_CLK_OUT => open,
      LOCKED         => eye_mmcm_locked,
      BUSY           => eye_mmcm_busy,
      PS_EN          => '0',
      PS_INCDEC      => '0',
      PS_DONE        => open
    );

  ---------------------------------------------------------------------------
  -- EYE_CONTROL / EYE_BIT_TARGET decode.
  ---------------------------------------------------------------------------
  eye_control_w  <= regs_out_local(C_INDEX_EYE_CONTROL    / 4).data;
  eye_bit_target <= regs_out_local(C_INDEX_EYE_BIT_TARGET / 4).data;

  eye_start     <= eye_control_w(0);
  eye_aggr_en   <= eye_control_w(1);
  eye_phase_sel <= eye_control_w(9 downto 4);
  eye_freq_sel  <= eye_control_w(15 downto 12);
  idelay_tap    <= eye_phase_sel(4 downto 0);

  -- LD/RECONFIG pulse on the rising edge of EYE_CONTROL[0] (start). This loads
  -- the selected IDELAY tap and re-launches the MMCM profile for the new
  -- measurement point. eye_engine has its own internal start edge-detect.
  p_start_edge : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        start_d_r <= '0';
      else
        start_d_r <= eye_start;
      end if;
    end if;
  end process;
  eye_ld_pulse <= eye_start and not start_d_r;

  ---------------------------------------------------------------------------
  -- eye_engine — TX gen + FSM on the swept eye_clk. This Z7-20 target has no
  -- phase-shifted CLKOUT1 yet (DS-P3.118 lands that on classic_eye first), so the
  -- RX checker shares eye_clk: CLK_RX => eye_clk preserves the single-clock
  -- behaviour. (The horizontal axis here stays the IDELAY tap until a sample_clk
  -- is wired in a later pass.)
  ---------------------------------------------------------------------------
  u_eye : entity tich.eye_engine
    generic map (
      G_ORDER      => G_PRBS_ORDER,
      G_AGGR_ORDER => G_AGGR_ORDER,
      G_LOCK_OK    => G_LOCK_OK,
      G_CNT_W      => G_CNT_W
    )
    port map (
      CLK_TX       => eye_clk,
      CLK_RX       => eye_clk,
      RST          => rst_sys,
      START        => eye_start,
      BIT_TARGET   => eye_bit_target,
      AGGRESSOR_EN => eye_aggr_en,
      TX_O         => tx_bit_w,
      RX_I         => rx_delayed_w,
      AGGR_O       => aggr_bit_w,
      BUSY         => eye_busy,
      DONE         => eye_done,
      LOCKED       => eye_locked,
      ERR_COUNT    => eye_err_count,
      BIT_COUNT    => eye_bit_count
    );

  ---------------------------------------------------------------------------
  -- idelay_scan (xilinx arch) — phase/timing axis on the RX loopback bit.
  -- Direct-entity instantiation with explicit architecture binds IDELAYE2.
  -- LD pulse loads idelay_tap each measurement; C = eye_clk (load domain).
  ---------------------------------------------------------------------------
  u_idelay : entity tich.idelay_scan(xilinx)
    generic map (
      G_TAP_W  => 5,
      G_TAP_PS => 78
    )
    port map (
      CLK    => eye_clk,
      RST    => rst_sys,
      REFCLK => refclk200,
      TAP    => idelay_tap,
      LD     => eye_ld_pulse,
      DIN    => rx_bit_w,
      DOUT   => rx_delayed_w,
      RDY    => idelay_rdy
    );

  ---------------------------------------------------------------------------
  -- PMOD JC loopback. jc[0]=TX out, jc[1]=RX in, jc[2]=AGGR out. Vivado
  -- infers OBUF/IBUF on these top-level inout ports automatically.
  ---------------------------------------------------------------------------
  jc(0)    <= tx_bit_w;       -- TX  (V15) → jumper → jc[1]
  rx_bit_w <= jc(1);          -- RX  (W15) ← jumper from jc[0]
  jc(2)    <= aggr_bit_w;     -- AGGR (T11)
  jc(3)    <= 'Z';            -- parked (high-Z)
  jc(4)    <= 'Z';
  jc(5)    <= 'Z';
  jc(6)    <= 'Z';
  jc(7)    <= 'Z';

  ---------------------------------------------------------------------------
  -- Regbank RO telemetry: EYE_STATUS + 48-bit ERR/BIT counters (lo/hi).
  ---------------------------------------------------------------------------
  telemetry_in(C_INDEX_EYE_STATUS / 4).data  <=
      (31 downto 3 => '0') & eye_locked & eye_done & eye_busy;
  telemetry_in(C_INDEX_EYE_STATUS / 4).valid <= '1';

  telemetry_in(C_INDEX_EYE_ERR_COUNT_LO / 4).data  <= eye_err_count(31 downto 0);
  telemetry_in(C_INDEX_EYE_ERR_COUNT_LO / 4).valid <= '1';
  telemetry_in(C_INDEX_EYE_ERR_COUNT_HI / 4).data  <=
      std_logic_vector(resize(unsigned(eye_err_count(G_CNT_W-1 downto 32)), 32));
  telemetry_in(C_INDEX_EYE_ERR_COUNT_HI / 4).valid <= '1';

  telemetry_in(C_INDEX_EYE_BIT_COUNT_LO / 4).data  <= eye_bit_count(31 downto 0);
  telemetry_in(C_INDEX_EYE_BIT_COUNT_LO / 4).valid <= '1';
  telemetry_in(C_INDEX_EYE_BIT_COUNT_HI / 4).data  <=
      std_logic_vector(resize(unsigned(eye_bit_count(G_CNT_W-1 downto 32)), 32));
  telemetry_in(C_INDEX_EYE_BIT_COUNT_HI / 4).valid <= '1';

  ---------------------------------------------------------------------------
  -- system_regbank_wrapper — AXI-Lite slave on M_AXI_GP0 (11-bit window).
  ---------------------------------------------------------------------------
  u_regbank : entity tich.system_regbank_wrapper
    port map (
      CLK            => clk_sys,
      RST            => rst_sys,
      S_AXI_AWADDR   => gp0_awaddr(10 downto 0),
      S_AXI_AWPROT   => gp0_awprot,
      S_AXI_AWVALID  => gp0_awvalid,
      S_AXI_AWREADY  => gp0_awready,
      S_AXI_WDATA    => gp0_wdata,
      S_AXI_WSTRB    => gp0_wstrb,
      S_AXI_WVALID   => gp0_wvalid,
      S_AXI_WREADY   => gp0_wready,
      S_AXI_BRESP    => gp0_bresp,
      S_AXI_BVALID   => gp0_bvalid,
      S_AXI_BREADY   => gp0_bready,
      S_AXI_ARADDR   => gp0_araddr(10 downto 0),
      S_AXI_ARPROT   => gp0_arprot,
      S_AXI_ARVALID  => gp0_arvalid,
      S_AXI_ARREADY  => gp0_arready,
      S_AXI_RDATA    => gp0_rdata,
      S_AXI_RRESP    => gp0_rresp,
      S_AXI_RVALID   => gp0_rvalid,
      S_AXI_RREADY   => gp0_rready,
      DATALOGGER_CONFIG => open,
      TELEMETRY_IN   => telemetry_in,
      REGS_OUT       => regs_out_local
    );

  ---------------------------------------------------------------------------
  -- deepskopion_bd — PS7 + smartconnects + reset. SAME unmodified BD as
  -- deepskopion_top. M_AXI_GP0 drives the regbank; every other BD port is
  -- driven to a benign idle (no datalogger / DMA / CAN / CTU in this target).
  ---------------------------------------------------------------------------
  u_bd : component deepskopion_bd
    port map (
      DDR_addr          => DDR_addr,
      DDR_ba            => DDR_ba,
      DDR_cas_n         => DDR_cas_n,
      DDR_ck_n          => DDR_ck_n,
      DDR_ck_p          => DDR_ck_p,
      DDR_cke           => DDR_cke,
      DDR_cs_n          => DDR_cs_n,
      DDR_dm            => DDR_dm,
      DDR_dq            => DDR_dq,
      DDR_dqs_n         => DDR_dqs_n,
      DDR_dqs_p         => DDR_dqs_p,
      DDR_odt           => DDR_odt,
      DDR_ras_n         => DDR_ras_n,
      DDR_reset_n       => DDR_reset_n,
      DDR_we_n          => DDR_we_n,
      FIXED_IO_ddr_vrn  => FIXED_IO_ddr_vrn,
      FIXED_IO_ddr_vrp  => FIXED_IO_ddr_vrp,
      FIXED_IO_mio      => FIXED_IO_mio,
      FIXED_IO_ps_clk   => FIXED_IO_ps_clk,
      FIXED_IO_ps_porb  => FIXED_IO_ps_porb,
      FIXED_IO_ps_srstb => FIXED_IO_ps_srstb,
      -- M_AXI_GP0 → regbank
      M_AXI_GP0_araddr  => gp0_araddr,
      M_AXI_GP0_arprot  => gp0_arprot,
      M_AXI_GP0_arready => gp0_arready,
      M_AXI_GP0_arvalid => gp0_arvalid,
      M_AXI_GP0_awaddr  => gp0_awaddr,
      M_AXI_GP0_awprot  => gp0_awprot,
      M_AXI_GP0_awready => gp0_awready,
      M_AXI_GP0_awvalid => gp0_awvalid,
      M_AXI_GP0_bready  => gp0_bready,
      M_AXI_GP0_bresp   => gp0_bresp,
      M_AXI_GP0_bvalid  => gp0_bvalid,
      M_AXI_GP0_rdata   => gp0_rdata,
      M_AXI_GP0_rready  => gp0_rready,
      M_AXI_GP0_rresp   => gp0_rresp,
      M_AXI_GP0_rvalid  => gp0_rvalid,
      M_AXI_GP0_wdata   => gp0_wdata,
      M_AXI_GP0_wready  => gp0_wready,
      M_AXI_GP0_wstrb   => gp0_wstrb,
      M_AXI_GP0_wvalid  => gp0_wvalid,
      -- CTU APB masters UNUSED in eye target: tie the slave-return legs idle,
      -- leave the master-driven outputs open.
      CANFD0_APB_paddr   => open,
      CANFD0_APB_penable => open,
      CANFD0_APB_prdata  => (others => '0'),
      CANFD0_APB_pready  => "1",          -- accept-and-ignore (no hang)
      CANFD0_APB_psel    => open,
      CANFD0_APB_pslverr => "0",
      CANFD0_APB_pwdata  => open,
      CANFD0_APB_pwrite  => open,
      CANFD1_APB_paddr   => open,
      CANFD1_APB_penable => open,
      CANFD1_APB_prdata  => (others => '0'),
      CANFD1_APB_pready  => "1",
      CANFD1_APB_psel    => open,
      CANFD1_APB_pslverr => "0",
      CANFD1_APB_pwdata  => open,
      CANFD1_APB_pwrite  => open,
      -- S_AXI_HP0 UNUSED: tie the slave inputs idle, leave outputs open.
      S_AXI_HP0_araddr  => (others => '0'),
      S_AXI_HP0_arburst => (others => '0'),
      S_AXI_HP0_arcache => (others => '0'),
      S_AXI_HP0_arid    => (others => '0'),
      S_AXI_HP0_arlen   => (others => '0'),
      S_AXI_HP0_arlock  => (others => '0'),
      S_AXI_HP0_arprot  => (others => '0'),
      S_AXI_HP0_arqos   => (others => '0'),
      S_AXI_HP0_arready => open,
      S_AXI_HP0_arsize  => (others => '0'),
      S_AXI_HP0_arvalid => '0',
      S_AXI_HP0_awaddr  => (others => '0'),
      S_AXI_HP0_awburst => (others => '0'),
      S_AXI_HP0_awcache => (others => '0'),
      S_AXI_HP0_awid    => (others => '0'),
      S_AXI_HP0_awlen   => (others => '0'),
      S_AXI_HP0_awlock  => (others => '0'),
      S_AXI_HP0_awprot  => (others => '0'),
      S_AXI_HP0_awqos   => (others => '0'),
      S_AXI_HP0_awready => open,
      S_AXI_HP0_awsize  => (others => '0'),
      S_AXI_HP0_awvalid => '0',
      S_AXI_HP0_bid     => open,
      S_AXI_HP0_bready  => '0',
      S_AXI_HP0_bresp   => open,
      S_AXI_HP0_bvalid  => open,
      S_AXI_HP0_rdata   => open,
      S_AXI_HP0_rid     => open,
      S_AXI_HP0_rlast   => open,
      S_AXI_HP0_rready  => '0',
      S_AXI_HP0_rresp   => open,
      S_AXI_HP0_rvalid  => open,
      S_AXI_HP0_wdata   => (others => '0'),
      S_AXI_HP0_wlast   => '0',
      S_AXI_HP0_wready  => open,
      S_AXI_HP0_wstrb   => (others => '0'),
      S_AXI_HP0_wvalid  => '0',
      -- Clocks + reset
      fclk_clk0           => clk_sys,
      fclk_aresetn        => aresetn,
      fclk_clk1           => clk_ddr,
      fclk_clk1_aresetn   => ddr_aresetn,
      -- EMIO peripherals UNUSED: master-out legs open, slave-in legs idle.
      UART0_TX            => open,
      UART0_RX            => '1',         -- UART idle high
      I2C0_SCL_I          => '1',         -- pulled-up bus idle
      I2C0_SCL_O          => open,
      I2C0_SCL_T          => open,
      I2C0_SDA_I          => '1',
      I2C0_SDA_O          => open,
      I2C0_SDA_T          => open,
      SPI0_SCLK_I         => '0',
      SPI0_SCLK_O         => open,
      SPI0_SCLK_T         => open,
      SPI0_MOSI_I         => '0',
      SPI0_MOSI_O         => open,
      SPI0_MOSI_T         => open,
      SPI0_MISO_I         => '0',
      SPI0_MISO_O         => open,
      SPI0_MISO_T         => open,
      SPI0_SS_I           => '1',
      SPI0_SS_O           => open,
      SPI0_SS_T           => open,
      GPIO_O              => open,
      GPIO_I              => (others => '0'),
      GPIO_T              => open,
      -- CAN EMIO UNUSED: drive RX recessive ('1'), leave TX open.
      CAN0_PHY_TX         => open,
      CAN0_PHY_RX         => '1',
      CAN1_PHY_TX         => open,
      CAN1_PHY_RX         => '1',
      -- No fabric interrupts in this target.
      IRQ_F2P             => (others => '0')
    );

  ---------------------------------------------------------------------------
  -- Liveness heartbeat + LED status.
  --   led[0] = ~1.5 Hz blink (clk_sys alive)
  --   led[1] = ref_locked    (200 MHz IDELAYCTRL refclk locked)
  --   led[2] = idelay_rdy    (IDELAYCTRL calibrated)
  --   led[3] = eye_mmcm_locked (swept-clock MMCM locked)
  ---------------------------------------------------------------------------
  p_hb : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        hb_div <= (others => '0');
      else
        hb_div <= hb_div + 1;
      end if;
    end if;
  end process;

  led(0) <= hb_div(hb_div'high);
  led(1) <= ref_locked;
  led(2) <= idelay_rdy;
  led(3) <= eye_mmcm_locked;

end architecture rtl;
