--------------------------------------------------------------------------------
-- File       : idelay_scan.vhd  (entity + architectures `model` and `xilinx`)
-- Project    : deepskopion — eye-scan / BERT instrument (DS-P2.66)
-- Purpose    : Programmable input-delay on the RX loopback pin — the HORIZONTAL
--              (timing) axis of the eye. TAP (0..31) selects how far the sampled
--              bit is delayed before the BERT checker; sweeping TAP across the
--              bit period and measuring BER at each step traces the bathtub.
--
-- TWO architectures of the SAME entity live in this ONE file (DS-P3.114):
--   * `model`  — generic behavioural transport delay (≈ G_TAP_PS per tap), NO
--                vendor library; a deterministic, dependency-free reference of
--                the phase axis.
--   * `xilinx` — the real 7-series IDELAYE2 + IDELAYCTRL in an IODELAY_GROUP.
--                Synth binds this (eye tops use `entity tich.idelay_scan(xilinx)`).
--                Since RTL-P3.861 nvc can simulate unisim, so the cocotb suite
--                now exercises THIS architecture too.
--
-- Folding the formerly-separate idelay_scan_xilinx.vhd in here (and retiring its
-- ip.yml library-attribution shim) is the DS-P3.114 collapse: the `xilinx`
-- architecture now rides into `library tich` alongside its entity by reference,
-- so the RTL-P3.480 architecture-only-file resolver gap no longer applies.
-- The contract — "DOUT is DIN delayed by TAP·tap, loadable on LD" — is identical
-- across both archs, so a sweep validated in sim maps 1:1 onto silicon.
--
-- Standard   : VHDL-2019 (IEEE 1076-2019)
-- Licence    : CERN-OHL-W-2.0
--------------------------------------------------------------------------------

library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

entity idelay_scan is
  generic (
    G_TAP_W  : positive := 5;    -- tap width: 2^5 = 32 IDELAYE2 taps
    G_TAP_PS : natural  := 78    -- per-tap delay (ps). IDELAYE2 nominal ≈78 ps @200 MHz REFCLK
  );
  port (
    CLK    : in  std_logic;                          -- load/clock domain (IDELAYE2.C)
    RST    : in  std_logic;                          -- sync, clears the tap to 0
    REFCLK : in  std_logic := '0';                   -- 200 MHz IDELAYCTRL reference. Used ONLY
                                                      -- by architecture `xilinx`; the GENERIC
                                                      -- `model` ignores it (default keeps the
                                                      -- sim testbench from having to drive it).
    TAP   : in  std_logic_vector(G_TAP_W-1 downto 0); -- desired delay tap (CNTVALUEIN)
    LD    : in  std_logic;                          -- pulse: latch TAP into the delay line
    DIN   : in  std_logic;                          -- raw RX bit (post-IBUF)
    DOUT  : out std_logic;                          -- delayed RX bit → prbs_check
    RDY   : out std_logic                           -- delay-controller ready (IDELAYCTRL.RDY)
  );
end entity idelay_scan;

architecture model of idelay_scan is
  signal tap_r : unsigned(G_TAP_W-1 downto 0) := (others => '0');
begin

  -- IDELAYE2 VAR_LOAD: the tap is captured from CNTVALUEIN on LD, in the C domain.
  P_Load : process (CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        tap_r <= (others => '0');
      elsif LD = '1' then
        tap_r <= unsigned(TAP);
      end if;
    end if;
  end process P_Load;

  -- Transport (not inertial) delay: a delay line passes even sub-period glitches,
  -- exactly like the real IDELAYE2 — essential for an honest eye at high tap.
  DOUT <= transport DIN after (to_integer(tap_r) * G_TAP_PS) * 1 ps;

  -- IDELAYCTRL is always ready in the model (no calibration to wait for).
  RDY <= '1';

end architecture model;


--------------------------------------------------------------------------------
-- architecture `xilinx` — real 7-series silicon (formerly idelay_scan_xilinx.vhd)
--
-- VAR_LOAD IDELAYE2 on the RX loopback pin, calibrated by an IDELAYCTRL in a
-- shared IODELAY_GROUP "eye_iodelay". REQUIRES a 200 MHz REFCLK on the entity's
-- REFCLK port (IDELAYCTRL spec). Synth and (post RTL-P3.861) the cocotb suite
-- both bind this architecture.
--------------------------------------------------------------------------------

library unisim;
  use unisim.vcomponents.all;

architecture xilinx of idelay_scan is

  attribute IODELAY_GROUP : string;
  attribute IODELAY_GROUP of i_idelayctrl : label is "eye_iodelay";
  attribute IODELAY_GROUP of i_idelaye2   : label is "eye_iodelay";

begin

  -- Tap calibration controller for the IODELAY_GROUP (one per group per region).
  i_idelayctrl : IDELAYCTRL
    port map (
      REFCLK => REFCLK,   -- 200 MHz
      RST    => RST,
      RDY    => RDY
    );

  -- VAR_LOAD: CNTVALUEIN (= TAP) is loaded into the delay line on LD, in the C
  -- domain. IDATAIN is the raw RX pin; DATAOUT is the delayed sample.
  i_idelaye2 : IDELAYE2
    generic map (
      DELAY_SRC             => "IDATAIN",
      IDELAY_TYPE           => "VAR_LOAD",
      IDELAY_VALUE          => 0,
      REFCLK_FREQUENCY      => 200.0,
      HIGH_PERFORMANCE_MODE => "TRUE",
      CINVCTRL_SEL          => "FALSE",
      PIPE_SEL              => "FALSE",
      SIGNAL_PATTERN        => "DATA"
    )
    port map (
      C           => CLK,
      REGRST      => RST,
      LD          => LD,
      LDPIPEEN    => '0',
      CE          => '0',
      INC         => '0',
      CINVCTRL    => '0',
      CNTVALUEIN  => TAP,        -- G_TAP_W must be 5 (IDELAYE2 is 5-bit)
      CNTVALUEOUT => open,
      IDATAIN     => DIN,
      DATAIN      => '0',
      DATAOUT     => DOUT
    );

end architecture xilinx;
