-------------------------------------------------------------------------------
-- File       : zs_msg_framer.vhd
-- Description: Wraps an incoming datalogger message into a zs_msg_header_t
--              frame, emitted as a 32-bit-wide word stream into the
--              rr-ddr-stream FIFO. PS-side DdrRingDataSource then walks
--              the DDR ring with valid zs_msg framing (magic 0x5A53)
--              instead of zs_resync alarms on every byte.
--
--              Per-frame surface (latched on IN_VALID && IN_READY):
--                - datalogger_id (8b)        — instance ID (per-datalogger)
--                - msg_class  (8b)        — DATA / STATUS / TRIGGER
--                - msg_type   (16b)       — Severity|Domain|EventID
--                - flags      (16b)       — generic flags (overflow, ...)
--                - timestamp  (64b)       — free-running global clock
--                - payload    (G_PAYLOAD_WORDS × 32b)
--
--              Emission FSM (G_PAYLOAD_WORDS = 1 PoC):
--                Cycle 0: word0 = [header_len | version | magic_hi | magic_lo]
--                Cycle 1: word1 = [msg_type_hi | msg_type_lo | msg_class | datalogger_id]
--                Cycle 2: word2 = timestamp[31:0]   (LE-byte-packed)
--                Cycle 3: word3 = timestamp[63:32]
--                Cycle 4: word4 = [flags_hi | flags_lo | payload_len_hi | payload_len_lo]
--                Cycle 5: word5 = payload[31:0]
--
--              Spec: docs/software/data_plane/message_formats/common_header.md
--                    sw/stream/zs_msg.py (PS parser mirror)
--
-- DS-P3.28 v1.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.zs_msg_pkg.all;

-- DS-T2.2 (2026-05-17): a hand-rolled 1-deep skid lives in the
-- architecture body. The functionally-equivalent rr.skid_buffer in
-- rr-ddr-stream's support/ tree was rejected because routertl's lint
-- pre-compile chain compiles `tich` before `rr`; a tich-side file
-- referencing `library rr` fails lint with "cannot find resource
-- library rr". Filed as a routertl-side gap; meanwhile the self-
-- contained skid avoids the inter-library compile-order dependency.

