-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- File       : edge_batcher.vhd
-- Description: Per-channel batching FSM for the EDGE_BATCH zs_msg payload
--              (DS-P2.47). Accumulates up to G_N_MAX edge_event entries
--              into a packed payload buffer, then hands the buffer off to
--              the downstream framer wrapper as a single batched frame.
--
--              Wire contract:
--                spec  docs/software/data_plane/message_formats/payloads/edge_batch.md
--                event_id  0x01 under DOMAIN_EDGE → msg_type 0x0601
--                entry layout (32 b, LE byte order on the DDR wire):
--                  bits [31:24] flags  (bit[2]=S, bit[1]=F, bit[0]=E)
--                  bits [23:16] channel (= G_CHANNEL_ID, datalogger_id)
--                  bits [15: 0] ts_delta (cycles since BATCH_ANCHOR_TS)
--
--              Inputs: a 32-bit edge_event word matching edge_datalogger.vhd
--              (device_id[31:27], S[26], F[25], E[24], timestamp_us[23:8],
--              sub_us_extension[7:0]) plus a synchronous 64-bit
--              clk_sys-tick TIMESTAMP — both arriving on the producer side
--              of the datalogger pipeline.
--
--              Flush conditions (edge_batch.md §4):
--                1. Capacity        n_r = G_N_MAX
--                2. Delta overflow  next event ts - anchor > 0xFFFF
--                3. Timer           idle_cycles = G_T_FLUSH_CYCLES
--                4. Reset           RST handled implicitly
--                (Channel switch flush doesn't fire here — this block
--                 serves a single channel; channel switch is observed
--                 only at the upstream demux that distributes events
--                 across one instance per channel.)
--
--              Single channel per instance. Top-level instantiates N
--              copies; the demux upstream routes events by device_id;
--              a round-robin arb downstream feeds zs_msg_framer.
--
-- DS-P2.47 Phase B1.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity edge_batcher is
  generic (
    -- Maximum events per batch. 32 is the PoC target — header overhead
    -- drops to ~14%, 5x efficiency over unbatched. Widen at the cost of
    -- payload-buffer BRAM (G_N_MAX * 32 bits).
    G_N_MAX           : natural := 32;

    -- Static channel/datalogger_id for this instance. Echoed into every
    -- entry's `channel` byte AND surfaced on BATCH_CHANNEL so the framer
    -- wrapper can stamp it into the zs_msg header's datalogger_id.
    G_CHANNEL_ID      : natural range 0 to 255 := 0;

    -- Idle cycles before a partial batch flushes. Default ~100 µs at
    -- 100 MHz. Keep ≤ 0xFFFF / 2 to leave room for late events before
    -- delta-overflow flush kicks in.
    G_T_FLUSH_CYCLES  : natural := 10_000
  );
  port (
    CLK              : in  std_logic;
    RST              : in  std_logic;

    -- Producer-side handshake. EV_READY drops only when the FSM is
    -- presenting a flush on the output and the framer hasn't taken it
    -- yet AND a pending overflow-event would have to be parked.
    EV_VALID         : in  std_logic;
    EV_READY         : out std_logic;
    -- 32-bit edge_event word — same encoding as edge_datalogger ED_MSG_OUT
    -- (device_id[31:27] | S[26] | F[25] | E[24] | ts_us[23:8] | ext[7:0]).
    -- The batcher only consumes S/F/E + a current-cycle timestamp; the
    -- producer-side device_id MUST equal G_CHANNEL_ID (enforced by the
    -- upstream demux; not re-checked here for area).
    EV_WORD          : in  std_logic_vector(31 downto 0);
    -- Global clk_sys-tick counter (DS-P3.30). Sampled at EV_VALID rising
    -- as the per-event timestamp; the first sample within an open batch
    -- becomes BATCH_ANCHOR_TS.
    TIMESTAMP_64     : in  std_logic_vector(63 downto 0);

    -- Downstream framer-wrapper handshake. BATCH_VALID stays high until
    -- BATCH_READY, then the FSM either drops back to IDLE (timer/cap
    -- flush) or re-arms with a single-event new batch (overflow flush).
    BATCH_VALID      : out std_logic;
    BATCH_READY      : in  std_logic;
    BATCH_ANCHOR_TS  : out std_logic_vector(63 downto 0);
    BATCH_CHANNEL    : out std_logic_vector(7  downto 0);
    BATCH_N          : out std_logic_vector(7  downto 0); -- count of valid entries
    BATCH_PAYLOAD    : out std_logic_vector(G_N_MAX*32 - 1 downto 0)
  );
end entity edge_batcher;


