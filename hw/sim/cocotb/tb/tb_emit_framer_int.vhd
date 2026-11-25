-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- tb_emit_framer_int.vhd — DS-P3.71 integration repro (sim only)
-------------------------------------------------------------------------------
-- Faithfully replicates deepskopion_top's error_event_emitter → 3:1 mux →
-- zs_msg_framer wiring (err > gather; aggregator leg omitted — irrelevant to
-- the loss path) so a cocotb test can reproduce the SILICON condition:
--
--   * downstream backpressured (OUT_READY=0  ≡ DDR ring Full),
--   * a competing gather/edge frame contending at the mux,
--   * ERR_CNT climbing (edge FIFO overflow) → emitter latches a pending report,
--   * then OUT_READY released → does the SEV_ERROR loss frame actually emerge
--     from the framer's OUT stream, or is it starved/dropped?
--
-- The mux expressions below are copied verbatim from deepskopion_top.vhd
-- (lines 1215-1243) with agg_fr_valid_w tied '0'.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.zs_msg_pkg.all;

entity tb_emit_framer_int is
  generic (
    G_FLUSH_CYCLES : positive := 20
  );
  port (
    CLK             : in  std_logic;
    RST             : in  std_logic;
    -- emitter stimulus
    ERR_CNT         : in  std_logic_vector(31 downto 0);
    TIMESTAMP       : in  std_logic_vector(63 downto 0);
    -- competing gather/edge frame (non-error)
    GATHER_VALID    : in  std_logic;
    GATHER_MSG_TYPE : in  std_logic_vector(15 downto 0);
    GATHER_PAYLOAD  : in  std_logic_vector(31 downto 0);
    -- downstream backpressure (0 = ring Full)
    OUT_READY       : in  std_logic;
    OUT_DATA        : out std_logic_vector(31 downto 0);
    OUT_VALID       : out std_logic;
    -- observability
    ERR_FR_VALID    : out std_logic;
    FRAMER_IN_READY : out std_logic
  );
end entity tb_emit_framer_int;

architecture rtl of tb_emit_framer_int is
  -- emitter outputs
  signal e_valid   : std_logic;
  signal e_ready   : std_logic;
  signal e_snid    : std_logic_vector(7 downto 0);
  signal e_class   : std_logic_vector(7 downto 0);
  signal e_mtype   : std_logic_vector(15 downto 0);
  signal e_flags   : std_logic_vector(15 downto 0);
  signal e_ts      : std_logic_vector(63 downto 0);
  signal e_pld     : std_logic_vector(31 downto 0);
  signal e_words   : std_logic_vector(7 downto 0);
  -- framer inputs (mux outputs)
  signal f_in_valid : std_logic;
  signal f_in_ready : std_logic;
  signal f_snid     : std_logic_vector(7 downto 0);
  signal f_class    : std_logic_vector(7 downto 0);
  signal f_mtype    : std_logic_vector(15 downto 0);
  signal f_flags    : std_logic_vector(15 downto 0);
  signal f_ts       : std_logic_vector(63 downto 0);
  signal f_pld      : std_logic_vector(31 downto 0);
  signal f_words    : std_logic_vector(7 downto 0);
begin
  u_emit : entity tich.error_event_emitter
    generic map (
      G_DOMAIN       => C_ZS_DOMAIN_EDGE,
      G_EVENT_ID     => C_ZS_ERR_EVT_INTERNAL_FIFO_OVERFLOW,
      G_DATALOGGER_ID   => (others => '0'),
      G_FLUSH_CYCLES => G_FLUSH_CYCLES
    )
    port map (
      CLK => CLK, RST => RST,
      ERR_CNT => ERR_CNT, TIMESTAMP => TIMESTAMP,
      FR_VALID => e_valid, FR_READY => e_ready,
      FR_DATALOGGER_ID => e_snid, FR_MSG_CLASS => e_class, FR_MSG_TYPE => e_mtype,
      FR_FLAGS => e_flags, FR_TIMESTAMP => e_ts, FR_PAYLOAD => e_pld, FR_WORDS => e_words
    );

  -- 3:1 mux (err > [agg omitted] > gather) — verbatim from deepskopion_top.
  f_in_valid <= e_valid or GATHER_VALID;
  f_snid     <= e_snid  when e_valid = '1' else (others => '0');
  f_class    <= e_class when e_valid = '1' else C_ZS_CLASS_DATA;
  f_mtype    <= e_mtype when e_valid = '1' else GATHER_MSG_TYPE;
  f_flags    <= e_flags when e_valid = '1' else (others => '0');
  f_ts       <= e_ts    when e_valid = '1' else TIMESTAMP;
  f_pld      <= e_pld   when e_valid = '1' else GATHER_PAYLOAD;
  f_words    <= e_words when e_valid = '1' else std_logic_vector(to_unsigned(1, 8));
  e_ready    <= f_in_ready when e_valid = '1' else '0';

  u_framer : entity tich.zs_msg_framer
    generic map (G_PAYLOAD_WORDS => 1)
    port map (
      CLK => CLK, RST => RST,
      IN_VALID => f_in_valid, IN_READY => f_in_ready,
      DATALOGGER_ID => f_snid, MSG_CLASS => f_class, MSG_TYPE => f_mtype,
      FLAGS => f_flags, TIMESTAMP => f_ts, PAYLOAD => f_pld,
      PAYLOAD_WORDS_LIVE => f_words,
      OUT_DATA => OUT_DATA, OUT_VALID => OUT_VALID, OUT_READY => OUT_READY
    );

  ERR_FR_VALID    <= e_valid;
  FRAMER_IN_READY <= f_in_ready;
end architecture rtl;
