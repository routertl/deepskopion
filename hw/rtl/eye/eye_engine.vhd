-- rr unknown-llm (AI-generated)
--------------------------------------------------------------------------------
-- File       : eye_engine.vhd
-- Project    : deepskopion — eye-scan / BERT instrument (DS-P2.66, DS-P3.118)
-- Purpose    : One-point BERT measurement engine. On a START pulse it runs a
--              PRBS over the PMOD loopback for BIT_TARGET bit periods, counts
--              received bit errors via the self-synchronising checker, freezes
--              the counters and raises DONE. The PS daemon sets the (freq,phase)
--              axes (clk_drp / MMCM phase-shift) AROUND this block, calls START
--              per grid point, and reads ERR/BIT to get that point's BER — the
--              cell of the bathtub / eye.
--
-- ── DUAL-CLOCK split (DS-P3.118) ──────────────────────────────────────────────
-- The TX generator and the RX checker now run on SEPARATE, frequency-locked but
-- independently phase-shiftable clocks:
--   CLK_TX — the swept TX bit-clock (MMCM CLKOUT0). Drives prbs_gen, the
--            aggressor gen, and the measurement FSM.
--   CLK_RX — the sample clock (MMCM CLKOUT1, same divide, dynamic PHASE-SHIFT).
--            Drives prbs_check, which samples RX_I on CLK_RX's edge.
-- Sweeping CLK_RX's phase across a full UI walks the sample point through the bit
-- period → a REAL eye (the IDELAY 2.5 ns tap covered <½ UI above 125 MHz and slid
-- with frequency, the DS-P3.118 trap). Because the two clocks share the MMCM VCO
-- they are frequency-locked; only their phase offset moves.
--
-- Why this is correct & self-synchronising: prbs_check predicts the next bit from
-- the RECEIVED history alone, so it locks regardless of the TX↔RX phase offset or
-- loopback latency — exactly the property that lets ONE engine measure every
-- phase. The only cross-domain signals are the run-window controls (run_en,
-- core_rst); both are wide LEVELS 2-FF-synchronised into CLK_RX (see P_RxCdc).
-- The counters are read after DONE when frozen (quasi-static), then 2-FF-synced
-- to clk_sys at the top.
--
-- Loopback seam: TX_O drives the PMOD OBUF; RX_I is the post-IDELAY sampled
--   return bit. In sim the testbench wires TX_O→RX_I (optionally injecting
--   errors / forcing a dead line) and clocks CLK_RX at a chosen phase offset.
--
-- Adversarial safety (bug-finding-is-success): a dead/disconnected loopback
--   never locks → BIT_COUNT stays 0 → the host reads "no measurement", NOT a
--   false BER=0. The engine cannot manufacture a green point.
--
-- Standard   : VHDL-2019 (IEEE 1076-2019)
-- Licence    : CERN-OHL-W-2.0
--------------------------------------------------------------------------------

library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

library tich;

entity eye_engine is
  generic (
    G_ORDER     : positive := 7;    -- PRBS order for the measured link (7/15/23/31)
    G_AGGR_ORDER: positive := 15;   -- aggressor PRBS order — DIFFERENT poly → uncorrelated
    G_LOCK_OK   : positive := 32;   -- checker consecutive-match lock threshold
    G_CNT_W     : positive := 48    -- error/bit counter width
  );
  port (
    CLK_TX       : in  std_logic;   -- TX bit clock (swept MMCM CLKOUT0): gen + FSM
    CLK_RX       : in  std_logic;   -- RX sample clock (MMCM CLKOUT1, phase-shifted): checker
    RST          : in  std_logic;   -- sync, active-high (applied in both domains)

    -- Control (from EYE_CONTROL / EYE_BIT_TARGET regbank slots) — CLK_TX domain ---
    START        : in  std_logic;   -- rising edge launches one measurement
    BIT_TARGET   : in  std_logic_vector(31 downto 0);  -- PRBS bit periods to drive
    AGGRESSOR_EN : in  std_logic;   -- drive AGGR_O with an uncorrelated PRBS (crosstalk)

    -- Loopback seam ------------------------------------------------------------
    TX_O         : out std_logic;   -- PRBS bit → PMOD OBUF              (CLK_TX)
    RX_I         : in  std_logic;   -- sampled loopback bit ← post-IDELAY IBUF (CLK_RX)
    AGGR_O       : out std_logic;   -- aggressor PRBS → adjacent PMOD pin (CLK_TX)

    -- Status (to EYE_STATUS / EYE_*_COUNT regbank slots) -----------------------
    BUSY         : out std_logic;                          -- CLK_TX (FSM)
    DONE         : out std_logic;                          -- CLK_TX (FSM)
    LOCKED       : out std_logic;                          -- CLK_RX (checker)
    ERR_COUNT    : out std_logic_vector(G_CNT_W-1 downto 0);  -- CLK_RX
    BIT_COUNT    : out std_logic_vector(G_CNT_W-1 downto 0);  -- CLK_RX

    -- REA debug taps: the live TX-generator (CLK_TX) and RX-checker (CLK_RX) LFSR
    -- shift registers, for on-chip capture. In a healthy loopback (or self-test)
    -- the recovered RX LFSR aligns with the TX LFSR once locked; on a dead line
    -- they diverge. (Both are re-synchronised to clk_sys at the top for the REA.)
    DBG_TX_SR    : out std_logic_vector(G_ORDER-1 downto 0);
    DBG_RX_SR    : out std_logic_vector(G_ORDER-1 downto 0)
  );
