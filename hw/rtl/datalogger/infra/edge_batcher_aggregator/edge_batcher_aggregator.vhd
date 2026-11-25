-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- File       : edge_batcher_aggregator.vhd
-- Description: Many-channel edge-event aggregator for the EDGE_BATCH zs_msg
--              payload (DS-P2.47). Wraps:
--                * a `device_id` demux that routes incoming 32-bit
--                  edge_event words to one of G_NUM_CHANNELS per-channel
--                  edge_batcher instances;
--                * the batchers themselves (one per channel ID);
--                * a fixed-priority arbiter (channel 0 wins on tie) that
--                  presents one ready batch at a time to the downstream
--                  zs_msg_framer's IN_* interface.
--
--              Wire contract:
--                spec   docs/software/data_plane/message_formats/payloads/edge_batch.md
--                event_id 0x01 under DOMAIN_EDGE → msg_type 0x0601, always
--                msg_class = DATA, flags = 0 in v1.
--
--              The aggregator's outputs match the zs_msg_framer's variable-
--              payload IN_* port set (DS-P2.47 Phase B2 step 1): DATALOGGER_ID,
--              MSG_CLASS, MSG_TYPE, FLAGS, TIMESTAMP, PAYLOAD,
--              PAYLOAD_WORDS_LIVE. The top-level wires them straight into
--              the framer.
--
--              DS-P2.153: THREE declared register stages — the PF-F0 100 MHz
--              timing cut. (1) EV input stage (C_EBA_INPUT_PIPE_STAGES = 1
--              cycle from the EV handshake to the selected batcher; the
--              resolved event timestamp is latched AT the handshake cycle,
--              so captured values are bit-identical to the unpipelined
--              block). (2) Registered arbitration grant
--              (C_EBA_ARB_PIPE_STAGES = 1). (3) FR output stage
--              (C_EBA_OUTPUT_PIPE_STAGES = 1: the granted winner is captured
--              store-and-forward and its batcher released at capture).
--              Flush emission latency vs the unpipelined aggregator =
--              exactly 3 cycles (EBA-REQ-40).
--
--              Single channel per batcher; multi-batch concurrency = up to
--              G_NUM_CHANNELS open batches at once. The priority arb does
--              not guarantee fairness across channels — if channel 0 keeps
--              flushing back-to-back it can starve channels 1..N-1. For PoC
--              this is fine (edge densities are similar across channels).
--              Round-robin / weighted arb is a follow-up if a hostile
--              workload surfaces starvation.
--
-- DS-P2.47 Phase B2 step 2.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

-- The `library tich` declaration forces routertl's auto-discovery to
-- compile this file AFTER tich.edge_batcher (see `entity tich.edge_batcher`
-- below). Without it the project lint chain compiles tich files in
-- glob order and trips on the unresolved entity. `rr sim --ip` against
-- this aggregator now needs the tich library pre-compiled in the work
-- path; project-wide flows handle that automatically.
library tich;

-- Wire-format enum constants — mirror tich.zs_msg_pkg. Inlined here
-- rather than `use tich.zs_msg_pkg.all` so the only reason we pull
-- tich is the entity-ordering trick above. Authoritative definitions
-- live in hw/rtl/pkg/zs_msg_pkg.vhd; mirror in
-- docs/software/data_plane/message_formats/event_and_datalogger_id_spec.md
-- and sw/stream/zs_msg.py.

