-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : tb_regbank_axilite_mon
-- Project    : deepskopion / RTL-P3.851 AXI-Lite conformance hunt (EPIC P2.719)
-------------------------------------------------------------------------------
-- File       : tb_regbank_axilite_mon.vhd
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Minimal sim wrapper around the REAL inner AXI4-Lite register-bank slave
--   (tich.system_regbank, base 0x40000000 on silicon). It exposes the bank's
--   flat S_AXI_* ports so a cocotb master can drive ADVERSARIAL traffic while
--   the routertl-native AxiLiteMonitor watches passively for AMBA (IHI0022E)
--   conformance violations on the slave's response channels.
--
--   Unlike tb_regbank_i2c_inj_seam, this wrapper instantiates ONLY the regbank
--   — no i2c_injector, no XPM FIFO — so the compile set is just the regbank +
--   its two packages (rom_info_pkg, system_regbank_pkg). The point of this
--   bench is the AXI-Lite slave conformance, NOT any downstream consumer.
--
--   A tiny telemetry feed drives one read-only slot (HEARTBEAT_CNT) with a
--   free-running counter via REGS_IN, so reads of that slot return live,
--   X/Z-free data (exercises the R-channel against a moving value). All other
--   slots behave as plain R/W storage.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.system_regbank_pkg.all;   -- t_memArrayReg, C_INDEX_*, C_NUM_USER_REGS

entity tb_regbank_axilite_mon is
  generic (
    G_AXI_ADDR_WIDTH : integer := 11;
    G_AXI_DATA_WIDTH : integer := 32
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
end entity tb_regbank_axilite_mon;

architecture str of tb_regbank_axilite_mon is

  signal regs_in_w  : t_memArrayReg := (others => (data => (others => '0'),
                                                   valid => '0'));
  signal regs_out_w : t_memArrayReg;

  signal heartbeat_r : unsigned(G_AXI_DATA_WIDTH-1 downto 0) := (others => '0');

  constant C_HB_IDX : integer := C_INDEX_HEARTBEAT_CNT / 4;

begin

  ---------------------------------------------------------------------------
  -- Telemetry feed — a free-running counter published to the read-only
  -- HEARTBEAT_CNT slot every cycle. Keeps the R channel returning live,
  -- resolvable data so AXI_XZ on RDATA is meaningfully exercised.
  ---------------------------------------------------------------------------
  P_HB : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        heartbeat_r <= (others => '0');
      else
        heartbeat_r <= heartbeat_r + 1;
      end if;
    end if;
  end process;

  regs_in_w(C_HB_IDX).data  <= std_logic_vector(heartbeat_r);
  regs_in_w(C_HB_IDX).valid <= '1';

  ---------------------------------------------------------------------------
  -- Inner auto-generated AXI4-Lite regbank slave — the DUT.
  ---------------------------------------------------------------------------
  u_regbank : entity tich.system_regbank
    generic map (
      G_ADDR_WIDTH => G_AXI_ADDR_WIDTH,
      G_DATA_WIDTH => G_AXI_DATA_WIDTH
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
      S_AXI_ARREADY => S_AXI_ARREADY,
      S_AXI_RDATA   => S_AXI_RDATA,
      S_AXI_RRESP   => S_AXI_RRESP,
      S_AXI_RVALID  => S_AXI_RVALID,
      S_AXI_RREADY  => S_AXI_RREADY,
      REGS_IN       => regs_in_w,
      REGS_OUT      => regs_out_w
    );

end architecture str;