end entity eye_engine;

architecture rtl of eye_engine is

  type state_t is (S_IDLE, S_ARM, S_RUN, S_DONE);
  signal state_r : state_t := S_IDLE;

  signal start_d   : std_logic := '0';
  signal start_pls : std_logic;

  signal run_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal core_rst  : std_logic := '1';   -- TX-side PRBS gen reset (held during S_ARM)
  signal run_en    : std_logic;          -- EN to the measured PRBS gen+check window
  signal aggr_en   : std_logic;          -- EN to the aggressor PRBS gen

  -- S_ARM is widened to C_ARM_CYCLES so that core_rst is a LEVEL several CLK_TX
  -- cycles wide. That guarantees the 2-FF synchroniser into CLK_RX captures the
  -- per-run clear (a 1-cycle pulse could be missed across the domain) and that
  -- the checker is cleanly reset before the run begins. The cost is a handful of
  -- idle cycles per measurement — negligible against BIT_TARGET (thousands).
  constant C_ARM_CYCLES : positive := 8;
  signal   arm_cnt_r    : integer range 0 to C_ARM_CYCLES := 0;

  signal tx_bit    : std_logic;
  signal aggr_bit  : std_logic;
  signal done_r    : std_logic := '0';

  -- CLK_RX-domain copies of the run-window controls (2-FF synchronised).
  signal run_en_meta,  run_en_rx  : std_logic := '0';
  signal core_rst_meta, core_rst_rx : std_logic := '1';
  signal chk_rst   : std_logic;

  -- Keep each 2-FF synchroniser pair packed + recognised by report_cdc. The first
  -- (meta) stage is the async capture; the XDC false-paths its D pin. (Ignored by
  -- nvc/ghdl — a user attribute string.)
  attribute ASYNC_REG : string;
  attribute ASYNC_REG of run_en_meta,   run_en_rx    : signal is "TRUE";
  attribute ASYNC_REG of core_rst_meta, core_rst_rx  : signal is "TRUE";