entity edge_batcher_aggregator is
  generic (
    -- Number of per-channel edge_batchers to instantiate.
    G_NUM_CHANNELS    : natural := 8;

    -- Per-batcher capacity (events per batch). Sets the payload width.
    G_N_MAX           : natural := 32;

    -- Per-batcher idle-flush timer (cycles).
    G_T_FLUSH_CYCLES  : natural := 10_000;

    -- Recover acquisition time from EV_WORD instead of stamping downstream
    -- transport time. Production enables this; false preserves legacy callers.
    G_USE_EVENT_TIMESTAMP : boolean := false;

    -- DS-P3.50 — sparse→dense channel remap. Packed list (N*8 bits, dense
    -- batcher k handles GLOBAL datalogger device_id G_CHANNEL_IDS[(k+1)*8-1:k*8]).
    -- The demux maps the incoming EV_WORD[31:27] global id to the DENSE batcher
    -- index that owns it; each batcher still STAMPS its global id into
    -- BATCH_CHANNEL, so FR_DATALOGGER_ID (PS-side decode) is unchanged. This
    -- decouples the batcher count from the global-id span — with edge ids
    -- 1,2,3,6 you instantiate 4 batchers (dense), not 7 with 0/4/5 dead.
    -- Default (length /= N*8, e.g. the x"00" sentinel) builds the IDENTITY map
    -- 0,1,…,N-1 — fully back-compatible with the pre-DS-P3.50 device_id=index
    -- behaviour.
    G_CHANNEL_IDS     : std_logic_vector := x"00"
  );
  port (
    CLK              : in  std_logic;
    RST              : in  std_logic;

    -- ── Upstream event stream ──────────────────────────────────────────
    EV_VALID         : in  std_logic;
    EV_READY         : out std_logic;
    -- 32-bit edge_event word (edge_datalogger ED_MSG_OUT shape):
    --   [31:27] device_id  → selects the target batcher
    --   [26] S, [25] F, [24] E   → per-event flags
    --   [23:0] low sample-clock timestamp (used when
    --          G_USE_EVENT_TIMESTAMP is true)
    EV_WORD          : in  std_logic_vector(31 downto 0);
    TIMESTAMP_64     : in  std_logic_vector(63 downto 0);

    -- ── Downstream → zs_msg_framer IN_* interface ─────────────────────
    -- Matches zs_msg_framer port-for-port so the top-level wires this
    -- aggregator straight into the framer with no glue.
    FR_VALID            : out std_logic;
    FR_READY            : in  std_logic;
    FR_DATALOGGER_ID       : out std_logic_vector(7  downto 0);
    FR_MSG_CLASS        : out std_logic_vector(7  downto 0);
    FR_MSG_TYPE         : out std_logic_vector(15 downto 0);
    FR_FLAGS            : out std_logic_vector(15 downto 0);
    FR_TIMESTAMP        : out std_logic_vector(63 downto 0);
    FR_PAYLOAD          : out std_logic_vector(G_N_MAX*32 - 1 downto 0);
    FR_PAYLOAD_WORDS    : out std_logic_vector(7 downto 0)
  );
end entity edge_batcher_aggregator;


