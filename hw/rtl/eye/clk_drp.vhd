-- rr unknown-llm (AI-generated)
--------------------------------------------------------------------------------
-- File       : clk_drp.vhd
-- Project    : deepskopion — eye-scan / BERT instrument (DS-P2.66, DS-P3.118/119)
-- Purpose    : MMCM DRP reconfiguration sequencer — the FREQUENCY axis of the
--              eye. On a RECONFIG pulse it walks the Dynamic Reconfiguration
--              Port read-modify-write sequence for the FREQ_SEL profile against
--              an MMCME2_ADV, then holds BUSY through the relock so the host
--              knows when the new TX bit-rate is stable and a fresh BER sweep is
--              valid.
--
-- This is VENDOR-NEUTRAL, synthesisable RTL — it is the DRP *master*; the MMCM
-- is the DRP *slave*. In sim the testbench models the slave (DRDY handshake +
-- a modelled register file that returns reserved-bit contents on a read and
-- captures writes), so the sequencer's XAPP888 behaviour is proven with no
-- `unisim`. On silicon the thin Phase-5 wrapper clk_mmcm_xilinx.vhd instantiates
-- THIS sequencer driving a real MMCME2_ADV's DRP port — the SAME logic, no
-- model/synth divergence.
--
-- ── DUAL-OUTPUT reconfig (DS-P3.118) ──────────────────────────────────────────
-- The eye now uses TWO MMCM outputs at the SAME swept divide: CLKOUT0 = eye_clk
-- (TX) and CLKOUT1 = sample_clk (RX, dynamic fine phase-shift). For sample_clk to
-- stay FREQUENCY-LOCKED to eye_clk as FREQ_SEL sweeps, BOTH outputs' counter
-- registers must be reprogrammed every reconfig. The sequence therefore grew from
-- 3 to 5 DRP accesses:
--   step 0: PowerReg     0x28  ← 0xFFFF                              (mask 0x0000)
--   step 1: CLKOUT0 Reg1 0x08  ← {PHASE_MUX,rsvd,HIGH_TIME,LOW_TIME} (mask 0x1000)
--   step 2: CLKOUT0 Reg2 0x09  ← {rsvd,MX,EDGE,NO_COUNT,DELAY}       (mask 0xFC00)
--   step 3: CLKOUT1 Reg1 0x0A  ← same divide as CLKOUT0             (mask 0x1000)
--   step 4: CLKOUT1 Reg2 0x0B  ← same divide as CLKOUT0             (mask 0xFC00)
-- each performed as read → (DO and MASK) or (DATA and not MASK) → write. The MASK
-- is the set of bits to PRESERVE from the read-back (the MMCM factory/reserved
-- attribute bits). After this burst the MMCM is released and relocks; the CLKOUT1
-- fine-phase accumulator resets to its nominal (attribute) phase — i.e. every
-- frequency change re-zeroes the sample phase, the desired behaviour.
--
-- ── The XAPP888 sequence (why the prior version never relocked on silicon) ──
-- A real MMCME2 will NOT relock unless, on every reconfig: (1) the PowerReg
-- (0x28) is written 0xFFFF, and (2) each clock-counter register is updated
-- read-modify-WRITE so the factory reserved bits read back from the slave are
-- preserved verbatim. (XAPP888 v1.8; the mmcme2_drp.v reference design.)
--
-- Because the VCO is held FIXED (CLKFBOUT_MULT=10, DIVCLK=1 → 1000 MHz) and only
-- the CLKOUT0/1 divides change, the M/D-derived LOCK and FILT registers do NOT
-- change (no 0x18/0x19/0x1A/0x4E/0x4F update). eye_clk = sample_clk = 1000 MHz/D.
--   ClkReg1 = HIGH_TIME[11:6] & LOW_TIME[5:0]; HT=ceil(D/2), LT=floor(D/2).
--   ClkReg2 bit7 = EDGE = (D mod 2); NO_COUNT=0, DELAY_TIME=0, MX=0 (zero phase).
-- Profiles per FREQ_SEL (sel 0..3 PINNED — the baked-in 4-freq silicon image):
--   0:D=10→100  1:D=8→125  2:D=6→166.7  3:D=5→200 MHz   (existing)
--   4:D=9→111   5:D=7→143   6:D=12→83.3  7:D=16→62.5
--   8:D=20→50 (RMII)  9:D=40→25         sel>=10 clamps to the 100 MHz base.
-- The denser grid feeds the fine-frequency sweep (DS-P3.119); fractional dividers
-- (CLKOUT_DIVIDE_F) are a later step — they touch extra MMCM regs and are a
-- silicon-only relock checkpoint (no nvc MMCM model).
--
-- Adversarial safety: if the MMCM never returns DRDY (dead DRP) the sequencer
-- holds BUSY and never asserts LOCKED — it cannot fake a completed reconfig.
--
-- Standard   : VHDL-2019 (IEEE 1076-2019)
-- Licence    : CERN-OHL-W-2.0
--------------------------------------------------------------------------------

