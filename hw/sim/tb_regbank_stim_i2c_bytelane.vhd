-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : tb_regbank_stim_i2c_bytelane
-- Project    : deepskopion / DS-T2.82 PolarFire I2C self-test data bytes
-------------------------------------------------------------------------------
-- File       : tb_regbank_stim_i2c_bytelane.vhd
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   DS-T2.82 sim reproduction at the pfsoc clock. On the PolarFire kit the
--   DE-C0 I2C self-test decoded ['start',(0xA0,ack),(0xAB,ack),(0x00,ack),
--   (0x00,ack),'stop'] instead of 0xAB/0xCD/0x3E. This bench wires the REAL
--   inner regbank (tich.system_regbank — the byte-lane WSTRB semantics every
--   CSR write lands in) to the REAL ds_stimulus_gen I2C master, exactly as
--   deepskopion_core.vhd fans the STIM_I2C_* slots out:
--
--       AXI-Lite ──▶ system_regbank ──REGS_OUT(STIM_I2C_*)──▶ ds_stimulus_gen
--                                                               │ SCL/SDA_T
--                                                     open-drain bus (pulled up)
--
--   so a cocotb AxiLiteMaster can land the STIM_I2C_DATA write either as ONE
--   32-bit store or as the four sub-word byte stores the kit's riscv64
--   CPython emits for ctypes.c_uint32(...).value = v, and the test decodes
--   the resulting bytes off the bus.
--
--   No responder is instantiated: the ACK bits were correct on silicon and
--   are not in question; only the DATA bytes the master shifts out are.
--   ACK slots therefore read as released ('1').
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;

library tich;
use tich.system_regbank_pkg.all;   -- t_memArrayReg, C_INDEX_STIM_I2C_*

entity tb_regbank_stim_i2c_bytelane is
  generic (
    G_AXI_ADDR_WIDTH : integer := 12;
    G_AXI_DATA_WIDTH : integer := 32
  );
  port (
    CLK : in std_logic;
    RST : in std_logic;

    -- ── Flat AXI4-Lite slave (driven by cocotbext.axi.AxiLiteMaster). ──
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
    S_AXI_RREADY  : in  std_logic;

    -- Resolved open-drain bus (released = '1').
    I2C_BUS_SCL : out std_logic;
    I2C_BUS_SDA : out std_logic
  );
end entity tb_regbank_stim_i2c_bytelane;

architecture str of tb_regbank_stim_i2c_bytelane is

  signal regs_in_w  : t_memArrayReg := (others => (data => (others => '0'),
                                                   valid => '0'));
  signal regs_out_w : t_memArrayReg;

  signal scl_t, sda_t : std_logic;

begin

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

  -- Slot fan-out mirrors deepskopion_core.vhd (stim_i2c_*_w assignments).
  u_stim : entity tich.ds_stimulus_gen
    port map (
      CLK           => CLK,
      RST           => RST,
      UART_CONTROL  => (others => '0'),
      UART_BAUD_DIV => (others => '0'),
      UART_DATA     => (others => '0'),
      UART_DATA_VLD => '0',
      UART_TX_O     => open,
      I2C_CONTROL   => regs_out_w(C_INDEX_STIM_I2C_CONTROL / 4).data,
      I2C_CLK_DIV   => regs_out_w(C_INDEX_STIM_I2C_CLK_DIV / 4).data,
      I2C_DATA      => regs_out_w(C_INDEX_STIM_I2C_DATA / 4).data,
      I2C_DATA_VLD  => regs_out_w(C_INDEX_STIM_I2C_DATA / 4).valid,
      I2C_SCL_O     => open,
      I2C_SCL_T     => scl_t,
      I2C_SDA_O     => open,
      I2C_SDA_T     => sda_t,
      SPI_CONTROL   => (others => '0'),
      SPI_CLK_DIV   => (others => '0'),
      SPI_DATA      => (others => '0'),
      SPI_DATA_VLD  => '0',
      SPI_SCLK_O    => open,
      SPI_MOSI_O    => open,
      SPI_SS_O      => open,
      EDGE_CONTROL  => (others => '0'),
      EDGE_O        => open
    );

  -- Open-drain resolution: the master's O legs are tied '0', so T='0'
  -- pulls the line low and T='1' releases it to the pull-up.
  I2C_BUS_SCL <= '0' when scl_t = '0' else '1';
  I2C_BUS_SDA <= '0' when sda_t = '0' else '1';

end architecture str;