architecture rtl of edge_batcher is

  -- ── State ────────────────────────────────────────────────────────────────
  type t_state is (S_IDLE, S_ACCUM, S_FLUSH);
  signal state_r : t_state := S_IDLE;

  -- Anchor timestamp, latched on first event of a batch.
  signal anchor_r : unsigned(63 downto 0) := (others => '0');

  -- DS-P2.153 (PF-F0 100 MHz): delta-overflow threshold, latched WITH the
  -- anchor (= anchor + 0x1_0000). The pre-DS-P2.153 overflow detect was a
  -- full 64-bit subtract (cur - anchor) followed by a 48-bit wide compare of
  -- the result — measured as part of the fabric's worst path class on the
  -- PolarFire -1 part. Against a pre-added registered threshold the detect
  -- collapses to ONE 64-bit comparator (a single carry chain, single-bit
  -- result), and the entry's ts_delta needs only a 16-bit subtract: when no
  -- overflow fires the true delta is < 2^16, so its low 16 bits equal
  -- cur[15:0] - anchor[15:0] mod 2^16. Values are bit-identical; the 64-bit
  -- add moves onto the anchor-load register path where it has a full cycle.
  signal ovf_threshold_r : unsigned(63 downto 0) := (others => '0');

  -- Count of valid entries currently in the buffer (0..G_N_MAX).
  signal n_r : natural range 0 to G_N_MAX := 0;

  -- Idle timer — counts cycles since last EV accepted (0 when fresh).
  signal idle_r : natural range 0 to G_T_FLUSH_CYCLES := 0;

  -- Packed payload buffer.
  signal payload_buf_r : std_logic_vector(G_N_MAX*32 - 1 downto 0)
                       := (others => '0');

  -- Parked-overflow stash. Holds one event that triggered a
  -- delta-overflow flush; consumed as the first entry of the next batch.
  signal pending_valid_r : std_logic := '0';
  signal pending_word_r  : std_logic_vector(31 downto 0) := (others => '0');
  signal pending_ts_r    : unsigned(63 downto 0)         := (others => '0');

  -- ── Combinational helpers ────────────────────────────────────────────────
  signal cur_ts_u  : unsigned(63 downto 0);
  signal delta16_u : unsigned(15 downto 0);
  signal overflow_w : std_logic;
  signal flags_w   : std_logic_vector(7 downto 0);
  signal entry_w   : std_logic_vector(31 downto 0);
  signal ready_w   : std_logic;
  signal handshake_w : std_logic;

