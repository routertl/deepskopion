-------------------------------------------------------------------------------
-- tb_regbank_datalogger_config_seam — DS-P3.73 contract bench.
--
-- Proves the writable per-datalogger CONFIG seam end-to-end:
--
--   AXI-Lite write 0x0020 ──▶ system_regbank ──REGS_OUT──▶ config_word(15:0)
--                                                            │
--                                          ┌─────────────────┴─────────────────┐
--                                config_control(DEV_A)              config_control(DEV_B)
--                                   CONFIG_OUT_A                        CONFIG_OUT_B
--
-- This is exactly how deepskopion_top.vhd wires it: the regbank's writable
-- DATALOGGER_CONFIG slot drives `config_word` (system_regbank_wrapper.vhd:269 does
-- `DATALOGGER_CONFIG <= regs_out_inner(C_DATALOGGER_CFG_IDX).data(15 downto 0)`), which
-- feeds datalogger_gather.CONFIG; every datalogger instance contains a config_control
-- that matches CONFIG[15:11] to its G_DEV_ID and latches CONFIG[10:0] on a
-- word-change (the addressed mux-by-id, v1.1 contract — no cfg_valid bit).
--
-- We instantiate the INNER system_regbank (not the wrapper) and mirror the
-- one-line slot decode here, exactly as tb_regbank_i2c_inj_seam mirrors the
-- top's injector fan-out — the wrapper's BRAM .hex ROMs are irrelevant to this
-- seam.
--
-- The cocotb test (test_regbank_datalogger_config_seam.py) drives AXI writes and
-- asserts: only the ADDRESSED datalogger latches the payload, a non-addressed one
-- is untouched, and a later write to a different id does not disturb the first.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.system_regbank_pkg.all;   -- t_memArrayReg, C_INDEX_DATALOGGER_CONFIG

entity tb_regbank_datalogger_config_seam is
  generic (
    G_AXI_ADDR_WIDTH : integer := 11;
    G_AXI_DATA_WIDTH : integer := 32;
    -- Two distinct, non-zero datalogger device ids (id 0 is reserved).
    G_DEV_A          : natural := 3;
    G_DEV_B          : natural := 7
  );
  port (
    CLK : in std_logic;
    RST : in std_logic;

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

    -- Observation taps: the 16-bit config word the regbank fans out, and the
    -- 11-bit latched payload at each of the two addressed dataloggers.
    OBS_CONFIG_WORD  : out std_logic_vector(15 downto 0);
    OBS_CONFIG_OUT_A : out std_logic_vector(10 downto 0);
    OBS_CONFIG_OUT_B : out std_logic_vector(10 downto 0)
  );
end entity tb_regbank_datalogger_config_seam;

architecture str of tb_regbank_datalogger_config_seam is

  signal regs_in_w  : t_memArrayReg := (others => (data => (others => '0'),
                                                   valid => '0'));
  signal regs_out_w : t_memArrayReg;

  signal config_word_w : std_logic_vector(15 downto 0);
  signal cfg_out_a     : std_logic_vector(10 downto 0);
  signal cfg_out_b     : std_logic_vector(10 downto 0);

begin

  ---------------------------------------------------------------------------
  -- Inner system_regbank — same instance the wrapper uses.
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

  ---------------------------------------------------------------------------
  -- Slot decode — the production wiring (system_regbank_wrapper.vhd:269 /
  -- deepskopion_top.vhd:1417): the writable DATALOGGER_CONFIG slot's low 16 bits
  -- ARE the broadcast CONFIG word.
  ---------------------------------------------------------------------------
  config_word_w <= regs_out_w(C_INDEX_DATALOGGER_CONFIG / 4).data(15 downto 0);

  ---------------------------------------------------------------------------
  -- Two datalogger-side config_control demuxes on distinct device ids — the
  -- per-datalogger block datalogger_gather instantiates for every instance.
  ---------------------------------------------------------------------------
  u_config_control_a : entity tich.config_control
    generic map (
      G_DEV_ID_WIDTH => 5,
      G_DEV_ID       => G_DEV_A,
      G_CONFIG_WIDTH => 16,
      G_SIM_EN       => 1,
      G_ILA_EN       => 0
    )
    port map (
      CLK        => CLK,
      RST        => RST,
      CONFIG     => config_word_w,
      CONFIG_OUT => cfg_out_a
    );

  u_config_control_b : entity tich.config_control
    generic map (
      G_DEV_ID_WIDTH => 5,
      G_DEV_ID       => G_DEV_B,
      G_CONFIG_WIDTH => 16,
      G_SIM_EN       => 1,
      G_ILA_EN       => 0
    )
    port map (
      CLK        => CLK,
      RST        => RST,
      CONFIG     => config_word_w,
      CONFIG_OUT => cfg_out_b
    );

  OBS_CONFIG_WORD  <= config_word_w;
  OBS_CONFIG_OUT_A <= cfg_out_a;
  OBS_CONFIG_OUT_B <= cfg_out_b;

end architecture str;
