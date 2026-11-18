-------------------------------------------------------------------------------
-- maxfreq_event_emitter.vhd  —  DS-P3.117 (in-band per-line max-frequency)
-------------------------------------------------------------------------------
-- Turns one edge_datalogger's latched max-frequency datum (ED_MAX_INTERVAL, the
-- SMALLEST validated edge-to-edge interval in CLK ticks, + the ED_MAX_NEW pulse
-- that fires when a new smaller min is latched) into an IN-BAND zs_msg frame:
--
--     severity = INFO | domain = G_DOMAIN | event_id = G_EVENT_ID
--     datalogger_id = G_DATALOGGER_ID
--     payload  = 8 bytes (2 words):
--        word0 = min_interval (CLK ticks, zero-extended to 32b)
--        word1 = flags[31:16] | ival_width[15:8] | channel[7:0]
--
-- This is the established in-band escape for a SECOND event_id under DOMAIN_EDGE:
-- the gather→edge_batcher aggregator hard-stamps event_id 0x01 (EVT_EDGE_BATCH)
-- on the normal per-event edge path, so the max-freq frame CANNOT ride that path.
-- Instead it is presented as its own framer input (like error_event_emitter for
-- the loss frames), and the top-level framer mux carries it through with the
-- msg_type stamped HERE — exactly mirroring error_event_emitter (DS-P3.70).
--
-- Emit policy (matches the SW/contract intent): a frame is queued whenever
--   * ED_MAX_NEW pulses (a new, smaller min interval was latched), OR
--   * RE_ARM pulses (host re-ping via CONFIG[2]; the latch was cleared to the
--     all-ones sentinel, so the consumer learns the line was re-armed).
-- The payload snapshots ED_MAX_INTERVAL on the trigger cycle. If a new trigger
-- arrives while a frame is still pending (FR_READY not yet seen), the snapshot
-- is refreshed to the newest value (we only ever care about the LATEST min) —
-- no accounting is lost because the value is a running minimum, not a counter.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.zs_msg_pkg.all;   -- make_msg_type, C_ZS_SEV_INFO, C_ZS_CLASS_DATA,
                           -- C_ZS_DOMAIN_EDGE, C_ZS_EVT_EDGE_MAXFREQ

entity maxfreq_event_emitter is
  generic (
    G_DOMAIN      : std_logic_vector(3 downto 0) := C_ZS_DOMAIN_EDGE;
    G_EVENT_ID    : std_logic_vector(7 downto 0) := C_ZS_EVT_EDGE_MAXFREQ;
    G_DATALOGGER_ID  : std_logic_vector(7 downto 0) := (others => '0');
    -- Width (bits) of the edge_datalogger min-interval register. Echoed into the
    -- payload so the SW applies the right all-ones sentinel mask. Must match
    -- the edge_datalogger G_MAXFREQ_WIDTH wired to this emitter.
    G_IVAL_WIDTH  : integer range 4 to 32 := 24
  );
  port (
    CLK           : in  std_logic;
    RST           : in  std_logic;
    -- Latched smallest validated edge-to-edge interval (CLK ticks), all-ones =
    -- "no edge yet / re-armed". Width G_IVAL_WIDTH; zero-extended to 32b in the
    -- payload.
    MAX_INTERVAL  : in  std_logic_vector(G_IVAL_WIDTH-1 downto 0);
    -- One-CLK pulse: a new (smaller) min interval was latched.
    MAX_NEW       : in  std_logic;
    -- One-CLK pulse: host re-arm (CONFIG[2]). The latch was cleared; emit so the
    -- consumer learns the line was re-armed (payload is the sentinel value).
    RE_ARM        : in  std_logic;
    TIMESTAMP     : in  std_logic_vector(63 downto 0);
    -- Framer-input handshake (2-word payload).
    FR_VALID      : out std_logic;
    FR_READY      : in  std_logic;
    FR_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    FR_MSG_CLASS  : out std_logic_vector(7 downto 0);
    FR_MSG_TYPE   : out std_logic_vector(15 downto 0);
    FR_FLAGS      : out std_logic_vector(15 downto 0);
    FR_TIMESTAMP  : out std_logic_vector(63 downto 0);
    FR_PAYLOAD    : out std_logic_vector(63 downto 0);   -- {word1, word0}
    FR_WORDS      : out std_logic_vector(7 downto 0)
  );
end entity maxfreq_event_emitter;

architecture rtl of maxfreq_event_emitter is
  signal pending_r : std_logic := '0';
  signal ival_r    : std_logic_vector(31 downto 0) := (others => '0');
  signal ts_r      : std_logic_vector(63 downto 0) := (others => '0');

  -- word1 is constant for a given channel/width (flags=0). Channel echoes the
  -- frame's global datalogger id so the SW payload-vs-header check passes.
  constant C_WORD1 : std_logic_vector(31 downto 0) :=
      x"0000"                                                   -- flags = 0
    & std_logic_vector(to_unsigned(G_IVAL_WIDTH, 8))           -- ival_width
    & G_DATALOGGER_ID;                                            -- channel
begin
  process (CLK)
    variable v_trigger : std_logic;
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        pending_r <= '0';
        ival_r    <= (others => '0');
        ts_r      <= (others => '0');
      else
        v_trigger := MAX_NEW or RE_ARM;

        -- Snapshot the LATEST latched interval on any trigger. Refresh even if a
        -- frame is already pending — the value is a running minimum, so the most
        -- recent snapshot is the one worth sending; nothing is lost.
        if v_trigger = '1' then
          ival_r    <= std_logic_vector(resize(unsigned(MAX_INTERVAL), 32));
          ts_r      <= TIMESTAMP;
          pending_r <= '1';
        end if;

        -- Clear pending once the framer accepts (and no fresh trigger this
        -- cycle — a coincident trigger keeps pending high with the new value).
        if pending_r = '1' and FR_READY = '1' and v_trigger = '0' then
          pending_r <= '0';
        end if;
      end if;
    end if;
  end process;

  FR_VALID      <= pending_r;
  FR_DATALOGGER_ID <= G_DATALOGGER_ID;
  FR_MSG_CLASS  <= C_ZS_CLASS_DATA;
  FR_MSG_TYPE   <= make_msg_type(C_ZS_SEV_INFO, G_DOMAIN, G_EVENT_ID);
  FR_FLAGS      <= (others => '0');
  FR_TIMESTAMP  <= ts_r;
  -- {word1, word0}: word0 (low) = min_interval; word1 (high) = meta. The framer
  -- emits payload words verbatim, LSB-word first, and the ring writes each word
  -- little-endian — so this lays out as the SW <IBBH> EDGE_MAXFREQ payload.
  FR_PAYLOAD    <= C_WORD1 & ival_r;
  FR_WORDS      <= std_logic_vector(to_unsigned(2, 8));
end architecture rtl;
