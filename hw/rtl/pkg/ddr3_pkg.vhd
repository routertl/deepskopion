
library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;

package ddr3_pkg is

  component mig_xem7305 is
    port (
      DDR3_dq             : inout std_logic_vector (15 downto 0);
      DDR3_dqs_p          : inout std_logic_vector (1 downto 0);
      DDR3_dqs_n          : inout std_logic_vector (1 downto 0);
      DDR3_addr           : out std_logic_vector (14 downto 0);
      DDR3_ba             : out std_logic_vector (2 downto 0);
      DDR3_ras_n          : out std_logic;
      DDR3_cas_n          : out std_logic;
      DDR3_we_n           : out std_logic;
      DDR3_reset_n        : out std_logic;
      DDR3_ck_p           : out std_logic_vector (0 to 0);
      DDR3_ck_n           : out std_logic_vector (0 to 0);
      DDR3_cke            : out std_logic_vector (0 to 0);
      DDR3_dm             : out std_logic_vector (1 downto 0);
      DDR3_odt            : out std_logic_vector (0 to 0);
      ui_clk_sync_rst     : out std_logic;
      ui_clk              : out std_logic;
      clk_100             : out std_logic;
      mmcm_locked         : out std_logic;
      init_calib_complete : out std_logic;
      axi4_burst_awid     : in std_logic_vector (1 downto 0);
      axi4_burst_awaddr   : in std_logic_vector (31 downto 0);
      axi4_burst_awlen    : in std_logic_vector (7 downto 0);
      axi4_burst_awsize   : in std_logic_vector (2 downto 0);
      axi4_burst_awburst  : in std_logic_vector (1 downto 0);
      axi4_burst_awlock   : in std_logic_vector (0 to 0);
      axi4_burst_awcache  : in std_logic_vector (3 downto 0);
      axi4_burst_awprot   : in std_logic_vector (2 downto 0);
      axi4_burst_awqos    : in std_logic_vector (3 downto 0);
      axi4_burst_awvalid  : in std_logic_vector (0 to 0);
      axi4_burst_awready  : out std_logic_vector (0 to 0);
      axi4_burst_wdata    : in std_logic_vector (127 downto 0);
      axi4_burst_wstrb    : in std_logic_vector (15 downto 0);
      axi4_burst_wlast    : in std_logic_vector (0 to 0);
      axi4_burst_wvalid   : in std_logic_vector (0 to 0);
      axi4_burst_wready   : out std_logic_vector (0 to 0);
      axi4_burst_bid      : out std_logic_vector (1 downto 0);
      axi4_burst_bresp    : out std_logic_vector (1 downto 0);
      axi4_burst_bvalid   : out std_logic_vector (0 to 0);
      axi4_burst_bready   : in std_logic_vector (0 to 0);
      axi4_burst_arid     : in std_logic_vector (1 downto 0);
      axi4_burst_araddr   : in std_logic_vector (31 downto 0);
      axi4_burst_arlen    : in std_logic_vector (7 downto 0);
      axi4_burst_arsize   : in std_logic_vector (2 downto 0);
      axi4_burst_arburst  : in std_logic_vector (1 downto 0);
      axi4_burst_arlock   : in std_logic_vector (0 to 0);
      axi4_burst_arcache  : in std_logic_vector (3 downto 0);
      axi4_burst_arprot   : in std_logic_vector (2 downto 0);
      axi4_burst_arqos    : in std_logic_vector (3 downto 0);
      axi4_burst_arvalid  : in std_logic_vector (0 to 0);
      axi4_burst_arready  : out std_logic_vector (0 to 0);
      axi4_burst_rid      : out std_logic_vector (1 downto 0);
      axi4_burst_rdata    : out std_logic_vector (127 downto 0);
      axi4_burst_rresp    : out std_logic_vector (1 downto 0);
      axi4_burst_rlast    : out std_logic_vector (0 to 0);
      axi4_burst_rvalid   : out std_logic_vector (0 to 0);
      axi4_burst_rready   : in std_logic_vector (0 to 0);
      axi4_stream_awid    : in std_logic_vector (1 downto 0);
      axi4_stream_awaddr  : in std_logic_vector (31 downto 0);
      axi4_stream_awlen   : in std_logic_vector (7 downto 0);
      axi4_stream_awsize  : in std_logic_vector (2 downto 0);
      axi4_stream_awburst : in std_logic_vector (1 downto 0);
      axi4_stream_awlock  : in std_logic_vector (0 to 0);
      axi4_stream_awcache : in std_logic_vector (3 downto 0);
      axi4_stream_awprot  : in std_logic_vector (2 downto 0);
      axi4_stream_awqos   : in std_logic_vector (3 downto 0);
      axi4_stream_awvalid : in std_logic_vector (0 to 0);
      axi4_stream_awready : out std_logic_vector (0 to 0);
      axi4_stream_wdata   : in std_logic_vector (127 downto 0);
      axi4_stream_wstrb   : in std_logic_vector (15 downto 0);
      axi4_stream_wlast   : in std_logic_vector (0 to 0);
      axi4_stream_wvalid  : in std_logic_vector (0 to 0);
      axi4_stream_wready  : out std_logic_vector (0 to 0);
      axi4_stream_bid     : out std_logic_vector (1 downto 0);
      axi4_stream_bresp   : out std_logic_vector (1 downto 0);
      axi4_stream_bvalid  : out std_logic_vector (0 to 0);
      axi4_stream_bready  : in std_logic_vector (0 to 0);
      axi4_stream_arid    : in std_logic_vector (1 downto 0);
      axi4_stream_araddr  : in std_logic_vector (31 downto 0);
      axi4_stream_arlen   : in std_logic_vector (7 downto 0);
      axi4_stream_arsize  : in std_logic_vector (2 downto 0);
      axi4_stream_arburst : in std_logic_vector (1 downto 0);
      axi4_stream_arlock  : in std_logic_vector (0 to 0);
      axi4_stream_arcache : in std_logic_vector (3 downto 0);
      axi4_stream_arprot  : in std_logic_vector (2 downto 0);
      axi4_stream_arqos   : in std_logic_vector (3 downto 0);
      axi4_stream_arvalid : in std_logic_vector (0 to 0);
      axi4_stream_arready : out std_logic_vector (0 to 0);
      axi4_stream_rid     : out std_logic_vector (1 downto 0);
      axi4_stream_rdata   : out std_logic_vector (127 downto 0);
      axi4_stream_rresp   : out std_logic_vector (1 downto 0);
      axi4_stream_rlast   : out std_logic_vector (0 to 0);
      axi4_stream_rvalid  : out std_logic_vector (0 to 0);
      axi4_stream_rready  : in std_logic_vector (0 to 0);
      ddr_rst             : in std_logic;
      mig_ref_clk_p       : in std_logic;
      mig_ref_clk_n       : in std_logic
    );
  end component mig_xem7305;
  
end package ddr3_pkg;

package body ddr3_pkg is

end package body ddr3_pkg;