-- rr unknown-llm (AI-generated)
--------------------------------------------------------------------------------
-- File       : clk_mmcm_xilinx.vhd  (silicon frequency + sample-phase axis)
-- Project    : deepskopion — eye-scan / BERT instrument (DS-P2.66, DS-P3.118)
-- Purpose    : The real 7-series frequency AND sample-phase axes of the eye.
--              Pairs the vendor-neutral DRP sequencer (clk_drp) with an
--              MMCME2_ADV so a FREQ_SEL + RECONFIG pulse reprograms BOTH outputs
--              and relocks:
--                CLKOUT0 = CLK_OUT      → eye_clk    (TX bit-clock; eye_engine TX)
--                CLKOUT1 = SAMPLE_CLK_OUT → sample_clk (RX checker clock)
--              CLKOUT1 carries CLKOUT1_USE_FINE_PS = TRUE, so PS_EN/PS_INCDEC step
--              its phase (≈ VCO_period/56 ≈ 17.86 ps at the fixed 1000 MHz VCO)
--              WITHOUT moving CLKOUT0 — sweeping it walks the RX sample point
--              across a full UI (the DS-P3.118 full-eye axis the 2.5 ns IDELAY
--              tap could not span above ~125 MHz).
--
-- Both outputs share the VCO ⇒ they are FREQUENCY-LOCKED; clk_drp now writes the
-- CLKOUT1 counter registers (0x0A/0x0B) too so sample_clk tracks eye_clk across
-- the sweep. A frequency RECONFIG resets the MMCM, so the CLKOUT1 phase returns
-- to its nominal (attribute) value — i.e. every freq change re-zeroes the sample
-- phase (the host re-sweeps from 0).
--
-- SYNTH-ONLY: carries `library unisim` → routertl drops it from the nvc sim/lint
-- compile and binds it only for Vivado synth. The DRP *sequencer* it wraps
-- (clk_drp) is vendor-neutral and fully sim-proven (test_clk_drp / tb_clk_drp);
-- the dual-output + fine-PS primitive behaviour is a SILICON checkpoint (no nvc
-- MMCM model).
--
-- LOCKED is gated on BOTH the sequencer finishing AND the real MMCM relocking,
-- so the host never reads "ready" before the new frequency is actually stable.
--
-- Phase-shift handshake (caller's contract): pulse PS_EN for exactly one DCLK
-- cycle with PS_INCDEC set (1=advance), then wait for PS_DONE before the next
-- step. The classic_eye_top edge-detects an EYE_CONTROL strobe into a clean
-- 1-cycle PS_EN and exposes PS_DONE + a fabric step counter to the host.
--
-- Standard   : VHDL-2019 (IEEE 1076-2019)
-- Licence    : CERN-OHL-W-2.0
--------------------------------------------------------------------------------

library ieee;
  use ieee.std_logic_1164.all;

library unisim;
  use unisim.vcomponents.all;

library tich;

entity clk_mmcm_xilinx is
  generic (
    G_LOCK_CYCLES     : positive := 8;     -- sequencer relock-wait (→ clk_drp)
    G_CLKIN_PERIOD_NS : real     := 10.0;  -- CLK_IN period (100 MHz FCLK0)
    G_CLKFBOUT_MULT   : real     := 10.0;  -- base VCO multiply
    G_CLKOUT0_DIVIDE  : real     := 10.0   -- base CLKOUT0/1 divide (→100 MHz base)
  );
  port (
    CLK_IN         : in  std_logic;                     -- reference clock (FCLK0)
    DCLK           : in  std_logic;                     -- DRP + PS + sequencer clock (clk_sys)
    RST            : in  std_logic;
    FREQ_SEL       : in  std_logic_vector(3 downto 0);
    RECONFIG       : in  std_logic;                     -- pulse → apply FREQ_SEL profile
    CLK_OUT        : out std_logic;                     -- swept TX bit-clock  (CLKOUT0, eye_clk)
    SAMPLE_CLK_OUT : out std_logic;                     -- swept RX sample clk (CLKOUT1, phase-shifted)
    LOCKED         : out std_logic;
    BUSY           : out std_logic;
    -- Dynamic fine phase-shift on CLKOUT1 (sample_clk). On DCLK.
    PS_EN          : in  std_logic;                     -- 1-cycle strobe → one phase step
    PS_INCDEC      : in  std_logic;                     -- 1 = advance, 0 = retard
    PS_DONE        : out std_logic                      -- step complete (MMCM PSDONE)
  );
end entity clk_mmcm_xilinx;

architecture xilinx of clk_mmcm_xilinx is
  -- DRP master ↔ MMCM slave
  signal daddr      : std_logic_vector(6 downto 0);
  signal di         : std_logic_vector(15 downto 0);
  signal do_w       : std_logic_vector(15 downto 0);
  signal den        : std_logic;
  signal dwe        : std_logic;
  signal drdy       : std_logic;
  signal mmcm_rst   : std_logic;             -- from sequencer, held during DRP writes
  signal drp_busy   : std_logic;
  signal drp_locked : std_logic;

  signal mmcm_rst_c : std_logic;
  signal clkfb      : std_logic;
  signal clkfb_buf  : std_logic;
  signal clk0_unbuf : std_logic;
  signal clk1_unbuf : std_logic;
  signal mmcm_lock  : std_logic;
begin

  -- Vendor-neutral DRP reconfiguration sequencer (sim-proven in test_clk_drp).
  -- It now drives a 5-access program (PowerReg + CLKOUT0 0x08/0x09 + CLKOUT1
  -- 0x0A/0x0B) so both outputs follow FREQ_SEL.
  u_drp : entity tich.clk_drp
    generic map (
      G_LOCK_CYCLES => G_LOCK_CYCLES
    )
    port map (
      CLK      => DCLK,
      RST      => RST,
      FREQ_SEL => FREQ_SEL,
      RECONFIG => RECONFIG,
      BUSY     => drp_busy,
      LOCKED   => drp_locked,
      DADDR    => daddr,
      DI       => di,
      DWE      => dwe,
      DEN      => den,
      DO       => do_w,
      DRDY     => drdy,
      MMCM_RST => mmcm_rst
    );

  -- Reset the MMCM both on global reset and while the sequencer rewrites it.
  mmcm_rst_c <= RST or mmcm_rst;

  i_mmcm : MMCME2_ADV
    generic map (
      CLKFBOUT_MULT_F    => G_CLKFBOUT_MULT,
      CLKIN1_PERIOD      => G_CLKIN_PERIOD_NS,
      CLKOUT0_DIVIDE_F   => G_CLKOUT0_DIVIDE,
      -- CLKOUT1 = the phase-shiftable sample clock at the SAME base divide.
      -- NB CLKOUT1_USE_FINE_PS is BOOLEAN in the VHDL unisim component
      -- (Vivado unisim_retarget_VCOMP.vhd) — pass TRUE, not the Verilog "TRUE".
      CLKOUT1_DIVIDE     => integer(G_CLKOUT0_DIVIDE),
      CLKOUT1_PHASE      => 0.0,
      CLKOUT1_USE_FINE_PS => TRUE
    )
    port map (
      CLKIN1    => CLK_IN,
      CLKIN2    => '0',
      CLKINSEL  => '1',
      CLKFBOUT  => clkfb,
      CLKFBIN   => clkfb_buf,
      CLKOUT0   => clk0_unbuf,
      CLKOUT1   => clk1_unbuf,
      CLKOUT2   => open,
      CLKOUT3   => open,
      CLKOUT4   => open,
      CLKOUT5   => open,
      CLKOUT6   => open,
      CLKOUT0B  => open,
      CLKOUT1B  => open,
      CLKOUT2B  => open,
      CLKOUT3B  => open,
      CLKFBOUTB => open,
      LOCKED    => mmcm_lock,
      RST       => mmcm_rst_c,
      PWRDWN    => '0',
      -- DRP — driven by the sequencer.
      DADDR     => daddr,
      DCLK      => DCLK,
      DEN       => den,
      DWE       => dwe,
      DI        => di,
      DO        => do_w,
      DRDY      => drdy,
      -- Dynamic fine phase-shift of CLKOUT1 (sample_clk). PSCLK = DCLK.
      PSCLK     => DCLK,
      PSEN      => PS_EN,
      PSINCDEC  => PS_INCDEC,
      PSDONE    => PS_DONE,
      CLKINSTOPPED => open,
      CLKFBSTOPPED => open
    );

  i_fb_bufg     : BUFG port map (I => clkfb,      O => clkfb_buf);
  i_out_bufg    : BUFG port map (I => clk0_unbuf, O => CLK_OUT);
  i_sample_bufg : BUFG port map (I => clk1_unbuf, O => SAMPLE_CLK_OUT);

  -- Ready only when the sequence is done AND the MMCM has actually relocked.
  LOCKED <= drp_locked and mmcm_lock;
  BUSY   <= drp_busy or (drp_locked and not mmcm_lock);

end architecture xilinx;
