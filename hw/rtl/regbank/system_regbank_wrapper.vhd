-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- File       : system_regbank_wrapper.vhd
-- Description: Wraps the auto-generated AXI-Lite system_regbank with two
--              streaming-read ROMs that PS firmware uses to introspect
--              the running bitstream:
--
--              - regbank manifest ROM  ← regbank_schema.hex (encoded
--                project_regbank.yml). Read via REGBANK_NUM_WORDS
--                (reset pointer) + REGBANK_DATA × N (drain).
--              - build-config ROM      ← config_schema.hex (encoded
--                datalogger_config.yml — which dataloggers + counts + pin
--                widths). Read via CONFIG_NUM_WORDS + CONFIG_DATA × N.
--
--              See datalogger_gather_gen.py for the v0.1 config-schema
--              encoding. The wrapper snoops the AXI-Lite AR channel
--              before forwarding to the inner regbank: a NUM_WORDS
--              read resets the corresponding pointer to 0; a DATA
--              read increments it by one. Both act on the AR
--              HANDSHAKE (ARVALID and ARREADY), once per read: the
--              inner regbank raises ARREADY a clock after ARVALID, so
--              ARVALID alone is high for two or more clocks of every
--              compliant read (DS-P1.82). Each cycle the ROM DOUT is
--              fed back through REGS_IN[DATA_INDEX] so the inner
--              regbank's latched view of that slot stays fresh; the
--              next SW read returns the latched word.
--
--              The wrapper owns all four streaming slots. Each ROM's
--              word count is the generated C_<name>_WORDS of
--              rom_info_pkg (G_<name>_WORDS); it sets that ROM's depth
--              (and pointer width) and is what <name>_NUM_WORDS reports,
--              on every top, whatever TELEMETRY_IN carries for those
--              slots (DS-P1.82). The count is not read from the hex file
--              at elaboration: Synplify stops at its loop limit on the
--              7275-line manifest (CD353, DS-P2.249). In simulation an
--              assertion checks the count against the hex file.
--
--              Companion to:
--                - hw/rtl/regbank/system_regbank.vhd   (auto-gen)
--                - hw/rtl/regbank/regbank_rom.vhd     (BRAM primitive)
--                - hw/rtl/pkg/rom_info_pkg.vhd         (C_REGBANK_HEX,
--                                                       C_CONFIG_HEX,
--                                                       C_*_WORDS counts)
-------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;
use STD.TEXTIO.all;

library tich;
use tich.rom_info_pkg.all;
use tich.system_regbank_pkg.all;

