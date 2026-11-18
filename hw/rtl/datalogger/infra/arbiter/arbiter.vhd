-------------------------------------------------------------------------------
-- File       : arbiter.vhd
-- Standard   : VHDL 2008
-- Description: N-input round-robin arbiter with per-input sync FIFOs and
--              optional chaining output FIFO. Slim post-D.4 shape — token
--              messages, metadata insertion, weights, RR fill-level mode,
--              and FLUSH machinery have all been removed (vestigial under
--              the ZS-frame stamping model — every message now self-
--              describes via the downstream zs_msg_framer).
--
--              Behaviour:
--                * One sync FIFO per input (G_FIFO_DEPTH wide, FWFT).
--                * Round-robin selector rotates ONLY when the currently
--                  selected FIFO is empty (skip empty inputs without
--                  consuming a slot) OR when one word has been forwarded
--                  to the output (one word per input per RR tour).
--                * READY='0' stalls the output handshake; selector stays
--                  on the current input until the consumer accepts.
--                * Optional G_OUTPUT_FIFO=1 inserts a chaining FIFO
--                  between the mux and DOUT — used by the 2nd-layer
--                  arbiter inside datalogger_gather.
-------------------------------------------------------------------------------

library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;
use IEEE.std_logic_misc.all;
library tich;
use tich.datalogger_pkg.all;
use tich.arbiter_pkg.all;

entity arbiter is
  generic (
    G_FIFO_DEPTH  : integer := 9;                     -- log2-style: FIFO holds 2^G_FIFO_DEPTH words
    G_NUM_INPUTS  : integer := 4;
    G_OUTPUT_FIFO : integer := 1;                     -- 1 = insert chaining FIFO on output; 0 = passthrough mux
    G_DATA_WIDTH  : integer := C_INPUT_WIDTH          -- DS-P3.29: parametric so the same arbiter can carry
                                                      -- bare 32-bit datalogger messages OR wider tagged tuples
                                                      -- (payload + datalogger_id + msg_type) through the chain.
  );
  port (
    CLK        : in  std_logic;
    RST        : in  std_logic;
    -- Per-input data + valid (flat). Bit i of DIN_VALID gates the
    -- (i+1)*G_DATA_WIDTH-1 downto i*G_DATA_WIDTH slice of DIN_DATA.
    DIN_DATA   : in  std_logic_vector(G_NUM_INPUTS * G_DATA_WIDTH - 1 downto 0);
    DIN_VALID  : in  std_logic_vector(G_NUM_INPUTS - 1 downto 0);
    -- Arbitrated output stream (flat).
    DOUT_DATA  : out std_logic_vector(G_DATA_WIDTH - 1 downto 0);
    DOUT_VALID : out std_logic;
    DOUT_EMPTY : out std_logic;
    DOUT_FULL  : out std_logic;
    -- Per-input FIFO status.
    EMPTY      : out std_logic_vector(0 to G_NUM_INPUTS - 1);
    FULL       : out std_logic_vector(0 to G_NUM_INPUTS - 1);
    FIFO_CNT   : out uFifoBufferCnt(0 to G_NUM_INPUTS - 1)(G_FIFO_DEPTH + 1 - 1 downto 0);
    -- Output-side ready (consumer back-pressure).
    READY      : in  std_logic
  );
end entity arbiter;

