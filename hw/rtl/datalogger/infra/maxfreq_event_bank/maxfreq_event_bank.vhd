-------------------------------------------------------------------------------
-- maxfreq_event_bank.vhd  —  DS-P3.117 (per-channel in-band max-frequency)
-------------------------------------------------------------------------------
-- A bank of N maxfreq_event_emitter instances + a fixed-priority N:1 merge, so
-- the 4 EDGE dataloggers each produce in-band MAX-FREQ frames that name the channel
-- whose min edge-to-edge interval just got smaller (or was re-armed):
--
--     severity = INFO | domain = G_DOMAIN | event_id = G_EVENT_ID (EVT_EDGE_MAXFREQ)
--     datalogger_id = G_DATALOGGER_IDS[channel]      (which channel reports)
--     payload    = {word1, word0}  (2 words):
--        word0 = that channel's latched min_interval (CLK ticks, zero-ext 32b)
--        word1 = flags[31:16] | ival_width[15:8] | channel[7:0]
--
-- This is the max-frequency sibling of error_event_bank (DS-T3.18): same
-- per-channel-emitter + fixed-priority merge shape, but the payload is 2 words
-- (64 bits) and the merged FR_* group is drop-in for a single
-- maxfreq_event_emitter, so deepskopion_top adds it as a 4th framer-mux input
-- exactly the way error_event_bank was added as the 3rd (DS-P3.70).
--
-- Per-channel inputs come from datalogger_gather's re-exported EDGE_MAX_IVALS /
-- EDGE_MAX_NEW buses (one slot per edge instance). RE_ARM is GLOBAL: the host
-- re-ping is CONFIG bit 2, broadcast to every edge_datalogger at once, so a single
-- rising-edge detect on CONFIG(G_REARM_BIT) drives every channel's emitter
-- RE_ARM in lock-step — each then emits the all-ones sentinel so the consumer
-- learns the line was re-armed (the edge_datalogger clears ED_MAX_NEW on re-arm, so
-- without this the re-ping would be invisible in-band).
--
-- Merge: fixed priority (ch0 highest). Contention is rare (a new min is latched
-- at most once per real edge that beats the running minimum); FR_READY is routed
-- to the selected channel ONLY, so exactly one emitter advances per framer
-- accept (no lost/duplicated snapshot). A pending channel that loses arbitration
-- holds its snapshot (the emitter only refreshes to the LATEST min while
-- pending) and drains on a later cycle — no starvation given the framer's
-- bursty idle.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.zs_msg_pkg.all;   -- C_ZS_DOMAIN_EDGE, C_ZS_EVT_EDGE_MAXFREQ

entity maxfreq_event_bank is
  generic (
    G_NUM_CH      : positive                     := 4;
    G_DOMAIN      : std_logic_vector(3 downto 0) := C_ZS_DOMAIN_EDGE;
    G_EVENT_ID    : std_logic_vector(7 downto 0) := C_ZS_EVT_EDGE_MAXFREQ;
    -- Per-channel datalogger ids, packed low-byte-first: channel k id is in
    -- G_DATALOGGER_IDS[(k+1)*8-1 : k*8]. Width must be G_NUM_CH*8.
    G_DATALOGGER_IDS : std_logic_vector             := x"00";
    -- Width (bits) of each edge_datalogger's min-interval register. Must match the
    -- edge_datalogger G_MAXFREQ_WIDTH feeding EDGE_MAX_IVALS.
    G_IVAL_WIDTH  : integer range 4 to 32         := 24;
    -- CONFIG bit that triggers a host re-ping / re-arm (edge_datalogger CONFIG[2]).
    G_REARM_BIT   : natural                       := 2
  );
  port (
    CLK           : in  std_logic;
    RST           : in  std_logic;
    -- Per-channel latched min edge-to-edge intervals, packed: channel k occupies
    -- MAX_IVALS[(k+1)*G_IVAL_WIDTH-1 : k*G_IVAL_WIDTH]. all-ones = no edge yet.
    MAX_IVALS     : in  std_logic_vector(G_NUM_CH*G_IVAL_WIDTH-1 downto 0);
    -- Per-channel one-CLK pulse: a new (smaller) min interval was latched.
    MAX_NEW       : in  std_logic_vector(G_NUM_CH-1 downto 0);
    -- Datalogger CONFIG bus — bit G_REARM_BIT is the global host re-ping.
    CONFIG        : in  std_logic_vector;
    TIMESTAMP     : in  std_logic_vector(63 downto 0);
    -- Merged framer-input handshake (drop-in for a single maxfreq_event_emitter).
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
end entity maxfreq_event_bank;

architecture rtl of maxfreq_event_bank is
  -- Normalise the packed id generic to a fixed descending range, independent of
  -- the actual's literal direction (a bit-string literal actual is ascending, so
  -- a downto slice on the raw generic mismatches). Positional copy preserves the
  -- low-byte-first byte order: channel k id lands in IDS[(k+1)*8-1 : k*8].
  constant IDS : std_logic_vector(G_NUM_CH*8-1 downto 0) := G_DATALOGGER_IDS;

  type t_byte_arr    is array (0 to G_NUM_CH-1) of std_logic_vector(7 downto 0);
  type t_word16_arr  is array (0 to G_NUM_CH-1) of std_logic_vector(15 downto 0);
  type t_word64_arr  is array (0 to G_NUM_CH-1) of std_logic_vector(63 downto 0);

  signal e_valid     : std_logic_vector(0 to G_NUM_CH-1);
  signal e_ready     : std_logic_vector(0 to G_NUM_CH-1);
  signal e_datalogger   : t_byte_arr;
  signal e_msg_class : t_byte_arr;
  signal e_msg_type  : t_word16_arr;
  signal e_flags     : t_word16_arr;
  signal e_ts        : t_word64_arr;
  signal e_payload   : t_word64_arr;
  signal e_words     : t_byte_arr;

  -- Global re-ping pulse: rising edge of CONFIG(G_REARM_BIT). Mirrors the
  -- edge_datalogger's own reArm rising-edge detect (CONFIG[2]) so the in-band
  -- sentinel frame is emitted on the same trigger the latches were cleared by.
  signal rearm_prev_r : std_logic := '0';
  signal rearm_pulse  : std_logic;
