-------------------------------------------------------------------------------
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-- Title      : axi package
-- Project    :
-------------------------------------------------------------------------------
-- File       : axi_pkg.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    :
-- Created    : 2024-07-06
-- Last update: 2024-07-18
-- Platform   :
-- Standard   : VHDL-2008
-------------------------------------------------------------------------------
-- Description: Contains AXI definitions, including subtypes
-------------------------------------------------------------------------------
-- Copyright (c) 2024
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2024-07-06  1.0      dmazure Created
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;
library tich;
use tich.system_config_pkg.all;

package axi_pkg is

  constant C_AXI_DATA_WIDTH  : integer := 128;
  constant C_AXI_ADDR_WIDTH  : integer := 32; -- 4G
  constant C_AXI_LEN_WIDTH   : integer := 8;
  constant C_AXI_SIZE_WIDTH  : integer := 3;
  constant C_AXI_BURST_WIDTH : integer := 2;
  constant C_AXI_LOCK_WIDTH  : integer := 1;
  constant C_AXI_CACHE_WIDTH : integer := 4;
  constant C_AXI_PROT_WIDTH  : integer := 3;
  constant C_AXI_STRB_WIDTH  : integer := C_AXI_DATA_WIDTH/8;
  constant C_AXI_RESP_WIDTH  : integer := 2;
  -- constant C_AXI_ID_WIDTH    : integer := integer(ceil(log2(real(C_NUM_ADDRESS_SPACES))));
  constant C_AXI_ID_WIDTH    : integer := 2; -- Defined by the MIG
  constant C_AXI_QOS_WIDTH   : integer := 4;

  -- DS-T2.60 / DS-T2.65: the Zynq-7 PS S_AXI_HP port is AXI3 (AWLEN is 4-bit),
  -- so 16 beats is the hard maximum. This is the SINGLE SOURCE of the datalogger→DDR
  -- write-burst cap, referenced by BOTH deepskopion_top's u_ddr_burst AND
  -- tb_datalogger_to_ddr's u_ddr — so the design and its AXI3 back-pressure test
  -- (test_axi3_burst_cap_under_backpressure) can never silently diverge (DS-T2.65).
  -- Reverting it here (>16) makes that test emit AWLEN>15 and FAIL.
  constant C_ZYNQ7_S_AXI_HP_MAX_BURST : integer := 16;

 type t_array_ddr_offset is array (natural range 0 to C_NUM_ADDRESS_SPACES-1) of unsigned(C_AXI_ADDR_WIDTH-1 downto 0);

  type t_axi4_h2c_bus is record
    -- Write address channel signals
    AWID    : std_logic_vector(C_AXI_ID_WIDTH-1 downto 0);
    AWADDR  : std_logic_vector(C_AXI_ADDR_WIDTH-1 downto 0);
    AWLEN   : std_logic_vector(C_AXI_LEN_WIDTH-1 downto 0);
    AWSIZE  : std_logic_vector(C_AXI_SIZE_WIDTH-1 downto 0);
    AWBURST : std_logic_vector(C_AXI_BURST_WIDTH-1 downto 0);
    AWLOCK  : std_logic_vector(C_AXI_LOCK_WIDTH-1 downto 0);
    AWCACHE : std_logic_vector(C_AXI_CACHE_WIDTH-1 downto 0);
    AWPROT  : std_logic_vector(C_AXI_PROT_WIDTH-1 downto 0);
    AWQOS   : std_logic_vector(C_AXI_QOS_WIDTH-1 downto 0);  -- if QOS is used
    AWVALID : std_logic;
    -- Write data channel signals
    WDATA   : std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
    WSTRB   : std_logic_vector(C_AXI_STRB_WIDTH-1 downto 0);
    WLAST   : std_logic;
    WVALID  : std_logic;
    -- Write response channel signal
    BREADY  : std_logic;
    -- Read address channel signals
    ARID    : std_logic_vector(C_AXI_ID_WIDTH-1 downto 0);
    ARADDR  : std_logic_vector(C_AXI_ADDR_WIDTH-1 downto 0);
    ARLEN   : std_logic_vector(C_AXI_LEN_WIDTH-1 downto 0);
    ARSIZE  : std_logic_vector(C_AXI_SIZE_WIDTH-1 downto 0);
    ARBURST : std_logic_vector(C_AXI_BURST_WIDTH-1 downto 0);
    ARLOCK  : std_logic_vector(C_AXI_LOCK_WIDTH-1 downto 0);
    ARCACHE : std_logic_vector(C_AXI_CACHE_WIDTH-1 downto 0);
    ARPROT  : std_logic_vector(C_AXI_PROT_WIDTH-1 downto 0);
    ARQOS   : std_logic_vector(C_AXI_QOS_WIDTH-1 downto 0);  -- if QOS is used
    ARVALID : std_logic;
    -- Read data channel signal
    RREADY  : std_logic;
  end record t_axi4_h2c_bus;

  type t_axi4_c2h_bus is record
    -- Write address channel signal
    AWREADY : std_logic;
    -- Write data channel signal
    WREADY  : std_logic;
    -- Write response channel signals
    BID     : std_logic_vector(C_AXI_ID_WIDTH-1 downto 0);
    BRESP   : std_logic_vector(C_AXI_RESP_WIDTH-1 downto 0);
    BVALID  : std_logic;
    -- Read address channel signal
    ARREADY : std_logic;
    -- Read data channel signals
    RID     : std_logic_vector(C_AXI_ID_WIDTH-1 downto 0);
    RDATA   : std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
    RRESP   : std_logic_vector(C_AXI_RESP_WIDTH-1 downto 0);
    RLAST   : std_logic;
    RVALID  : std_logic;
  end record t_axi4_c2h_bus;

  type t_array_axi4_h2c_bus is array (natural range 0 to C_NUM_ADDRESS_SPACES-1) of t_axi4_h2c_bus;
  type t_array_axi4_c2h_bus is array (natural range 0 to C_NUM_ADDRESS_SPACES-1) of t_axi4_c2h_bus;


  constant C_AXI4_H2C_INIT : t_axi4_h2c_bus := (
      AWID    => (others => '0'),
      AWADDR  => (others => '0'),
      AWLEN   => (others => '0'),
      AWSIZE  => (others => '0'),
      AWBURST => (others => '0'),
      AWLOCK  => (others => '0'),
      AWCACHE => (others => '0'),
      AWPROT  => (others => '0'),
      AWQOS   => (others => '0'),
      AWVALID => '0',
      WDATA   => (others => '0'),
      WSTRB   => (others => '0'),
      WLAST   => '0',
      WVALID  => '0',
      BREADY  => '0',
      ARID    => (others => '0'),
      ARADDR  => (others => '0'),
      ARLEN   => (others => '0'),
      ARSIZE  => (others => '0'),
      ARBURST => (others => '0'),
      ARLOCK  => (others => '0'),
      ARCACHE => (others => '0'),
      ARPROT  => (others => '0'),
      ARQOS   => (others => '0'),
      ARVALID => '0',
      RREADY  => '0'
  );

  constant C_AXI4_C2H_INIT : t_axi4_c2h_bus := (
      AWREADY => '0',
      WREADY  => '0',
      BID     => (others => '0'),
      BRESP   => (others => '0'),
      BVALID  => '0',
      ARREADY => '0',
      RID     => (others => '0'),
      RDATA   => (others => '0'),
      RRESP   => (others => '0'),
      RLAST   => '0',
      RVALID  => '0'
  );
 end package axi_pkg;