library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

entity clk_drp is
  generic (
    G_LOCK_CYCLES : positive := 8    -- min relock wait before LOCKED (a floor; the
                                     -- wrapper also gates on the real MMCM LOCKED)
  );
  port (
    CLK       : in  std_logic;                      -- DRP access clock (= DCLK)
    RST       : in  std_logic;                      -- sync, active-high
    FREQ_SEL  : in  std_logic_vector(3 downto 0);   -- frequency-profile index
    RECONFIG  : in  std_logic;                      -- rising edge launches a reconfig

    BUSY      : out std_logic;                      -- reconfiguration in progress
    LOCKED    : out std_logic;                      -- MMCM relocked at the new frequency

    -- DRP master port (→ MMCME2_ADV DRP slave) -------------------------------
    DADDR     : out std_logic_vector(6 downto 0);
    DI        : out std_logic_vector(15 downto 0);
    DWE       : out std_logic;
    DEN       : out std_logic;
    DO        : in  std_logic_vector(15 downto 0);  -- read-back (used by the RMW)
    DRDY      : in  std_logic;
    MMCM_RST  : out std_logic                       -- hold the MMCM in reset across reconfig
  );
end entity clk_drp;

architecture rtl of clk_drp is

  -- Read → modify → write, one register at a time, for the C_NSTEPS-long program.
  type state_t is (S_IDLE, S_RD, S_RD_ACK, S_WR, S_WR_ACK, S_RELOCK);
  signal state_r : state_t := S_IDLE;

  signal recfg_d : std_logic := '0';
  signal recfg_p : std_logic;

  signal prof_r  : unsigned(3 downto 0) := (others => '0');

  ---------------------------------------------------------------------------
  -- The fixed DRP program: address, and the MASK = bits to PRESERVE from the
  -- read-back (the MMCM's factory/reserved attribute bits). VCO is unchanged so
  -- only the PowerReg + the CLKOUT0 *and* CLKOUT1 counter registers are touched.
  ---------------------------------------------------------------------------
  constant C_NSTEPS : positive := 5;
  signal   idx_r    : integer range 0 to C_NSTEPS := 0;

  type addr_rom_t is array (0 to C_NSTEPS-1) of std_logic_vector(6 downto 0);
  type word_rom_t is array (0 to C_NSTEPS-1) of std_logic_vector(15 downto 0);

  constant C_ADDR : addr_rom_t := (
    0 => std_logic_vector(to_unsigned(16#28#, 7)),   -- PowerReg
    1 => std_logic_vector(to_unsigned(16#08#, 7)),   -- CLKOUT0 ClkReg1
    2 => std_logic_vector(to_unsigned(16#09#, 7)),   -- CLKOUT0 ClkReg2
    3 => std_logic_vector(to_unsigned(16#0A#, 7)),   -- CLKOUT1 ClkReg1
    4 => std_logic_vector(to_unsigned(16#0B#, 7))    -- CLKOUT1 ClkReg2
  );
  constant C_MASK : word_rom_t := (
    0 => x"0000",   -- PowerReg: overwrite fully (no reserved bits to keep)
    1 => x"1000",   -- CLKOUT0 ClkReg1: preserve reserved bit [12]
    2 => x"FC00",   -- CLKOUT0 ClkReg2: preserve reserved bits [15:10]
    3 => x"1000",   -- CLKOUT1 ClkReg1: preserve reserved bit [12]
    4 => x"FC00"    -- CLKOUT1 ClkReg2: preserve reserved bits [15:10]
  );

  -- Per-profile CLKOUT high/low/edge encoding (the WRITE data, before masking).
  -- ClkReg1 = (HIGH_TIME<<6)|LOW_TIME; ClkReg2 bit7 = EDGE. PHASE_MUX/MX/DELAY=0.
  -- CLKOUT0 and CLKOUT1 share these (frequency-locked).
  type prof_rom_t is array (0 to 15) of std_logic_vector(15 downto 0);
  constant C_CLKREG1 : prof_rom_t := (
    0      => x"0145",   -- D=10 (HT5,LT5) → 100   MHz (base)   [PINNED]
    1      => x"0104",   -- D=8  (HT4,LT4) → 125   MHz          [PINNED]
    2      => x"00C3",   -- D=6  (HT3,LT3) → 166.7 MHz          [PINNED]
    3      => x"00C2",   -- D=5  (HT3,LT2) → 200   MHz          [PINNED]
    4      => x"0144",   -- D=9  (HT5,LT4) → 111.1 MHz
    5      => x"0103",   -- D=7  (HT4,LT3) → 142.9 MHz
    6      => x"0186",   -- D=12 (HT6,LT6) →  83.3 MHz
    7      => x"0208",   -- D=16 (HT8,LT8) →  62.5 MHz
    8      => x"028A",   -- D=20 (HT10,LT10)→ 50.0 MHz (RMII)
    9      => x"0514",   -- D=40 (HT20,LT20)→ 25.0 MHz
    others => x"0145"    -- clamp undefined profiles to the safe 100 MHz base
  );
  constant C_CLKREG2 : prof_rom_t := (
    3      => x"0080",   -- D=5  is odd → EDGE=1
    4      => x"0080",   -- D=9  is odd → EDGE=1
    5      => x"0080",   -- D=7  is odd → EDGE=1
    others => x"0000"
  );

  -- The intended WRITE data for step k of profile `prof` (pre-mask). CLKOUT1
  -- (steps 3/4) reuses the CLKOUT0 divide so both outputs share the frequency.
  function step_data (prof : unsigned; k : integer) return std_logic_vector is
  begin
    case k is
      when 0      => return x"FFFF";                       -- PowerReg
      when 1      => return C_CLKREG1(to_integer(prof));   -- CLKOUT0 ClkReg1
      when 2      => return C_CLKREG2(to_integer(prof));   -- CLKOUT0 ClkReg2
      when 3      => return C_CLKREG1(to_integer(prof));   -- CLKOUT1 ClkReg1
      when others => return C_CLKREG2(to_integer(prof));   -- CLKOUT1 ClkReg2
    end case;
  end function;

  signal daddr_r : std_logic_vector(6 downto 0)  := (others => '0');
  signal di_r    : std_logic_vector(15 downto 0) := (others => '0');
  signal den_r   : std_logic := '0';
  signal dwe_r   : std_logic := '0';

  signal lockc_r : integer range 0 to G_LOCK_CYCLES := 0;
  signal locked_r: std_logic := '0';

begin

  recfg_p <= RECONFIG and not recfg_d;

  DADDR    <= daddr_r;
  DI       <= di_r;
  DEN      <= den_r;
  DWE      <= dwe_r;
  BUSY     <= '1' when state_r /= S_IDLE else '0';
  LOCKED   <= locked_r;
  -- Hold the MMCM in reset across the whole DRP access window (reads + writes);
  -- release it only in S_RELOCK so it can relock at the new divide.
  MMCM_RST <= '1' when (state_r = S_RD or state_r = S_RD_ACK or
                        state_r = S_WR or state_r = S_WR_ACK) else '0';

  P_Seq : process (CLK)
  begin
    if rising_edge(CLK) then
      recfg_d <= RECONFIG;
      den_r   <= '0';        -- DEN/DWE are single-cycle strobes (default low)
      dwe_r   <= '0';

      if RST = '1' then
        state_r  <= S_IDLE;
        idx_r    <= 0;
        lockc_r  <= 0;
        locked_r <= '0';
        den_r    <= '0';
        dwe_r    <= '0';
      else
        case state_r is

          when S_IDLE =>
            if recfg_p = '1' then
              prof_r   <= unsigned(FREQ_SEL);
              idx_r    <= 0;
              locked_r <= '0';
              state_r  <= S_RD;
            end if;

          when S_RD =>
            -- Present the address and pulse a READ (DEN=1, DWE=0) for one cycle.
            daddr_r <= C_ADDR(idx_r);
            den_r   <= '1';
            dwe_r   <= '0';
            state_r <= S_RD_ACK;

          when S_RD_ACK =>
            -- Wait for the slave's DRDY; on it, DO carries the current register.
            -- Form the write word: keep the masked (reserved) bits from DO, take
            -- the rest from the profile data. A missing DRDY parks us here.
            if DRDY = '1' then
              di_r <= (DO and C_MASK(idx_r)) or
                      (step_data(prof_r, idx_r) and not C_MASK(idx_r));
              state_r <= S_WR;
            end if;

          when S_WR =>
            -- Present the same address + the RMW data; pulse a WRITE (DEN=DWE=1).
            daddr_r <= C_ADDR(idx_r);
            den_r   <= '1';
            dwe_r   <= '1';
            state_r <= S_WR_ACK;

          when S_WR_ACK =>
            if DRDY = '1' then
              if idx_r = C_NSTEPS - 1 then
                lockc_r <= 0;
                state_r <= S_RELOCK;
              else
                idx_r   <= idx_r + 1;
                state_r <= S_RD;
              end if;
            end if;

          when S_RELOCK =>
            -- MMCM reset released here; wait the relock floor, then LOCKED. The
            -- wrapper ANDs this with the real MMCM LOCKED before telling the host.
            if lockc_r = G_LOCK_CYCLES - 1 then
              locked_r <= '1';
              state_r  <= S_IDLE;
            else
              lockc_r <= lockc_r + 1;
            end if;

        end case;
      end if;
    end if;
  end process P_Seq;

end architecture rtl;