entity system_regbank_wrapper is
  generic (
    G_ADDR_WIDTH : integer := 11;
    G_DATA_WIDTH : integer := 32;

    -- ROM init file paths. Default to the clone-relative paths baked into
    -- rom_info_pkg by the SDK generator — these only resolve when the
    -- elaborating tool's cwd is the project root (true for `rr build`/
    -- `rr synth`, and for Vivado thanks to the stage_regbank_roms_pre.tcl
    -- pre-synth hook that recreates that relative path under [pwd]). An
    -- out-of-tree caller (e.g. a sim harness invoked from an arbitrary
    -- temp cwd) overrides these generics with absolute paths instead of
    -- depending on cwd at all — see tb_regbank_wrapper_rom_seam.vhd.
    G_REGBANK_HEX_FILE : string := C_REGBANK_HEX;
    G_CONFIG_HEX_FILE  : string := C_CONFIG_HEX;

    -- ROM word counts (DS-P2.249). Default: the counts the SDK generator
    -- wrote into rom_info_pkg from the same hex files. A caller that passes
    -- its own hex file passes its word count too; simulation asserts that
    -- the count equals the file's non-empty line count.
    G_REGBANK_WORDS : natural := to_integer(unsigned(C_REGBANK_WORDS));
    G_CONFIG_WORDS  : natural := to_integer(unsigned(C_CONFIG_WORDS))
  );
  port (
    CLK           : in  std_logic;
    RST           : in  std_logic;

    -- AXI4-Lite Slave Interface (pass-through to inner regbank)
    S_AXI_AWADDR  : in  std_logic_vector(G_ADDR_WIDTH-1 downto 0);
    S_AXI_AWPROT  : in  std_logic_vector(2 downto 0);
    S_AXI_AWVALID : in  std_logic;
    S_AXI_AWREADY : out std_logic;

    S_AXI_WDATA   : in  std_logic_vector(G_DATA_WIDTH-1 downto 0);
    S_AXI_WSTRB   : in  std_logic_vector(G_DATA_WIDTH/8-1 downto 0);
    S_AXI_WVALID  : in  std_logic;
    S_AXI_WREADY  : out std_logic;

    S_AXI_BRESP   : out std_logic_vector(1 downto 0);
    S_AXI_BVALID  : out std_logic;
    S_AXI_BREADY  : in  std_logic;

    S_AXI_ARADDR  : in  std_logic_vector(G_ADDR_WIDTH-1 downto 0);
    S_AXI_ARPROT  : in  std_logic_vector(2 downto 0);
    S_AXI_ARVALID : in  std_logic;
    S_AXI_ARREADY : out std_logic;

    S_AXI_RDATA   : out std_logic_vector(G_DATA_WIDTH-1 downto 0);
    S_AXI_RRESP   : out std_logic_vector(1 downto 0);
    S_AXI_RVALID  : out std_logic;
    S_AXI_RREADY  : in  std_logic;

    -- Decoded datalogger CONFIG bus — low 16 bits of the DATALOGGER_CONFIG
    -- register. Drives datalogger_gather.CONFIG (which then routes per-
    -- datalogger-id through config_control). Stable across cycles —
    -- updates only when SW writes the DATALOGGER_CONFIG slot.
    DATALOGGER_CONFIG : out std_logic_vector(15 downto 0);

    -- Telemetry fan-in. Top wrapper assembles a sparse t_memArrayReg
    -- — driving specific RO slot indices (HEARTBEAT_CNT, PKT_CNT_*,
    -- ERR_CNT_*, etc.) and leaving the rest zero. The wrapper
    -- forwards these into the inner regbank's REGS_IN, EXCEPT for
    -- the two streaming-ROM DATA slots which the wrapper itself
    -- owns (REGBANK_DATA, CONFIG_DATA).
    TELEMETRY_IN   : in  t_memArrayReg := (others => (data => (others => '0'), valid => '0'));

    -- Full user-logic record interface — surfaced for downstream
    -- consumers that want to observe SW writes (not used by the
    -- top wrapper today; kept for symmetry with the inner entity).
    REGS_OUT       : out t_memArrayReg
  );
end entity system_regbank_wrapper;

