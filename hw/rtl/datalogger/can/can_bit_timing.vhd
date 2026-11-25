-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- can_bit_timing.vhd  —  CAN / CAN-FD bit-clock recovery (DS-P3.100 v0.1,
--                        DS-P2.64 v0.2 → B2 unified direct-count dual-rate sampler)
-------------------------------------------------------------------------------
-- CAN has NO clock wire (NRZ on a single line). This block recovers where each
-- bit is and samples it at the right instant, the way a real CAN controller's
-- bit-timing logic does — simplified for a PASSIVE datalogger on a clean line.
--
-- B2 (DS-P2.64): both phases are DIRECT cycle-counters. A bit period is
-- `cyc_per_bit` clk_sys cycles; we sample at `sample_cyc` cycles into the bit.
-- There are TWO {cyc_per_bit, sample_cyc} config pairs, selected by RATE_SEL:
--   * NOMINAL (RATE_SEL='0'): the arbitration rate. Derived from the legacy
--     PRESCALER + G_NUM_TQ/G_SAMPLE_TQ interface so this block stays drop-in for
--     the silicon-validated classic-CAN datalogger:
--         cyc_per_bit = PRESCALER * G_NUM_TQ      (= old TQ count, EXACT integer)
--         sample_cyc  = PRESCALER * G_SAMPLE_TQ   (= old 75% sample point, EXACT)
--     ⇒ bit-IDENTICAL to v0.1 by construction (proven by the equivalence TB,
--       CAN-REQ-23). The old block subdivided a bit into G_NUM_TQ time quanta of
--       PRESCALER cycles each and sampled at TQ G_SAMPLE_TQ; counting cycles
--       directly to the same totals lands every BIT_START / sample on the same
--       clk edge.
--   * DATA (RATE_SEL='1'): the CAN-FD fast data phase. The pair is supplied
--     DIRECTLY (DATA_CYC_PER_BIT / DATA_SAMPLE_CYC) because the prescaler/TQ
--     subdivision is a SLOW-signal tool: a fast bit is only tens of cycles long,
--     so fine TQ placement buys nothing and the integer prescaler becomes
--     unrepresentable (100 MHz, 2 Mbit = 50 cyc/bit ⇒ presc 50/16 = 3.125). So
--     for the data phase we drop the TQ abstraction entirely and count cycles.
--
-- WHY a direct count (not two prescalers / a faster clock): FF-count is a wash
-- (log2(cyc/bit) either way); the real win is REPRESENTABILITY + honest sample
-- placement across a wide rate span on one clk_sys.  (Decision 1, CAN_FD_SCOPING.)
--
-- RESYNC: dominant='0' beats recessive='1'. Every dominant bit gives a
-- recessive->dominant ("falling") edge; we snap the bit clock to it (hard sync).
-- Bit-stuffing guarantees an edge at least every ~5 bits, so between edges we
-- free-run the counter; same-clock drift is negligible. A passive datalogger has no
-- SJW constraint, so snapping on every falling edge is simplest and robust.
--
-- FORCED RESYNC on the RATE_SEL edge (CAN-REQ-14): at the BRS→data (and
-- data→nominal at the CRC delimiter) transition there is NO falling edge to lock
-- to at the switch instant, so a RATE_SEL change, while synced, re-anchors cnt and
-- loads the new pair.
--   * nominal→data (DS-T2.97): ISO 11898-1 switches AT the sample point of the BRS
--     bit, and that instant is the data-rate sample point of the same bit; the rest
--     of BRS is the data phase-2 segment. So cnt := DATA_SAMPLE_CYC + (clocks since
--     the nominal sample), and the first data bit (ESI) starts DATA_CYC_PER_BIT -
--     DATA_SAMPLE_CYC later, exactly where the transmitter starts it. (cnt := 0 put
--     the ESI sample one data phase-2 early; only an ESI falling edge hid it.)
--   * data→nominal (CRC delimiter): cnt := 0, anchored to the delimiter.
-- The nominal sample point is NOMINAL_SAMPLE_CYC when non-zero (software-set to the
-- bus's own sample point, DS-T2.97), else G_SAMPLE_TQ * PRESCALER.
--
-- OUT: BIT_VALID pulses 1 clk at the sample point with BIT_VALUE = the sampled
-- level; the de-stuff stage (Block 2) consumes that one-bit-per-strobe stream.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity can_bit_timing is
  generic (
    G_NUM_TQ    : positive := 16;   -- nominal: cyc_per_bit = G_NUM_TQ * PRESCALER
    G_SAMPLE_TQ : positive := 12;   -- nominal: sample_cyc  = G_SAMPLE_TQ * PRESCALER (75% of 16)
    G_PRESC_W   : positive := 10;   -- PRESCALER width (clk cycles per TQ, nominal)
    G_CNT_W     : positive := 16    -- direct cycle-counter width (>= bits to hold max cyc_per_bit)
  );
  port (
    CLK       : in  std_logic;
    RST       : in  std_logic;
    ENABLE    : in  std_logic;                            -- gate (CONFIG enable)
    RX        : in  std_logic;                            -- CAN line (dom='0'/rec='1')
    PRESCALER : in  std_logic_vector(G_PRESC_W-1 downto 0); -- nominal clk cycles per TQ (>=1)
    -- ── CAN-FD dual-rate (DS-P2.64) — all DEFAULTED so the classic instantiation
    --    is unchanged (RATE_SEL='0' ⇒ pure nominal, exactly the v0.1 behaviour) ──
    RATE_SEL         : in std_logic := '0';                            -- 0=nominal, 1=data phase
    DATA_CYC_PER_BIT : in std_logic_vector(G_CNT_W-1 downto 0) := (others => '0'); -- data: bit period (cycles)
    DATA_SAMPLE_CYC  : in std_logic_vector(G_CNT_W-1 downto 0) := (others => '0'); -- data: sample offset (cycles)
    -- DS-T2.97: nominal sample offset (cycles); 0 = G_SAMPLE_TQ * PRESCALER.
    NOMINAL_SAMPLE_CYC : in std_logic_vector(G_CNT_W-1 downto 0) := (others => '0');
    BIT_VALID : out std_logic;                            -- 1-clk strobe at the sample point
    BIT_VALUE : out std_logic;                            -- RX sampled at the sample point
    BIT_START : out std_logic;                            -- 1-clk strobe at each bit start (cnt=0)
    SYNCED    : out std_logic                             -- locked (>=1 falling edge seen)
  );
end entity can_bit_timing;

architecture rtl of can_bit_timing is
  -- 2-FF input synchroniser (RX is async) + 1 delayed copy for edge detect.
  -- Reset/idle level is recessive '1' (CAN idle is recessive).  UNCHANGED from
  -- v0.1 — the metastability-sensitive front-end is NOT touched by B2, so silicon
  -- risk is not reopened; only the deterministic counter below changed.
  signal rx_m, rx_s, rx_d : std_logic := '1';
  signal cnt      : unsigned(G_CNT_W-1 downto 0) := (others => '0');  -- cycles since this bit's start
  signal synced_r : std_logic := '0';
  signal rate_d   : std_logic := '0';                                -- RATE_SEL delayed, for edge detect

  signal presc_u  : unsigned(G_PRESC_W-1 downto 0);
  -- Nominal pair, derived from PRESCALER (combinational). Width product fits in
  -- G_CNT_W as long as G_NUM_TQ * 2^G_PRESC_W <= 2^G_CNT_W (16 * 2^10 = 2^14, ok @16).
  signal nom_cyc_per_bit : unsigned(G_CNT_W-1 downto 0);
  signal nom_sample_cyc  : unsigned(G_CNT_W-1 downto 0);
  -- Active pair, muxed by RATE_SEL.
  signal cur_cyc_per_bit : unsigned(G_CNT_W-1 downto 0);
  signal cur_sample_cyc  : unsigned(G_CNT_W-1 downto 0);
begin
  presc_u <= unsigned(PRESCALER);
  SYNCED  <= synced_r;

  -- Nominal pair = exact integer multiples of the prescaler (no rounding), so the
  -- direct count reaches BIT_START / sample on the SAME clk edge as the old TQ
  -- accumulator did (CAN-REQ-23).
  nom_cyc_per_bit <= resize(presc_u * to_unsigned(G_NUM_TQ,    G_CNT_W), G_CNT_W);
  nom_sample_cyc  <= unsigned(NOMINAL_SAMPLE_CYC) when unsigned(NOMINAL_SAMPLE_CYC) /= 0
                     else resize(presc_u * to_unsigned(G_SAMPLE_TQ, G_CNT_W), G_CNT_W);

  cur_cyc_per_bit <= unsigned(DATA_CYC_PER_BIT) when RATE_SEL = '1' else nom_cyc_per_bit;
  cur_sample_cyc  <= unsigned(DATA_SAMPLE_CYC)  when RATE_SEL = '1' else nom_sample_cyc;

  process(CLK)
    variable falling   : boolean;
    variable rate_edge : boolean;
  begin
    if rising_edge(CLK) then
      -- 1-clk output strobes default low
      BIT_VALID <= '0';
      BIT_START <= '0';

      -- input synchroniser + recessive->dominant edge detect
      rx_m <= RX;
      rx_s <= rx_m;
      rx_d <= rx_s;
      falling := (rx_d = '1' and rx_s = '0');

      -- RATE_SEL edge detect (forced-resync trigger at the rate switch boundary).
      -- rate_d is the registered copy from last clk; compare BEFORE updating it.
      rate_edge := (RATE_SEL /= rate_d);

      if ENABLE = '1' then
        if falling then
          -- hard (re)sync: snap the bit clock to the edge — this dominant bit starts now
          cnt       <= (others => '0');
          synced_r  <= '1';
          BIT_START <= '1';
        elsif rate_edge and synced_r = '1' and RATE_SEL = '1' and cnt >= nom_sample_cyc then
          -- nominal→data at the BRS sample point: continue in the data phase from
          -- the data sample point (see FORCED RESYNC above). Not a bit start.
          cnt       <= unsigned(DATA_SAMPLE_CYC) + (cnt - nom_sample_cyc);
        elsif rate_edge and synced_r = '1' then
          -- data→nominal (and the defensive nominal→data case): anchor cnt:=0
          cnt       <= (others => '0');
          BIT_START <= '1';
        elsif synced_r = '1' then
          -- free-run the bit counter between edges
          if cnt >= cur_cyc_per_bit - 1 then
            cnt       <= (others => '0');
            BIT_START <= '1';
          else
            cnt <= cnt + 1;
          end if;
        end if;

        -- Sample once per bit at the sample-point cycle. Structurally identical to
        -- v0.1 (the sample test is independent of the (re)sync branch and uses the
        -- CURRENT, pre-reset counter), so a falling edge that lands exactly on the
        -- sample point samples in BOTH blocks ⇒ bit-identical (CAN-REQ-23). Only
        -- the FD rate-switch boundary is excluded: the BRS bit was already sampled
        -- at the nominal rate one bit earlier, so don't double-sample on the anchor.
        if synced_r = '1' and not rate_edge and cnt = cur_sample_cyc then
          BIT_VALID <= '1';
          BIT_VALUE <= rx_s;
        end if;
      end if;

      rate_d <= RATE_SEL;  -- update the delayed copy every clk

      if RST = '1' or ENABLE = '0' then
        cnt      <= (others => '0');
        synced_r <= '0';
      end if;
    end if;
  end process;

end architecture rtl;
