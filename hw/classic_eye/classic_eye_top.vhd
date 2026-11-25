-- rr unknown-llm (AI-generated)
--------------------------------------------------------------------------------
-- File       : classic_eye_top.vhd
-- Project    : deepskopion — PMOD eye-scan / BERT instrument (DS-P3.115)
-- Status     : LEAN standalone eye top for the Zybo CLASSIC (PB200-279,
--              xc7z010clg400-1, board `zybo`). A stripped sibling of
--              eye_loopback_top (Z7-20) that instantiates the LEAN classic_eye_bd
--              (PS7 + M_AXI_GP0 + DDR/FIXED_IO + fclk only — NO datalogger fabric,
--              NO CTU CAN FD, NO S_AXI_HP0/ddr_stream, NO EMIO). The eye
--              instrument is polled via the AXI-Lite regbank (M_AXI_GP0).
--
-- WHY THE CLASSIC: the eye quantifies PMOD-connector + flying-jumper signal
-- integrity, and the real crosstalk case (vhdl-ethernet eth0/eth1 TX1 on the
-- JC1 V15/W15 pair) lives on this exact board. Measuring the eye on the same
-- board + PMOD closes the loop.
--
-- ARCHITECTURE (identical instrument to eye_loopback_top; only the BD + board +
-- PMOD mapping change):
--
--   fclk_clk0 (100 MHz, from classic_eye_bd)
--        ├─► clk_sys (system/AXI-Lite/DRP/eye-control clock)
--        ├─► MMCME2_ADV (×10 /5) ─► refclk200 (200 MHz, BUFG) ─► IDELAYCTRL
--        └─► clk_mmcm_xilinx (DRP-reconfigurable, FREQ_SEL axis) ─► eye_clk
--
--   PMOD JC loopback — Classic `zybo` pinout (routertl/boards/zybo/pinout.yml):
--     eye_engine.TX_O   → jc[0]  (V15, JC1_P) -- drive (== eth TX1 victim pin)
--     jc[4] (W15, JC1_N) → idelay_scan.DIN → DOUT → eye_engine.RX_I  -- jumper V15→W15
--     eye_engine.AGGR_O → jc[1]  (T11, JC2_P) -- adjacent-pair crosstalk aggressor
--   NB: on the Classic, V15/W15 are the jc[0]/jc[4] DIFF PAIR — NOT jc[0]/jc[1]
--   as on the Z7-20. The aggressor sits on the physically adjacent JC2_P pin.
--
--   regbank (AXI-Lite, M_AXI_GP0):
--     EYE_CONTROL  [0]=start [1]=aggressor_en [9:4]=phase_sel(6b) [15:12]=freq_sel(4b)
--     EYE_BIT_TARGET ; EYE_STATUS [0]=busy [1]=done [2]=locked
--     EYE_ERR_COUNT_LO/HI, EYE_BIT_COUNT_LO/HI
--
-- The shared system_regbank (project_regbank.yml, also used by canfd_demo) is
-- reused UNCHANGED — DATALOGGER_CONFIG is left open here; only the EYE_* slots are
-- wired. No regbank regen, so canfd_demo is unaffected.
--
-- Standard   : VHDL-2019 (IEEE 1076-2019)
-- Licence    : CERN-OHL-W-2.0
--------------------------------------------------------------------------------

library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

library tich;
library rr_rea;
  use tich.system_regbank_pkg.all;   -- t_memArrayReg, C_INDEX_EYE_*

library unisim;
  use unisim.vcomponents.all;        -- MMCME2_ADV, BUFG (200 MHz REFCLK gen)

entity classic_eye_top is
  generic (
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
    -- PMOD JC loopback: jc[0]=TX (V15), jc[4]=RX (W15), jc[1]=AGGR (T11).
    jc  : inout std_logic_vector(7 downto 0);
    -- PS ENET0 PHY (onboard Realtek RTL8211E) reset, active-low. The reset is a
    -- PL fabric pin (E17), NOT a PS MIO — so the bitstream MUST drive it HIGH or
    -- the PHY stays in reset and PS Ethernet (eth0) never comes up (VHD-P3.15 /
    -- RTL-P3.648). Needed so the eye daemon can stream the live bathtub to
    -- RouteWave over TCP.
    eth_rst_b : out std_logic
  );