begin

  rearm_pulse  <= CONFIG(G_REARM_BIT) and not rearm_prev_r;

  p_rearm_edge : process (CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        rearm_prev_r <= '0';
      else
        rearm_prev_r <= CONFIG(G_REARM_BIT);
      end if;
    end if;
  end process p_rearm_edge;

  -- One coalescing emitter per channel, tagged with that channel's datalogger id.
  GEN_EMITTERS : for k in 0 to G_NUM_CH-1 generate
    u_emit : entity tich.maxfreq_event_emitter
      generic map (
        G_DOMAIN     => G_DOMAIN,
        G_EVENT_ID   => G_EVENT_ID,
        G_DATALOGGER_ID => IDS((k+1)*8-1 downto k*8),
        G_IVAL_WIDTH => G_IVAL_WIDTH
      )
      port map (
        CLK           => CLK,
        RST           => RST,
        MAX_INTERVAL  => MAX_IVALS((k+1)*G_IVAL_WIDTH-1 downto k*G_IVAL_WIDTH),
        MAX_NEW       => MAX_NEW(k),
        RE_ARM        => rearm_pulse,
        TIMESTAMP     => TIMESTAMP,
        FR_VALID      => e_valid(k),
        FR_READY      => e_ready(k),
        FR_DATALOGGER_ID => e_datalogger(k),
        FR_MSG_CLASS  => e_msg_class(k),
        FR_MSG_TYPE   => e_msg_type(k),
        FR_FLAGS      => e_flags(k),
        FR_TIMESTAMP  => e_ts(k),
        FR_PAYLOAD    => e_payload(k),
        FR_WORDS      => e_words(k)
      );
  end generate GEN_EMITTERS;

  -- Fixed-priority N:1 merge. First pending channel (lowest index) wins; its
  -- FR_READY is the framer's, all others get '0', so only the winner advances.
  p_mux : process(all)
  begin
    FR_VALID      <= '0';
    FR_DATALOGGER_ID <= (others => '0');
    FR_MSG_CLASS  <= (others => '0');
    FR_MSG_TYPE   <= (others => '0');
    FR_FLAGS      <= (others => '0');
    FR_TIMESTAMP  <= (others => '0');
    FR_PAYLOAD    <= (others => '0');
    FR_WORDS      <= (others => '0');
    e_ready       <= (others => '0');
    for k in 0 to G_NUM_CH-1 loop
      if e_valid(k) = '1' then
        FR_VALID      <= '1';
        FR_DATALOGGER_ID <= e_datalogger(k);
        FR_MSG_CLASS  <= e_msg_class(k);
        FR_MSG_TYPE   <= e_msg_type(k);
        FR_FLAGS      <= e_flags(k);
        FR_TIMESTAMP  <= e_ts(k);
        FR_PAYLOAD    <= e_payload(k);
        FR_WORDS      <= e_words(k);
        e_ready(k)    <= FR_READY;
        exit;
      end if;
    end loop;
  end process p_mux;

end architecture rtl;