architecture rtl of system_regbank_wrapper is

  -- ROM sizes come from the generated word counts (DS-P1.82, DS-P2.249). A
  -- fixed 10-bit depth once held a 7275-word regbank manifest in 1024 words:
  -- regbank_rom dropped the rest and the pointer wrapped.
  -- pragma translate_off
  impure function hex_word_count(path : string) return natural is
    file     f : text open read_mode is path;
    variable l : line;
    variable n : natural := 0;
  begin
    while not endfile(f) loop
      readline(f, l);
      if l'length > 0 then
        n := n + 1;
      end if;
    end loop;
    return n;
  end function;
  -- pragma translate_on

  function clog2(n : natural) return natural is
    variable bits : natural := 1;  -- at least one address bit
  begin
    while 2**bits < n loop
      bits := bits + 1;
    end loop;
    return bits;
  end function;

  constant C_REGBANK_WORD_COUNT : natural := G_REGBANK_WORDS;
  constant C_CONFIG_WORD_COUNT  : natural := G_CONFIG_WORDS;
  constant C_REGBANK_DEPTH_BITS : natural := clog2(C_REGBANK_WORD_COUNT);
  constant C_CONFIG_DEPTH_BITS  : natural := clog2(C_CONFIG_WORD_COUNT);

  -- Word-index byte-offsets from project_regbank.yml. C_INDEX_* are
  -- byte addresses (the generator's convention), so >> 2 to word.
  constant C_REGBANK_NUM_IDX  : integer := C_INDEX_REGBANK_NUM_WORDS / 4;
  constant C_REGBANK_DATA_IDX : integer := C_INDEX_REGBANK_DATA      / 4;
  constant C_CONFIG_NUM_IDX   : integer := C_INDEX_CONFIG_NUM_WORDS  / 4;
  constant C_CONFIG_DATA_IDX  : integer := C_INDEX_CONFIG_DATA       / 4;
  constant C_DATALOGGER_CFG_IDX  : integer := C_INDEX_DATALOGGER_CONFIG    / 4;

  -- Pointer registers (one per ROM)
  signal regbank_rom_addr : unsigned(C_REGBANK_DEPTH_BITS-1 downto 0) := (others => '0');
  signal config_rom_addr  : unsigned(C_CONFIG_DEPTH_BITS-1 downto 0) := (others => '0');

  -- ROM data outputs (1-cycle read latency)
  signal regbank_rom_data : std_logic_vector(G_DATA_WIDTH-1 downto 0);
  signal config_rom_data  : std_logic_vector(G_DATA_WIDTH-1 downto 0);

  -- Inner regbank user-logic feed
  signal regs_in        : t_memArrayReg := (others => (data => (others => '0'), valid => '0'));
  signal regs_out_inner : t_memArrayReg;

  -- AR snoop — capture which word index SW is reading this cycle.
  signal ar_handshake : std_logic;
  signal arready_i    : std_logic;
  signal ar_word_idx  : integer range 0 to 2**(G_ADDR_WIDTH-2) - 1;

begin

  -- pragma translate_off
  -- DS-P2.249: the generated counts must describe the hex files the ROMs
  -- load. Simulation only: Synplify cannot run this loop over the
  -- 7275-line manifest (CD353), and synthesis loads the ROMs from the same
  -- files the generator counted.
  assert hex_word_count(G_REGBANK_HEX_FILE) = G_REGBANK_WORDS
    report "system_regbank_wrapper: " & G_REGBANK_HEX_FILE & " holds "
           & integer'image(hex_word_count(G_REGBANK_HEX_FILE))
           & " words, G_REGBANK_WORDS = " & integer'image(G_REGBANK_WORDS)
    severity failure;
  assert hex_word_count(G_CONFIG_HEX_FILE) = G_CONFIG_WORDS
    report "system_regbank_wrapper: " & G_CONFIG_HEX_FILE & " holds "
           & integer'image(hex_word_count(G_CONFIG_HEX_FILE))
           & " words, G_CONFIG_WORDS = " & integer'image(G_CONFIG_WORDS)
    severity failure;
  -- pragma translate_on

  ---------------------------------------------------------------------------
  -- AR snoop — fires once per read, on the clock the inner regbank accepts
  -- the address (ARVALID and ARREADY). ARADDR is stable while ARVALID waits
  -- for ARREADY (AXI), so it names the slot being read on that clock.
  --
  -- Note: this snoop runs in parallel with the inner regbank. It does
  -- NOT gate AR — both wrapper and inner observe the same AR handshake.
  ---------------------------------------------------------------------------
  S_AXI_ARREADY <= arready_i;
  ar_handshake  <= S_AXI_ARVALID and arready_i;
  ar_word_idx  <= to_integer(unsigned(S_AXI_ARADDR(G_ADDR_WIDTH-1 downto 2)));

  P_REGBANK_PTR : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        regbank_rom_addr <= (others => '0');
      elsif ar_handshake = '1' then
        if ar_word_idx = C_REGBANK_NUM_IDX then
          regbank_rom_addr <= (others => '0');
        elsif ar_word_idx = C_REGBANK_DATA_IDX then
          regbank_rom_addr <= regbank_rom_addr + 1;
        end if;
      end if;
    end if;
  end process;

  P_CONFIG_PTR : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        config_rom_addr <= (others => '0');
      elsif ar_handshake = '1' then
        if ar_word_idx = C_CONFIG_NUM_IDX then
          config_rom_addr <= (others => '0');
        elsif ar_word_idx = C_CONFIG_DATA_IDX then
          config_rom_addr <= config_rom_addr + 1;
        end if;
      end if;
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- Two BRAM-init ROMs — loaded at synth from the .hex files
  -- referenced by C_REGBANK_HEX / C_CONFIG_HEX in rom_info_pkg.
  ---------------------------------------------------------------------------
  u_regbank_rom : entity tich.regbank_rom
    generic map (
      G_MEM_DEPTH => C_REGBANK_DEPTH_BITS,
      G_MEM_WIDTH => G_DATA_WIDTH,
      G_HEX_FILE  => G_REGBANK_HEX_FILE
    )
    port map (
      CLK  => CLK,
      ADDR => regbank_rom_addr,
      DOUT => regbank_rom_data
    );

  u_config_rom : entity tich.regbank_rom
    generic map (
      G_MEM_DEPTH => C_CONFIG_DEPTH_BITS,
      G_MEM_WIDTH => G_DATA_WIDTH,
      G_HEX_FILE  => G_CONFIG_HEX_FILE
    )
    port map (
      CLK  => CLK,
      ADDR => config_rom_addr,
      DOUT => config_rom_data
    );

  ---------------------------------------------------------------------------
  -- REGS_IN injection — three layers, evaluated in order so later
  -- layers win on collision:
  --   1. Telemetry slots (HEARTBEAT_CNT, PKT_CNT_*, ERR_CNT_*, …):
  --      copy from TELEMETRY_IN (top wrapper assembles the sparse
  --      array). Slots the top doesn't touch stay zero.
  --   2. Streaming ROM slots: DATA (REGBANK_DATA, CONFIG_DATA)
  --      continuously refresh from the corresponding ROM DOUT so the
  --      inner regbank's latched view stays one step ahead of the
  --      next SW read; NUM_WORDS (REGBANK_NUM_WORDS, CONFIG_NUM_WORDS)
  --      carry the word count read from each hex file.
  --   3. (Reserved) future per-slot wrapper-owned overrides.
  --
  -- The valid pulse triggers reg_bank latching every cycle; reads see
  -- the most recent value.
  ---------------------------------------------------------------------------
  P_REGS_IN_DRIVE : process(CLK)
  begin
    if rising_edge(CLK) then
      -- Layer 1: telemetry fan-in
      regs_in <= TELEMETRY_IN;
      -- Layer 2: streaming-ROM data (overrides telemetry on these
      -- two indices, but telemetry callers shouldn't touch them).
      regs_in(C_REGBANK_DATA_IDX).data  <= regbank_rom_data;
      regs_in(C_REGBANK_DATA_IDX).valid <= '1';
      regs_in(C_CONFIG_DATA_IDX).data   <= config_rom_data;
      regs_in(C_CONFIG_DATA_IDX).valid  <= '1';
      regs_in(C_REGBANK_NUM_IDX).data   <= std_logic_vector(to_unsigned(C_REGBANK_WORD_COUNT, G_DATA_WIDTH));
      regs_in(C_REGBANK_NUM_IDX).valid  <= '1';
      regs_in(C_CONFIG_NUM_IDX).data    <= std_logic_vector(to_unsigned(C_CONFIG_WORD_COUNT, G_DATA_WIDTH));
      regs_in(C_CONFIG_NUM_IDX).valid   <= '1';
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- Inner auto-generated regbank — pass-through AXI-Lite + the streaming
  -- ROM data via REGS_IN.
  ---------------------------------------------------------------------------
  u_inner : entity tich.system_regbank
    generic map (
      G_ADDR_WIDTH => G_ADDR_WIDTH,
      G_DATA_WIDTH => G_DATA_WIDTH
    )
    port map (
      CLK           => CLK,
      RST           => RST,
      S_AXI_AWADDR  => S_AXI_AWADDR,
      S_AXI_AWPROT  => S_AXI_AWPROT,
      S_AXI_AWVALID => S_AXI_AWVALID,
      S_AXI_AWREADY => S_AXI_AWREADY,
      S_AXI_WDATA   => S_AXI_WDATA,
      S_AXI_WSTRB   => S_AXI_WSTRB,
      S_AXI_WVALID  => S_AXI_WVALID,
      S_AXI_WREADY  => S_AXI_WREADY,
      S_AXI_BRESP   => S_AXI_BRESP,
      S_AXI_BVALID  => S_AXI_BVALID,
      S_AXI_BREADY  => S_AXI_BREADY,
      S_AXI_ARADDR  => S_AXI_ARADDR,
      S_AXI_ARPROT  => S_AXI_ARPROT,
      S_AXI_ARVALID => S_AXI_ARVALID,
      S_AXI_ARREADY => arready_i,
      S_AXI_RDATA   => S_AXI_RDATA,
      S_AXI_RRESP   => S_AXI_RRESP,
      S_AXI_RVALID  => S_AXI_RVALID,
      S_AXI_RREADY  => S_AXI_RREADY,
      REGS_IN       => regs_in,
      REGS_OUT      => regs_out_inner
    );

  ---------------------------------------------------------------------------
  -- Top-level fan-out: tap REGS_OUT for the DATALOGGER_CONFIG slot and
  -- expose its low 16 bits as a flat output. Mirror the full record
  -- array to REGS_OUT for any other top-level consumer.
  ---------------------------------------------------------------------------
  REGS_OUT       <= regs_out_inner;
  DATALOGGER_CONFIG <= regs_out_inner(C_DATALOGGER_CFG_IDX).data(15 downto 0);

end architecture rtl;
