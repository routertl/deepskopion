-- rr unknown-llm (AI-generated)
--------------------------------------------------------------------------------
-- glitch_filter — async-input conditioning for datalogger lines (DS-T3.5)
--
--   2-FF synchroniser (metastability, leads) → debounce that only accepts a
--   level after it has been stable for the rejection window. Fixes the
--   uart_datalogger silicon mis-decode: the datalogger auto-measures baud from the
--   minimum data-edge distance, so a single spurious 1-clk edge on the
--   unconditioned async EMIO line collapses the measured baud → garbage decode.
--
--   SIZING — from the captured line's MAX FREQUENCY, not a fixed time:
--     The rejection window MUST be larger than a glitch (≈1–2 clks of
--     metastability) yet SMALLER than the shortest real pulse, otherwise a real
--     edge on a FAST line (SPI/QSPI clock) would be eaten. The shortest real
--     pulse is the half-period of a square wave at G_MAX_FREQ_HZ, i.e.
--     G_CLK_HZ/(2·G_MAX_FREQ_HZ) clocks. The window is therefore
--         clamp(2, G_MAX_GLITCH_CLKS, min_half_period_clks − 1)
--     so it self-scales per datalogger:
--       * slow line (UART ≤1 Mbaud, I2C): window = G_MAX_GLITCH_CLKS — real
--         glitch rejection.
--       * fast line (SPI/QSPI ≥~25 MHz @100 MHz clk): min_half ≈ 2 → window
--         floors at 2, i.e. it degenerates to a pure 2-FF synchroniser (a line
--         whose pulses are ~1 clk wide cannot be glitch-filtered — only
--         synchronised). Clock-following protocols (SPI/I2C) tolerate this:
--         they sample on their own SCLK/SCL, so a residual edge does not wreck
--         a self-measured baud the way it does for UART.
--------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

entity glitch_filter is
  generic (
    G_CLK_HZ          : positive := 100_000_000;  -- clk_sys frequency
    G_MAX_FREQ_HZ     : positive := 1_000_000;    -- max fundamental freq of the captured line
    G_MAX_GLITCH_CLKS : positive := 4             -- reject glitches up to this many clks (≥ metastability)
  );
  port (
    CLK     : in  std_logic;
    RST     : in  std_logic;
    D_ASYNC : in  std_logic;                      -- raw asynchronous input
    Q       : out std_logic                       -- synchronised + glitch-filtered output
  );
end entity glitch_filter;

architecture rtl of glitch_filter is

  -- Shortest real pulse (half-period at G_MAX_FREQ_HZ), in clocks. Guarded ≥1 so
  -- the math is safe even when the line is near/over the clk's Nyquist (the
  -- filter then degenerates to a 2-FF synchroniser).
  function f_min_half(clk_hz, max_freq : positive) return positive is
    variable h : integer := clk_hz / (2 * max_freq);
  begin
    if h < 1 then return 1; else return h; end if;
  end function;

  -- Debounce window = min(glitch target, min_half - 1), floored at 2 (so a
  -- single metastable sample can never pass) — never ≥ the shortest real pulse.
  function f_window(clk_hz, max_freq, glitch : positive) return positive is
    variable cap : integer := f_min_half(clk_hz, max_freq) - 1;
    variable w   : integer;
  begin
    w := glitch;
    if cap < w then w := cap; end if;
    if w < 2 then w := 2; end if;
    return w;
  end function;

  constant C_STABLE : positive := f_window(G_CLK_HZ, G_MAX_FREQ_HZ, G_MAX_GLITCH_CLKS);

  -- 2-FF synchroniser (metastability hardening) — leads the debounce.
  signal meta_r : std_logic := '1';
  signal sync_r : std_logic := '1';
  -- Debounce / glitch filter on the synchronised line.
  signal filt_r : std_logic := '1';
  signal cnt_r  : integer range 0 to C_STABLE - 1 := 0;

  attribute async_reg : string;
  attribute async_reg of meta_r : signal is "TRUE";
  attribute async_reg of sync_r : signal is "TRUE";
begin

  process (CLK)
  begin
    if rising_edge(CLK) then
      meta_r <= D_ASYNC;
      sync_r <= meta_r;

      if sync_r = filt_r then
        cnt_r <= 0;                          -- matches output → reset the streak
      elsif cnt_r = C_STABLE - 1 then
        filt_r <= sync_r;                    -- held long enough → accept the edge
        cnt_r  <= 0;
      else
        cnt_r <= cnt_r + 1;                  -- candidate edge, still counting
      end if;

      if RST = '1' then
        -- DS-T2.45: come out of reset already TRACKING the line, not forced to a
        -- hard '1'. The old idle-HIGH reset value matched I2C/UART/SPI (idle
        -- high) but mismatched an EDGE line that idles LOW: Q started '1' and
        -- the spurious startup '1'->'0' convergence was captured by an
        -- edge_datalogger enabled during the settling window as phantom falling
        -- edge(s). Keeping the 2-FF synchroniser running through reset and
        -- loading filt_r from sync_r makes Q reach the ACTUAL line level by
        -- reset deassertion for ANY idle polarity (idle-high lines still
        -- converge to '1' — bit-identical to before; idle-low lines converge to
        -- '0' — no phantom). cnt_r held cleared so the debounce starts fresh.
        meta_r <= D_ASYNC;
        sync_r <= meta_r;
        filt_r <= sync_r;
        cnt_r  <= 0;
      end if;
    end if;
  end process;

  Q <= filt_r;

end architecture rtl;