begin

  -- One measurement per 0→1 transition of START (CSR-pulse idiom: write 0 then
  -- 1 to re-arm; a held-high START does not retrigger).
  start_pls <= START and not start_d;

  -- EN is high only while measuring; core_rst clears the PRBS state in S_ARM.
  run_en  <= '1' when state_r = S_RUN else '0';
  aggr_en <= run_en and AGGRESSOR_EN;

  ---------------------------------------------------------------------------
  -- TX plane (CLK_TX): PRBS generator + aggressor + measurement FSM.
  ---------------------------------------------------------------------------

  -- TX-side PRBS generator (the link stimulus).
  u_tx_gen : entity tich.prbs_gen
    generic map ( G_ORDER => G_ORDER )
    port map (
      CLK    => CLK_TX,
      RST    => core_rst,
      EN     => run_en,
      DOUT   => tx_bit,
      DBG_SR => DBG_TX_SR
    );

  -- Aggressor PRBS — different order ⇒ different sequence ⇒ uncorrelated crosstalk.
  u_aggr_gen : entity tich.prbs_gen
    generic map ( G_ORDER => G_AGGR_ORDER )
    port map (
      CLK  => CLK_TX,
      RST  => core_rst,
      EN   => aggr_en,
      DOUT => aggr_bit
    );

  TX_O   <= tx_bit;
  AGGR_O <= aggr_bit when AGGRESSOR_EN = '1' else '0';
  BUSY   <= '1' when (state_r = S_ARM or state_r = S_RUN) else '0';
  DONE   <= done_r;

  P_Fsm : process (CLK_TX)
  begin
    if rising_edge(CLK_TX) then
      start_d  <= START;

      if RST = '1' then
        state_r   <= S_IDLE;
        run_cnt_r <= (others => '0');
        arm_cnt_r <= 0;
        core_rst  <= '1';
        done_r    <= '0';
      else
        case state_r is

          when S_IDLE =>
            core_rst <= '0';
            if start_pls = '1' then
              run_cnt_r <= unsigned(BIT_TARGET);
              arm_cnt_r <= 0;
              core_rst  <= '1';          -- hold the PRBS gen + checker in reset
              done_r    <= '0';
              state_r   <= S_ARM;
            end if;

          when S_ARM =>
            -- Hold core_rst for C_ARM_CYCLES so the clear LEVEL is wide enough to
            -- cross safely into CLK_RX, then start driving.
            core_rst <= '1';
            if arm_cnt_r = C_ARM_CYCLES - 1 then
              core_rst <= '0';
              if run_cnt_r = 0 then
                state_r <= S_DONE;       -- BIT_TARGET = 0 → defined no-op
              else
                state_r <= S_RUN;
              end if;
            else
              arm_cnt_r <= arm_cnt_r + 1;
            end if;

          when S_RUN =>
            -- Drive exactly BIT_TARGET bit periods, then freeze. The checker
            -- accumulates ERR/BIT only post-lock, so the fill+align phase is
            -- never miscounted; BIT_COUNT < BIT_TARGET by the lock latency.
            core_rst <= '0';
            if run_cnt_r = 1 then
              state_r <= S_DONE;
            end if;
            run_cnt_r <= run_cnt_r - 1;

          when S_DONE =>
            -- run_en is low ⇒ the checker holds its frozen counters (core_rst is
            -- low too, so they are NOT cleared). Latch DONE; wait for the next
            -- START pulse to re-arm.
            core_rst <= '0';
            done_r   <= '1';
            if start_pls = '1' then
              run_cnt_r <= unsigned(BIT_TARGET);
              arm_cnt_r <= 0;
              core_rst  <= '1';
              done_r    <= '0';
              state_r   <= S_ARM;
            end if;

        end case;
      end if;
    end if;
  end process P_Fsm;

  ---------------------------------------------------------------------------
  -- CDC: run-window controls (CLK_TX) → CLK_RX. Both are wide levels:
  --   run_en   : high for the whole BIT_TARGET window (≥ thousands of cycles)
  --   core_rst : high for C_ARM_CYCLES at the start of every run
  -- 2-FF synchronise each. The checker is reset by (RST or core_rst_rx) and
  -- enabled by run_en_rx — so it clears at S_ARM and FREEZES (EN low, no reset)
  -- when run_en drops at S_DONE, leaving the counters valid to read.
  ---------------------------------------------------------------------------
  P_RxCdc : process (CLK_RX)
  begin
    if rising_edge(CLK_RX) then
      run_en_meta   <= run_en;     run_en_rx   <= run_en_meta;
      core_rst_meta <= core_rst;   core_rst_rx <= core_rst_meta;
    end if;
  end process P_RxCdc;

  chk_rst <= RST or core_rst_rx;

  ---------------------------------------------------------------------------
  -- RX plane (CLK_RX): self-synchronising checker — counts bit errors on the
  -- returned link, sampling RX_I on the phase-shifted sample clock.
  ---------------------------------------------------------------------------
  u_rx_chk : entity tich.prbs_check
    generic map (
      G_ORDER   => G_ORDER,
      G_LOCK_OK => G_LOCK_OK,
      G_CNT_W   => G_CNT_W
    )
    port map (
      CLK         => CLK_RX,
      RST         => chk_rst,
      EN          => run_en_rx,
      RX          => RX_I,
      LOCKED      => LOCKED,
      ERROR_COUNT => ERR_COUNT,
      BIT_COUNT   => BIT_COUNT,
      DBG_RXSR    => DBG_RX_SR
    );

end architecture rtl;
