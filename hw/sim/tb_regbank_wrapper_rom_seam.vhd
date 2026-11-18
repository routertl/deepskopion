-------------------------------------------------------------------------------
-- Title      : tb_regbank_wrapper_rom_seam
-- Project    : deepskopion / DS-T4.1 out-of-tree ROM-path seam
-------------------------------------------------------------------------------
-- File       : tb_regbank_wrapper_rom_seam.vhd
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Minimal sim wrapper around the REAL tich.system_regbank_wrapper — the
--   ONLY testbench in this tree that elaborates the wrapper's two BRAM-init
--   ROMs (regbank_schema.hex / config_schema.hex). Every other regbank seam
--   tb (tb_regbank_i2c_inj_seam, tb_regbank_spi_inj_seam,
--   tb_regbank_datalogger_config_seam, tb_regbank_axilite_mon) deliberately
--   instantiates the INNER tich.system_regbank instead, sidestepping the
--   ROMs entirely — see the "not system_regbank_wrapper" comments in those
--   files. That sidestep is exactly the workaround this ticket removes: a
--   caller that actually needs the wrapper (e.g. the real deepskopion_core)
--   was, before this fix, only elaborable when the tool's cwd happened to be
--   the project root (regbank_rom.vhd's `file ... open read_mode is
--   G_HEX_FILE` is a plain TEXTIO open, resolved by the simulator against
--   its OWN process cwd at elaboration time, not the source file's
--   location).
--
--   G_REGBANK_HEX_FILE / G_CONFIG_HEX_FILE pass straight through to the
--   wrapper's identically-named generics (system_regbank_wrapper.vhd). The
--   cocotb harness (test_regbank_wrapper_rom_seam.py) computes these as
--   ABSOLUTE paths from the test module's own __file__, never from cwd, and
--   passes them in via `generics=`. Left at their defaults (the clone-
--   relative rom_info_pkg constants) this tb reproduces the original
--   cwd-dependent failure when run from anywhere but the project root.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.rom_info_pkg.all;         -- C_REGBANK_HEX, C_CONFIG_HEX defaults
use tich.system_regbank_pkg.all;   -- t_memArrayReg

entity tb_regbank_wrapper_rom_seam is
  generic (
    G_AXI_ADDR_WIDTH   : integer := 11;
    G_AXI_DATA_WIDTH   : integer := 32;
    G_REGBANK_HEX_FILE : string  := C_REGBANK_HEX;
    G_CONFIG_HEX_FILE  : string  := C_CONFIG_HEX
  );
  port (
    CLK : in std_logic;
    RST : in std_logic;

    -- ── Flat AXI4-Lite slave (driven by a cocotb master BFM). ──
    S_AXI_AWADDR  : in  std_logic_vector(G_AXI_ADDR_WIDTH-1 downto 0);
    S_AXI_AWPROT  : in  std_logic_vector(2 downto 0) := (others => '0');
    S_AXI_AWVALID : in  std_logic;
    S_AXI_AWREADY : out std_logic;

    S_AXI_WDATA   : in  std_logic_vector(G_AXI_DATA_WIDTH-1 downto 0);
    S_AXI_WSTRB   : in  std_logic_vector(G_AXI_DATA_WIDTH/8-1 downto 0);
    S_AXI_WVALID  : in  std_logic;
    S_AXI_WREADY  : out std_logic;

    S_AXI_BRESP   : out std_logic_vector(1 downto 0);
    S_AXI_BVALID  : out std_logic;
    S_AXI_BREADY  : in  std_logic;

    S_AXI_ARADDR  : in  std_logic_vector(G_AXI_ADDR_WIDTH-1 downto 0);
    S_AXI_ARPROT  : in  std_logic_vector(2 downto 0) := (others => '0');
    S_AXI_ARVALID : in  std_logic;
    S_AXI_ARREADY : out std_logic;

    S_AXI_RDATA   : out std_logic_vector(G_AXI_DATA_WIDTH-1 downto 0);
    S_AXI_RRESP   : out std_logic_vector(1 downto 0);
    S_AXI_RVALID  : out std_logic;
    S_AXI_RREADY  : in  std_logic
  );
end entity tb_regbank_wrapper_rom_seam;

architecture str of tb_regbank_wrapper_rom_seam is

  signal telemetry_w : t_memArrayReg := (others => (data => (others => '0'),
                                                     valid => '0'));
  signal regs_out_w  : t_memArrayReg;
  signal datalogger_cfg_w : std_logic_vector(15 downto 0);

begin

  ---------------------------------------------------------------------------
  -- The REAL wrapper under test — including its two ROM instances. This is
  -- the seam DS-P2.100 found broken: out-of-tree elaboration of THIS entity
  -- (not the inner regbank) is what depended on cwd.
  ---------------------------------------------------------------------------
  u_wrapper : entity tich.system_regbank_wrapper
    generic map (
      G_ADDR_WIDTH       => G_AXI_ADDR_WIDTH,
      G_DATA_WIDTH       => G_AXI_DATA_WIDTH,
      G_REGBANK_HEX_FILE => G_REGBANK_HEX_FILE,
      G_CONFIG_HEX_FILE  => G_CONFIG_HEX_FILE
    )
    port map (
      CLK            => CLK,
      RST            => RST,
      S_AXI_AWADDR   => S_AXI_AWADDR,
      S_AXI_AWPROT   => S_AXI_AWPROT,
      S_AXI_AWVALID  => S_AXI_AWVALID,
      S_AXI_AWREADY  => S_AXI_AWREADY,
      S_AXI_WDATA    => S_AXI_WDATA,
      S_AXI_WSTRB    => S_AXI_WSTRB,
      S_AXI_WVALID   => S_AXI_WVALID,
      S_AXI_WREADY   => S_AXI_WREADY,
      S_AXI_BRESP    => S_AXI_BRESP,
      S_AXI_BVALID   => S_AXI_BVALID,
      S_AXI_BREADY   => S_AXI_BREADY,
      S_AXI_ARADDR   => S_AXI_ARADDR,
      S_AXI_ARPROT   => S_AXI_ARPROT,
      S_AXI_ARVALID  => S_AXI_ARVALID,
      S_AXI_ARREADY  => S_AXI_ARREADY,
      S_AXI_RDATA    => S_AXI_RDATA,
      S_AXI_RRESP    => S_AXI_RRESP,
      S_AXI_RVALID   => S_AXI_RVALID,
      S_AXI_RREADY   => S_AXI_RREADY,
      DATALOGGER_CONFIG => datalogger_cfg_w,
      TELEMETRY_IN   => telemetry_w,
      REGS_OUT       => regs_out_w
    );

end architecture str;