entity zs_msg_framer is
  generic (
    -- Maximum payload words per frame. Sets the PAYLOAD port width
    -- (G_PAYLOAD_WORDS*32 bits) and the upper bound of the runtime
    -- PAYLOAD_WORDS_LIVE count. Default 1 keeps the historic
    -- single-event behaviour for any caller that doesn't drive the
    -- new live count port.
    G_PAYLOAD_WORDS : integer := 1
  );
  port (
    CLK         : in  std_logic;
    RST         : in  std_logic;

    -- Per-frame input handshake. Latch all metadata + payload when
    -- (IN_VALID && IN_READY) — IN_READY is high only when the FSM is
    -- in S_IDLE; while emitting, IN_READY drops and the upstream
    -- (datalogger_gather's burst arbiter) holds its valid signal.
    IN_VALID    : in  std_logic;
    IN_READY    : out std_logic;
    DATALOGGER_ID  : in  std_logic_vector(7 downto 0);
    MSG_CLASS   : in  std_logic_vector(7 downto 0);
    MSG_TYPE    : in  std_logic_vector(15 downto 0);
    FLAGS       : in  std_logic_vector(15 downto 0);
    TIMESTAMP   : in  std_logic_vector(63 downto 0);
    PAYLOAD     : in  std_logic_vector(G_PAYLOAD_WORDS*32-1 downto 0);
    -- DS-P2.47 Phase B2: runtime count of payload 32-bit words to emit
    -- for THIS frame, in [1, G_PAYLOAD_WORDS]. Sampled on the input
    -- handshake. Defaulted to G_PAYLOAD_WORDS so legacy instantiations
    -- that don't drive this port keep their fixed-payload behaviour.
    PAYLOAD_WORDS_LIVE : in std_logic_vector(7 downto 0)
                         := std_logic_vector(to_unsigned(G_PAYLOAD_WORDS, 8));

    -- Output: one 32-bit word per cycle while in S_EMIT, GATED by OUT_READY.
    -- The FSM holds word_idx_r while OUT_READY=0 (see p_fsm S_EMIT), so the
    -- framer is fully back-pressure-safe — it never advances a word the sink
    -- cannot accept. OUT_READY is the downstream's not-full / accept signal:
    -- since DDR-P3.10 the top level (deepskopion_top u_ddr_burst) AND this
    -- block's TB wire it to rr-ddr-stream's REAL s_axis_ready_o (its front-end
    -- FIFO not-full). The earlier note here ("rr-ddr-stream has no wr-ready
    -- surface, so the top wires OUT_READY to '1'") was WRONG and is obsolete —
    -- genuine FIFO back-pressure is honoured end-to-end.
    OUT_DATA    : out std_logic_vector(31 downto 0);
    OUT_VALID   : out std_logic;
    OUT_READY   : in  std_logic
  );
end entity zs_msg_framer;


architecture rtl of zs_msg_framer is

  -- Upper bound on frame length in 32-bit words (header + max payload).
  -- The runtime payload_words_r picks the actual emission length within
  -- [C_ZS_NUM_HDR_WORDS, C_MAX_FRAME_WORDS].
  constant C_MAX_FRAME_WORDS   : natural := C_ZS_NUM_HDR_WORDS + G_PAYLOAD_WORDS;
  constant C_MAX_LAST_WORD_IDX : natural := C_MAX_FRAME_WORDS - 1;
  constant C_FIRST_PAYLOAD_IDX : natural := C_ZS_NUM_HDR_WORDS;       -- = 5

  type t_state is (S_IDLE, S_EMIT);
  signal state_r     : t_state := S_IDLE;
  signal word_idx_r  : natural range 0 to C_MAX_LAST_WORD_IDX := 0;

  -- Latched metadata + payload (held stable during the emission).
  signal datalogger_id_r     : std_logic_vector(7 downto 0)  := (others => '0');
  signal msg_class_r      : std_logic_vector(7 downto 0)  := (others => '0');
  signal msg_type_r       : std_logic_vector(15 downto 0) := (others => '0');
  signal flags_r          : std_logic_vector(15 downto 0) := (others => '0');
  signal timestamp_r      : std_logic_vector(63 downto 0) := (others => '0');
  signal payload_r        : std_logic_vector(G_PAYLOAD_WORDS*32-1 downto 0) := (others => '0');
  -- Live payload-words count for THIS frame (clamped on latch to
  -- [1, G_PAYLOAD_WORDS]). Drives last-word-index + payload_len_bytes.
  signal payload_words_r  : natural range 1 to G_PAYLOAD_WORDS := 1;

  -- Computed: last word index for the current frame.
  signal last_word_idx_w  : natural range C_ZS_NUM_HDR_WORDS - 1 to C_MAX_LAST_WORD_IDX;
  -- Computed: payload_len in bytes (= payload_words_r * 4), 16-bit field.
  signal payload_len_slv_w : std_logic_vector(15 downto 0);

  signal out_data_c : std_logic_vector(31 downto 0);

  ---------------------------------------------------------------------------
  -- DS-T2.2 (2026-05-17) — input-side 1-deep skid (hand-rolled)
  --
  -- Pre-2026-05-17 the FSM read DATALOGGER_ID/MSG_*/TIMESTAMP/PAYLOAD
  -- combinationally on the rising edge that latched a new tuple. With
  -- IN_READY combinational and a 1-cycle gap between IN_READY rising and
  -- the latch edge, the framer was vulnerable to same-edge writes from
  -- the producer: under sustained IN_VALID + immediate re-handshake the
  -- second tuple would be replaced by the third before the FSM sampled.
  --
  -- The 1-deep skid below registers the tuple ON the handshake edge into
  -- pre_*_r registers; the FSM samples those registers on its own clock
  -- edge. The producer can change input ports immediately after the
  -- handshake without corrupting the in-flight latch.
  --
  -- Per-field pre-latch registers (skid stage A).
  ---------------------------------------------------------------------------
  signal pre_valid_r          : std_logic := '0';
  signal pre_datalogger_id_r     : std_logic_vector(7 downto 0)  := (others => '0');
  signal pre_msg_class_r      : std_logic_vector(7 downto 0)  := (others => '0');
  signal pre_msg_type_r       : std_logic_vector(15 downto 0) := (others => '0');
  signal pre_flags_r          : std_logic_vector(15 downto 0) := (others => '0');
  signal pre_timestamp_r      : std_logic_vector(63 downto 0) := (others => '0');
  signal pre_payload_r        : std_logic_vector(G_PAYLOAD_WORDS*32 - 1 downto 0) := (others => '0');
  signal pre_payload_words_r  : std_logic_vector(7 downto 0)  :=
    std_logic_vector(to_unsigned(G_PAYLOAD_WORDS, 8));

  -- Combinational drain indicator — true when the FSM consumes the
  -- buffered tuple this cycle (state IDLE while skid full).
  signal drain_w : std_logic;

begin

  -- DS-T2.2 deep-dive 2026-05-17: IN_READY is combinational from
  -- {pre_valid_r, drain_w}. High when the skid is empty OR when the
  -- FSM is draining this cycle (so a new tuple can refill the slot
  -- in the same cycle as the drain). Without the drain term, the
  -- skid filled by a sustained-valid producer would never expose
  -- IN_READY=1 again after the first push, because pre_valid_r
  -- stays 1 across drain+refill cycles.
  drain_w  <= '1' when (pre_valid_r = '1' and state_r = S_IDLE) else '0';
  IN_READY <= '1' when (pre_valid_r = '0' or drain_w = '1') else '0';

  ---------------------------------------------------------------------------
  -- Skid process — capture inputs on (IN_VALID && IN_READY), drain when
  -- the FSM consumes (state_r = S_IDLE while pre_valid_r = '1').
  ---------------------------------------------------------------------------
  p_skid : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        pre_valid_r <= '0';
      else
        -- DS-T2.2 deep-dive 2026-05-17: the previous if/elsif chain
        -- handled drain XOR accept per edge, so when both conditions
        -- were true on the same cycle (FSM IDLE with skid full +
        -- producer offering a new tuple), drain won and the new
        -- tuple was dropped. The fix mirrors rr.skid_buffer's
        -- "both handshakes in ONE" semantics: drain + refill in the
        -- same cycle. The accept condition uses drain_w (declared
        -- combinationally above) so it matches IN_READY exactly.
        if IN_VALID = '1' and (pre_valid_r = '0' or drain_w = '1') then
          pre_valid_r          <= '1';
          pre_datalogger_id_r     <= DATALOGGER_ID;
          pre_msg_class_r      <= MSG_CLASS;
          pre_msg_type_r       <= MSG_TYPE;
          pre_flags_r          <= FLAGS;
          pre_timestamp_r      <= TIMESTAMP;
          pre_payload_r        <= PAYLOAD;
          pre_payload_words_r  <= PAYLOAD_WORDS_LIVE;
        elsif drain_w = '1' then
          pre_valid_r <= '0';
        end if;
      end if;
    end if;
  end process;

  -- DS-T2.47: mask OUT_VALID with RST. state_r only returns to S_IDLE on the
  -- synchronous reset edge, so without this mask an in-flight offered beat keeps
  -- OUT_VALID HIGH through the first reset cycle (AXIS_RST / AMBA IHI0051 §2.7.1
  -- violation). Same class as RTL-T2.26 (regbank VALID-into-reset).
  OUT_VALID <= '1' when (state_r = S_EMIT and RST = '0') else '0';

  -- Combinational helpers driven from the latched runtime word count.
  last_word_idx_w   <= (C_ZS_NUM_HDR_WORDS - 1) + payload_words_r;
  payload_len_slv_w <= std_logic_vector(to_unsigned(payload_words_r * 4, 16));

  ---------------------------------------------------------------------------
  -- FSM — latch from the skid output on (skid_valid && skid_ready),
  -- then emit (5 + payload_words_r) words.
  ---------------------------------------------------------------------------
  p_fsm : process(CLK)
    -- DS-P2.187: spans the whole 8-bit PAYLOAD_WORDS_LIVE range. It was
    -- `range 1 to G_PAYLOAD_WORDS`, which made the clamp below dead code:
    -- a live count of 0 or >G_PAYLOAD_WORDS was a range violation on the
    -- assignment itself (nvc fatal; undefined after synthesis) before the
    -- clamp could run. ZSF-REQ-52, test_zs_msg_framer_live_payload.
    variable v_latched_words : natural range 0 to 255;
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        state_r         <= S_IDLE;
        word_idx_r      <= 0;
        payload_words_r <= G_PAYLOAD_WORDS;
      else
        case state_r is
          when S_IDLE =>
            if pre_valid_r = '1' then
              datalogger_id_r <= pre_datalogger_id_r;
              msg_class_r  <= pre_msg_class_r;
              msg_type_r   <= pre_msg_type_r;
              flags_r      <= pre_flags_r;
              timestamp_r  <= pre_timestamp_r;
              payload_r    <= pre_payload_r;
              -- Clamp the live word count into [1, G_PAYLOAD_WORDS].
              -- Anything outside that range is a caller contract bug
              -- — we clip to the cap and never emit zero-payload
              -- frames (which the wire format forbids for non-status
              -- DATA classes, see edge_batch.md §4).
              v_latched_words := to_integer(unsigned(pre_payload_words_r));
              if v_latched_words < 1 then
                v_latched_words := 1;
              elsif v_latched_words > G_PAYLOAD_WORDS then
                v_latched_words := G_PAYLOAD_WORDS;
              end if;
              payload_words_r <= v_latched_words;
              state_r      <= S_EMIT;
              word_idx_r   <= 0;
            end if;

          when S_EMIT =>
            if OUT_READY = '1' then
              if word_idx_r = last_word_idx_w then
                state_r    <= S_IDLE;
                word_idx_r <= 0;
              else
                word_idx_r <= word_idx_r + 1;
              end if;
            end if;
        end case;
      end if;
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- Word emission — combinational lookup of (word_idx_r → 32-bit word).
  -- Byte layout follows common_header.md (LE-packed 20-byte struct).
  -- VHDL std_logic_vector(31 downto 0) is MSB-first; per-byte unpacking
  -- to LE storage means the MSB of the slv is byte 3 (highest address).
  -- Concatenations below order bytes [3,2,1,0] from left to right.
  ---------------------------------------------------------------------------
  p_emit : process(word_idx_r, datalogger_id_r, msg_class_r, msg_type_r,
                   flags_r, timestamp_r, payload_r, payload_len_slv_w)
    variable v_pw_idx   : natural range 0 to G_PAYLOAD_WORDS - 1;
    variable v_lo       : natural;
  begin
    case word_idx_r is
      -- Word 0: bytes [magic_lo, magic_hi, version, header_len]
      when 0 =>
        out_data_c <= C_ZS_HEADER_LEN_WORDS &
                      C_ZS_VERSION &
                      C_ZS_MAGIC(15 downto 8) &  -- 'Z' = 0x5A (byte 1 in memory)
                      C_ZS_MAGIC(7 downto 0);    -- 'S' = 0x53 (byte 0 in memory)

      -- Word 1: bytes [datalogger_id, msg_class, msg_type_lo, msg_type_hi]
      when 1 =>
        out_data_c <= msg_type_r(15 downto 8) &
                      msg_type_r(7 downto 0) &
                      msg_class_r &
                      datalogger_id_r;

      -- Word 2: timestamp[31:0] LE-byte-packed
      when 2 =>
        out_data_c <= timestamp_r(31 downto 24) &
                      timestamp_r(23 downto 16) &
                      timestamp_r(15 downto 8) &
                      timestamp_r(7 downto 0);

      -- Word 3: timestamp[63:32] LE-byte-packed
      when 3 =>
        out_data_c <= timestamp_r(63 downto 56) &
                      timestamp_r(55 downto 48) &
                      timestamp_r(47 downto 40) &
                      timestamp_r(39 downto 32);

      -- Word 4: bytes [payload_len_lo, payload_len_hi, flags_lo, flags_hi].
      -- payload_len is the runtime byte count = payload_words_r * 4.
      when 4 =>
        out_data_c <= flags_r(15 downto 8) &
                      flags_r(7 downto 0) &
                      payload_len_slv_w(15 downto 8) &
                      payload_len_slv_w(7 downto 0);

      -- Word 5+: payload word (word_idx_r - 5) of the latched buffer.
      -- The raw 32-bit datalogger/batch words ride through verbatim.
      when others =>
        if word_idx_r >= C_FIRST_PAYLOAD_IDX then
          v_pw_idx := word_idx_r - C_FIRST_PAYLOAD_IDX;
          v_lo     := v_pw_idx * 32;
          out_data_c <= payload_r(v_lo + 31 downto v_lo);
        else
          out_data_c <= (others => '0');
        end if;
    end case;
  end process;

  OUT_DATA <= out_data_c;

end architecture rtl;
