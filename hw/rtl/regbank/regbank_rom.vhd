-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- File       : regbank_rom.vhd
-- Description: BRAM-init ROM loaded from a hex file at synthesis time.
--              Single read port, registered output (1-cycle read latency).
--              Used by system_regbank_wrapper to expose the encoded
--              regbank manifest (regbank_schema.hex) and build-config
--              manifest (config_schema.hex) to PS firmware via the
--              streaming-read register pattern.
--
--              Pattern: SW reads <name>_NUM_WORDS to seed the count
--              (which also resets the pointer in the wrapper), then
--              reads <name>_DATA N times to drain. Each DATA read
--              advances the pointer by one, so the next read returns
--              the next word.
-------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;
use STD.TEXTIO.all;

entity regbank_rom is
  generic (
    G_MEM_DEPTH : natural := 9;                    -- address width in bits (ROM depth = 2^N)
    G_MEM_WIDTH : natural := 32;                   -- data width in bits
    G_HEX_FILE  : string  := "regbank_schema.hex"  -- path to the synth-time hex file
  );
  port (
    CLK  : in  std_logic;
    ADDR : in  unsigned(G_MEM_DEPTH-1 downto 0);
    DOUT : out std_logic_vector(G_MEM_WIDTH-1 downto 0)
  );
end entity regbank_rom;

architecture rtl of regbank_rom is

  -- Sentinel value for uninitialized memory — easy to spot in a wave
  -- view as a "we read past the end" signature.
  constant C_DEFAULT_INIT : std_logic_vector(G_MEM_WIDTH-1 downto 0) := x"BAD00BAD";

  type t_rom_array is array (natural range 0 to 2**G_MEM_DEPTH-1) of std_logic_vector(G_MEM_WIDTH-1 downto 0);

  impure function load_rom return t_rom_array is
    file     hex_file : text open read_mode is G_HEX_FILE;
    variable rom      : t_rom_array := (others => C_DEFAULT_INIT);
    variable line_buf : line;
    variable word     : std_logic_vector(G_MEM_WIDTH-1 downto 0);
    variable idx      : integer := 0;
    -- DS-P2.249: Synplify bounds elaboration loops by syn_looplimit and
    -- stops with CD353 on the 7275-line regbank manifest. The loop runs at
    -- most once per ROM word (idx <= rom'high), so that is its limit.
    attribute syn_looplimit : integer;
    attribute syn_looplimit of L_LOAD : label is 2**G_MEM_DEPTH + 1;
  begin
    L_LOAD : while not endfile(hex_file) and idx <= rom'high loop
      readline(hex_file, line_buf);
      hread(line_buf, word);
      rom(idx) := word;
      idx := idx + 1;
    end loop L_LOAD;
    -- DS-P1.82: a hex file longer than the ROM used to be cut off here
    -- without a message (a 7275-word manifest in a 1024-word ROM).
    assert endfile(hex_file)
      report "regbank_rom: " & G_HEX_FILE & " holds more than 2**G_MEM_DEPTH = "
             & integer'image(2**G_MEM_DEPTH) & " words; raise G_MEM_DEPTH"
      severity failure;
    return rom;
  end function;

  signal rom_data : t_rom_array := load_rom;

begin

  P_READ : process(CLK)
  begin
    if rising_edge(CLK) then
      DOUT <= rom_data(to_integer(ADDR));
    end if;
  end process;

end architecture rtl;