end entity classic_eye_top;

architecture rtl of classic_eye_top is

  ---------------------------------------------------------------------------
  -- classic_eye_bd — LEAN PS subsystem. Only DDR/FIXED_IO + M_AXI_GP0 (flat
  -- AXI-Lite) + fclk_clk0/fclk_aresetn. No HP0/CTU-APB/EMIO/IRQ ports exist.
  ---------------------------------------------------------------------------
  component classic_eye_bd is
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
      -- EMIO GPIO (width 1): GPIO_O(0) is the Linux-driven RTL8211E reset
      -- (PS GPIO line 54 = EMIO bit 0). GPIO_I tied off, GPIO_T ignored.
      GPIO_O            : out   std_logic_vector(0 downto 0);
      GPIO_I            : in    std_logic_vector(0 downto 0);
      GPIO_T            : out   std_logic_vector(0 downto 0);
      fclk_clk0         : out   std_logic;
      fclk_aresetn      : out   std_logic_vector(0 to 0)
    );
  end component classic_eye_bd;

  ---------------------------------------------------------------------------
  -- Clock + reset
  ---------------------------------------------------------------------------
  signal clk_sys      : std_logic;                 -- 100 MHz fclk_clk0
  signal rst_sys      : std_logic;
  signal aresetn      : std_logic_vector(0 to 0);

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

  -- Swept RX SAMPLE clock (CLKOUT1, same frequency as eye_clk, dynamic fine
  -- phase-shift) — the DS-P3.118 full-UI horizontal axis. eye_engine's checker
  -- runs on this; sweeping its phase walks the sample point across a bit period.
  signal sample_clk     : std_logic;
  -- Phase-shift command (clk_sys = PSCLK domain).
  signal eye_ps_step    : std_logic;                  -- EYE_CONTROL[16]
  signal eye_ps_incdec  : std_logic;                  -- EYE_CONTROL[17]
  signal eye_ps_rst     : std_logic;                  -- EYE_CONTROL[18]
  signal ps_step_d_r    : std_logic := '0';
  signal ps_rst_d_r     : std_logic := '0';
  signal ps_en_pulse    : std_logic;                  -- 1-cycle PSEN strobe → MMCM
  signal ps_done        : std_logic;                  -- MMCM PSDONE (clk_sys)
  signal ps_busy_r      : std_logic := '0';           -- step in flight (en..done)
  signal ps_count_r     : signed(15 downto 0) := (others => '0');  -- net phase steps

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
  signal idelay_tap     : std_logic_vector(4 downto 0);   -- low 5 bits of phase_sel

  signal start_d_r      : std_logic := '0';
  signal eye_ld_pulse   : std_logic;          -- clk_sys-domain LD pulse (idelay tap load, per START)
  -- MMCM RECONFIG fires on freq_sel CHANGE only, NOT on every START — a held
  -- freq_sel just leaves the MMCM locked at that profile. The DRP sequencer now
  -- performs the full XAPP888 read-modify-write (PowerReg + reserved-bit
  -- preservation, clk_drp), so freq_sel 1/2/3 relock cleanly and the whole
  -- frequency axis is live (the prior placeholder froze eye_clk on silicon).
  -- The reconfig + relock run entirely on clk_sys/the MMCM — the system plane
  -- (regbank, REA, control) stays alive on FCLK throughout, so the host can poll
  -- BUSY/LOCKED and never blindly samples a mid-reconfig eye.
  signal eye_reconfig   : std_logic;
  signal freq_sel_prev  : std_logic_vector(3 downto 0) := (others => '0');

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

  -- Self-test mux + REA (on-chip analyzer) debug.
  signal selftest_en    : std_logic;   -- EYE_CONTROL[2]: internal TX→RX loopback
  signal rx_to_engine   : std_logic;   -- bit the checker actually sees
  signal eye_tx_sr      : std_logic_vector(G_PRBS_ORDER-1 downto 0);  -- TX LFSR
  signal eye_rx_sr      : std_logic_vector(G_PRBS_ORDER-1 downto 0);  -- recovered RX LFSR
  signal rea_probe      : std_logic_vector(31 downto 0);

  ---------------------------------------------------------------------------
  -- Clock-domain crossings between the SYSTEM plane (clk_sys = FCLK) and the
  -- swept DUT plane (eye_clk). The eye_engine IS the logic under test and runs
  -- wholly on eye_clk; everything that talks to software or observes (regbank,
  -- DRP, REA, heartbeat) runs on clk_sys so it stays alive and MMCM-independent
  -- even while the swept clock is mid-reconfig. The boundary crossings:
  --   START    clk_sys → eye_clk : 2-FF level sync (engine edge-detects it)
  --   status   eye_clk → clk_sys : 2-FF sync on busy/done/locked single bits
  --   counters eye_clk → clk_sys : double-register — read by SW only after DONE,
  --            when the counters are frozen (a quasi-static / MCP crossing)
  --   probes   eye_clk → clk_sys : 2-FF vector sync feeding the REA on clk_sys
  ---------------------------------------------------------------------------
  signal eye_start_meta : std_logic := '0';
  signal eye_start_sync : std_logic := '0';

  signal busy_meta, busy_sync : std_logic := '0';
  signal done_meta, done_sync : std_logic := '0';
  signal lock_meta, lock_sync : std_logic := '0';
  signal mmlk_meta, mmlk_sync : std_logic := '0';   -- eye_mmcm_locked → clk_sys (REA)

  signal err_cnt_s1, err_cnt_s2 : std_logic_vector(G_CNT_W-1 downto 0) := (others => '0');
  signal bit_cnt_s1, bit_cnt_s2 : std_logic_vector(G_CNT_W-1 downto 0) := (others => '0');

  -- eye_clk-domain probe bundle: tx_sr, rx_sr, then the 4 serial/loopback bits.
  constant C_PBW : natural := 2*G_PRBS_ORDER + 4;
  signal pb_raw  : std_logic_vector(C_PBW-1 downto 0);
  signal pb_meta : std_logic_vector(C_PBW-1 downto 0) := (others => '0');
  signal pb_sync : std_logic_vector(C_PBW-1 downto 0) := (others => '0');

  -- Mark the 2-FF synchroniser stages as CDC synchronisers so Vivado keeps each
  -- meta/sync pair packed together and report_cdc recognises them. The first
  -- (meta) stage is the async capture; the XDC false-paths its D pin. (Ignored
  -- by nvc/ghdl — a user attribute string.) Counter buses err/bit_cnt are a
  -- multi-cycle/quasi-static bus crossing handled by set_max_delay, not ASYNC_REG.
  attribute ASYNC_REG : string;
  attribute ASYNC_REG of eye_start_meta, eye_start_sync : signal is "TRUE";
  attribute ASYNC_REG of busy_meta, busy_sync : signal is "TRUE";
  attribute ASYNC_REG of done_meta, done_sync : signal is "TRUE";
  attribute ASYNC_REG of lock_meta, lock_sync : signal is "TRUE";
  attribute ASYNC_REG of mmlk_meta, mmlk_sync : signal is "TRUE";
  attribute ASYNC_REG of pb_meta,   pb_sync   : signal is "TRUE";

  -- Liveness heartbeat (clk_sys).
  signal hb_div : unsigned(26 downto 0) := (others => '0');

  -- EMIO GPIO[0] = Linux-owned RTL8211E reset (eth_rst_b).
  --   GPIO_T(0) is the PS direction (1=input/PS-not-driving at boot, 0=output).
  --   GPIO_O(0) is the PS output value (resets to 0).
  -- BOOT-HIGH mux: while the PS hasn't taken the line (GPIO_T='1', the power-up
  -- default), drive eth_rst_b HIGH so the PHY is RELEASED at power-up (matches the
  -- proven static-high behaviour — the PHY needs to be out of reset for MDIO). Once
  -- Linux drives the EMIO GPIO as an output (GPIO_T='0', e.g. the mdio bus
  -- reset-gpios pulse), eth_rst_b follows GPIO_O(0). GPIO_I(0) loops eth_rst_b back
  -- so Linux can READ the actual reset level (devmem GPIO bank2 DATA_2_RO bit0) —
  -- the prior build grounded GPIO_I and was blind to the line state.
  signal gpio_o_w     : std_logic_vector(0 downto 0);
  signal gpio_i_w     : std_logic_vector(0 downto 0);
  signal gpio_t_w     : std_logic_vector(0 downto 0);
  signal eth_rst_int  : std_logic;

