-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT

-- Project FIFO boundary for portable synchronous buffering.
--
-- G_FIFO_DEPTH remains the address width (the FIFO contains 2**G_FIFO_DEPTH
-- words), preserving the historical deepskopion interface.  The implementation
-- is supplied by the registry-pinned open-logic FIFO.  G_FIFO_FWFT selects the
-- observable read contract: first-word fall-through, or a one-cycle registered
-- output for the UART playback path.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library olo;

entity fifo_sync is
  generic (
    G_FIFO_DEPTH   : integer := 6;
    G_FIFO_WIDTH   : integer := 32;
    G_FIFO_FWFT    : integer := 1;
    G_FULL_SLACK   : integer := 6;
    G_EMPTY_SLACK  : integer := 6;
    G_DEBUG_TRACES : integer := 0;
    G_CALLER_NAME  : string  := "not defined"
  );
  port (
    CLK : in std_logic;
    RST : in std_logic;

    DIN        : in  std_logic_vector(G_FIFO_WIDTH - 1 downto 0);
    WR_EN      : in  std_logic;
    RD_EN      : in  std_logic;
    DOUT       : out std_logic_vector(G_FIFO_WIDTH - 1 downto 0);
    FULL       : out std_logic;
    EMPTY      : out std_logic;
    PROG_FULL  : out std_logic;
    PROG_EMPTY : out std_logic;
    DATA_CNT   : out std_logic_vector(G_FIFO_DEPTH downto 0) := (others => '0');
    RST_BUSY   : out std_logic
  );
end entity fifo_sync;

architecture rtl of fifo_sync is
  constant C_FIFO_WORD_DEPTH : positive := 2**G_FIFO_DEPTH;

  signal fifo_dout      : std_logic_vector(G_FIFO_WIDTH - 1 downto 0);
  signal fifo_dout_std  : std_logic_vector(G_FIFO_WIDTH - 1 downto 0) := (others => '0');
  signal fifo_full      : std_logic;
  signal fifo_empty     : std_logic;
  signal fifo_alm_full  : std_logic;
  signal fifo_alm_empty : std_logic;
  signal fifo_in_level  : std_logic_vector(G_FIFO_DEPTH downto 0);
  signal fifo_out_level : std_logic_vector(G_FIFO_DEPTH downto 0);
  signal fifo_in_valid  : std_logic;
  signal fifo_out_ready : std_logic;

  -- Match the former boundary's reset-busy contract: assertion is immediate,
  -- then traffic remains blocked for four complete clocks after reset release.
  signal rst_pipe : std_logic_vector(3 downto 0) := (others => '1');
  signal rst_busy_int : std_logic;
begin
  assert G_FIFO_DEPTH <= 30
    report "G_FIFO_DEPTH too large: 2 ** G_FIFO_DEPTH overflows an integer"
    severity failure;
  assert G_FIFO_FWFT = 0 or G_FIFO_FWFT = 1
    report "G_FIFO_FWFT must be 0 (registered read) or 1 (FWFT)"
    severity failure;
  assert G_FULL_SLACK >= 0 and G_FULL_SLACK <= C_FIFO_WORD_DEPTH
    report "G_FULL_SLACK must be within the FIFO depth"
    severity failure;
  assert G_EMPTY_SLACK >= 0 and G_EMPTY_SLACK <= C_FIFO_WORD_DEPTH
    report "G_EMPTY_SLACK must be within the FIFO depth"
    severity failure;

  GEN_DEBUG_TRACES : if G_DEBUG_TRACES = 1 generate
    assert false report "FIFO caller: " & G_CALLER_NAME severity note;
    assert false report "FIFO words: " & integer'image(C_FIFO_WORD_DEPTH) severity note;
  end generate GEN_DEBUG_TRACES;

  P_RESET_BUSY : process(CLK, RST)
  begin
    if RST = '1' then
      rst_pipe <= (others => '1');
    elsif rising_edge(CLK) then
      rst_pipe <= rst_pipe(rst_pipe'high - 1 downto 0) & '0';
    end if;
  end process P_RESET_BUSY;

  rst_busy_int <= '1' when rst_pipe /= "0000" else '0';
  RST_BUSY     <= rst_busy_int;

  -- Requests during reset-busy are ignored, as they were by the former FIFO
  -- primitive.  The open-logic core itself receives the synchronous reset.
  fifo_in_valid  <= WR_EN and not rst_busy_int;
  fifo_out_ready <= RD_EN and not rst_busy_int;

  i_fifo : entity olo.olo_base_fifo_sync
    generic map (
      Width_g         => G_FIFO_WIDTH,
      Depth_g         => C_FIFO_WORD_DEPTH,
      AlmFullOn_g     => true,
      AlmFullLevel_g  => C_FIFO_WORD_DEPTH - G_FULL_SLACK,
      AlmEmptyOn_g    => true,
      AlmEmptyLevel_g => G_EMPTY_SLACK,
      RamStyle_g      => "auto",
      RamBehavior_g   => "RBW",
      ReadyRstState_g => '0'
    )
    port map (
      Clk       => CLK,
      Rst       => RST,
      In_Data   => DIN,
      In_Valid  => fifo_in_valid,
      In_Ready  => open,
      In_Level  => fifo_in_level,
      Out_Data  => fifo_dout,
      Out_Valid => open,
      Out_Ready => fifo_out_ready,
      Out_Level => fifo_out_level,
      Full      => fifo_full,
      AlmFull   => fifo_alm_full,
      Empty     => fifo_empty,
      AlmEmpty  => fifo_alm_empty
    );

  -- Keep status deterministic for the entire reset-busy interval, including
  -- time zero before the open-logic core has observed its synchronous reset.
  FULL       <= '0' when rst_busy_int = '1' else fifo_full;
  EMPTY      <= '1' when rst_busy_int = '1' else fifo_empty;
  PROG_FULL  <= '0' when rst_busy_int = '1' else fifo_alm_full;
  PROG_EMPTY <= '1' when rst_busy_int = '1' else fifo_alm_empty;
  DATA_CNT   <= (others => '0') when rst_busy_int = '1' else fifo_in_level;

  GEN_FWFT : if G_FIFO_FWFT = 1 generate
    DOUT <= (others => '0') when rst_busy_int = '1' else fifo_dout;
  end generate GEN_FWFT;

  GEN_REGISTERED_READ : if G_FIFO_FWFT = 0 generate
    P_REGISTER_DOUT : process(CLK)
    begin
      if rising_edge(CLK) then
        if RST = '1' then
          fifo_dout_std <= (others => '0');
        elsif fifo_out_ready = '1' and fifo_empty = '0' then
          fifo_dout_std <= fifo_dout;
        end if;
      end if;
    end process P_REGISTER_DOUT;

    DOUT <= fifo_dout_std;
  end generate GEN_REGISTERED_READ;
end architecture rtl;