architecture rtl of edge_batcher_aggregator is

  -- Constant zs_msg fields for every EDGE_BATCH frame the aggregator emits.
  --   msg_class = DATA (0x00)
  --   msg_type  = SEV_INFO(0x0) | DOMAIN_EDGE(0x6) | EVT_EDGE_BATCH(0x01)
  --             = 0x0601
  constant C_CLASS_DATA          : std_logic_vector(7 downto 0)  := X"00";
  constant C_MSG_TYPE_EDGE_BATCH : std_logic_vector(15 downto 0) := X"0601";

  -- ── DS-P3.50 sparse→dense channel-id map ────────────────────────────────
  -- Effective per-batcher GLOBAL ids: use G_CHANNEL_IDS when it is the right
  -- length, else build the identity map (back-compat). Normalised to a fixed
  -- descending range so slicing is direction-independent of the literal actual.
  function f_id_map return std_logic_vector is
    variable v : std_logic_vector(G_NUM_CHANNELS*8 - 1 downto 0) := (others => '0');
  begin
    if G_CHANNEL_IDS'length = G_NUM_CHANNELS*8 then
      v := G_CHANNEL_IDS;                       -- positional copy (MSB-first)
    else
      for k in 0 to G_NUM_CHANNELS - 1 loop     -- identity 0,1,…,N-1
        v((k+1)*8 - 1 downto k*8) := std_logic_vector(to_unsigned(k, 8));
      end loop;
    end if;
    return v;
  end function f_id_map;

  constant C_IDS : std_logic_vector(G_NUM_CHANNELS*8 - 1 downto 0) := f_id_map;

  -- Global device_id → dense batcher index, or -1 if no batcher owns it.
  function f_dense_idx(global_id : natural) return integer is
  begin
    for k in 0 to G_NUM_CHANNELS - 1 loop
      if to_integer(unsigned(C_IDS((k+1)*8 - 1 downto k*8))) = global_id then
        return k;
      end if;
    end loop;
    return -1;
  end function f_dense_idx;

  -- Per-channel handshake + batch-output buses.
  signal ev_valid_per_ch       : std_logic_vector(G_NUM_CHANNELS - 1 downto 0);
  signal ev_ready_per_ch       : std_logic_vector(G_NUM_CHANNELS - 1 downto 0);
  signal batch_valid_per_ch    : std_logic_vector(G_NUM_CHANNELS - 1 downto 0);
  signal batch_ready_per_ch    : std_logic_vector(G_NUM_CHANNELS - 1 downto 0);

  -- Wide buses for per-channel batch outputs (anchor_ts / channel / n /
  -- payload). Packed flat as G_NUM_CHANNELS × per-element-width because
  -- arrays-of-vectors with generic widths trip GHDL/nvc consistency
  -- checks in some library combinations — flat slvs always synth clean.
  signal batch_anchor_ts_flat  : std_logic_vector(G_NUM_CHANNELS*64 - 1 downto 0);
  signal batch_channel_flat    : std_logic_vector(G_NUM_CHANNELS*8  - 1 downto 0);
  signal batch_n_flat          : std_logic_vector(G_NUM_CHANNELS*8  - 1 downto 0);
  signal batch_payload_flat    : std_logic_vector(G_NUM_CHANNELS*G_N_MAX*32 - 1 downto 0);

  -- Decoded GLOBAL ID for the incoming event, and the DENSE batcher index it
  -- maps to (-1 = no batcher owns this id).
  signal ev_ch_id_w   : natural range 0 to 2 ** 5 - 1;
  signal dense_idx_w  : integer range -1 to G_NUM_CHANNELS - 1;

  -- ── DS-P2.153 input pipeline stage (PF-F0 100 MHz timing closure) ───────
  -- PF-F0 measured the fabric's ONE failing path class at 100 MHz on the
  -- PolarFire MPFS095T -1 (WNS -5.5..-6.2 ns): the combinational
  -- f_event_timestamp datapath (24b compare + 40b decrement + mux) fanning
  -- out to every batcher's TIMESTAMP_64 and on into state_r / the wide
  -- payload_buf variable-slot write. This stage cuts that path at the
  -- batcher boundary: the EV word, its one-hot dense-batcher select, and the
  -- RESOLVED event timestamp are registered on the EV handshake, and the
  -- batchers consume the registered copies one cycle later.
  --
  -- DECLARED latency: C_EBA_INPUT_PIPE_STAGES = 1 cycle from the
  -- EV_VALID/EV_READY handshake to the selected batcher observing the event
  -- (EBA-REQ-40; pinned by test_edge_agg_pipeline_latency_p2_153.py).
  -- Behaviour-preserving by construction: the timestamp is LATCHED at the
  -- handshake cycle — exactly the value the unpipelined batcher sampled on
  -- that same edge — so every captured anchor/delta/payload is
  -- bit-identical; only frame emission shifts one cycle producer-relative.
  -- The stage re-arms in the cycle it drains (full 1 event/cycle rate).
  --
  -- in_ts_r holds one 64-bit copy of the resolved timestamp PER BATCHER
  -- (identical values). syn_preserve stops Synplify's equivalent-register
  -- merge so each batcher's 64-bit delta subtractor is fed by its own
  -- locally-placeable register bank instead of one high-fanout copy — the
  -- fan-out routing was part of the measured miss. Quartus/Vivado legs met
  -- timing even unpipelined, so only the Synplify attribute is needed;
  -- simulators ignore the user-declared attribute.
  constant C_EBA_INPUT_PIPE_STAGES : natural := 1;

  -- DS-P2.153 arb + output stages (see p_arb / p_output_stage below): the
  -- arbitration grant is registered, and the winning batch is captured
  -- store-and-forward on the FR_* side. Total declared latency of a flush vs
  -- the unpipelined aggregator = input + arb + output = 3 cycles
  -- (EBA-REQ-40). The three C_EBA_*_PIPE_STAGES constants ARE the declaration
  -- surface: test_edge_agg_pipeline_latency_p2_153.py parses them from this
  -- file and asserts measured flush latency == their sum == 3, so editing a
  -- constant without the matching RTL/contract change goes red.
  constant C_EBA_ARB_PIPE_STAGES    : natural := 1;
  constant C_EBA_OUTPUT_PIPE_STAGES : natural := 1;

  signal in_valid_r : std_logic := '0';
  signal in_word_r  : std_logic_vector(31 downto 0) := (others => '0');
  signal in_sel_r   : std_logic_vector(G_NUM_CHANNELS - 1 downto 0) := (others => '0');
  signal in_ts_r    : std_logic_vector(G_NUM_CHANNELS*64 - 1 downto 0) := (others => '0');

  attribute syn_preserve : boolean;
  attribute syn_preserve of in_ts_r : signal is true;

  -- The registered event leaves the stage this cycle (its batcher is ready).
  signal stage_fire_w : std_logic;
  -- The stage can take a new MAPPED event this cycle.
  signal in_ready_w   : std_logic;

  -- Output pipeline stage registers (store-and-forward of the arb winner).
  signal out_valid_r   : std_logic := '0';
  signal out_ts_r      : std_logic_vector(63 downto 0) := (others => '0');
  signal out_id_r      : std_logic_vector(7 downto 0)  := (others => '0');
  signal out_n_r       : std_logic_vector(7 downto 0)  := (others => '0');
  signal out_payload_r : std_logic_vector(G_N_MAX*32 - 1 downto 0) := (others => '0');
  signal out_fire_w    : std_logic;
  signal out_capture_w : std_logic;

  -- Registered arbitration grant (third declared stage — see p_arb).
  signal grant_valid_r : std_logic := '0';
  signal grant_sel_r   : natural range 0 to G_NUM_CHANNELS - 1 := 0;

  -- Arbiter outputs.
  signal sel_w        : natural range 0 to G_NUM_CHANNELS - 1;
  signal any_valid_w  : std_logic;
  signal event_timestamp_w : std_logic_vector(63 downto 0);

  function f_event_timestamp(
    now_ts : std_logic_vector(63 downto 0);
    word   : std_logic_vector(31 downto 0)) return std_logic_vector is
    variable v_now : unsigned(63 downto 0) := unsigned(now_ts);
    variable v_ts  : unsigned(63 downto 0) := unsigned(now_ts);
  begin
    v_ts(23 downto 0) := unsigned(word(23 downto 0));
    if unsigned(word(23 downto 0)) > v_now(23 downto 0)
       and v_now(63 downto 24) /= 0 then
      v_ts(63 downto 24) := v_now(63 downto 24) - 1;
    end if;
    return std_logic_vector(v_ts);
  end function f_event_timestamp;