begin

  eth_rst_int  <= '1' when gpio_t_w(0) = '1' else gpio_o_w(0);  -- boot-high, else PS-driven
  gpio_i_w(0)  <= eth_rst_int;                                  -- readback to PS (GPIO_I)

  rst_sys <= not aresetn(0);

  ---------------------------------------------------------------------------
  -- 200 MHz REFCLK for IDELAYCTRL. 100 MHz × MULT(10) = 1000 MHz VCO,
  -- ÷ DIVIDE(5) = 200 MHz. Feedback + output through BUFGs.
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
      RECONFIG       => eye_reconfig,   -- freq-change-gated (NOT per-START) — see signal decl
      CLK_OUT        => eye_clk,        -- CLKOUT0 → TX bit-clock (eye_engine CLK_TX)
      SAMPLE_CLK_OUT => sample_clk,     -- CLKOUT1 → phase-shifted RX sample clk (CLK_RX)
      LOCKED         => eye_mmcm_locked,
      BUSY           => eye_mmcm_busy,
      PS_EN          => ps_en_pulse,    -- 1-cycle clk_sys strobe per phase step
      PS_INCDEC      => eye_ps_incdec,  -- EYE_CONTROL[17]
      PS_DONE        => ps_done
    );

  ---------------------------------------------------------------------------
  -- EYE_CONTROL / EYE_BIT_TARGET decode.
  ---------------------------------------------------------------------------
  eye_control_w  <= regs_out_local(C_INDEX_EYE_CONTROL    / 4).data;
  eye_bit_target <= regs_out_local(C_INDEX_EYE_BIT_TARGET / 4).data;

  eye_start     <= eye_control_w(0);
  eye_aggr_en   <= eye_control_w(1);
  selftest_en   <= eye_control_w(2);   -- internal TX→RX loopback (bypass PMOD+IDELAY)
  eye_phase_sel <= eye_control_w(9 downto 4);
  eye_freq_sel  <= eye_control_w(15 downto 12);
  idelay_tap    <= eye_phase_sel(4 downto 0);
  -- DS-P3.118 sample-clock phase-shift command (clk_sys = PSCLK domain).
  eye_ps_step   <= eye_control_w(16);  -- 0→1 = advance one MMCM fine step
  eye_ps_incdec <= eye_control_w(17);  -- 1 = advance, 0 = retard
  eye_ps_rst    <= eye_control_w(18);  -- 0→1 = zero the fabric step counter

  -- LD pulse (idelay tap load) on the rising edge of EYE_CONTROL[0] (start), and
  -- freq_sel tracking so the MMCM reconfigs only when the requested freq changes.
  p_start_edge : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        start_d_r     <= '0';
        freq_sel_prev <= (others => '0');
      else
        start_d_r     <= eye_start;
        freq_sel_prev <= eye_freq_sel;
      end if;
    end if;
  end process;
  eye_ld_pulse <= eye_start and not start_d_r;
  -- Single clk_sys-cycle pulse when freq_sel changes (freq_sel_prev catches up
  -- the next cycle). At a constant freq (e.g. 0) this stays low → no reconfig.
  eye_reconfig <= '1' when eye_freq_sel /= freq_sel_prev else '0';

  ---------------------------------------------------------------------------
  -- DS-P3.118 sample-clock phase-shift control (clk_sys = PSCLK domain).
  --   ps_en_pulse : a clean 1-cycle PSEN strobe on the rising edge of
  --                 EYE_CONTROL.ps_step, suppressed while a step is in flight
  --                 (the MMCM needs PSDONE before the next PSEN).
  --   ps_busy_r   : set on the strobe, cleared on PSDONE — the host polls it.
  --   ps_count_r  : net applied steps (+1 advance / −1 retard per PSDONE). Cleared
  --                 on ps_rst OR on a freq reconfig (the relock re-zeroes the
  --                 real sample phase, so the count must follow).
  ---------------------------------------------------------------------------
  ps_en_pulse <= eye_ps_step and not ps_step_d_r and not ps_busy_r;

  p_ps_ctrl : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        ps_step_d_r <= '0';
        ps_rst_d_r  <= '0';
        ps_busy_r   <= '0';
        ps_count_r  <= (others => '0');
      else
        ps_step_d_r <= eye_ps_step;
        ps_rst_d_r  <= eye_ps_rst;

        if eye_reconfig = '1' or (eye_ps_rst = '1' and ps_rst_d_r = '0') then
          ps_count_r <= (others => '0');
        elsif ps_done = '1' then
          if eye_ps_incdec = '1' then
            ps_count_r <= ps_count_r + 1;
          else
            ps_count_r <= ps_count_r - 1;
          end if;
        end if;

        if ps_en_pulse = '1' then
          ps_busy_r <= '1';
        elsif ps_done = '1' then
          ps_busy_r <= '0';
        end if;
      end if;
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- CDC: START (clk_sys) → eye_clk. 2-FF level synchroniser; the eye_engine's
  -- own edge-detector then turns the clean synced level into its run pulse.
  ---------------------------------------------------------------------------
  p_start_cdc : process(eye_clk)
  begin
    if rising_edge(eye_clk) then
      eye_start_meta <= eye_start;
      eye_start_sync <= eye_start_meta;
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- CDC: eye_engine status + counters (eye_clk) → clk_sys. Single-bit status
  -- through 2-FF syncs; the 48-bit counters double-registered. Software only
  -- reads the counters after polling STATUS.done (synced), by which point the
  -- eye_engine has frozen them for many cycles → the registered copy is the
  -- settled value (a quasi-static / multi-cycle crossing, no Gray code needed).
  ---------------------------------------------------------------------------
  p_status_cdc : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      busy_meta <= eye_busy;   busy_sync <= busy_meta;
      done_meta <= eye_done;   done_sync <= done_meta;
      lock_meta <= eye_locked; lock_sync <= lock_meta;
      mmlk_meta <= eye_mmcm_locked; mmlk_sync <= mmlk_meta;
      err_cnt_s1 <= eye_err_count; err_cnt_s2 <= err_cnt_s1;
      bit_cnt_s1 <= eye_bit_count; bit_cnt_s2 <= bit_cnt_s1;
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- CDC: REA probe bundle (eye_clk) → clk_sys, so the analyzer samples in the
  -- stable system domain (MMCM-independent — survives a freq reconfig). At
  -- freq_sel=0 eye_clk≈clk_sys so the LFSRs still read as "rotating"; at higher
  -- profiles the capture aliases but the quantitative eye is the BER counters,
  -- not the REA. Bundle order mirrors the rea_probe packing below.
  ---------------------------------------------------------------------------
  pb_raw <= rx_to_engine & rx_delayed_w & aggr_bit_w & tx_bit_w & eye_rx_sr & eye_tx_sr;
  p_probe_cdc : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      pb_meta <= pb_raw;
      pb_sync <= pb_meta;
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- eye_engine — TX gen + FSM on the swept eye_clk (CLK_TX); RX checker on the
  -- phase-shifted sample_clk (CLK_RX). The two are frequency-locked (shared MMCM
  -- VCO); the engine's internal run_en/core_rst CDC crosses eye_clk→sample_clk.
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
      CLK_RX       => sample_clk,
      RST          => rst_sys,
      START        => eye_start_sync,   -- CDC: clk_sys START, 2-FF synced into eye_clk
      BIT_TARGET   => eye_bit_target,   -- quasi-static: stable before START rises
      AGGRESSOR_EN => eye_aggr_en,
      TX_O         => tx_bit_w,
      RX_I         => rx_to_engine,
      AGGR_O       => aggr_bit_w,
      BUSY         => eye_busy,
      DONE         => eye_done,
      LOCKED       => eye_locked,
      ERR_COUNT    => eye_err_count,
      BIT_COUNT    => eye_bit_count,
      DBG_TX_SR    => eye_tx_sr,
      DBG_RX_SR    => eye_rx_sr
    );

  -- LOOPBACK_SELFTEST (EYE_CONTROL[2]): feed the TX PRBS straight back into the
  -- checker, bypassing the PMOD pins + IDELAY. Proves the eye_engine/PRBS/checker
  -- in isolation — if this LOCKS but the real path doesn't, the fault is purely
  -- the external V15↔W15 jumper / IO ring; if even this won't lock, the engine
  -- itself is broken. Default (bit=0) is the real post-IDELAY loopback.
  rx_to_engine <= tx_bit_w when selftest_en = '1' else rx_delayed_w;

  ---------------------------------------------------------------------------
  -- REA (RouteRTL Embedded Analyzer) — JTAG-side on-chip capture of the eye
  -- datapath so the TX/RX LFSRs, the serial loopback bits and the lock/run
  -- status are watchable LIVE in RouteWave (`rr ila capture --core u_eye_rea`
  -- → `stream attach`). Sampled on clk_sys (FCLK, the system plane) via a 2-FF
  -- sync of the eye_clk-domain bits, so the analyzer is MMCM-independent and
  -- keeps capturing across a frequency reconfig. Probe layout (LSB-first) MUST
  -- mirror debug/u_eye_rea.yml. Assumes G_PRBS_ORDER <= 7.
  ---------------------------------------------------------------------------
  -- Bit-domain taps come from the 2-FF-synced bundle pb_sync (see p_probe_cdc);
  -- order: [2G-1:0]=tx_sr,rx_sr  [2G]=tx_bit  [2G+1]=aggr  [2G+2]=rx_delayed
  -- [2G+3]=rx_to_eng (G=G_PRBS_ORDER, assumed ≤7 so the serial bits land at 14+).
  rea_probe(2*G_PRBS_ORDER-1 downto 0) <= pb_sync(2*G_PRBS_ORDER-1 downto 0);
  rea_probe(14)           <= pb_sync(2*G_PRBS_ORDER);     -- serial TX bit (→ V15)
  -- NB: do NOT tap the raw pre-IDELAY rx_bit_w — that IBUF output feeds the
  -- IDELAYE2 over a dedicated IOB→IODELAY route and is not freely fabric-routable
  -- (Vivado: "jc_IBUF[4] is not completely routed"). The post-IDELAY rx_delayed_w
  -- (bit 16) carries the same returned bit; bit 15 shows the aggressor instead.
  rea_probe(15)           <= pb_sync(2*G_PRBS_ORDER+1);   -- aggressor PRBS bit (→ T11)
  rea_probe(16)           <= pb_sync(2*G_PRBS_ORDER+2);   -- post-IDELAY RX
  rea_probe(17)           <= pb_sync(2*G_PRBS_ORDER+3);   -- bit the checker sees
  rea_probe(18)           <= busy_sync;        -- eye status (synced to clk_sys)
  rea_probe(19)           <= done_sync;
  rea_probe(20)           <= lock_sync;
  rea_probe(21)           <= selftest_en;      -- clk_sys native
  rea_probe(22)           <= eye_start;        -- clk_sys native
  rea_probe(23)           <= mmlk_sync;        -- swept-clock MMCM lock (synced)
  rea_probe(24)           <= eye_mmcm_busy;    -- MMCM DRP reconfig (clk_sys / DCLK domain)
  rea_probe(25)           <= ps_busy_r;        -- DS-P3.118: sample-clock PS step in flight
  rea_probe(31 downto 26) <= (others => '0');

  u_eye_rea : entity rr_rea.rr_rea_xilinx7
    generic map (
      G_SAMPLE_W    => 32,
      G_DEPTH       => 4096,
      G_TIMESTAMP_W => 32,
      G_NUM_CHAN    => 1,
      G_CTRL_CHAIN  => 1
    )
    port map (
      sample_clk_i   => clk_sys,   -- system plane: MMCM-independent, survives reconfig
      sample_rst_i   => rst_sys,
      probe_i        => rea_probe,
      ext_trigger_i  => '0',
      trigger_o      => open,
      tap_tck        => open,
      tap_tms        => open,
      tap_tdi        => open,
      tap_tdo        => open
    );

  ---------------------------------------------------------------------------
  -- idelay_scan (xilinx arch) — phase/timing axis on the RX loopback bit.
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
  -- PMOD JC loopback — Classic `zybo` pinout: jc[0]=V15(TX), jc[4]=W15(RX),
  -- jc[1]=T11(AGGR). All other JC pins parked high-Z.
  ---------------------------------------------------------------------------
  jc(0)    <= tx_bit_w;       -- TX  (V15, JC1_P) → jumper → jc[4]
  rx_bit_w <= jc(4);          -- RX  (W15, JC1_N) ← jumper from jc[0]
  jc(1)    <= aggr_bit_w;     -- AGGR (T11, JC2_P)
  jc(2)    <= 'Z';
  jc(3)    <= 'Z';
  jc(5)    <= 'Z';
  jc(6)    <= 'Z';
  jc(7)    <= 'Z';

  -- Onboard RTL8211E reset, now LINUX-OWNED via EMIO GPIO[0] (was a static '1').
  -- Linux pulses GPIO_O(0) through the mdio bus reset-gpios (DT) at macb probe:
  -- a clean assert-low → hold → deassert-high pulse, which a static high never
  -- gave — that left the PHY in whatever state it powered up in (E17 floats until
  -- the fabric configures), and it never re-latched → "Could not attach PHY (-22)".
  -- Boot-high (GPIO_T='1' at power-up) releases the PHY immediately; once Linux
  -- drives the EMIO GPIO as output it follows GPIO_O(0). See eth_rst_int above.
  eth_rst_b <= eth_rst_int;

  ---------------------------------------------------------------------------
  -- Regbank RO telemetry: EYE_STATUS + 48-bit ERR/BIT counters (lo/hi).
  ---------------------------------------------------------------------------
  -- All four telemetry sources are the clk_sys-synced copies (see p_status_cdc):
  -- single-bit status 2-FF synced, the 48-bit counters double-registered (read
  -- by SW only after STATUS.done, when they are frozen and stable).
  telemetry_in(C_INDEX_EYE_STATUS / 4).data  <=
      (31 downto 3 => '0') & lock_sync & done_sync & busy_sync;
  telemetry_in(C_INDEX_EYE_STATUS / 4).valid <= '1';

  telemetry_in(C_INDEX_EYE_ERR_COUNT_LO / 4).data  <= err_cnt_s2(31 downto 0);
  telemetry_in(C_INDEX_EYE_ERR_COUNT_LO / 4).valid <= '1';
  telemetry_in(C_INDEX_EYE_ERR_COUNT_HI / 4).data  <=
      std_logic_vector(resize(unsigned(err_cnt_s2(G_CNT_W-1 downto 32)), 32));
  telemetry_in(C_INDEX_EYE_ERR_COUNT_HI / 4).valid <= '1';

  telemetry_in(C_INDEX_EYE_BIT_COUNT_LO / 4).data  <= bit_cnt_s2(31 downto 0);
  telemetry_in(C_INDEX_EYE_BIT_COUNT_LO / 4).valid <= '1';
  telemetry_in(C_INDEX_EYE_BIT_COUNT_HI / 4).data  <=
      std_logic_vector(resize(unsigned(bit_cnt_s2(G_CNT_W-1 downto 32)), 32));
  telemetry_in(C_INDEX_EYE_BIT_COUNT_HI / 4).valid <= '1';

  -- DS-P3.118 sample-clock phase status: [15:0]=ps_count (signed net steps),
  -- [16]=ps_busy. Native clk_sys (PSCLK) domain — no CDC needed.
  telemetry_in(C_INDEX_EYE_PHASE_STATUS / 4).data  <=
      (31 downto 17 => '0') & ps_busy_r & std_logic_vector(ps_count_r);
  telemetry_in(C_INDEX_EYE_PHASE_STATUS / 4).valid <= '1';

  ---------------------------------------------------------------------------
  -- system_regbank_wrapper — AXI-Lite slave on M_AXI_GP0 (11-bit window).
  -- DATALOGGER_CONFIG unused on the lean eye target (no datalogger fabric).
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
  -- classic_eye_bd — lean PS7 subsystem. M_AXI_GP0 drives the regbank;
  -- DDR/FIXED_IO propagate to PS pins; fclk_clk0/aresetn clock the fabric.
  ---------------------------------------------------------------------------
  u_bd : component classic_eye_bd
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
      GPIO_O            => gpio_o_w,
      GPIO_I            => gpio_i_w,
      GPIO_T            => gpio_t_w,
      fclk_clk0         => clk_sys,
      fclk_aresetn      => aresetn
    );

  ---------------------------------------------------------------------------
  -- Liveness heartbeat + LED status.
  --   led[0] = ~1.5 Hz blink (clk_sys alive)
  --   led[1] = ref_locked       (200 MHz IDELAYCTRL refclk locked)
  --   led[2] = idelay_rdy        (IDELAYCTRL calibrated)
  --   led[3] = eye_mmcm_locked   (swept-clock MMCM locked)
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