architecture RTL of arbiter is

  -----------------------------------------------------------------------------
  -- Constants
  -----------------------------------------------------------------------------
  constant C_ONE_HOT_INIT       : std_logic_vector(G_NUM_INPUTS - 1 downto 0) := (0 => '1', others => '0');
  constant C_ARB_FIFO_FWFT      : integer := 1;
  constant C_ARB_FIFO_CNT_WIDTH : integer := G_FIFO_DEPTH + C_ARB_FIFO_FWFT;

  -----------------------------------------------------------------------------
  -- Per-input FIFO bundle
  -----------------------------------------------------------------------------
  type t_fifo_data_array is array (0 to G_NUM_INPUTS - 1) of std_logic_vector(G_DATA_WIDTH - 1 downto 0);
  type t_fifo_cnt_array  is array (0 to G_NUM_INPUTS - 1) of std_logic_vector(C_ARB_FIFO_CNT_WIDTH - 1 downto 0);

  signal fifo_din      : t_fifo_data_array := (others => (others => '0'));
  signal fifo_dout     : t_fifo_data_array := (others => (others => '0'));
  signal fifo_wr_en    : std_logic_vector(0 to G_NUM_INPUTS - 1) := (others => '0');
  signal fifo_rd_en    : std_logic_vector(0 to G_NUM_INPUTS - 1) := (others => '0');
  signal fifo_full     : std_logic_vector(0 to G_NUM_INPUTS - 1) := (others => '0');
  signal fifo_empty    : std_logic_vector(0 to G_NUM_INPUTS - 1) := (others => '1');
  signal fifo_data_cnt : t_fifo_cnt_array := (others => (others => '0'));
  signal fifo_rst_busy : std_logic_vector(0 to G_NUM_INPUTS - 1) := (others => '0');

  signal fifos_busy : std_logic := '0';

  -----------------------------------------------------------------------------
  -- Mux + RR selector
  -----------------------------------------------------------------------------
  signal sel_r        : std_logic_vector(G_NUM_INPUTS - 1 downto 0) := C_ONE_HOT_INIT;
  signal sel_index    : integer range 0 to G_NUM_INPUTS - 1 := 0;
  signal mux_data     : std_logic_vector(G_DATA_WIDTH - 1 downto 0) := (others => '0');
  signal mux_valid    : std_logic := '0';
  signal ready_int    : std_logic := '0';
  signal accept_word  : std_logic := '0';   -- mux_valid AND ready_int — output handshake fires