begin

  event_timestamp_w <= f_event_timestamp(TIMESTAMP_64, EV_WORD)
                       when G_USE_EVENT_TIMESTAMP and EV_WORD(26) = '0'
                       else TIMESTAMP_64;

  -- ── Per-channel batchers ────────────────────────────────────────────
  gen_batchers : for i in 0 to G_NUM_CHANNELS - 1 generate
    bat_i : entity tich.edge_batcher
      generic map (
        G_N_MAX          => G_N_MAX,
        -- DS-P3.50: dense batcher i stamps the GLOBAL id it owns (C_IDS[i]),
        -- so FR_DATALOGGER_ID stays the global datalogger device_id (1/2/3/6…).
        G_CHANNEL_ID     => to_integer(unsigned(C_IDS((i+1)*8 - 1 downto i*8))),
        G_T_FLUSH_CYCLES => G_T_FLUSH_CYCLES
      )
      port map (
        CLK              => CLK,
        RST              => RST,
        EV_VALID         => ev_valid_per_ch(i),
        EV_READY         => ev_ready_per_ch(i),
        -- DS-P2.153: batchers consume the REGISTERED pipeline-stage copies
        -- (each batcher its own preserved timestamp register bank).
        EV_WORD          => in_word_r,
        TIMESTAMP_64     => in_ts_r((i+1)*64 - 1 downto i*64),
        BATCH_VALID      => batch_valid_per_ch(i),
        BATCH_READY      => batch_ready_per_ch(i),
        BATCH_ANCHOR_TS  => batch_anchor_ts_flat((i+1)*64 - 1 downto i*64),
        BATCH_CHANNEL    => batch_channel_flat((i+1)*8 - 1 downto i*8),
        BATCH_N          => batch_n_flat((i+1)*8 - 1 downto i*8),
        BATCH_PAYLOAD    => batch_payload_flat((i+1)*G_N_MAX*32 - 1
                                                downto i*G_N_MAX*32)
      );
  end generate gen_batchers;

  -- ── Input demux ─────────────────────────────────────────────────────
  -- Decode the event's GLOBAL device_id, map it to the dense batcher index,
  -- and route EV_VALID / EV_READY there. DS-P3.50: an UNMAPPED id (no batcher
  -- owns it) is DROPPED-AND-CONSUMED (EV_READY='1', no batcher sees the pulse)
  -- — NOT stalled. The pre-DS-P3.50 code presented EV_READY='0' for an
  -- out-of-range id, which is a permanent producer stall (deadlock), not a
  -- drop; the sparse map widens the unmapped surface (ids 0/4/5/7… with a
  -- {1,2,3,6} map), so a malformed/unexpected id must never wedge the whole
  -- edge pipe. Range-safety is still an upstream invariant; this is the
  -- fail-safe.
  ev_ch_id_w  <= to_integer(unsigned(EV_WORD(31 downto 27)));
  dense_idx_w <= f_dense_idx(ev_ch_id_w);

  -- ── DS-P2.153 input pipeline stage ──────────────────────────────────
  -- A mapped event is registered on the EV handshake; its batcher sees the
  -- registered copies one cycle later (C_EBA_INPUT_PIPE_STAGES). The stage
  -- drains the cycle its batcher is ready, and re-arms in that same cycle.
  p_stage_fire : process(in_valid_r, in_sel_r, ev_ready_per_ch)
    variable v_fire : std_logic;
  begin
    v_fire := '0';
    for i in 0 to G_NUM_CHANNELS - 1 loop
      if in_sel_r(i) = '1' and ev_ready_per_ch(i) = '1' then
        v_fire := '1';
      end if;
    end loop;
    stage_fire_w <= in_valid_r and v_fire;
  end process p_stage_fire;

  in_ready_w <= (not in_valid_r) or stage_fire_w;

  -- Unmapped id → drop-and-consume WITHOUT touching the stage (fail-safe, no
  -- deadlock — DS-P3.50 semantics unchanged; a stalled stage still consumes
  -- an unmapped event, since it never enters the register).
  EV_READY <= in_ready_w when dense_idx_w >= 0
              else '1';

  p_input_stage : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        in_valid_r <= '0';
        in_sel_r   <= (others => '0');
      else
        if EV_VALID = '1' and in_ready_w = '1' and dense_idx_w >= 0 then
          in_valid_r <= '1';
          in_word_r  <= EV_WORD;
          in_sel_r   <= (others => '0');
          in_sel_r(dense_idx_w) <= '1';
          -- One identical, preserved copy of the resolved timestamp per
          -- batcher (see the in_ts_r rationale above).
          for i in 0 to G_NUM_CHANNELS - 1 loop
            in_ts_r((i+1)*64 - 1 downto i*64) <= event_timestamp_w;
          end loop;
        elsif stage_fire_w = '1' then
          in_valid_r <= '0';
        end if;
      end if;
    end if;
  end process p_input_stage;

  -- Demux the REGISTERED event to its batcher.
  gen_demux : for i in 0 to G_NUM_CHANNELS - 1 generate
    ev_valid_per_ch(i) <= in_valid_r and in_sel_r(i);
  end generate gen_demux;

  -- ── Fixed-priority arbiter (lowest index wins) ──────────────────────
  -- For PoC this is fine; round-robin / weighted arb is a follow-up if
  -- a hostile workload surfaces starvation.
  --
  -- DS-P2.153: the arb runs over batch_valid MASKED by this cycle's capture
  -- acknowledge, and its DECISION IS REGISTERED (grant_valid_r/grant_sel_r —
  -- the third declared pipeline stage, C_EBA_ARB_PIPE_STAGES). With the two
  -- other stages in, the fit's residual −2.2 ns class was exactly this cone:
  -- batcher state_r → BATCH_VALID → priority arb → capture decision fanning
  -- into the batchers' enables and the G_N_MAX*32-bit capture-mux selects.
  -- The registered grant makes every select/enable a register output; the
  -- ack mask stops the just-captured (still-S_FLUSH-for-one-more-cycle)
  -- batcher from being granted, and thus captured, twice.
  p_arb : process(batch_valid_per_ch, batch_ready_per_ch)
    variable v_sel : integer range 0 to G_NUM_CHANNELS - 1;
    variable v_any : std_logic;
  begin
    v_sel := 0;
    v_any := '0';
    for i in 0 to G_NUM_CHANNELS - 1 loop
      if v_any = '0' and batch_valid_per_ch(i) = '1'
         and batch_ready_per_ch(i) = '0' then
        v_sel := integer(i);
        v_any := '1';
      end if;
    end loop;
    sel_w       <= v_sel;
    any_valid_w <= v_any;
  end process p_arb;

  p_grant : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        grant_valid_r <= '0';
        grant_sel_r   <= 0;
      else
        grant_valid_r <= any_valid_w;
        grant_sel_r   <= sel_w;
      end if;
    end if;
  end process p_grant;

  -- ── DS-P2.153 output pipeline stage (store-and-forward) ─────────────
  -- With the input stage in, the measured PF-F0 fit's worst class MOVED to
  -- the aggregator's OUTPUT cone: the 4 × G_N_MAX*32-bit payload_buf banks
  -- through this priority mux, on through the core's framer input merge into
  -- zs_msg_framer's pre_payload_r / pre_timestamp_r (endpoint slack −5.5 ns
  -- at 100 MHz) — and the merge's priority READY chain (framer IN_READY +
  -- this arb's combinational valid) fanning back into the batchers', maxfreq
  -- emitters' and gather arbiters' enables. Registering the winning batch
  -- HERE cuts both directions at once: FR_* become register outputs, and the
  -- batcher release (BATCH_READY) becomes a local capture pulse instead of a
  -- framer-deep combinational cone.
  --
  -- Store-and-forward: the winning batch is CAPTURED into out_*_r and its
  -- batcher acknowledged immediately — the batcher may start accumulating
  -- its next batch while the captured one waits for FR_READY. One batch of
  -- buffering; per-batcher order unchanged; arbitration priority unchanged
  -- (evaluated at capture). Adds the second declared latency cycle
  -- (C_EBA_OUTPUT_PIPE_STAGES; EBA-REQ-40).
  out_fire_w    <= out_valid_r and FR_READY;
  out_capture_w <= grant_valid_r and ((not out_valid_r) or out_fire_w);

  p_output_stage : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        out_valid_r <= '0';
      else
        if out_capture_w = '1' then
          out_valid_r   <= '1';
          out_ts_r      <= batch_anchor_ts_flat((grant_sel_r + 1) * 64 - 1 downto grant_sel_r * 64);
          out_id_r      <= batch_channel_flat((grant_sel_r + 1) * 8 - 1 downto grant_sel_r * 8);
          out_n_r       <= batch_n_flat((grant_sel_r + 1) * 8 - 1 downto grant_sel_r * 8);
          out_payload_r <= batch_payload_flat((grant_sel_r + 1) * G_N_MAX * 32 - 1
                                               downto grant_sel_r * G_N_MAX * 32);
        elsif out_fire_w = '1' then
          out_valid_r <= '0';
        end if;
      end if;
    end if;
  end process p_output_stage;

  -- ── Registered outputs ──────────────────────────────────────────────
  -- AMBA §2.7.1 parity with the batcher: VALID low during reset even though
  -- out_valid_r resets synchronously.
  FR_VALID         <= '1' when (out_valid_r = '1' and RST = '0') else '0';
  FR_TIMESTAMP     <= out_ts_r;
  FR_DATALOGGER_ID    <= out_id_r;
  FR_PAYLOAD_WORDS <= out_n_r;
  FR_PAYLOAD       <= out_payload_r;

  -- Constant per-frame metadata for EDGE_BATCH frames.
  FR_MSG_CLASS <= C_CLASS_DATA;
  FR_MSG_TYPE  <= C_MSG_TYPE_EDGE_BATCH;
  FR_FLAGS     <= (others => '0');

  -- Acknowledge the selected batcher on CAPTURE (not on the framer's
  -- accept): the batch now lives in out_*_r, so the batcher is free.
  p_ready_route : process(out_capture_w, grant_sel_r)
  begin
    batch_ready_per_ch <= (others => '0');
    if out_capture_w = '1' then
      batch_ready_per_ch(grant_sel_r) <= '1';
    end if;
  end process p_ready_route;

end architecture rtl;
