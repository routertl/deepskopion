-------------------------------------------------------------------------------
-- error_event_bank.vhd  —  DS-T3.18 (per-channel in-band loss provenance)
-------------------------------------------------------------------------------
-- A bank of N error_event_emitter instances + a fixed-priority N:1 merge, so a
-- multi-channel protocol (the 4 EDGE dataloggers) produces in-band loss frames that
-- name the OVERFLOWING channel:
--
--     severity = ERROR | domain = G_DOMAIN | event_id = G_EVENT_ID
--     datalogger_id = G_DATALOGGER_IDS[channel]      (which channel lost)
--     payload    = that channel's lost_count delta
--
-- Each channel k watches its own monotonic loss counter ERR_CNTS[k] and is tagged
-- with G_DATALOGGER_IDS[k]. This extends the single-aggregate emitter (DS-P3.70,
-- G_DATALOGGER_ID = 0, channel-agnostic) to per-channel attribution — the producer
-- side of the wave_stream `loss` kind v0.11 (carry datalogger_id/channel).
--
-- Merge: fixed priority (ch0 highest). Contention is astronomically rare — each
-- emitter goes pending at most once per G_FLUSH_CYCLES (~1 ms) and the framer's
-- ERROR input is top priority, so pending channels drain in consecutive cycles;
-- no starvation. FR_READY is routed to the selected channel ONLY, so exactly one
-- emitter advances its baseline per framer accept (no double-count). The merged
-- FR_* port group is drop-in compatible with a single error_event_emitter, so
-- deepskopion_top's 3:1 framer mux is unchanged.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.zs_msg_pkg.all;   -- C_ZS_DOMAIN_EDGE, C_ZS_ERR_EVT_INTERNAL_FIFO_OVERFLOW

entity error_event_bank is
  generic (
    G_NUM_CH       : positive                     := 4;
    G_DOMAIN       : std_logic_vector(3 downto 0) := C_ZS_DOMAIN_EDGE;
    G_EVENT_ID     : std_logic_vector(7 downto 0) := C_ZS_ERR_EVT_INTERNAL_FIFO_OVERFLOW;
    -- Per-channel datalogger ids, packed low-byte-first: channel k id is in
    -- G_DATALOGGER_IDS[(k+1)*8-1 : k*8]. Width must be G_NUM_CH*8.
    G_DATALOGGER_IDS  : std_logic_vector             := x"00";
    G_FLUSH_CYCLES : positive                     := 100_000
  );
  port (
    CLK           : in  std_logic;
    RST           : in  std_logic;
    -- Per-channel monotonic running loss counts, packed: channel k in
    -- ERR_CNTS[(k+1)*32-1 : k*32].
    ERR_CNTS      : in  std_logic_vector(G_NUM_CH*32-1 downto 0);
    TIMESTAMP     : in  std_logic_vector(63 downto 0);
    -- Merged framer-input handshake (drop-in for a single error_event_emitter).
    FR_VALID      : out std_logic;
    FR_READY      : in  std_logic;
    FR_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    FR_MSG_CLASS  : out std_logic_vector(7 downto 0);
    FR_MSG_TYPE   : out std_logic_vector(15 downto 0);
    FR_FLAGS      : out std_logic_vector(15 downto 0);
    FR_TIMESTAMP  : out std_logic_vector(63 downto 0);
    FR_PAYLOAD    : out std_logic_vector(31 downto 0);   -- lost_count delta
    FR_WORDS      : out std_logic_vector(7 downto 0)
  );
end entity error_event_bank;

architecture rtl of error_event_bank is
  -- Normalise the packed id generic to a fixed descending range, independent of
  -- the actual's literal direction (a bit-string literal actual is ascending, so
  -- a downto slice on the raw generic mismatches). Positional copy preserves the
  -- low-byte-first byte order: channel k id lands in IDS[(k+1)*8-1 : k*8].
  constant IDS : std_logic_vector(G_NUM_CH*8-1 downto 0) := G_DATALOGGER_IDS;

  type t_byte_arr   is array (0 to G_NUM_CH-1) of std_logic_vector(7 downto 0);
  type t_word16_arr is array (0 to G_NUM_CH-1) of std_logic_vector(15 downto 0);
  type t_word32_arr is array (0 to G_NUM_CH-1) of std_logic_vector(31 downto 0);
  type t_word64_arr is array (0 to G_NUM_CH-1) of std_logic_vector(63 downto 0);

  signal e_valid     : std_logic_vector(0 to G_NUM_CH-1);
  signal e_ready     : std_logic_vector(0 to G_NUM_CH-1);
  signal e_datalogger   : t_byte_arr;
  signal e_msg_class : t_byte_arr;
  signal e_msg_type  : t_word16_arr;
  signal e_flags     : t_word16_arr;
  signal e_ts        : t_word64_arr;
  signal e_payload   : t_word32_arr;
  signal e_words     : t_byte_arr;
begin

  -- One coalescing emitter per channel, tagged with that channel's datalogger id.
  GEN_EMITTERS : for k in 0 to G_NUM_CH-1 generate
    u_emit : entity tich.error_event_emitter
      generic map (
        G_DOMAIN       => G_DOMAIN,
        G_EVENT_ID     => G_EVENT_ID,
        G_DATALOGGER_ID   => IDS((k+1)*8-1 downto k*8),
        G_FLUSH_CYCLES => G_FLUSH_CYCLES
      )
      port map (
        CLK           => CLK,
        RST           => RST,
        ERR_CNT       => ERR_CNTS((k+1)*32-1 downto k*32),
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