begin

  -----------------------------------------------------------------------------
  -- Per-input FIFOs
  -----------------------------------------------------------------------------
  GEN_FIFO_IN : for i in 0 to G_NUM_INPUTS - 1 generate
    fifo_din(i)   <= DIN_DATA((i + 1) * G_DATA_WIDTH - 1 downto i * G_DATA_WIDTH);
    -- DS-T2.1 (2026-05-16) — gate wr_en on `not fifo_rst_busy(i)` in
    -- addition to `not fifo_full(i)`. The XPM FWFT FIFO holds RST_BUSY
    -- high for ~3 cycles after RST drops; without this gate writes
    -- arriving during the RST_BUSY window are silently dropped by the
    -- macro. Contract: producers driving DIN_VALID(i) immediately on
    -- the RST-release cycle see the writes dropped — fix moves that
    -- gating inside the arbiter so the producer doesn't need to know
    -- about FIFO internals.
    fifo_wr_en(i) <= DIN_VALID(i) and not fifo_full(i) and not fifo_rst_busy(i);

    i_fifo_in : entity tich.fifo_sync
      generic map (
        G_FIFO_DEPTH  => G_FIFO_DEPTH,
        G_FIFO_WIDTH  => G_DATA_WIDTH,
        G_FIFO_FWFT   => C_ARB_FIFO_FWFT,
        G_CALLER_NAME => "Arbiter Buffer In"
      )
      port map (
        RST       => RST,
        CLK       => CLK,
        DIN       => fifo_din(i),
        WR_EN     => fifo_wr_en(i),
        RD_EN     => fifo_rd_en(i),
        DOUT      => fifo_dout(i),
        FULL      => fifo_full(i),
        PROG_FULL => open,
        EMPTY     => fifo_empty(i),
        DATA_CNT  => fifo_data_cnt(i),
        RST_BUSY  => fifo_rst_busy(i)
      );

    FULL(i)     <= fifo_full(i);
    EMPTY(i)    <= fifo_empty(i);
    FIFO_CNT(i) <= unsigned(fifo_data_cnt(i));
  end generate GEN_FIFO_IN;

  fifos_busy <= or_reduce(fifo_rst_busy);

  -----------------------------------------------------------------------------
  -- One-hot to index conversion
  -----------------------------------------------------------------------------
  PR_ONE_HOT_TO_INDEX : process (sel_r)
  begin
    sel_index <= 0;
    for i in 0 to G_NUM_INPUTS - 1 loop
      if sel_r(i) = '1' then
        sel_index <= i;
      end if;
    end loop;
  end process;

  -----------------------------------------------------------------------------
  -- Mux + handshake
  -- The selected FIFO is FWFT, so `fifo_dout(sel_index)` already holds the
  -- next word when fifo_empty(sel_index)='0'. accept_word pulses for one
  -- cycle when the downstream consumer takes a word.
  -----------------------------------------------------------------------------
  mux_data    <= fifo_dout(sel_index);
  mux_valid   <= not fifo_empty(sel_index) and not fifos_busy;
  accept_word <= mux_valid and ready_int;

  -- Pop the selected FIFO exactly when the consumer accepts the word.
  GEN_RD_EN : for i in 0 to G_NUM_INPUTS - 1 generate
    fifo_rd_en(i) <= accept_word when (i = sel_index) else '0';
  end generate GEN_RD_EN;

  -----------------------------------------------------------------------------
  -- Round-robin rotation
  -- Rotate one-hot when EITHER (a) we just popped a word from the current
  -- selected input (fair sharing — one word per tour) OR (b) the current
  -- selected input is empty and there's no rst-busy storm (skip dead
  -- slots without burning output bandwidth).
  -----------------------------------------------------------------------------
  PR_SEL : process (CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        sel_r <= C_ONE_HOT_INIT;
      elsif fifos_busy = '0' then
        if accept_word = '1' then
          sel_r <= sel_r(G_NUM_INPUTS - 2 downto 0) & sel_r(G_NUM_INPUTS - 1);
        elsif fifo_empty(sel_index) = '1' then
          sel_r <= sel_r(G_NUM_INPUTS - 2 downto 0) & sel_r(G_NUM_INPUTS - 1);
        end if;
      end if;
      assert unsigned(sel_r) /= 0
        report "arbiter: sel_r collapsed to all-zero (one-hot invariant broken)"
        severity error;
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- Output stage — optional chaining FIFO or direct mux pass-through.
  -----------------------------------------------------------------------------
  GEN_FIFO_OUT : if G_OUTPUT_FIFO > 0 generate
    signal fifo_out_din      : std_logic_vector(G_DATA_WIDTH - 1 downto 0);
    signal fifo_out_dout     : std_logic_vector(G_DATA_WIDTH - 1 downto 0);
    signal fifo_out_wr_en    : std_logic;
    signal fifo_out_rd_en    : std_logic;
    signal fifo_out_full     : std_logic;
    signal fifo_out_empty    : std_logic;
    signal fifo_out_data_cnt : std_logic_vector(C_ARB_FIFO_CNT_WIDTH - 1 downto 0);
  begin

    fifo_out_din   <= mux_data;
    fifo_out_wr_en <= accept_word;
    fifo_out_rd_en <= READY and not fifo_out_empty;
    ready_int      <= not fifo_out_full;

    i_fifo_out : entity tich.fifo_sync
      generic map (
        G_FIFO_DEPTH  => G_FIFO_DEPTH,
        G_FIFO_WIDTH  => G_DATA_WIDTH,
        G_FIFO_FWFT   => C_ARB_FIFO_FWFT,
        G_CALLER_NAME => "Arbiter Buffer Out"
      )
      port map (
        RST      => RST,
        CLK      => CLK,
        DIN      => fifo_out_din,
        WR_EN    => fifo_out_wr_en,
        RD_EN    => fifo_out_rd_en,
        DOUT     => fifo_out_dout,
        FULL     => fifo_out_full,
        EMPTY    => fifo_out_empty,
        DATA_CNT => fifo_out_data_cnt,
        RST_BUSY => open
      );

    DOUT_DATA  <= fifo_out_dout;
    DOUT_VALID <= not fifo_out_empty;
    DOUT_EMPTY <= fifo_out_empty;
    DOUT_FULL  <= fifo_out_full;

  else generate

    -- Direct pass-through mux; consumer must use READY for back-pressure.
    ready_int  <= READY and not fifos_busy;
    DOUT_DATA  <= mux_data;
    DOUT_VALID <= mux_valid and ready_int;
    DOUT_EMPTY <= not mux_valid;
    DOUT_FULL  <= '0';

  end generate GEN_FIFO_OUT;

end architecture RTL;
