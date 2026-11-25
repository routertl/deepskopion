-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- can_destuff.vhd  —  CAN / CAN-FD bit de-stuffing (DS-P3.100 v0.1, DS-P2.64 v0.2)
-------------------------------------------------------------------------------
-- CAN is NRZ with no clock wire, so the transmitter inserts a complementary
-- "stuff" bit after every 5 consecutive identical bits (active SOF..CRC) to
-- guarantee edges for resync. This block removes them: it forwards data bits and
-- silently drops the stuff bit after a 5-run. The same rule is the bus's first
-- line of error detection — 6 identical bits where a stuff was due can only mean
-- corruption, so that raises STUFF_ERROR.
--
-- TWO STUFFING MODES (CAN-REQ-17), selected by STUFF_MODE within the STUFF_EN
-- window:
--   * DYNAMIC  (STUFF_MODE='0', SOF..end-of-data): value-driven — a stuff bit
--     follows every 5 identical bits. The classic, silicon-validated path.
--   * FIXED    (STUFF_MODE='1', CAN-FD stuff-count + CRC fields): position-driven
--     — a fixed stuff bit precedes the field and then recurs after every 4 bits
--     (so stuff bits sit at positions 0,5,10,… of the fixed-mode stream: one
--     stuff + four real, repeating). Each fixed stuff bit MUST equal the inverse
--     of the immediately preceding bit on the wire; a violation is a FORM_ERROR
--     (never a silent drop). Per ISO 11898-1:2015 / Bosch CAN FD v1.0.
--
-- THE ADJACENCY CORNER (CAN-REQ-17): if the dynamic region ends on a run of 5
-- identical bits, the wire carries ONE stuff bit before the stuff count: the
-- fixed one (ISO 11898-1:2015 10.5: "there shall be only the fixed stuff bit,
-- there shall not be two consecutive stuff bits"). It is the inverse of the run,
-- so it satisfies the dynamic rule too; it is not counted in the stuff count and
-- not covered by the CRC. So on entering FIXED mode with run=5 the bit is checked
-- as the pending dynamic stuff (6 identical -> STUFF_ERROR) AND consumed as fixed
-- position 0; the next bit is stuff-count b0 (pos 1). Matches the CTU CAN FD core.
--
-- STUFF_EN frames the stuffed region (the frame FSM raises it at SOF, drops it
-- after the CRC). When low (CRC-delim / ACK / EOF — fixed-form) bits pass through.
--
-- IN: one sampled bit per IN_VALID strobe (from can_bit_timing).
-- OUT: the de-stuffed data bit stream + STUFF_ERROR (dynamic 6-run violation)
--      + FORM_ERROR (fixed stuff bit not the inverse of the preceding bit).
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;       -- DS-P3.107: to_unsigned for the debug tap

entity can_destuff is
  port (
    CLK         : in  std_logic;
    RST         : in  std_logic;
    STUFF_EN    : in  std_logic;                 -- high during the stuffed region (SOF..CRC)
    STUFF_MODE  : in  std_logic := '0';          -- 0 = dynamic (classic), 1 = fixed (FD stuff-count+CRC)
    IN_VALID    : in  std_logic;                 -- one sampled bit from can_bit_timing
    IN_BIT      : in  std_logic;
    OUT_VALID   : out std_logic;                 -- de-stuffed data bit (stuff bits removed)
    OUT_BIT     : out std_logic;
    STUFF_ERROR : out std_logic;                 -- 1-clk pulse: dynamic 6th identical bit
    FORM_ERROR  : out std_logic;                 -- 1-clk pulse: fixed stuff bit not inverse of prior
    -- DS-P3.107 debug tap: the fixed-engine position + dynamic run length — the
    -- suspected desync in the data→stuff-count handoff that corrupts the silicon
    -- sc/CRC capture. {fmode, fpos[2:0], run[2:0]} = 7 bits, Moore (from regs).
    DBG_DESTUFF : out std_logic_vector(6 downto 0) := (others => '0')
  );
end entity can_destuff;

architecture rtl of can_destuff is
  signal prev   : std_logic := '1';                 -- last bit on the (stuffed) wire
  signal run    : integer range 1 to 6 := 1;        -- length of the current identical run (dynamic)
  signal active : std_logic := '0';                 -- inside a dynamic stuffed region (post first bit)
  signal fmode  : std_logic := '0';                 -- fixed engine has anchored
  signal fpos   : integer range 0 to 4 := 0;        -- position (mod 5) of the CURRENT fixed-mode bit
begin
  process(CLK)
  begin
    if rising_edge(CLK) then
      OUT_VALID   <= '0';
      STUFF_ERROR <= '0';
      FORM_ERROR  <= '0';

      if STUFF_EN = '0' then
        -- fixed-form region: forward every bit, keep state cleared
        run    <= 1;
        active <= '0';
        fmode  <= '0';
        fpos   <= 0;
        if IN_VALID = '1' then
          OUT_VALID <= '1';
          OUT_BIT   <= IN_BIT;
          prev      <= IN_BIT;
        end if;

      elsif IN_VALID = '1' then
        if STUFF_MODE = '0' then
          -- ─────────────── DYNAMIC mode (classic, unchanged) ───────────────
          fmode <= '0';
          fpos  <= 0;
          if active = '0' then
            -- first bit of the stuffed region (SOF): emit + start the run
            active    <= '1';
            prev      <= IN_BIT;
            run       <= 1;
            OUT_VALID <= '1';
            OUT_BIT   <= IN_BIT;
          elsif run = 5 then
            -- this bit sits in the stuff position — it MUST be opposite prev
            if IN_BIT = prev then
              STUFF_ERROR <= '1';      -- 6 identical -> stuff violation
            end if;                    -- else legal stuff bit: drop it (no OUT_VALID)
            prev <= IN_BIT;
            run  <= 1;
          else
            -- ordinary data bit: emit + extend/restart the run
            OUT_VALID <= '1';
            OUT_BIT   <= IN_BIT;
            if IN_BIT = prev then
              run <= run + 1;
            else
              run  <= 1;
              prev <= IN_BIT;
            end if;
          end if;

        else
          -- ─────────────── FIXED mode (FD stuff-count + CRC) ───────────────
          if fmode = '0' then
            -- first bit since STUFF_MODE rose
            if run = 5 then
              -- ADJACENCY: the dynamic region ended on a 5-run. This bit is the one
              -- stuff bit there, the fixed one at pos 0 (ISO 10.5); 6 identical
              -- bits is a stuff violation. The next bit is pos 1 (stuff-count b0).
              if IN_BIT = prev then
                STUFF_ERROR <= '1';
              end if;
              prev  <= IN_BIT;
              run   <= 1;
              fmode <= '1';
              fpos  <= 1;
            else
              -- no pending dynamic stuff: this bit IS the first fixed stuff (pos 0)
              if IN_BIT = prev then
                FORM_ERROR <= '1';     -- fixed stuff must be the inverse of prior
              end if;
              prev  <= IN_BIT;
              fmode <= '1';
              fpos  <= 1;              -- pos 0 consumed; next bit is pos 1
            end if;
          else
            -- fixed engine running; the current bit's position is fpos
            if fpos = 0 then
              -- fixed stuff bit: inverse-of-prior or FORM_ERROR
              if IN_BIT = prev then
                FORM_ERROR <= '1';
              end if;
              prev <= IN_BIT;
            else
              -- real bit (stuff-count / parity / CRC): pass through
              OUT_VALID <= '1';
              OUT_BIT   <= IN_BIT;
              prev      <= IN_BIT;
            end if;
            if fpos = 4 then
              fpos <= 0;
            else
              fpos <= fpos + 1;
            end if;
          end if;
        end if;
      end if;

      if RST = '1' then
        prev <= '1'; run <= 1; active <= '0'; fmode <= '0'; fpos <= 0;
      end if;
    end if;
  end process;

  -- DS-P3.107 debug tap (concurrent, Moore from the registered state).
  DBG_DESTUFF <= fmode
                 & std_logic_vector(to_unsigned(fpos, 3))
                 & std_logic_vector(to_unsigned(run, 3));

end architecture rtl;