begin

  cur_ts_u <= unsigned(TIMESTAMP_64);
  -- 16-bit entry delta (see ovf_threshold_r rationale above: exact whenever
  -- overflow_w does not fire; anchor only meaningful when n_r > 0).
  delta16_u <= cur_ts_u(15 downto 0) - anchor_r(15 downto 0) when n_r > 0
               else (others => '0');

  -- A new event's delta would overflow the 16-bit ts_delta field: one
  -- comparator against the registered anchor + 0x1_0000 threshold. Only
  -- meaningful while a batch is open (n_r > 0); when n_r = 0, the
  -- incoming event becomes the new anchor and the delta is 0.
  overflow_w <= '1' when (n_r > 0 and cur_ts_u >= ovf_threshold_r)
                else '0';

  -- Compose the 4-byte entry for the candidate event.
  -- EV_WORD bit map (matches edge_datalogger.vhd):
  --   [26] = S, [25] = F, [24] = E
  flags_w <= "00000" & EV_WORD(26) & EV_WORD(25) & EV_WORD(24);

  entry_w <= flags_w &                                          -- [31:24]
             std_logic_vector(to_unsigned(G_CHANNEL_ID, 8)) &   -- [23:16]
             std_logic_vector(delta16_u);                      -- [15: 0]

  -- EV_READY signals the FSM's willingness to accept on this cycle —
  -- combinational on state alone (NOT gated by EV_VALID, per the
  -- standard handshake contract). The overflow case still asserts
  -- READY: we accept the event into the pending stash and close the
  -- current batch, so the producer sees one clean handshake.
  ready_w <= '1' when state_r = S_IDLE
             else '1' when (state_r = S_ACCUM and n_r < G_N_MAX)
             else '0';

  handshake_w <= EV_VALID and ready_w;

  -- ── Outputs ──────────────────────────────────────────────────────────────
  EV_READY        <= ready_w;
  -- AMBA §2.7.1: VALID must be LOW during reset. state_r resets SYNCHRONOUSLY, so a
  -- reset asserted mid-flush would otherwise hold BATCH_VALID high for one cycle.
  BATCH_VALID     <= '1' when (state_r = S_FLUSH and RST = '0') else '0';
  BATCH_ANCHOR_TS <= std_logic_vector(anchor_r);
  BATCH_CHANNEL   <= std_logic_vector(to_unsigned(G_CHANNEL_ID, 8));
  BATCH_N         <= std_logic_vector(to_unsigned(n_r, 8));
  BATCH_PAYLOAD   <= payload_buf_r;

  -- ── Sequential FSM ───────────────────────────────────────────────────────
  p_fsm : process(CLK)
    variable v_write_slot : natural range 0 to G_N_MAX - 1;
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        state_r         <= S_IDLE;
        anchor_r        <= (others => '0');
        ovf_threshold_r <= (others => '0');
        n_r             <= 0;
        idle_r          <= 0;
        payload_buf_r   <= (others => '0');
        pending_valid_r <= '0';
      else
        case state_r is

          ----------------------------------------------------------
          when S_IDLE =>
            -- IDLE means n_r = 0, no pending. (pending_valid_r is
            -- consumed inline at the S_FLUSH → S_ACCUM transition
            -- below, so we never observe pending_valid_r = '1' here.)
            if handshake_w = '1' then
              anchor_r        <= cur_ts_u;
              -- 64-bit add on the register-load path (full cycle). Wrap of
              -- the 64-bit tick counter (5849 years @100 MHz) can misjudge
              -- overflow for one batch near the wrap point — accepted.
              ovf_threshold_r <= cur_ts_u + to_unsigned(16#1_0000#, 64);
              payload_buf_r(31 downto 0) <=
                flags_w &
                std_logic_vector(to_unsigned(G_CHANNEL_ID, 8)) &
                X"0000";
              n_r             <= 1;
              idle_r          <= 0;
              state_r         <= S_ACCUM;
            end if;

          ----------------------------------------------------------
          when S_ACCUM =>
            -- Priority: delta-overflow flush > capacity flush > timer
            -- flush > normal accumulation. Overflow + capacity both
            -- gated by a handshake firing (handshake_w); timer fires
            -- only when idle. Note: ready_w is high in S_ACCUM iff
            -- n_r < G_N_MAX, so handshake_w being '1' here always
            -- means n_r < G_N_MAX (no need to recheck).
            if handshake_w = '1' and overflow_w = '1' then
              -- The handshake fires (producer sees ack and drops
              -- VALID), but the event lands in the pending stash —
              -- not in the current batch. The open batch closes;
              -- the parked event re-emerges as the new batch's
              -- anchor on the FLUSH → ACCUM transition.
              pending_valid_r <= '1';
              pending_word_r  <= EV_WORD;
              pending_ts_r    <= cur_ts_u;
              state_r         <= S_FLUSH;
            elsif handshake_w = '1' then
              v_write_slot := n_r;
              payload_buf_r((v_write_slot + 1) * 32 - 1 downto v_write_slot * 32)
                <= entry_w;
              if n_r + 1 = G_N_MAX then
                -- Capacity flush.
                n_r     <= G_N_MAX;
                idle_r  <= 0;
                state_r <= S_FLUSH;
              else
                n_r     <= n_r + 1;
                idle_r  <= 0;
              end if;
            else
              -- No handshake this cycle — advance idle timer.
              if idle_r + 1 >= G_T_FLUSH_CYCLES and n_r > 0 then
                idle_r  <= 0;
                state_r <= S_FLUSH;
              else
                idle_r <= idle_r + 1;
              end if;
            end if;

          ----------------------------------------------------------
          when S_FLUSH =>
            if BATCH_READY = '1' then
              if pending_valid_r = '1' then
                -- A parked overflow event needs to start the next
                -- batch. Consume it inline (FLUSH → ACCUM directly,
                -- skipping IDLE) so the producer's still-asserted
                -- VALID line — if any — isn't re-admitted as a
                -- duplicate event during a transient IDLE pass.
                anchor_r        <= pending_ts_r;
                ovf_threshold_r <= pending_ts_r + to_unsigned(16#1_0000#, 64);
                payload_buf_r   <= (others => '0');
                payload_buf_r(31 downto 0) <=
                  ("00000" & pending_word_r(26) & pending_word_r(25)
                           & pending_word_r(24)) &
                  std_logic_vector(to_unsigned(G_CHANNEL_ID, 8)) &
                  X"0000";
                n_r             <= 1;
                idle_r          <= 0;
                pending_valid_r <= '0';
                state_r         <= S_ACCUM;
              else
                -- Empty out → IDLE.
                n_r           <= 0;
                idle_r        <= 0;
                payload_buf_r <= (others => '0');
                state_r       <= S_IDLE;
              end if;
            end if;

        end case;
      end if;
    end if;
  end process p_fsm;

end architecture rtl;
