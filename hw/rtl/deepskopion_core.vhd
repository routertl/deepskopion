-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-- DE-H0 portable assembly. Platform clocks, buses, pins and debug transports
-- cross this entity; no vendor library or primitive is permitted below it.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library tich;
use tich.rom_info_pkg.all;
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;
use tich.system_regbank_pkg.all;
use tich.zs_msg_pkg.all;
library rr;
library ctu_can_fd_rtl;

entity deepskopion_core is
  generic (
    G_DDR_BASE_ADDR : unsigned(31 downto 0) := x"1000_0000";
    G_DDR_WIN_WIDTH : integer := 28;
    G_AXI_MAX_BURST : positive := 16;
    G_DDR_DATA_WIDTH : positive := 128;
    G_SYS_CLK_HZ : positive := 100_000_000;
    -- PF-H0 (DS-P2.124) decision D3: which rr-ddr-stream backend the core's DMA
    -- write instance elaborates. "axi" (default) selects rr.ddr_stream_axi, the
    -- Zynq/DE25-proven body; "fic" selects rr.ddr_stream_fic, the PolarFire SoC
    -- MSS-FIC sibling (DDR-P2.15). ddr_stream_fic is a drop-in AXI-record wrapper
    -- around the SAME ddr_stream_axi core with PolarFire FIC defaults (64-bit
    -- native, 64-beat cap), so the core-side contract is byte-identical either
    -- arm. This is a BACKWARD-COMPATIBLE, default-off edit: Zynq and DE25 leave
    -- G_DDR_BACKEND at "axi" and elaborate exactly the u_ddr_burst instance they
    -- always have (the guard test_core_ddr_base_generic_guard.py still matches
    -- that label + generic map), while deepskopion_pfsoc_top sets "fic". It IS a
    -- shared-core edit, which is why the plan says "no behaviour change to
    -- Zynq/DE25", not "no core edit" — the invariance is re-proven by the
    -- already-green xilinx_baseline / de25_baseline elaborating the "axi" arm.
    G_DDR_BACKEND : string := "axi";
    -- DS-P2.67: TXT buffers per CTU CAN FD core (the core's own default is 4).
    -- The Zynq top sets 2: with four, the TX priority select over the buffers
    -- feeds the arbitrator's timestamp compare in one cycle and canfd_demo
    -- missed 100 MHz on xc7z020-1 (WNS -0.538 / -0.696 ns); with two it meets
    -- timing (WNS +0.121 ns). The core reports the count in
    -- TXTB_INFO.TXT_BUFFER_COUNT; the Zynq kernel's ctucanfd driver reads it
    -- (sw/linux/kernel_patches/ctucanfd_ntxbufs.sh). DE25 keeps 4: its kernel
    -- step runs no patch script, so its driver still assumes four.
    G_CTU_TXT_BUFFERS : natural range 2 to 8 := 4;
    -- DS-P1.90: the fabric stimulus generator also drives the EMIO legs
    -- INSIDE the core, one protocol at a time, while that protocol's
    -- STIM_*_CONTROL enable bit[0] is set; with the bit clear each leg is the
    -- emio_*_i port, unchanged. The Zynq top sets it: there the emio_* ports
    -- come from the PS EMIO and the stim_*_o ports have no top-level consumer,
    -- so without it the stimulus generator drove nothing on that board. DE25
    -- and PolarFire loop stim_*_o back into emio_*_i at the top and keep the
    -- default (false), which elaborates the pre-DS-P1.90 wiring.
    G_STIM_DRIVES_EMIO : boolean := false
  );
  port (
    clk_sys_i : in std_logic; rst_sys_i : in std_logic;
    clk_ddr_i : in std_logic; rst_ddr_i : in std_logic;
    led : out std_logic_vector(3 downto 0);
    ja : in std_logic_vector(7 downto 0);
    jb : in std_logic_vector(7 downto 0);

    s_axil_awaddr_i : in std_logic_vector(31 downto 0);
    s_axil_awprot_i : in std_logic_vector(2 downto 0);
    s_axil_awvalid_i : in std_logic; s_axil_awready_o : out std_logic;
    s_axil_wdata_i : in std_logic_vector(31 downto 0);
    s_axil_wstrb_i : in std_logic_vector(3 downto 0);
    s_axil_wvalid_i : in std_logic; s_axil_wready_o : out std_logic;
    s_axil_bresp_o : out std_logic_vector(1 downto 0);
    s_axil_bvalid_o : out std_logic; s_axil_bready_i : in std_logic;
    s_axil_araddr_i : in std_logic_vector(31 downto 0);
    s_axil_arprot_i : in std_logic_vector(2 downto 0);
    s_axil_arvalid_i : in std_logic; s_axil_arready_o : out std_logic;
    s_axil_rdata_o : out std_logic_vector(31 downto 0);
    s_axil_rresp_o : out std_logic_vector(1 downto 0);
    s_axil_rvalid_o : out std_logic; s_axil_rready_i : in std_logic;

    canfd0_apb_paddr_i : in std_logic_vector(31 downto 0);
    canfd0_apb_penable_i, canfd0_apb_psel_i, canfd0_apb_pwrite_i : in std_logic;
    canfd0_apb_pwdata_i : in std_logic_vector(31 downto 0);
    canfd0_apb_prdata_o : out std_logic_vector(31 downto 0);
    canfd0_apb_pready_o, canfd0_apb_pslverr_o : out std_logic;
    canfd1_apb_paddr_i : in std_logic_vector(31 downto 0);
    canfd1_apb_penable_i, canfd1_apb_psel_i, canfd1_apb_pwrite_i : in std_logic;
    canfd1_apb_pwdata_i : in std_logic_vector(31 downto 0);
    canfd1_apb_prdata_o : out std_logic_vector(31 downto 0);
    canfd1_apb_pready_o, canfd1_apb_pslverr_o : out std_logic;

    m_axi_h2c_o : out rr.ddr_stream_pkg.axi4_h2c_bus_t;
    m_axi_c2h_i : in rr.ddr_stream_pkg.axi4_c2h_bus_t;
    irq_o : out std_logic_vector(2 downto 0);

    emio_uart_tx_i : in std_logic; emio_uart_rx_o : out std_logic;
    emio_i2c_scl_o_i, emio_i2c_scl_t_i : in std_logic;
    emio_i2c_sda_o_i, emio_i2c_sda_t_i : in std_logic;
    emio_i2c_scl_i_o, emio_i2c_sda_i_o : out std_logic;
    emio_spi_sclk_o_i, emio_spi_sclk_t_i : in std_logic;
    emio_spi_mosi_o_i, emio_spi_mosi_t_i : in std_logic;
    emio_spi_miso_o_i, emio_spi_miso_t_i : in std_logic;
    emio_spi_ss_o_i, emio_spi_ss_t_i : in std_logic;
    emio_spi_sclk_i_o, emio_spi_mosi_i_o : out std_logic;
    emio_spi_miso_i_o, emio_spi_ss_i_o : out std_logic;
    emio_gpio_o_i, emio_gpio_t_i : in std_logic; emio_gpio_i_o : out std_logic;
    emio_can0_tx_i, emio_can1_tx_i : in std_logic; emio_can_rx_o : out std_logic;

    -- DS-P2.100 / DS-D.38 option a: fabric-side stimulus generator drives —
    -- the DE25 top loops these back into the emio_*_i legs above in place
    -- of the DS-P2.95 tied-off constants (Zynq top leaves them open; the
    -- real PS7 EMIO still drives emio_*_i there). Push-pull UART/SPI legs
    -- need no _T (see ds_stimulus_gen.vhd header); I2C stays open-drain.
    stim_uart_tx_o    : out std_logic;
    stim_i2c_scl_o_o, stim_i2c_scl_t_o : out std_logic;
    stim_i2c_sda_o_o, stim_i2c_sda_t_o : out std_logic;
    stim_spi_sclk_o_o : out std_logic;
    stim_spi_mosi_o_o : out std_logic;
    stim_spi_ss_o_o   : out std_logic;

    -- DS-P2.126: PolarFire MSS peripherals routed to the fabric (the EMIO
    -- analogue). EMIO_MUX_SEL bits [11:8] select these legs over the legacy
    -- emio_* inputs (Zybo PS EMIO, or the ds_stimulus_gen loopback on DE25
    -- and PolarFire). Bits reset to 0, so a platform that ties these inputs
    -- idle keeps today's capture. Edge/pulse has no MSS generator — that
    -- stays on bit[7] and ds_stimulus_gen.
    --   bit[8]  I2C  ← MSS I2C_0 open-drain OE (1 = drive low)
    --   bit[9]  UART ← MSS MMUART_1 TX
    --   bit[10] SPI  ← MSS SPI_0 master (CLK/DO/SS1); GPIO CS follows SS
    --   bit[11] CAN  ← MSS CAN_0 TX into the wired-AND bus
    mss_uart_tx_i : in std_logic;
    mss_uart_rx_o : out std_logic;
    mss_i2c_scl_oe_i, mss_i2c_sda_oe_i : in std_logic;
    mss_i2c_scl_o, mss_i2c_sda_o : out std_logic;
    mss_spi_clk_i, mss_spi_clk_oe_i : in std_logic;
    mss_spi_do_i, mss_spi_do_oe_i : in std_logic;
    mss_spi_ss_i, mss_spi_ss_oe_i : in std_logic;
    mss_spi_di_o : out std_logic;
    mss_can_tx_i : in std_logic;
    mss_can_rx_o : out std_logic;

    rea_probe_o : out std_logic_vector(31 downto 0);
    rea_trigger_i : in std_logic;
    rea_tap_tck_i, rea_tap_tms_i, rea_tap_tdi_i, rea_tap_tdo_i : in std_logic
  );
end entity deepskopion_core;

architecture rtl of deepskopion_core is

  ---------------------------------------------------------------------------
  -- deepskopion_bd — Vivado-generated wrapper sourced from
  -- hw/bd/deepskopion_bd.tcl. Provides the PS7, two smartconnects,
  -- proc_sys_reset, and exposes M_AXI_GP0 / S_AXI_HP0 / fclk_clk0 /
  -- fclk_aresetn alongside the PS dedicated pins.
  ---------------------------------------------------------------------------


  ---------------------------------------------------------------------------
  -- Clock + reset (from BD's fclk_clk0 / fclk_aresetn)
  ---------------------------------------------------------------------------
  signal clk_sys      : std_logic;                   -- 100 MHz from BD FCLK_CLK0
  signal rst_sys      : std_logic;
  -- DS-P2.153: registered + replication-hinted copy of rst_sys_i (see the
  -- p_rst_sys_reg rationale at the assignment).
  signal rst_sys_q    : std_logic := '1';
  attribute syn_maxfan : integer;
  attribute syn_maxfan of rst_sys_q : signal is 250;
  signal aresetn      : std_logic_vector(0 to 0);
  signal clk_ddr      : std_logic;                   -- 125 MHz from BD FCLK_CLK1
  signal rst_ddr      : std_logic;
  signal ddr_aresetn  : std_logic_vector(0 to 0);

  ---------------------------------------------------------------------------
  -- AXI4 master record bundles to/from rr-ddr-stream (internal-only)
  ---------------------------------------------------------------------------
  signal axi_h2c : rr.ddr_stream_pkg.axi4_h2c_bus_t;
  signal axi_c2h : rr.ddr_stream_pkg.axi4_c2h_bus_t;

  -- DS-P2.45: rr-ddr-stream RR--P3.6 widened the AXI record data lanes
  -- from 128 to 512 bits (independent user/DDR widths). The Zynq-7 BD's
  -- S_AXI_HP0 slave is 128 bits with a 16-bit WSTRB. Slice the record
  -- to the BD's native width at the boundary; the upper record bits
  -- stay constant zero (the design only drives [ddr_data_width_g-1:0]).
  signal s_axi_hp0_rdata : std_logic_vector(127 downto 0);
  signal s_axi_hp0_wdata : std_logic_vector(127 downto 0);
  signal s_axi_hp0_wstrb : std_logic_vector(15 downto 0);

  ---------------------------------------------------------------------------
  -- AXI-Lite slave from BD's M_AXI_GP0 (flat signals; sliced into regbank's
  -- 11-bit address window).
  ---------------------------------------------------------------------------
  signal gp0_awaddr  : std_logic_vector(31 downto 0);
  signal gp0_awprot  : std_logic_vector(2 downto 0);
  signal gp0_awvalid : std_logic;
  signal gp0_awready : std_logic;
  signal gp0_wdata   : std_logic_vector(31 downto 0);
  signal gp0_wstrb   : std_logic_vector(3 downto 0);
  signal gp0_wvalid  : std_logic;
  signal gp0_wready  : std_logic;
  signal gp0_bresp   : std_logic_vector(1 downto 0);
  signal gp0_bvalid  : std_logic;
  signal gp0_bready  : std_logic;
  signal gp0_araddr  : std_logic_vector(31 downto 0);
  signal gp0_arprot  : std_logic_vector(2 downto 0);
  signal gp0_arvalid : std_logic;
  signal gp0_arready : std_logic;
  signal gp0_rdata   : std_logic_vector(31 downto 0);
  signal gp0_rresp   : std_logic_vector(1 downto 0);
  signal gp0_rvalid  : std_logic;
  signal gp0_rready  : std_logic;

  ---------------------------------------------------------------------------
  -- Free-running timestamp counter (DS-P3.30, 2026-05-17).
  --
  -- Full counter is 64-bit so the zs_msg header's timestamp field
  -- carries an absolute time that doesn't wrap on any practical
  -- capture (~5800 years at 100 MHz). The per-protocol datalogger chain
  -- still consumes a 32-bit TIMESTAMP slice (C_TIMESTAMP_WIDTH) — the
  -- datalogger payload formats embed only short ts slices anyway — so
  -- we expose two views of the SAME counter:
  --   timestamp   : std_logic_vector(31:0)  → datalogger_gather port
  --                                            (per-protocol slicing)
  --   timestamp64 : std_logic_vector(63:0)  → zs_msg_framer header
  ---------------------------------------------------------------------------
  signal timestamp_r : unsigned(63 downto 0) := (others => '0');
  signal timestamp   : std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);

  ---------------------------------------------------------------------------
  -- Per-datalogger CONFIG bus — driven by regbank DATALOGGER_CONFIG.
  ---------------------------------------------------------------------------
  signal config_word : std_logic_vector(C_DATALOGGER_CONFIG_WIDTH-1 downto 0);

  ---------------------------------------------------------------------------
  -- datalogger_gather → zs_msg_framer wiring. DS-P3.29: per-source
  -- metadata (datalogger_id + msg_type) now threaded through the chain
  -- alongside the 32-bit payload.
  ---------------------------------------------------------------------------
  signal dout_payload    : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal dout_datalogger_id : std_logic_vector(7 downto 0);
  signal dout_msg_type   : std_logic_vector(15 downto 0);
  signal dout_valid      : std_logic;
  signal fifo_csr        : t_fifoCsr;
  signal dout_req             : std_logic;
  signal ddr_busy              : std_logic;
  -- DDR-P3.10: input-side back-pressure from rr-ddr-stream's skid.
  signal ddr_s_axis_ready      : std_logic;

  ---------------------------------------------------------------------------
  -- Heartbeat / liveness LEDs
  ---------------------------------------------------------------------------
  signal hb_div : unsigned(26 downto 0) := (others => '0');

  ---------------------------------------------------------------------------
  -- Logical capture_pin matrix — 64-bit bus, gather slices per
  -- datalogger_config.yml indices. DS-P1.66: board_pin holds every source the
  -- core can capture (PMOD / EMIO mux / tap, order = board_pins.yml) and the
  -- generator's C_CAPTURE_BOARD_PIN says which one feeds each logical pin.
  ---------------------------------------------------------------------------
  signal capture_pin : std_logic_vector(C_NUM_DATALOGGER_PINS-1 downto 0) := (others => '0');
  signal board_pin   : std_logic_vector(C_NUM_BOARD_PINS-1 downto 0);

  ---------------------------------------------------------------------------
  -- Telemetry fan-in into regbank wrapper.
  ---------------------------------------------------------------------------
  signal heartbeat_cnt           : unsigned(31 downto 0) := (others => '0');
  signal fifo_overflow_cnt : unsigned(31 downto 0) := (others => '0');
  signal telemetry_in            : t_memArrayReg := (others => (data => (others => '0'), valid => '0'));

  -- Per-protocol PKT_CNT_*/ERR_CNT_* bundle driven by datalogger_gather
  -- (DS-P3.26). Indexed by datalogger_config_pkg.C_PROTO_IDX_<NAME>.
  signal proto_counters : t_protoCounterArray(0 to C_NUM_PROTO_COUNTERS-1)
                        := (others => C_INIT_PROTO_COUNTER);

  ---------------------------------------------------------------------------
  -- DS-P2.51 Phase 1: PS EMIO wires + per-protocol pre-gather mux.
  --
  -- EMIO_MUX_SEL slot decode:
  --   bit[0] = I2C #4  source select  (0 = PMOD, 1 = internal I2C)
  --   bit[1] = UART #12 source select (0 = PMOD, 1 = internal UART)
  --   bit[2] = SPI #8  source select  (0 = PMOD, 1 = internal SPI)
  --   bit[8]  = internal I2C  is MSS fabric (0 = legacy emio/stimulus)
  --   bit[9]  = internal UART is MSS fabric
  --   bit[10] = internal SPI  is MSS fabric
  --   bit[11] = internal CAN  is MSS fabric
  ---------------------------------------------------------------------------
  signal regs_out_local : t_memArrayReg;
  signal emio_mux_sel   : std_logic_vector(2 downto 0);
  signal mss_i2c_sel    : std_logic;   -- DS-P2.126 EMIO_MUX_SEL bit[8]
  signal mss_uart_sel   : std_logic;   -- DS-P2.126 EMIO_MUX_SEL bit[9]
  signal mss_spi_sel    : std_logic;   -- DS-P2.126 EMIO_MUX_SEL bit[10]
  signal mss_can_sel    : std_logic;   -- DS-P2.126 EMIO_MUX_SEL bit[11]
  signal mss_spi_clk_w  : std_logic;
  signal mss_spi_do_w   : std_logic;
  signal mss_spi_ss_w   : std_logic;
  signal emio_edge_sel  : std_logic;   -- DS-P3.71: EMIO_MUX_SEL bit[3] (edge self-test)

  -- DS-P2.100: fabric-side stimulus generator (ds_stimulus_gen) regbank
  -- unpack + output legs. See instantiation below (near the injectors) for
  -- the CONTROL/DATA/DATA_VLD wiring.
  signal stim_uart_control_w  : std_logic_vector(31 downto 0);
  signal stim_uart_baud_w     : std_logic_vector(31 downto 0);
  signal stim_uart_data_w     : std_logic_vector(31 downto 0);
  signal stim_uart_data_vld_w : std_logic;
  signal stim_i2c_control_w   : std_logic_vector(31 downto 0);
  signal stim_i2c_clk_div_w   : std_logic_vector(31 downto 0);
  signal stim_i2c_data_w      : std_logic_vector(31 downto 0);
  signal stim_i2c_data_vld_w  : std_logic;
  signal stim_spi_control_w   : std_logic_vector(31 downto 0);
  signal stim_spi_clk_div_w   : std_logic_vector(31 downto 0);
  signal stim_spi_data_w      : std_logic_vector(31 downto 0);
  signal stim_spi_data_vld_w  : std_logic;
  signal stim_edge_control_w  : std_logic_vector(31 downto 0);
  signal stim_edge_w          : std_logic;  -- ds_stimulus_gen EDGE_O (already in clk_sys, no CDC needed)
  signal stim_edge_sel        : std_logic;  -- DS-P2.100: EMIO_MUX_SEL bit[7]
  -- DS-P1.90: generator output legs, and the EMIO legs as the core consumes
  -- them (the emio_*_i port, or the generator while its enable bit is set
  -- and G_STIM_DRIVES_EMIO is true).
  signal stim_uart_tx_w    : std_logic;
  signal stim_i2c_scl_o_w, stim_i2c_scl_t_w : std_logic;
  signal stim_i2c_sda_o_w, stim_i2c_sda_t_w : std_logic;
  signal stim_spi_sclk_w, stim_spi_mosi_w, stim_spi_ss_w : std_logic;
  signal stim_uart_on, stim_i2c_on, stim_spi_on : std_logic;
  signal emio_uart_tx_w    : std_logic;
  signal emio_i2c_scl_o_w, emio_i2c_scl_t_w : std_logic;
  signal emio_i2c_sda_o_w, emio_i2c_sda_t_w : std_logic;
  signal emio_spi_sclk_o_w, emio_spi_sclk_t_w : std_logic;
  signal emio_spi_mosi_o_w, emio_spi_mosi_t_w : std_logic;
  signal emio_spi_ss_o_w, emio_spi_ss_t_w : std_logic;
  signal emio_gpio_o_w, emio_gpio_t_w : std_logic;

  -- EMIO legs (out from BD, into our bus model)
  signal uart0_tx_w     : std_logic;
  signal uart0_rx_w     : std_logic;  -- driven back into BD; idle '1' in Phase 1
  signal i2c0_scl_o_w   : std_logic;
  signal i2c0_scl_t_w   : std_logic;
  signal i2c0_sda_o_w   : std_logic;
  signal i2c0_sda_t_w   : std_logic;
  signal spi0_sclk_o_w  : std_logic;
  signal spi0_sclk_t_w  : std_logic;  -- unused in master mode; PS keeps '0'
  signal spi0_mosi_o_w  : std_logic;
  signal spi0_mosi_t_w  : std_logic;
  signal spi0_miso_o_w  : std_logic;  -- ignored (slave-mode tri-state); we
  signal spi0_miso_t_w  : std_logic;  -- model master → no MISO drive
  signal spi0_ss_o_w    : std_logic;
  signal spi0_ss_t_w    : std_logic;

  -- DS-P3.102: CAN0/CAN1 EMIO legs + the fabric wired-AND CAN bus. The two PS
  -- CAN controllers (EMIO) form a real 2-node bus entirely in fabric (open-drain
  -- wired-AND: dominant '0' wins, recessive '1' idle), so no transceiver is
  -- needed for bring-up. A configured can_datalogger taps it as board pin
  -- CAN_BUS (DS-P1.66). Idle (both controllers recessive) → can_bus_w = '1',
  -- so this is harmless when CAN is unused. See docs/BRING_UP_CAN.md.
  signal can0_phy_tx_w  : std_logic;   -- PS CAN0 controller TX (into the AND)
  signal can1_phy_tx_w  : std_logic;   -- PS CAN1 controller TX (into the AND)
  signal can_bus_w      : std_logic;   -- the wired-AND bus (→ both RX + datalogger)
  -- DS-P3.109: 2× CTU CAN FD soft cores join the SAME wired-AND bus as FD traffic
  -- nodes (classic PS-CAN and FD-CTU coexist — whichever pair is enabled drives;
  -- the disabled pair idles recessive '1' and the AND is unaffected). Build 1:
  -- APB idle-tied (cores present → validates Z7-20 fit/timing); the live loop wires
  -- M_AXI_GP1 → 2× axi_apb_bridge for SW config + IRQ_F2P + the ctucanfd driver.
  signal ctu_a_tx_w     : std_logic;   -- CTU CAN FD node A TX (into the AND)
  signal ctu_b_tx_w     : std_logic;   -- CTU CAN FD node B TX (into the AND)
  signal ctu_arstn_w    : std_logic;   -- active-low reset for the CTU cores
  signal ctu_a_irq_w    : std_logic;   -- CTU A irq → IRQ_F2P[1] (GIC SPI #30)
  signal ctu_b_irq_w    : std_logic;   -- CTU B irq → IRQ_F2P[2] (GIC SPI #31)
  signal irq_f2p_w      : std_logic_vector(2 downto 0);  -- [2:0] = {ctu_b, ctu_a, ring_alarm}
  -- DS-P3.109: APB buses from the BD (M_AXI_GP1 → axi_apb_bridge, APB3 — no
  -- pstrb/pprot) → CTU s_apb_* slaves. psel/pready/pslverr are 1-bit (0 to 0)
  -- vectors as the bridge emits them. BD master → CTU: paddr/penable/psel/pwrite/
  -- pwdata; CTU → BD master: prdata/pready/pslverr.
  signal canfd0_apb_paddr_w   : std_logic_vector(31 downto 0);
  signal canfd0_apb_penable_w : std_logic;
  signal canfd0_apb_psel_w    : std_logic_vector(0 to 0);
  signal canfd0_apb_pwrite_w  : std_logic;
  signal canfd0_apb_pwdata_w  : std_logic_vector(31 downto 0);
  signal canfd0_apb_prdata_w  : std_logic_vector(31 downto 0);
  signal canfd0_apb_pready_w  : std_logic_vector(0 to 0);
  signal canfd0_apb_pslverr_w : std_logic_vector(0 to 0);
  signal canfd1_apb_paddr_w   : std_logic_vector(31 downto 0);
  signal canfd1_apb_penable_w : std_logic;
  signal canfd1_apb_psel_w    : std_logic_vector(0 to 0);
  signal canfd1_apb_pwrite_w  : std_logic;
  signal canfd1_apb_pwdata_w  : std_logic_vector(31 downto 0);
  signal canfd1_apb_prdata_w  : std_logic_vector(31 downto 0);
  signal canfd1_apb_pready_w  : std_logic_vector(0 to 0);
  signal canfd1_apb_pslverr_w : std_logic_vector(0 to 0);

  -- DS-T2.16: EMIO GPIO bit 0 = fabric-driven SPI chip-select. The Linux SPI
  -- core toggles it around each transfer via cs-gpios (project.yml
  -- linux.dt_nodes) — replacing the native SPI0_SS_O that never asserts at the
  -- EMIO seam. spi0_cs_gpio_w is the resolved CS the SPI injector watches.
  signal gpio_o_w       : std_logic_vector(0 downto 0);
  signal gpio_i_w       : std_logic_vector(0 downto 0);
  signal gpio_t_w       : std_logic_vector(0 downto 0);
  signal spi0_cs_gpio_w : std_logic;
  -- DS-T2.18 timing: registered copy of the GPIO CS for the high-fanout SPI
  -- DATALOGGER path (gpio→resolution→ds_emio_mux→qspi_datalogger was a long
  -- combinational net, WNS -0.051). Registering it breaks the path; the 1-cycle
  -- (10 ns) skew vs SCLK/MOSI is negligible at SPI's ~µs bit period. The SPI
  -- INJECTOR keeps the unregistered spi0_cs_gpio_w (its path met timing in v9).
  signal spi0_cs_gpio_r : std_logic := '1';

  -- Bus levels (the actual wire the datalogger sees + the PS reads back via _I)
  signal i2c0_bus_scl_w : std_logic;
  signal i2c0_bus_sda_w : std_logic;
  signal spi0_bus_sclk_w: std_logic;
  signal spi0_bus_mosi_w: std_logic;
  signal spi0_bus_miso_w: std_logic;
  signal spi0_bus_ss_w  : std_logic;

  -- Muxed sources feeding capture_pin (chosen between PMOD pin and EMIO bus).
  signal src_i2c_scl_w  : std_logic;
  signal src_i2c_sda_w  : std_logic;
  signal src_uart_tx_w  : std_logic;  -- COMBINATIONAL EMIO UART0 TX (async to clk_sys);
                                      -- conditioned by the gather's per-pin glitch_filter
                                      -- (DS-P2.10), not a separate top-level filter.
  signal src_spi_ss_w   : std_logic;
  signal src_spi_mosi_w : std_logic;
  signal src_spi_miso_w : std_logic;
  signal src_spi_sclk_w : std_logic;

  ---------------------------------------------------------------------------
  -- DS-P2.51 Phase 2: I2C/SPI injector responders.
  --
  -- Each injector wraps the matching slave block with a regbank-fed TX
  -- FIFO. The slave drives its half of the bus open-drain (I2C) or
  -- push-pull (SPI MISO when CS asserted); the bus model below combines
  -- master + responder drives into the resolved `*_bus_*` signals.
  ---------------------------------------------------------------------------
  -- I2C injector
  signal i2c_inj_control_w  : std_logic_vector(31 downto 0);
  signal i2c_inj_datain_w   : std_logic_vector(31 downto 0);
  signal i2c_inj_datain_vld : std_logic;
  signal i2c_inj_status_w   : std_logic_vector(31 downto 0);
  signal i2c_inj_sda_o_w    : std_logic;
  signal i2c_inj_sda_t_w    : std_logic;

  -- SPI injector
  signal spi_inj_control_w  : std_logic_vector(31 downto 0);
  signal spi_inj_datain_w   : std_logic_vector(31 downto 0);
  signal spi_inj_datain_vld : std_logic;
  signal spi_inj_status_w   : std_logic_vector(31 downto 0);
  signal spi_inj_miso_o_w   : std_logic;
  signal spi_inj_miso_t_w   : std_logic;

  -- DS-T9 / DS-T2.11: EMIO-loop REA probe word (32 bits) + regbank tap.
  -- The probe_word spans the full EMIO loopback seam (PS master out,
  -- injector drive-back, PS read-in, muxed datalogger sources, mux sel, framer
  -- liveness). It feeds BOTH the rr_rea_xilinx7 REA core (cycle-accurate JTAG
  -- waveform) and the EMIO_PROBE regbank RO slot (static AXI/devmem snapshot —
  -- only the low 31 bits, so sticky&probe stays in one 32-bit reg; the new
  -- SPI SS read-in leg is REA-capture-only).
  --
  -- DS-T9 widening (2026-05-31): bit 31 adds the SPI SS read-in (spi0_bus_ss_w)
  -- — the most diagnostic still-unprobed read-in leg: it confirms chip-select
  -- reaches the bus the SPI injector samples ("does the injector get
  -- triggered"). CAPPED AT 32 BITS: rr_rea_xilinx7's trigger value/mask are
  -- 32-bit JTAG regs (rr_rea_regbank trig_value_r), so G_SAMPLE_W must be <= 32
  -- — G_SAMPLE_W=35 failed synth with [Synth 8-11324] array-index-out-of-range
  -- (RTL-P2.658). The SPI SCLK/MOSI + UART RX read-in legs are deferred until
  -- the IP supports wider triggers.
  -- NOTE: I2C SCL/SDA and SPI MISO read-in legs are NOT added — u_bd's *_I
  -- ports are fed by the resolved i2c0_bus_*_w / spi0_bus_miso_w nets, which
  -- bits 4/3/11 ALREADY probe (I2C0_SCL_I=>i2c0_bus_scl_w,
  -- SPI0_MISO_I=>spi0_bus_miso_w). Re-probing them would be duplication.
  signal emio_probe_word  : std_logic_vector(31 downto 0);
  signal rea_trigger_out  : std_logic;
  signal rea_trig_sticky  : std_logic := '0';
  -- DS-P3.56 P3: the DDR-commit REA's BSCANE2 tap, routed to the jtag_datalogger
  -- so it watches the live USER1 scan (datalogger-watching-REA bring-up).
  signal rea_tap_tck      : std_logic;
  signal rea_tap_tms      : std_logic;
  signal rea_tap_tdi      : std_logic;
  signal rea_tap_tdo      : std_logic;

  -- ── DS-T2.18 DDR-commit instrumentation ──────────────────────────────────
  -- Diagnoses why frames enqueue (RING_WRITE_PTR advances on framer_out_valid)
  -- but never land in DRAM (cold-cache DRAM read = 100% zero). Three observers:
  --   1. clk_ddr liveness: a free-running divider in the clk_ddr (fclk_clk1,
  --      125 MHz) domain — deliberately NOT reset by rst_ddr, so it ticks even
  --      if rst_ddr is stuck. CDC'd to clk_sys + edge-counted → CLK_DDR_TICKS.
  --      Distinguishes "dead clock" (count stuck 0) from "stuck reset".
  --   2. rst_ddr observed in clk_sys: live state + a sticky "ever deasserted"
  --      flag (persistent from config) → DDR_RESET_STATUS.
  --   3. The HP0 write handshake on a clk_ddr-clocked REA core (repurposes the
  --      single USER1 BSCANE2 chain — rr's xsdb capture path is USER1-only).
  attribute async_reg : string;
  -- clk_ddr-domain free-running liveness divider (init-valued, no reset).
  signal clk_ddr_div_r       : unsigned(7 downto 0) := (others => '0');
  signal clk_ddr_toggle_r    : std_logic := '0';
  -- clk_sys-side synchroniser + edge counter for the toggle.
  signal clk_ddr_tgl_sync    : std_logic_vector(1 downto 0) := (others => '0');
  attribute async_reg of clk_ddr_tgl_sync : signal is "TRUE";
  signal clk_ddr_tgl_sync_d  : std_logic := '0';
  signal clk_ddr_live_cnt    : unsigned(31 downto 0) := (others => '0');
  -- rst_ddr observed in clk_sys: 2-FF sync (init reset-asserted) + sticky.
  signal rst_ddr_sync        : std_logic_vector(1 downto 0) := (others => '1');
  attribute async_reg of rst_ddr_sync : signal is "TRUE";
  signal rst_ddr_ever_deassert : std_logic := '0';
  -- clk_ddr-domain benign power-on reset for the REA core (rst_sys synced into
  -- clk_ddr — NOT the suspect rst_ddr, so a stuck rst_ddr can't blind the REA).
  signal rst_sys_in_ddr      : std_logic_vector(1 downto 0) := (others => '1');
  attribute async_reg of rst_sys_in_ddr : signal is "TRUE";
  -- DDR-commit REA probe word (clk_ddr domain). Drives the existing
  -- rea_trigger_out / rea_trig_sticky anchor (repurposed from the EMIO core).
  signal ddr_commit_probe_word : std_logic_vector(31 downto 0);

  -- DS-T2.34: i2c REA probe word (re-repurposes the single USER1 REA core back
  -- to the i2c seam + slave FSM, on clk_sys). Captures the real bus SCL/SDA, the
  -- injector drive/release, and the slave's bit counter + detected edges + FSM
  -- state — to localise the premature-SDA-release / SCL-miscount that fails ~27%
  -- of multi-byte master reads on silicon (sim is 9/9). LSB-first layout matches
  -- debug/i2c_seam.yml.
  signal i2c_slave_dbg_w : std_logic_vector(19 downto 0);
  signal i2c_dbg_fifo_w  : std_logic_vector(15 downto 0);  -- DS-T2.34 FIFO internals
  signal i2c_dbg_shift_w : std_logic_vector(7 downto 0);   -- DS-T2.34 slave latched byte
  signal i2c_dbg2_w      : std_logic_vector(7 downto 0);   -- DS-T2.34 bail-decision conds
  signal i2c_probe_word  : std_logic_vector(31 downto 0);


  -- ── DS-T2.20 / DS-T1.21: DMA commit-progress observability (PS-LOOP-REQ-9) ─
  -- The DMA write (commit) address from ddr_stream_axi.wr_commit_addr_o and its
  -- write-idle flag — so the PS RingOracle can walk the ring up to the COMMIT
  -- point, not its own enqueue counter (RING_WRITE_PTR), and know when the DMA
  -- has caught up. wr_commit_addr is clk_ddr-domain (native to the REA).
  signal ddr_wr_commit_addr   : std_logic_vector(31 downto 0);  -- clk_ddr
  signal ddr_write_idle       : std_logic;                       -- from ddr_stream
  -- DS-P2.167: ddr_write_idle is a clk_sys signal (not upBusBusyCdc), but
  -- p_wp_sticky samples it on clk_ddr. 2-FF sync into clk_ddr with ASYNC_REG.
  signal ddr_write_idle_ddr_sync : std_logic_vector(1 downto 0) := (others => '1');
  attribute async_reg of ddr_write_idle_ddr_sync : signal is "TRUE";
  -- clk_sys observers for the regbank: write_idle (clean 1-bit CDC) + a toggle
  -- handshake for ddr_commit_addr (DS-T1.21). Instead of an incoherent plain
  -- 2-FF across 32 bits, commit_addr_held_ddr is held stable in clk_ddr while
  -- a toggle handshake synchronizes req/ack across domains, ensuring coherent,
  -- skew-free 32-bit sampling into ddr_commit_addr_sys (RING_COMMIT_PTR).
  signal ddr_write_idle_sync  : std_logic_vector(1 downto 0) := (others => '0');
  attribute async_reg of ddr_write_idle_sync : signal is "TRUE";

  signal commit_addr_held_ddr : std_logic_vector(31 downto 0) := (others => '0');
  signal commit_req_tgl_ddr   : std_logic := '0';
  signal commit_ack_sync_ddr  : std_logic_vector(1 downto 0) := (others => '0');
  attribute async_reg of commit_ack_sync_ddr : signal is "TRUE";

  signal commit_req_sync_sys  : std_logic_vector(1 downto 0) := (others => '0');
  attribute async_reg of commit_req_sync_sys : signal is "TRUE";
  signal commit_ack_tgl_sys   : std_logic := '0';
  signal ddr_commit_addr_sys  : std_logic_vector(31 downto 0) := (others => '0');

  -- ── DS-T1.13: write-plane ARMING observability (rr_ddr_stream 0.3.2 vs 0.3.1) ─
  -- The DS-T1.12 wedge (RING_COMMIT_PTR stuck at 0, DDR_RESET_STATUS write_idle=1,
  -- no AW ever issued) is proven on silicon by A/B but does NOT reproduce in sim.
  -- Per the sim-pass/silicon-fail rule the mechanism must be OBSERVED, so tap the
  -- uploader's arming path: the CDC empty/full flags it uses to decide there is
  -- data to write (ddr_csr_o, registered in clk_sys), the raw plane reset/CS
  -- state (dbg_reset_status_o), and STICKY "ever happened since rst_ddr" latches
  -- for the AW/W/B handshake + write-active. A single stuck bit here (e.g.
  -- chip_empty never falling, or aw_valid never seen) names the first divergence
  -- vs the 0.3.1 control. The stickies encode the whole power-on→first-ingress
  -- history in ONE clk_sys read, so the A/B is a register compare over the proven
  -- devmem channel (DDR_RESET_STATUS upper bits) rather than a live REA trace
  -- whose Altera readback is UNCONFIRMED (rea-issp Rung 0b, hw-bitstream-confirm
  -- is Xilinx-only). The full word is ALSO routed to the clk_sys REA (rea_probe).
  signal ddr_csr_w            : rr.ddr_stream_pkg.fifo_csr_t;   -- clk_sys (registered in IP)
  signal ddr_dbg_reset_status : std_logic_vector(3 downto 0);  -- clk_ddr raw {pipeBusy,pipeBusyDdr,uploader_cs,downloader_cs}
  -- clk_ddr sticky "seen since rst_ddr" arming latches (set-once, hold to reset)
  signal aw_valid_seen_ddr    : std_logic := '0';   -- axi_h2c.AWVALID ever '1'  ("first-write arm")
  signal aw_hs_seen_ddr       : std_logic := '0';   -- AWVALID & AWREADY ever (first AW accepted)
  signal w_valid_seen_ddr     : std_logic := '0';   -- axi_h2c.WVALID ever '1'
  signal b_valid_seen_ddr     : std_logic := '0';   -- axi_c2h.BVALID ever '1' (write response returned)
  signal wr_active_seen_ddr   : std_logic := '0';   -- ddr_write_idle ever '0' (DMA ever initiated)
  signal wr_err_seen_ddr      : std_logic := '0';   -- BVALID & BRESP/=OKAY ever
  -- clk_sys scalar 2-FF syncs of the (monotonic-level) stickies + raw dbg bits (DS-T1.21).
  -- Each independent diagnostic wire uses an isolated 2-flop shift register to avoid
  -- multi-bit ASYNC_REG vector hazards.
  signal aw_valid_seen_sync_r : std_logic_vector(1 downto 0) := (others => '0');
  signal aw_hs_seen_sync_r    : std_logic_vector(1 downto 0) := (others => '0');
  signal w_valid_seen_sync_r  : std_logic_vector(1 downto 0) := (others => '0');
  signal b_valid_seen_sync_r  : std_logic_vector(1 downto 0) := (others => '0');
  signal wr_active_seen_sync_r: std_logic_vector(1 downto 0) := (others => '0');
  signal wr_err_seen_sync_r   : std_logic_vector(1 downto 0) := (others => '0');
  attribute async_reg of aw_valid_seen_sync_r : signal is "TRUE";
  attribute async_reg of aw_hs_seen_sync_r    : signal is "TRUE";
  attribute async_reg of w_valid_seen_sync_r  : signal is "TRUE";
  attribute async_reg of b_valid_seen_sync_r  : signal is "TRUE";
  attribute async_reg of wr_active_seen_sync_r: signal is "TRUE";
  attribute async_reg of wr_err_seen_sync_r   : signal is "TRUE";
  signal wp_seen_sync         : std_logic_vector(5 downto 0) := (others => '0');

  -- DS-P2.167: dbg_reset_status synchronizers into clk_sys — one chain per async
  -- source to eliminate CDC-11 launch flop fan-out and Quartus CDC-50012
  -- multi-clock domain synchronizer violations:
  --   * pipeBusy (bit 3): native clk_sys; 1-FF register in clk_sys (no ASYNC_REG)
  --   * pipeBusyDdr (bit 2): native clk_ddr; dedicated 2-FF sync chain into clk_sys
  --   * CS (bits 1..0): single 2-FF chain into clk_sys prevents launch flop
  --     fan-out to 2 chains.
  -- DS-P3.196: since rr_ddr_stream 0.3.4 (DDR-T2.17) the IP drives the CS bit
  -- combinationally (uploader_cs_r and not pipeBusyDdr), so it is registered once
  -- in clk_ddr (wp_dbg_cs_ddr_r) before the crossing. Without that flop the AND
  -- feeds the synchronizer's first stage directly (Vivado CDC-10).
  signal wp_dbg_pipe_busy_r    : std_logic := '0';
  signal wp_dbg_pbusy_ddr_meta : std_logic := '0';
  signal wp_dbg_pbusy_ddr_sync : std_logic := '0';
  signal wp_dbg_cs_ddr_r       : std_logic := '0';  -- clk_ddr launch flop for the CS crossing
  signal wp_dbg_cs_meta        : std_logic := '0';
  signal wp_dbg_cs_sync        : std_logic := '0';
  attribute async_reg of wp_dbg_pbusy_ddr_meta : signal is "TRUE";
  attribute async_reg of wp_dbg_pbusy_ddr_sync : signal is "TRUE";
  attribute async_reg of wp_dbg_cs_meta        : signal is "TRUE";
  attribute async_reg of wp_dbg_cs_sync        : signal is "TRUE";

  signal wp_dbg_sync          : std_logic_vector(3 downto 0) := (others => '0');
  signal write_plane_dbg_word : std_logic_vector(31 downto 0);  -- clk_sys REA/telemetry word

  -- DS-P3.71 / DS-T1.21 CDC: 2-FF sync for the async injected-SPI source feeding the clk_sys
  -- edge dataloggers (EMIO_MUX_SEL bit[3] self-test path). edge_datalogger samples
  -- EDGE_IN directly, so without this the src_spi_* → edge_detector arc is an
  -- unconstrained CDC that closed only marginally (+0.011 ns). Metastability is
  -- contained here; the async crossing is declared via set_false_path in timing.xdc.
  -- Each independent SPI line uses an isolated scalar 2-flop shift register (DS-T1.21).
  signal spi_edge_sclk_sync_r : std_logic_vector(1 downto 0) := (others => '0');
  signal spi_edge_mosi_sync_r : std_logic_vector(1 downto 0) := (others => '0');
  signal spi_edge_miso_sync_r : std_logic_vector(1 downto 0) := (others => '0');
  signal spi_edge_ss_sync_r   : std_logic_vector(1 downto 0) := (others => '0');
  signal spi_edge_sync_r      : std_logic_vector(3 downto 0) := (others => '0');
  attribute async_reg of spi_edge_sclk_sync_r : signal is "TRUE";
  attribute async_reg of spi_edge_mosi_sync_r : signal is "TRUE";
  attribute async_reg of spi_edge_miso_sync_r : signal is "TRUE";
  attribute async_reg of spi_edge_ss_sync_r   : signal is "TRUE";

  -- DS-P3.69 / DS-T1.21: 2-FF ASYNC_REG sync for the injected I2C SCL/SDA feeding
  -- EDGE0/EDGE1 (EMIO_MUX_SEL bit[4]) — real Edge Detector edges → RouteWave
  -- sigrok i2c decode. Bit order {sda,scl} = i2c_edge_sync_r(1..0).
  -- Each line uses an isolated scalar 2-flop shift register (DS-T1.21).
  signal i2c_edge_sel         : std_logic;
  signal i2c_edge_scl_sync_r  : std_logic_vector(1 downto 0) := (others => '0');
  signal i2c_edge_sda_sync_r  : std_logic_vector(1 downto 0) := (others => '0');
  signal i2c_edge_sync_r      : std_logic_vector(1 downto 0) := (others => '0');
  attribute async_reg of i2c_edge_scl_sync_r : signal is "TRUE";
  attribute async_reg of i2c_edge_sda_sync_r : signal is "TRUE";

  -- DS-T3.5 dogfood: route the UART0_TX line into EDGE0 (id=1) so the edge
  -- datalogger captures the RAW UART waveform → RouteWave EdgeStreams + host-side
  -- sigrok UART decode, to inspect the real silicon edges that drive the
  -- spurious parity_error (HW-only, unmodeled by the clean BFM). EMIO_MUX_SEL
  -- bit[5]=1 engages it (highest priority on EDGE0, > I2C/SPI self-test).
  signal uart_edge_sel    : std_logic;
  signal uart_edge_meta_r : std_logic := '0';
  signal uart_edge_sync_r : std_logic := '0';
  attribute async_reg of uart_edge_meta_r : signal is "TRUE";
  attribute async_reg of uart_edge_sync_r : signal is "TRUE";

  -- DS-P3.107 / DS-T1.21 debug: dual edge-capture of the CAN-FD bus to localize the silicon-
  -- only fixed-stuff CAPTURE defect. EDGE0(id=1) ← can_bus_w (RX: the exact
  -- wired-AND line the protocol datalogger id20 samples); EDGE1(id=2) ← ctu_a_tx_w
  -- (TX: CTU-A's transmitter, frame1's source). Raw edges = decode-independent
  -- ground truth → offline can_ctu_oracle gives the TRUE sc/CRC to compare vs the
  -- protocol datalogger's reported sc/Cap17, splitting a front-end/sampling fault
  -- from a destuff/capture fault. EMIO_MUX_SEL bit[6]=1 engages it (highest
  -- priority on EDGE0/EDGE1). Both sources are clk_sys-domain (CTU aclk=clk_sys,
  -- the bus is fabric) so the 2-FF is strictly belt-and-braces, kept for the
  -- uniform CDC discipline. Bit order {tx=ctu_a, rx=bus} = can_edge_sync_r(1..0).
  -- Each line uses an isolated scalar 2-flop shift register (DS-T1.21).
  signal can_edge_sel        : std_logic;
  signal can_edge_rx_sync_r  : std_logic_vector(1 downto 0) := (others => '0');
  signal can_edge_tx_sync_r  : std_logic_vector(1 downto 0) := (others => '0');
  signal can_edge_sync_r     : std_logic_vector(1 downto 0) := (others => '0');
  attribute async_reg of can_edge_rx_sync_r : signal is "TRUE";
  attribute async_reg of can_edge_tx_sync_r : signal is "TRUE";

  -- DS-P3.166 / DS-T1.21: 2-FF synchronizer for the async EMIO / PMOD pin probe
  -- word before sampling into telemetry_in(EMIO_PROBE) (Finding F5). Prevents
  -- metastability from raw ja[*], jb[*] and PS7 EMIO pins reaching the AXI-Lite
  -- regbank. The 31 probe bits are independent status lines (no word coherence
  -- is claimed), so each bit gets its own scalar 2-flop shift register.
  type emio_probe_sync_t is array (30 downto 0) of std_logic_vector(1 downto 0);
  signal emio_probe_sync_r : emio_probe_sync_t := (others => (others => '0'));
  signal emio_probe_sync_w : std_logic_vector(30 downto 0);
  attribute async_reg of emio_probe_sync_r : signal is "TRUE";

  -- DS-T2.26 probe: discriminate where the in-band loss frame is lost on silicon.
  signal emit_valid_cnt_r : unsigned(31 downto 0) := (others => '0');  -- cycles err_fr_valid=1
  signal emit_done_cnt_r  : unsigned(31 downto 0) := (others => '0');  -- emitter→framer handshakes

  -- DDR ring write counter (DS-P3.27 Phase B). With the zs_msg_framer
  -- in the loop (DS-P3.28 v1), each datalogger message produces a 6-word
  -- frame (5 header words + 1 payload word), so the counter advances
  -- by 6 per source message. PS reads RING_WRITE_PTR, computes byte
  -- offset = (count * 4) mod RING_BYTES, and drains zs_msg frames out
  -- of the mmaped ring window.
  signal ring_write_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal ddr_backpressure_cnt_r : unsigned(31 downto 0) := (others => '0');  -- DS-T2.22 mech B
  -- DS-P3.91: burst arbiter grant counter. The regbank ARB_GRANT_CNT slot
  -- was undriven (read the ROM-init 0). Counts gather burst-arbiter GRANTS — the
  -- valid&&ready handshakes (dout_valid & dout_req). NOT raw valid cycles:
  -- the arbiter HOLDS valid until the downstream READY accepts (datalogger_gather
  -- comment "IN_READY drops and the burst arbiter holds"), so counting valid
  -- alone would over-count one grant as many.
  signal arb_grant_cnt_r : unsigned(31 downto 0) := (others => '0');

  -- ── DS-P3.60: ring-fill watermark IRQ (hysteresis, drives IRQ_F2P[0]) ──────
  -- The PoC drain is polled (DS-P2.59 SW read pointer). DS-T2.22 made the
  -- eventual ring-full loss VISIBLE after the fact; DS-P3.60 raises an
  -- out-of-band PL→PS interrupt at a HIGH watermark (75% full) so the PS
  -- drains BEFORE overflow — early warning, not a post-mortem counter.
  --
  -- Schmitt-trigger (two watermarks) to avoid interrupt thrash: the alarm
  -- LATCH sets when fill crosses HIGH (75%) and clears ONLY when the consumer
  -- has drained back below LOW (50%). So one fill excursion = exactly one
  -- interrupt episode; the line stays asserted through the whole drain and
  -- re-arms only after a deep (≥25% headroom) drain. Level-sensitive (GIC
  -- <0 29 4> = level-high) so a slow PS can never miss the edge.
  --
  -- fill = (commit_off - read_off) mod RING_BYTES, both taken as the low
  -- G_DDR_WIN_WIDTH bits (G_DDR_BASE_ADDR = 0x1000_0000 is exactly 2^28-
  -- aligned, so the low 28 bits of the absolute commit address ARE the
  -- ring-relative offset; same frame as the SW-written RING_READ_PTR). The
  -- watermark + hysteresis FSM lives in the u_ring_watermark instance below.
  signal ring_alarm_w     : std_logic;                       -- live alarm level
  signal ring_alarm_cnt_w : std_logic_vector(31 downto 0);   -- # excursions ≥ HIGH
  signal ring_fill_w      : std_logic_vector(G_DDR_WIN_WIDTH - 1 downto 0);
  signal ring_alarm_irq   : std_logic_vector(0 downto 0);    -- → IRQ_F2P[0]

  -- zs_msg_framer wiring (DS-P3.28 v1). Datalogger-source tagging is
  -- placeholder: datalogger_id is a per-frame counter, domain is SYS.
  -- DS-P3.29 (2026-05-17) — landed: per-source (datalogger_id, msg_type)
  -- threaded through the arbiter chain. Frames now land on
  -- per-protocol channels in RouteWave instead of the generic
  -- pl_heartbeat lane.
  signal framer_in_valid    : std_logic;
  signal framer_in_ready    : std_logic;
  signal framer_out_data    : std_logic_vector(31 downto 0);
  signal framer_out_valid   : std_logic;
  signal timestamp64        : std_logic_vector(63 downto 0);

  -- DS-P2.47 Phase B2 step 3: variable-payload framer + EDGE_BATCH path.
  -- The framer is now configured for up to G_PAYLOAD_WORDS_MAX = 32 words
  -- per frame. A 2:1 mux at its input picks between:
  --   * the non-EDGE single-word pass-through (datalogger_gather → framer),
  --   * the edge_batcher_aggregator's variable-N batches (DOMAIN_EDGE
  --     events accumulated by per-channel edge_batchers).
  constant C_PAYLOAD_WORDS_MAX : integer := 32;

  -- DS-P3.50: the edge aggregator no longer demuxes EV_WORD[31:27] directly as
  -- the batcher index (which forced max(edge id)+1 = 7 batchers with 0/4/5 dead,
  -- DS-T10). It now maps the SPARSE global ids (1,2,3,6) onto C_EDGE_DATALOGGERS
  -- DENSE batchers via G_CHANNEL_IDS => C_EDGE_IDS_FLAT_W — exactly 4 batchers,
  -- no dead ones, ~2.7k LUTs reclaimed, and the dead-batcher[4] WNS path gone.

  -- Gather output classifier: msg_type[11:8] = DOMAIN_EDGE (0x6) routes
  -- the event to the aggregator; everything else bypasses to the framer.
  signal gather_is_edge_w   : std_logic;
  signal gather_to_agg_v_w  : std_logic;   -- valid → aggregator
  signal gather_to_fr_v_w   : std_logic;   -- valid → non-EDGE pass-through

  -- Aggregator handshake + outputs.
  signal agg_ev_ready_w     : std_logic;
  signal agg_fr_valid_w     : std_logic;
  signal agg_fr_ready_w     : std_logic;
  signal agg_fr_datalogger_id_w : std_logic_vector(7  downto 0);
  signal agg_fr_msg_class_w  : std_logic_vector(7  downto 0);
  signal agg_fr_msg_type_w   : std_logic_vector(15 downto 0);
  signal agg_fr_flags_w      : std_logic_vector(15 downto 0);
  signal agg_fr_timestamp_w  : std_logic_vector(63 downto 0);
  signal agg_fr_payload_w    : std_logic_vector(C_PAYLOAD_WORDS_MAX*32 - 1 downto 0);
  signal agg_fr_words_w      : std_logic_vector(7  downto 0);

  -- Non-EDGE pass-through tuple (gather output, zero-padded to the wide
  -- framer PAYLOAD width).
  signal nonedge_ready_w     : std_logic;
  signal nonedge_payload_w   : std_logic_vector(C_PAYLOAD_WORDS_MAX*32 - 1 downto 0);

  -- 2:1 arb output → framer input. Aggregator wins on tie (its batches
  -- are rarer than per-event non-EDGE traffic; a starvation guard would
  -- be a follow-up if a hostile EDGE workload monopolises the framer).
  signal framer_datalogger_w    : std_logic_vector(7  downto 0);
  signal framer_msg_class_w  : std_logic_vector(7  downto 0);
  signal framer_msg_type_w   : std_logic_vector(15 downto 0);
  signal framer_flags_w      : std_logic_vector(15 downto 0);
  signal framer_timestamp_w  : std_logic_vector(63 downto 0);
  signal framer_payload_w    : std_logic_vector(C_PAYLOAD_WORDS_MAX*32 - 1 downto 0);
  signal framer_words_live_w : std_logic_vector(7  downto 0);

  -- DS-P3.70 piece 3: in-band error-event emitter — the 3rd (top-priority)
  -- framer input. Coalesces ERR_CNT_EDGE deltas into rate-limited SEV_ERROR
  -- frames (edge internal-FIFO-overflow lost_count) so RouteWave can mark the
  -- loss inline. Error frames are rare + important → win the framer arbitration.
  signal err_fr_valid_w      : std_logic;
  signal err_fr_ready_w      : std_logic;
  signal err_fr_datalogger_id_w : std_logic_vector(7  downto 0);
  signal err_fr_msg_class_w  : std_logic_vector(7  downto 0);
  signal err_fr_msg_type_w   : std_logic_vector(15 downto 0);
  signal err_fr_flags_w      : std_logic_vector(15 downto 0);
  signal err_fr_timestamp_w  : std_logic_vector(63 downto 0);
  signal err_fr_payload32_w  : std_logic_vector(31 downto 0);
  signal err_fr_words_w      : std_logic_vector(7  downto 0);

  -- DS-T3.18: per-EDGE-instance running loss counters from the gather (channel k
  -- in bits [(k+1)*32-1 : k*32]). Each feeds a per-channel emitter inside the
  -- error_event_bank so the in-band loss frame names the overflowing channel.
  signal edge_err_cnts_w     : std_logic_vector(C_EDGE_DATALOGGERS*32-1 downto 0);

  -- DS-P3.117 piece 4: in-band max-frequency bank — the 4th framer input. Per
  -- EDGE channel, watches the gather's re-exported min-interval tap and emits an
  -- INFO | DOMAIN_EDGE | EVT_EDGE_MAXFREQ frame (2-word payload) naming the
  -- channel whose min just got smaller (or was re-armed via CONFIG[2]). Mirrors
  -- the error_event_bank precedent; lowest framer priority (the running min is
  -- not loss-critical). Payload is 64 bits (word1|word0), unlike the error path's
  -- 32-bit single word.
  signal edge_max_ivals_w    : std_logic_vector(C_EDGE_DATALOGGERS*C_EDGE_MAXFREQ_WIDTH-1 downto 0);
  signal edge_max_new_w      : std_logic_vector(C_EDGE_DATALOGGERS-1 downto 0);
  signal mf_fr_valid_w       : std_logic;
  signal mf_fr_ready_w       : std_logic;
  signal mf_fr_datalogger_id_w  : std_logic_vector(7  downto 0);
  signal mf_fr_msg_class_w   : std_logic_vector(7  downto 0);
  signal mf_fr_msg_type_w    : std_logic_vector(15 downto 0);
  signal mf_fr_flags_w       : std_logic_vector(15 downto 0);
  signal mf_fr_timestamp_w   : std_logic_vector(63 downto 0);
  signal mf_fr_payload64_w   : std_logic_vector(63 downto 0);
  signal mf_fr_words_w       : std_logic_vector(7  downto 0);

  -- Pack the per-EDGE-channel device ids into the flat generic the
  -- error_event_bank expects (channel k id → bits [(k+1)*8-1 : k*8]).
  function f_edge_ids_flat return std_logic_vector is
    variable v : std_logic_vector(C_EDGE_DATALOGGERS*8-1 downto 0) := (others => '0');
  begin
    for k in 0 to C_EDGE_DATALOGGERS-1 loop
      v((k+1)*8-1 downto k*8) := std_logic_vector(to_unsigned(C_EDGE_IDS(k), 8));
    end loop;
    return v;
  end function f_edge_ids_flat;
  constant C_EDGE_IDS_FLAT_W : std_logic_vector(C_EDGE_DATALOGGERS*8-1 downto 0)
                             := f_edge_ids_flat;

  -- Compile-time ring locator constants exposed to PS firmware via the
  -- regbank RO slots (RING_BASE_LOW/HIGH, RING_BYTES). Derived from
  -- generics; SW doesn't need a recompile when the ring moves or
  -- resizes — it just re-reads these slots after a bitstream load.
  constant C_RING_BASE_LOW  : std_logic_vector(31 downto 0)
    := std_logic_vector(G_DDR_BASE_ADDR);
  constant C_RING_BASE_HIGH : std_logic_vector(31 downto 0)
    := (others => '0');  -- Zynq-7: 32-bit physical address space
  constant C_RING_BYTES     : std_logic_vector(31 downto 0)
    := std_logic_vector(to_unsigned(2**G_DDR_WIN_WIDTH, 32));

begin

  ---------------------------------------------------------------------------
  -- Clock / reset distribution. fclk_clk0 is the single fabric clock;
  -- fclk_aresetn is the active-low synchronous reset out of the BD's
  -- proc_sys_reset. rr-ddr-stream uses a single clock (sys==ddr).
  ---------------------------------------------------------------------------
  clk_sys <= clk_sys_i;
  -- DS-P2.153 (PF-F0 100 MHz): rst_sys is consumed SYNCHRONOUSLY by ~20k
  -- fabric loads, and on the PolarFire fit the single upstream reset flop's
  -- fan-out net was the measured −2.70 ns worst class (the late reset sat
  -- inside every sync-reset data mux from the batchers' payload_buf to the
  -- gather FIFOs and the framer). Register the incoming reset ONCE here —
  -- inside the hierarchy that owns the loads, so synthesis CAN replicate it
  -- (syn_maxfan; a hint outside the core cannot replicate across the port
  -- boundary) — and fan the whole core from the registered copy. The entire
  -- core sees reset assert/deassert uniformly one clk_sys cycle later;
  -- replica skew is irrelevant for a synchronous reset. Init '1' = core
  -- powers up in reset. Behaviour-guarded by the DE25/Zynq baselines.
  p_rst_sys_reg : process(clk_sys_i)
  begin
    if rising_edge(clk_sys_i) then
      rst_sys_q <= rst_sys_i;
    end if;
  end process;
  rst_sys <= rst_sys_q;
  clk_ddr <= clk_ddr_i;
  rst_ddr <= rst_ddr_i;

  gp0_awaddr <= s_axil_awaddr_i;
  gp0_awprot <= s_axil_awprot_i;
  gp0_awvalid <= s_axil_awvalid_i;
  s_axil_awready_o <= gp0_awready;
  gp0_wdata <= s_axil_wdata_i;
  gp0_wstrb <= s_axil_wstrb_i;
  gp0_wvalid <= s_axil_wvalid_i;
  s_axil_wready_o <= gp0_wready;
  s_axil_bresp_o <= gp0_bresp;
  s_axil_bvalid_o <= gp0_bvalid;
  gp0_bready <= s_axil_bready_i;
  gp0_araddr <= s_axil_araddr_i;
  gp0_arprot <= s_axil_arprot_i;
  gp0_arvalid <= s_axil_arvalid_i;
  s_axil_arready_o <= gp0_arready;
  s_axil_rdata_o <= gp0_rdata;
  s_axil_rresp_o <= gp0_rresp;
  s_axil_rvalid_o <= gp0_rvalid;
  gp0_rready <= s_axil_rready_i;

  canfd0_apb_paddr_w <= canfd0_apb_paddr_i;
  canfd0_apb_penable_w <= canfd0_apb_penable_i;
  canfd0_apb_psel_w(0) <= canfd0_apb_psel_i;
  canfd0_apb_pwrite_w <= canfd0_apb_pwrite_i;
  canfd0_apb_pwdata_w <= canfd0_apb_pwdata_i;
  canfd0_apb_prdata_o <= canfd0_apb_prdata_w;
  canfd0_apb_pready_o <= canfd0_apb_pready_w(0);
  canfd0_apb_pslverr_o <= canfd0_apb_pslverr_w(0);
  canfd1_apb_paddr_w <= canfd1_apb_paddr_i;
  canfd1_apb_penable_w <= canfd1_apb_penable_i;
  canfd1_apb_psel_w(0) <= canfd1_apb_psel_i;
  canfd1_apb_pwrite_w <= canfd1_apb_pwrite_i;
  canfd1_apb_pwdata_w <= canfd1_apb_pwdata_i;
  canfd1_apb_prdata_o <= canfd1_apb_prdata_w;
  canfd1_apb_pready_o <= canfd1_apb_pready_w(0);
  canfd1_apb_pslverr_o <= canfd1_apb_pslverr_w(0);

  axi_c2h <= m_axi_c2h_i;
  m_axi_h2c_o <= axi_h2c;

  -- DS-P2.126: bits [11:8] steer the internal (non-PMOD) source from the
  -- legacy emio_* legs onto the MSS fabric peripherals. Cleared, every
  -- assignment below is the pre-DS-P2.126 connection.
  mss_i2c_sel  <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(8);
  mss_uart_sel <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(9);
  mss_spi_sel  <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(10);
  mss_can_sel  <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(11);
  -- MSS SPI master: OE=1 drives the pin, OE=0 parks CLK low / DO low / SS high.
  mss_spi_clk_w <= mss_spi_clk_i when mss_spi_clk_oe_i = '1' else '0';
  mss_spi_do_w  <= mss_spi_do_i  when mss_spi_do_oe_i  = '1' else '0';
  mss_spi_ss_w  <= mss_spi_ss_i  when mss_spi_ss_oe_i  = '1' else '1';

  -- DS-P1.90: the EMIO legs as consumed below. A generator leg wins only while
  -- G_STIM_DRIVES_EMIO and that protocol's STIM_*_CONTROL bit[0] are both set.
  stim_uart_on <= stim_uart_control_w(0) when G_STIM_DRIVES_EMIO else '0';
  stim_i2c_on  <= stim_i2c_control_w(0)  when G_STIM_DRIVES_EMIO else '0';
  stim_spi_on  <= stim_spi_control_w(0)  when G_STIM_DRIVES_EMIO else '0';
  emio_uart_tx_w    <= stim_uart_tx_w   when stim_uart_on = '1' else emio_uart_tx_i;
  emio_i2c_scl_o_w  <= stim_i2c_scl_o_w when stim_i2c_on = '1' else emio_i2c_scl_o_i;
  emio_i2c_scl_t_w  <= stim_i2c_scl_t_w when stim_i2c_on = '1' else emio_i2c_scl_t_i;
  emio_i2c_sda_o_w  <= stim_i2c_sda_o_w when stim_i2c_on = '1' else emio_i2c_sda_o_i;
  emio_i2c_sda_t_w  <= stim_i2c_sda_t_w when stim_i2c_on = '1' else emio_i2c_sda_t_i;
  emio_spi_sclk_o_w <= stim_spi_sclk_w  when stim_spi_on = '1' else emio_spi_sclk_o_i;
  emio_spi_sclk_t_w <= '0'              when stim_spi_on = '1' else emio_spi_sclk_t_i;
  emio_spi_mosi_o_w <= stim_spi_mosi_w  when stim_spi_on = '1' else emio_spi_mosi_o_i;
  emio_spi_mosi_t_w <= '0'              when stim_spi_on = '1' else emio_spi_mosi_t_i;
  emio_spi_ss_o_w   <= stim_spi_ss_w    when stim_spi_on = '1' else emio_spi_ss_o_i;
  emio_spi_ss_t_w   <= '0'              when stim_spi_on = '1' else emio_spi_ss_t_i;
  -- The SPI datalogger frames on the GPIO chip-select (DS-T2.18), so the
  -- generator's SS drives it too, as the DE25 top's loopback does.
  emio_gpio_o_w     <= stim_spi_ss_w    when stim_spi_on = '1' else emio_gpio_o_i;
  emio_gpio_t_w     <= '0'              when stim_spi_on = '1' else emio_gpio_t_i;

  uart0_tx_w <= mss_uart_tx_i when mss_uart_sel = '1' else emio_uart_tx_w;
  emio_uart_rx_o <= uart0_rx_w;
  mss_uart_rx_o <= uart0_rx_w;
  i2c0_scl_o_w <= '0' when mss_i2c_sel = '1' else emio_i2c_scl_o_w;
  i2c0_scl_t_w <= (not mss_i2c_scl_oe_i) when mss_i2c_sel = '1' else emio_i2c_scl_t_w;
  i2c0_sda_o_w <= '0' when mss_i2c_sel = '1' else emio_i2c_sda_o_w;
  i2c0_sda_t_w <= (not mss_i2c_sda_oe_i) when mss_i2c_sel = '1' else emio_i2c_sda_t_w;
  emio_i2c_scl_i_o <= i2c0_bus_scl_w;
  emio_i2c_sda_i_o <= i2c0_bus_sda_w;
  mss_i2c_scl_o <= i2c0_bus_scl_w;
  mss_i2c_sda_o <= i2c0_bus_sda_w;
  spi0_sclk_o_w <= mss_spi_clk_w when mss_spi_sel = '1' else emio_spi_sclk_o_w;
  spi0_sclk_t_w <= '0' when mss_spi_sel = '1' else emio_spi_sclk_t_w;
  spi0_mosi_o_w <= mss_spi_do_w when mss_spi_sel = '1' else emio_spi_mosi_o_w;
  spi0_mosi_t_w <= '0' when mss_spi_sel = '1' else emio_spi_mosi_t_w;
  spi0_miso_o_w <= emio_spi_miso_o_i;
  spi0_miso_t_w <= emio_spi_miso_t_i;
  spi0_ss_o_w <= mss_spi_ss_w when mss_spi_sel = '1' else emio_spi_ss_o_w;
  spi0_ss_t_w <= '0' when mss_spi_sel = '1' else emio_spi_ss_t_w;
  emio_spi_sclk_i_o <= spi0_bus_sclk_w;
  emio_spi_mosi_i_o <= spi0_bus_mosi_w;
  emio_spi_miso_i_o <= spi0_bus_miso_w;
  emio_spi_ss_i_o <= spi0_bus_ss_w;
  mss_spi_di_o <= spi0_bus_miso_w;
  -- DS-T2.18: the SPI datalogger frames on the GPIO chip-select, not native SS.
  gpio_o_w(0) <= mss_spi_ss_w when mss_spi_sel = '1' else emio_gpio_o_w;
  gpio_t_w(0) <= '0' when mss_spi_sel = '1' else emio_gpio_t_w;
  emio_gpio_i_o <= gpio_i_w(0);
  can0_phy_tx_w <= mss_can_tx_i when mss_can_sel = '1' else emio_can0_tx_i;
  can1_phy_tx_w <= emio_can1_tx_i;
  emio_can_rx_o <= can_bus_w;
  mss_can_rx_o <= can_bus_w;
  irq_o <= irq_f2p_w;

  ---------------------------------------------------------------------------
  -- Datalogger input matrix (DS-P2.51 Phase 1)
  --
  -- Per-protocol 2:1 mux: external PMOD pin vs PS EMIO bus, selected
  -- by EMIO_MUX_SEL bits 0/1/2 (I2C / UART / SPI). Default = external
  -- (PMOD); set the regbank bit to flip to the EMIO internal-loop.
  -- See header §1 for the slot table.
  --
  -- EMIO bus model (resolved in this block, just below the mux):
  --
  --   * I2C0 — open-drain.  bus = '0' iff the master is actively
  --     pulling the line low (_T='0' AND _O='0'); else '1' (pulled up).
  --     No PL slave in Phase 1, so the only puller is the master itself;
  --     SDA goes back high once master releases. The master's _I leg is
  --     fed from the same bus level so START / addr / NAK timing is real.
  --
  --   * SPI0 — master mode, push-pull. SCLK / MOSI / SS come from the
  --     master; MISO has no driver in Phase 1 (no echo slave), so the
  --     line sits at '0'. The master's MISO_I is wired to '0' too —
  --     spidev reads all zeros, which is the honest answer.
  --
  --   * UART0 — no slave needed (passive datalogger). RX line into PS is
  --     held at idle '1' so the UART controller doesn't see a permanent
  --     break condition.
  ---------------------------------------------------------------------------

  -- ── EMIO_MUX_SEL tap (low 3 bits of the regbank slot) ────────────────
  emio_mux_sel <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(2 downto 0);

  -- ── EMIO bus resolution + per-protocol source select ─────────────────
  -- Carved into tich.ds_emio_mux (DS-T2.11) so the self-validation loop is
  -- simulatable standalone. The resolved *_bus_w levels feed BOTH the PS
  -- master `_I` legs (u_bd, below) and the injector bus inputs; the src_*_w
  -- muxed sources feed the capture_pin assembly. See ds_emio_mux.vhd header.
  u_emio_mux : entity tich.ds_emio_mux
    port map (
      EMIO_MUX_SEL   => emio_mux_sel,
      -- external PMOD sources
      PMOD_I2C_SCL   => ja(0),
      PMOD_I2C_SDA   => ja(1),
      PMOD_UART_TX   => jb(0),
      PMOD_SPI_SS    => ja(4),
      PMOD_SPI_MOSI  => ja(5),
      PMOD_SPI_MISO  => ja(6),
      PMOD_SPI_SCLK  => ja(7),
      -- PS EMIO master legs (from the BD)
      UART0_TX       => uart0_tx_w,
      I2C0_SCL_O     => i2c0_scl_o_w,
      I2C0_SCL_T     => i2c0_scl_t_w,
      I2C0_SDA_O     => i2c0_sda_o_w,
      I2C0_SDA_T     => i2c0_sda_t_w,
      SPI0_SCLK_O    => spi0_sclk_o_w,
      SPI0_MOSI_O    => spi0_mosi_o_w,
      SPI0_SS_O      => spi0_ss_o_w,
      SPI0_CS_GPIO   => spi0_cs_gpio_r,  -- DS-T2.18: SPI datalogger CS = registered GPIO CS (timing)
      -- injector responder drives
      I2C_INJ_SDA_O  => i2c_inj_sda_o_w,
      I2C_INJ_SDA_T  => i2c_inj_sda_t_w,
      SPI_INJ_MISO_O => spi_inj_miso_o_w,
      SPI_INJ_MISO_T => spi_inj_miso_t_w,
      -- resolved bus levels (→ u_bd `_I` legs + injector bus inputs)
      I2C0_BUS_SCL   => i2c0_bus_scl_w,
      I2C0_BUS_SDA   => i2c0_bus_sda_w,
      SPI0_BUS_SCLK  => spi0_bus_sclk_w,
      SPI0_BUS_MOSI  => spi0_bus_mosi_w,
      SPI0_BUS_MISO  => spi0_bus_miso_w,
      SPI0_BUS_SS    => spi0_bus_ss_w,
      UART0_RX       => uart0_rx_w,
      -- muxed datalogger sources (→ capture_pin assembly)
      SRC_I2C_SCL    => src_i2c_scl_w,
      SRC_I2C_SDA    => src_i2c_sda_w,
      SRC_UART_TX    => src_uart_tx_w,
      SRC_SPI_SS     => src_spi_ss_w,
      SRC_SPI_MOSI   => src_spi_mosi_w,
      SRC_SPI_MISO   => src_spi_miso_w,
      SRC_SPI_SCLK   => src_spi_sclk_w
    );

  -- ── board_pin assembly ──────────────────────────────────────────────────
  -- DS-P1.66: the core no longer decides which logical capture pin a source
  -- lands on. It offers each source once, here, and the generated
  -- C_CAPTURE_BOARD_PIN (datalogger_config_pkg) routes it (gen_capture_pin).
  -- JA0/JA1 (I2C mux), JA4-7 (SPI mux), JB0 (UART mux) carry the EMIO
  -- self-test sources; JB2-5 the EDGE self-test chains; the default mix lands
  -- I2C #4/#5, SPI #8, UART #12/#13 and EDGE x4 on exactly these.
  board_pin(C_BOARD_PIN_JA0) <= src_i2c_scl_w;     -- PMOD ja(0) / EMIO I2C SCL
  board_pin(C_BOARD_PIN_JA1) <= src_i2c_sda_w;     -- PMOD ja(1) / EMIO I2C SDA
  board_pin(C_BOARD_PIN_JA2) <= ja(2);
  board_pin(C_BOARD_PIN_JA3) <= ja(3);
  board_pin(C_BOARD_PIN_JA4) <= src_spi_ss_w;      -- PMOD ja(4) / EMIO SPI SS
  board_pin(C_BOARD_PIN_JA5) <= src_spi_mosi_w;    -- PMOD ja(5) / EMIO SPI MOSI
  board_pin(C_BOARD_PIN_JA6) <= src_spi_miso_w;    -- PMOD ja(6) / EMIO SPI MISO
  board_pin(C_BOARD_PIN_JA7) <= src_spi_sclk_w;    -- PMOD ja(7) / EMIO SPI SCLK
  -- DS-P2.10: the UART line is conditioned by the gather's system-wide per-pin
  -- glitch_filter (one conditioner per datalogger input, same as every other line),
  -- so NO separate top-level UART filter here — that would double-condition the
  -- UART and is redundant. The async EMIO UART0 TX (src_uart_tx_w) is fed raw to
  -- its capture pin; the gather's glitch_filter on that pin has its own 2-FF
  -- synchroniser for the clk_sys crossing and the debounce that kills the
  -- spurious 1-clk edges (the DS-T3.5 fix, now applied uniformly system-wide).
  board_pin(C_BOARD_PIN_JB0) <= src_uart_tx_w;     -- PMOD jb(0) / EMIO UART0 TX
  board_pin(C_BOARD_PIN_JB1) <= jb(1);
  -- EDGE ×4. DS-P3.71 self-test: when EMIO_MUX_SEL bit[3]=1, the four edge
  -- dataloggers observe the FULL injected SPI signal set (EDGE#1←SCLK, #2←MOSI,
  -- #3←MISO, #4←CS) instead of the unconnected PMOD jb[5:2]. A single SPI
  -- injection then (a) overflows EDGE#1 via the fast SCLK edges → the DS-P3.70
  -- in-band loss path (ERR_CNT_EDGE climbs, emitter reports lost_count), AND
  -- (b) gives a fully DECODABLE SPI capture across the 4 logic-analyzer channels
  -- for RouteWave protocol decode — SCLK alone can't be decoded (no data/CS).
  emio_edge_sel <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(3);
  i2c_edge_sel  <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(4);   -- DS-P3.69
  uart_edge_sel <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(5);   -- DS-T3.5 dogfood
  can_edge_sel  <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(6);   -- DS-P3.107 CAN dual edge-capture
  stim_edge_sel <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(7);   -- DS-P2.100 stimulus edge/pulse source
  -- DS-P3.71/P3.69 CDC: 2-FF sync the async injected SPI/I2C sources into clk_sys
  -- before they feed the edge dataloggers. The PMOD jb path stays raw — it's a
  -- package-pin async input STA already treats as I/O.
  p_spi_edge_cdc : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      spi_edge_sclk_sync_r <= spi_edge_sclk_sync_r(0) & src_spi_sclk_w;
      spi_edge_mosi_sync_r <= spi_edge_mosi_sync_r(0) & src_spi_mosi_w;
      spi_edge_miso_sync_r <= spi_edge_miso_sync_r(0) & src_spi_miso_w;
      spi_edge_ss_sync_r   <= spi_edge_ss_sync_r(0)   & src_spi_ss_w;
      spi_edge_sync_r      <= spi_edge_ss_sync_r(1) & spi_edge_miso_sync_r(1)
                            & spi_edge_mosi_sync_r(1) & spi_edge_sclk_sync_r(1);

      i2c_edge_scl_sync_r  <= i2c_edge_scl_sync_r(0)  & src_i2c_scl_w;
      i2c_edge_sda_sync_r  <= i2c_edge_sda_sync_r(0)  & src_i2c_sda_w;
      i2c_edge_sync_r      <= i2c_edge_sda_sync_r(1) & i2c_edge_scl_sync_r(1);

      uart_edge_meta_r     <= src_uart_tx_w;                  -- DS-T3.5 dogfood
      uart_edge_sync_r     <= uart_edge_meta_r;

      can_edge_rx_sync_r   <= can_edge_rx_sync_r(0)   & can_bus_w;
      can_edge_tx_sync_r   <= can_edge_tx_sync_r(0)   & ctu_a_tx_w;
      can_edge_sync_r      <= can_edge_tx_sync_r(1) & can_edge_rx_sync_r(1);
    end if;
  end process;
  -- EDGE0/EDGE1: UART dogfood (bit[5]) > I2C self-test (bit[4]) > SPI self-test
  -- (bit[3]) > PMOD. DS-P3.69 device_id contract: EDGE0(id=1)←SCL, EDGE1(id=2)←SDA.
  -- DS-T3.5: when bit[5]=1, EDGE0 ← UART0_TX so the edge datalogger streams the raw
  -- UART waveform (host-side sigrok UART decode + visual compare vs the PL decode).
  board_pin(C_BOARD_PIN_JB2) <= can_edge_sync_r(0) when can_edge_sel  = '1'  -- EDGE#1 ← CAN bus RX (DS-P3.107)
                                else uart_edge_sync_r when uart_edge_sel = '1'        --         or UART0 TX (dogfood)
                                else i2c_edge_sync_r(0) when i2c_edge_sel  = '1'      --         or I2C SCL
                                else spi_edge_sync_r(0) when emio_edge_sel = '1'      --         or SPI SCLK
                                else stim_edge_w when stim_edge_sel = '1'             --         or DS-P2.100 stim edge/pulse
                                else jb(2);
  board_pin(C_BOARD_PIN_JB3) <= can_edge_sync_r(1) when can_edge_sel  = '1'  -- EDGE#2 ← CTU-A TX (DS-P3.107)
                                else i2c_edge_sync_r(1) when i2c_edge_sel  = '1'      --         or I2C SDA
                                else spi_edge_sync_r(1) when emio_edge_sel = '1'      --         or SPI MOSI
                                else jb(3);
  board_pin(C_BOARD_PIN_JB4) <= spi_edge_sync_r(2) when emio_edge_sel = '1' else jb(4);  -- EDGE#3 ← MISO
  board_pin(C_BOARD_PIN_JB5) <= spi_edge_sync_r(3) when emio_edge_sel = '1' else jb(5);  -- EDGE#4 ← CS
  board_pin(C_BOARD_PIN_JB6) <= jb(6);
  board_pin(C_BOARD_PIN_JB7) <= jb(7);
  -- JTAG #16 (DS-P3.56 P3): fed from the DDR-commit REA's BSCANE2 tap, NOT a
  -- package pin — the datalogger watches the live USER1 scan under the hood. The
  -- tap is JTAG-TCK-domain; the datalogger's 2-FF input syncs handle the async
  -- crossing (slow TCK vs 100 MHz clk_sys, so oversampling is exact).
  board_pin(C_BOARD_PIN_REA_TCK) <= rea_tap_tck;
  board_pin(C_BOARD_PIN_REA_TMS) <= rea_tap_tms;
  board_pin(C_BOARD_PIN_REA_TDI) <= rea_tap_tdi;
  board_pin(C_BOARD_PIN_REA_TDO) <= rea_tap_tdo;
  -- DS-P3.102: CAN0 RX (datalogger id 20) ← the PS-EMIO 2-node fabric CAN bus.
  -- Two PS CAN controllers talk to each other over the fabric wired-AND; the
  -- passive datalogger decodes the live bus. Was idle '1' (recessive) — still idles
  -- recessive when the controllers are down, so no behavioural change to the PoC.
  board_pin(C_BOARD_PIN_CAN_BUS) <= can_bus_w;

  -- ── capture_pin assembly (DS-P1.66) ─────────────────────────────────────
  -- Each logical pin the config uses takes the board pin the generator
  -- allocated to it; an unused logical pin keeps its '0' default.
  gen_capture_pin : for i in capture_pin'range generate
    gen_wired : if C_CAPTURE_BOARD_PIN(i) /= C_BOARD_PIN_NONE generate
      capture_pin(i) <= board_pin(C_CAPTURE_BOARD_PIN(i));
    end generate gen_wired;
  end generate gen_capture_pin;

  ---------------------------------------------------------------------------
  -- Free-running timestamp
  ---------------------------------------------------------------------------
  p_timestamp : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        timestamp_r <= (others => '0');
      else
        timestamp_r <= timestamp_r + 1;
      end if;
    end if;
  end process;
  timestamp <= std_logic_vector(timestamp_r(C_TIMESTAMP_WIDTH-1 downto 0));

  -- DS-P3.109: assemble the 3-bit IRQ_F2P bus. LSB-first to match the GIC SPI
  -- mapping in the DT (IRQ_F2P[0]=SPI29, [1]=SPI30, [2]=SPI31).
  irq_f2p_w <= ctu_b_irq_w & ctu_a_irq_w & ring_alarm_irq(0);

  -- DS-P3.102: the CAN bus IS one wired-AND. Dominant '0' from either controller
  -- pulls the bus low; recessive '1' only when BOTH release. The datalogger is
  -- passive (reads the bus, never drives) so it is NOT a term in the AND.
  -- DS-P3.109: classic PS-CAN legs AND the 2× CTU CAN-FD legs share the wired-AND
  -- bus (dominant '0' wins; an idle/disabled node holds its TX recessive '1').
  can_bus_w <= can0_phy_tx_w and can1_phy_tx_w and ctu_a_tx_w and ctu_b_tx_w;

  -- ── DS-P3.109: 2× CTU CAN FD soft cores (FD traffic nodes on the fabric bus) ──
  -- Stage-2 (live): cores driven by Linux via M_AXI_GP1 → axi_apb_bridge → s_apb
  -- (validated reachable on silicon, DS-T1.6 — devmem reads DEVICE_ID 0xCAFD),
  -- with each core's irq routed to IRQ_F2P[1]/[2] so the mainline ctucanfd driver
  -- binds the ctu,ctucanfd DT nodes and brings up can2/can3. CAN_tx/rx sit on the
  -- shared fabric wired-AND bus alongside the classic PS-CAN legs.
  ctu_arstn_w <= not rst_sys;

  U_CtuCanFd_A : entity ctu_can_fd_rtl.can_top_apb
    generic map (txt_buffer_count => G_CTU_TXT_BUFFERS)  -- DS-P2.67
    port map (
      aclk          => clk_sys,
      arstn         => ctu_arstn_w,
      scan_enable   => '0',
      res_n_out     => open,
      irq           => ctu_a_irq_w,                       -- → IRQ_F2P[1] (GIC SPI #30, can2)
      CAN_tx        => ctu_a_tx_w,
      CAN_rx        => can_bus_w,
      timestamp     => std_logic_vector(timestamp_r),
      s_apb_paddr   => canfd0_apb_paddr_w,                -- ← M_AXI_GP1 / axi_apb_bridge_canfd0
      s_apb_penable => canfd0_apb_penable_w,
      s_apb_pprot   => "000",                             -- bridge is APB3 (no pprot)
      s_apb_prdata  => canfd0_apb_prdata_w,
      s_apb_pready  => canfd0_apb_pready_w(0),
      s_apb_psel    => canfd0_apb_psel_w(0),
      s_apb_pslverr => canfd0_apb_pslverr_w(0),
      s_apb_pstrb   => "1111",                            -- bridge is APB3 (no pstrb) → full-word
      s_apb_pwdata  => canfd0_apb_pwdata_w,
      s_apb_pwrite  => canfd0_apb_pwrite_w);

  U_CtuCanFd_B : entity ctu_can_fd_rtl.can_top_apb
    generic map (txt_buffer_count => G_CTU_TXT_BUFFERS)  -- DS-P2.67
    port map (
      aclk          => clk_sys,
      arstn         => ctu_arstn_w,
      scan_enable   => '0',
      res_n_out     => open,
      irq           => ctu_b_irq_w,                       -- → IRQ_F2P[2] (GIC SPI #31, can3)
      CAN_tx        => ctu_b_tx_w,
      CAN_rx        => can_bus_w,
      timestamp     => std_logic_vector(timestamp_r),
      s_apb_paddr   => canfd1_apb_paddr_w,                -- ← M_AXI_GP1 / axi_apb_bridge_canfd1
      s_apb_penable => canfd1_apb_penable_w,
      s_apb_pprot   => "000",                             -- bridge is APB3 (no pprot)
      s_apb_prdata  => canfd1_apb_prdata_w,
      s_apb_pready  => canfd1_apb_pready_w(0),
      s_apb_psel    => canfd1_apb_psel_w(0),
      s_apb_pslverr => canfd1_apb_pslverr_w(0),
      s_apb_pstrb   => "1111",                            -- bridge is APB3 (no pstrb) → full-word
      s_apb_pwdata  => canfd1_apb_pwdata_w,
      s_apb_pwrite  => canfd1_apb_pwrite_w);

  -- DS-T2.16: resolve EMIO GPIO bit 0 into the SPI chip-select the injector
  -- watches. The Linux gpio framework drives it as an output (GPIO_T(0)='0')
  -- around each spidev transfer; when released (T='1') the line idles high
  -- (deasserted, active-low CS). GPIO_I loops the driven value back so a PS
  -- read-back of the GPIO returns what it drove.
  spi0_cs_gpio_w <= gpio_o_w(0) when gpio_t_w(0) = '0' else '1';
  gpio_i_w(0)    <= spi0_cs_gpio_w;

  -- DS-T2.18 timing: register the GPIO CS for the datalogger path (see signal decl).
  p_spi_cs_gpio_reg : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        spi0_cs_gpio_r <= '1';            -- idle high = CS deasserted (active-low)
      else
        spi0_cs_gpio_r <= spi0_cs_gpio_w;
      end if;
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- Datalogger aggregator (PoC config: I2C×2, UART×2, SPI×1, Edge×4)
  ---------------------------------------------------------------------------
  u_datalogger_gather : entity tich.datalogger_gather
    generic map (
      G_SYS_CLK => G_SYS_CLK_HZ  -- DS-T1.10: parametrized (100M Zynq default / 50M DE25); datalogger_baud math depends on this
    )
    port map (
      CLK              => clk_sys,
      RST              => rst_sys,
      CAPTURE_PINS       => capture_pin,
      CONFIG           => config_word,
      TIMESTAMP        => timestamp,
      DOUT_PAYLOAD    => dout_payload,
      DOUT_DATALOGGER_ID => dout_datalogger_id,
      DOUT_MSG_TYPE   => dout_msg_type,
      DOUT_VALID      => dout_valid,
      DOUT_REQ        => dout_req,
      FIFO_CSR   => fifo_csr,
      PROTO_COUNTERS   => proto_counters,
      EDGE_ERR_CNTS    => edge_err_cnts_w,
      -- DS-P3.117: per-channel max-frequency taps → maxfreq_event_bank.
      EDGE_MAX_IVALS   => edge_max_ivals_w,
      EDGE_MAX_NEW     => edge_max_new_w
    );

  ---------------------------------------------------------------------------
  -- DDR streaming DMA — burst path. rr-ddr-stream's master record
  -- bundle wires straight to the BD's S_AXI_HP0 slave port (see u_bd
  -- port map above).
  ---------------------------------------------------------------------------
  ---------------------------------------------------------------------------
  -- zs_msg framer — wraps every 32-bit burst-path payload into a
  -- 6-word zs_msg frame (5 header + 1 payload). PS-side
  -- DdrRingDataSource walks the resulting ring with valid framing.
  --
  -- DS-P3.29 (2026-05-17): datalogger_id + msg_type are now driven from
  -- the gather chain. The gather extracts:
  --   datalogger_id from payload[31:27] (zero-extended to 8 bits)
  --   msg_type   = SEV_INFO | DOMAIN_<PROTO> | payload[26:24]
  -- per-protocol — see datalogger_gather.vhd auto-gen body. The mod-256
  -- counter placeholder is gone.
  --
  -- Remaining placeholders for now (own tickets):
  --   flags:     0 (no error condition propagation yet).
  --
  -- DS-P3.30 (2026-05-17) — timestamp_r is now 64-bit; the framer
  -- gets the full counter. Per-protocol datalogger slicing keeps the
  -- low 32 bits via `timestamp` (see signal declaration).
  ---------------------------------------------------------------------------
  timestamp64 <= std_logic_vector(timestamp_r);

  -- ── EDGE-domain classifier (DS-P2.47 Phase B2 step 3) ──────────────────
  -- msg_type[11:8] = DOMAIN_EDGE → route to aggregator; else → bypass.
  -- DS-T2.84: except the edge lifecycle frame (event_id EVT_EDGE_LIFECYCLE),
  -- which bypasses to the framer as its own 0x0608 zs_msg. Batched, it kept only
  -- S/F/E and reached EDGE_EVENTS as an S=1/E=0 "line is low" readback.
  -- DS-P2.16: a Stream-RLE word (EVT_EDGE_RLE) bypasses the same way, as 0x0609.
  gather_is_edge_w  <= is_edge_batch_word(dout_msg_type);
  gather_to_agg_v_w <= dout_valid and gather_is_edge_w;
  gather_to_fr_v_w  <= dout_valid and (not gather_is_edge_w);

  -- Zero-pad the gather's 32-bit payload up to the framer's wide port.
  nonedge_payload_w(31 downto 0) <= dout_payload;
  nonedge_payload_w(C_PAYLOAD_WORDS_MAX*32 - 1 downto 32) <= (others => '0');

  -- Aggregator instance. DS-P3.50: the aggregator now maps the SPARSE global
  -- edge ids (1,2,3,6) onto C_EDGE_DATALOGGERS DENSE batchers via G_CHANNEL_IDS —
  -- exactly 4 batchers, no dead ones. Previously G_NUM_CHANNELS=C_EDGE_AGG_CHANNELS
  -- = max(edge id)+1 = 7 demuxed EV_WORD[31:27] directly as the batcher index,
  -- so batchers 0/4/5 were instantiated but DEAD (~2.7k wasted LUTs, and the
  -- post-route WNS=-0.011ns path sat on dead batcher[4]/id 4). The dense map
  -- decouples the batcher count from the global-id span; each batcher still
  -- stamps its GLOBAL id, so FR_DATALOGGER_ID (1/2/3/6) is unchanged. Unmapped ids
  -- drop-and-consume (no deadlock). Reuses C_EDGE_IDS_FLAT_W (= C_EDGE_IDS).
  gen_edge_pipeline : if C_EDGE_DATALOGGERS > 0 generate
    u_edge_agg : entity tich.edge_batcher_aggregator
      generic map (
        G_NUM_CHANNELS   => C_EDGE_DATALOGGERS,
        G_N_MAX          => C_PAYLOAD_WORDS_MAX,
        G_T_FLUSH_CYCLES => 10_000,
        G_USE_EVENT_TIMESTAMP => true,
        G_CHANNEL_IDS    => C_EDGE_IDS_FLAT_W
      )
      port map (
        CLK              => clk_sys,
        RST              => rst_sys,
        EV_VALID         => gather_to_agg_v_w,
        EV_READY         => agg_ev_ready_w,
        EV_WORD          => dout_payload,
        TIMESTAMP_64     => timestamp64,
        FR_VALID         => agg_fr_valid_w,
        FR_READY         => agg_fr_ready_w,
        FR_DATALOGGER_ID => agg_fr_datalogger_id_w,
        FR_MSG_CLASS     => agg_fr_msg_class_w,
        FR_MSG_TYPE      => agg_fr_msg_type_w,
        FR_FLAGS         => agg_fr_flags_w,
        FR_TIMESTAMP     => agg_fr_timestamp_w,
        FR_PAYLOAD       => agg_fr_payload_w,
        FR_PAYLOAD_WORDS => agg_fr_words_w
      );

    u_error_bank : entity tich.error_event_bank
      generic map (
        G_NUM_CH       => C_EDGE_DATALOGGERS,
        G_DOMAIN       => C_ZS_DOMAIN_EDGE,
        G_EVENT_ID     => C_ZS_ERR_EVT_INTERNAL_FIFO_OVERFLOW,
        G_DATALOGGER_IDS => C_EDGE_IDS_FLAT_W,
        G_FLUSH_CYCLES => 100_000          -- ~1 ms @ 100 MHz: rate-limit reports
      )
      port map (
        CLK           => clk_sys,
        RST           => rst_sys,
        ERR_CNTS      => edge_err_cnts_w,
        TIMESTAMP     => timestamp64,
        FR_VALID      => err_fr_valid_w,
        FR_READY      => err_fr_ready_w,
        FR_DATALOGGER_ID => err_fr_datalogger_id_w,
        FR_MSG_CLASS  => err_fr_msg_class_w,
        FR_MSG_TYPE   => err_fr_msg_type_w,
        FR_FLAGS      => err_fr_flags_w,
        FR_TIMESTAMP  => err_fr_timestamp_w,
        FR_PAYLOAD    => err_fr_payload32_w,
        FR_WORDS      => err_fr_words_w
      );

    u_maxfreq_bank : entity tich.maxfreq_event_bank
      generic map (
        G_NUM_CH      => C_EDGE_DATALOGGERS,
        G_DOMAIN      => C_ZS_DOMAIN_EDGE,
        G_EVENT_ID    => C_ZS_EVT_EDGE_MAXFREQ,
        G_DATALOGGER_IDS => C_EDGE_IDS_FLAT_W,
        G_IVAL_WIDTH  => C_EDGE_MAXFREQ_WIDTH,
        G_REARM_BIT   => 2                       -- CONFIG[2] = host re-ping
      )
      port map (
        CLK           => clk_sys,
        RST           => rst_sys,
        MAX_IVALS     => edge_max_ivals_w,
        MAX_NEW       => edge_max_new_w,
        CONFIG        => config_word,
        TIMESTAMP     => timestamp64,
        FR_VALID      => mf_fr_valid_w,
        FR_READY      => mf_fr_ready_w,
        FR_DATALOGGER_ID => mf_fr_datalogger_id_w,
        FR_MSG_CLASS  => mf_fr_msg_class_w,
        FR_MSG_TYPE   => mf_fr_msg_type_w,
        FR_FLAGS      => mf_fr_flags_w,
        FR_TIMESTAMP  => mf_fr_timestamp_w,
        FR_PAYLOAD    => mf_fr_payload64_w,
        FR_WORDS      => mf_fr_words_w
      );
  else generate
    agg_ev_ready_w         <= '1';
    agg_fr_valid_w         <= '0';
    agg_fr_datalogger_id_w <= (others => '0');
    agg_fr_msg_class_w     <= (others => '0');
    agg_fr_msg_type_w      <= (others => '0');
    agg_fr_flags_w         <= (others => '0');
    agg_fr_timestamp_w     <= (others => '0');
    agg_fr_payload_w       <= (others => '0');
    agg_fr_words_w         <= (others => '0');

    err_fr_valid_w         <= '0';
    err_fr_datalogger_id_w <= (others => '0');
    err_fr_msg_class_w     <= (others => '0');
    err_fr_msg_type_w      <= (others => '0');
    err_fr_flags_w         <= (others => '0');
    err_fr_timestamp_w     <= (others => '0');
    err_fr_payload32_w     <= (others => '0');
    err_fr_words_w         <= (others => '0');

    mf_fr_valid_w          <= '0';
    mf_fr_datalogger_id_w  <= (others => '0');
    mf_fr_msg_class_w      <= (others => '0');
    mf_fr_msg_type_w       <= (others => '0');
    mf_fr_flags_w          <= (others => '0');
    mf_fr_timestamp_w      <= (others => '0');
    mf_fr_payload64_w      <= (others => '0');
    mf_fr_words_w          <= (others => '0');
  end generate gen_edge_pipeline;

  -- 4:1 arb at framer input. Priority: ERROR > aggregator > MAX-FREQ > non-EDGE.
  -- Error frames are rare (coalesced, ~1/ms max) + important, so they win — and
  -- the loss they report is UPSTREAM of the ring, so the report still traverses
  -- it. The MAX-FREQ frame sits ABOVE the non-EDGE bypass but BELOW the EDGE
  -- batch/error paths: it is a low-rate running-minimum update, never loss- or
  -- ordering-critical, so it must not displace a batch or an error frame; it only
  -- borrows otherwise-idle framer cycles. A pending MF frame holds its snapshot
  -- (the emitter refreshes to the latest min while pending) until it wins.
  framer_in_valid     <= err_fr_valid_w or agg_fr_valid_w or mf_fr_valid_w or gather_to_fr_v_w;
  framer_datalogger_w    <= err_fr_datalogger_id_w when err_fr_valid_w = '1'
                                      else agg_fr_datalogger_id_w when agg_fr_valid_w = '1'
                                      else mf_fr_datalogger_id_w  when mf_fr_valid_w = '1'
                                      else dout_datalogger_id;
  framer_msg_class_w  <= err_fr_msg_class_w  when err_fr_valid_w = '1'
                                      else agg_fr_msg_class_w  when agg_fr_valid_w = '1'
                                      else mf_fr_msg_class_w   when mf_fr_valid_w = '1'
                                      else C_ZS_CLASS_DATA;
  framer_msg_type_w   <= err_fr_msg_type_w   when err_fr_valid_w = '1'
                                      else agg_fr_msg_type_w   when agg_fr_valid_w = '1'
                                      else mf_fr_msg_type_w    when mf_fr_valid_w = '1'
                                      else dout_msg_type;
  framer_flags_w      <= err_fr_flags_w      when err_fr_valid_w = '1'
                                      else agg_fr_flags_w      when agg_fr_valid_w = '1'
                                      else mf_fr_flags_w       when mf_fr_valid_w = '1'
                                      else (others => '0');
  framer_timestamp_w  <= err_fr_timestamp_w  when err_fr_valid_w = '1'
                                      else agg_fr_timestamp_w  when agg_fr_valid_w = '1'
                                      else mf_fr_timestamp_w   when mf_fr_valid_w = '1'
                                      else timestamp64;
  framer_payload_w    <= std_logic_vector(resize(unsigned(err_fr_payload32_w),
                                                 framer_payload_w'length))
                                            when err_fr_valid_w = '1'
                                      else agg_fr_payload_w    when agg_fr_valid_w = '1'
                                      else std_logic_vector(resize(unsigned(mf_fr_payload64_w),
                                                 framer_payload_w'length))
                                            when mf_fr_valid_w = '1'
                                      else nonedge_payload_w;
  framer_words_live_w <= err_fr_words_w      when err_fr_valid_w = '1'
                                      else agg_fr_words_w      when agg_fr_valid_w = '1'
                                      else mf_fr_words_w       when mf_fr_valid_w = '1'
                                      else std_logic_vector(to_unsigned(1, 8));

  -- Route framer's IN_READY back to the winning input (priority order).
  err_fr_ready_w  <= framer_in_ready when err_fr_valid_w = '1' else '0';
  agg_fr_ready_w  <= framer_in_ready when (err_fr_valid_w = '0' and agg_fr_valid_w = '1') else '0';
  mf_fr_ready_w   <= framer_in_ready when (err_fr_valid_w = '0' and agg_fr_valid_w = '0'
                                           and mf_fr_valid_w = '1') else '0';
  nonedge_ready_w <= framer_in_ready when (err_fr_valid_w = '0' and agg_fr_valid_w = '0'
                                           and mf_fr_valid_w = '0') else '0';

  -- DS-T2.26 probe: count cycles the emitter holds a pending loss frame
  -- (err_fr_valid) and how many it hands off to the framer (valid & ready).
  -- Saturating. EMIT_VALID_CNT>0 & EMIT_DONE_CNT=0 ⇒ framer never accepted (mux/
  -- idle starvation); both >0 ⇒ loss is DOWNSTREAM in the uploader; both 0 ⇒
  -- emitter never latched a report on silicon.
  p_emit_probe : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        emit_valid_cnt_r <= (others => '0');
        emit_done_cnt_r  <= (others => '0');
      else
        if err_fr_valid_w = '1' then
          emit_valid_cnt_r <= emit_valid_cnt_r + 1;   -- free-running (wraps at 2^32)
        end if;
        if err_fr_valid_w = '1' and err_fr_ready_w = '1' then
          emit_done_cnt_r <= emit_done_cnt_r + 1;
        end if;
      end if;
    end if;
  end process;

  u_zs_framer : entity tich.zs_msg_framer
    generic map (
      G_PAYLOAD_WORDS => C_PAYLOAD_WORDS_MAX
    )
    port map (
      CLK                 => clk_sys,
      RST                 => rst_sys,
      IN_VALID            => framer_in_valid,
      IN_READY            => framer_in_ready,
      DATALOGGER_ID          => framer_datalogger_w,
      MSG_CLASS           => framer_msg_class_w,
      MSG_TYPE            => framer_msg_type_w,
      FLAGS               => framer_flags_w,
      TIMESTAMP           => framer_timestamp_w,
      PAYLOAD             => framer_payload_w,
      PAYLOAD_WORDS_LIVE  => framer_words_live_w,
      OUT_DATA            => framer_out_data,
      OUT_VALID           => framer_out_valid,
      OUT_READY           => ddr_s_axis_ready  -- DDR-P3.10: real back-pressure
                                                -- from rr-ddr-stream's input skid.
    );

  -- DS-P1.9 (2026-05-18): write-only configuration. PS drains DDR via its
  -- own master (Zynq PS DDR controller is directly addressable), so the
  -- read plane is elided via enable_read_plane_g=false (DDR-P1.1 generic).
  -- The else-generate inside ddr_stream_axi ties m_axis_*, AR/R record
  -- fields, and downBusBusy* to safe defaults — no need to tie m_axis_*
  -- here. m_axi4_c2h_bus_i stays wired because the write plane consumes
  -- the B-channel.
  -- PF-H0 (DS-P2.124) D3: default-off backend select. The "axi" arm below is
  -- the Zynq/DE25 body, elaborated bit-identically whenever G_DDR_BACKEND="axi"
  -- (its default). The "fic" arm (further down) is the PolarFire SoC MSS-FIC
  -- sibling and is only reachable from deepskopion_pfsoc_top. Both wrap the same
  -- ddr_stream_axi core, so the core-side contract is identical; only the
  -- vendor-facing AXI defaults differ (fic clamps bursts to 64 beats).
  gen_ddr_axi : if G_DDR_BACKEND = "axi" generate
  u_ddr_burst : entity rr.ddr_stream_axi
    generic map (
      axi_id_g            => 0,
      fifo_depth_g        => 9,
      ddr_data_width_g    => G_DDR_DATA_WIDTH,
      ddr_addr_width_g    => 32,
      -- DS-T3.32: preserve all 32 address bits. Converting x"A0000000" to
      -- INTEGER overflows during elaboration on 32-bit-signed VHDL tools.
      ddr_base_addr_g     => std_logic_vector(G_DDR_BASE_ADDR),
      buffer_width_g      => G_DDR_WIN_WIDTH,
      -- DS-T2.60: cap the write-burst to 16 beats (AWLEN<=15). The target is
      -- Zynq-7 S_AXI_HP, which is AXI3 (AWLEN is 4-bit, 16-beat max). Left at
      -- the AXI4 default (256) the burstw_setter clamps to 255, so under
      -- sustained AW->W back-pressure (DRAM refresh / HP interconnect arb) the
      -- FIFO fills and AWLEN climbs past 15 -> SmartConnect must split-or-drop
      -- the oversized burst -> silent data loss. 16 is the AXI3 ceiling.
      max_burst_len_g     => G_AXI_MAX_BURST,  -- DS-T2.60/T2.65 (single-sourced, axi_pkg)
      enable_read_plane_g => false
    )
    port map (
      clk_sys_i        => clk_sys,
      clk_ddr_i        => clk_ddr,
      rst_sys_i        => rst_sys,
      rst_ddr_i        => rst_ddr,
      -- DDR-P3.10: AXI-Stream handshake. Producer side wired from the
      -- zs_msg_framer (now back-pressurable via OUT_READY).
      s_axis_data_i    => framer_out_data,
      s_axis_valid_i   => framer_out_valid,
      s_axis_ready_o   => ddr_s_axis_ready,
      -- m_axis_data_o / m_axis_valid_o are driven low by the read-plane
      -- else-generate and left unassociated (mode 'out'). m_axis_ready_i
      -- is mode 'in' without a default, so VHDL still requires an
      -- association; tie low (its consumer in the else-generate is dead).
      m_axis_ready_i   => '0',
      -- DS-P2.59: software-managed ring DRAIN pointer. With the read plane
      -- elided there is no HW downloader, so the PS drives the ring's read
      -- side: it writes the byte offset it has consumed (RING_READ_PTR, 0x154).
      -- The slot's .data holds the value (clk_sys), and .valid is the regbank's
      -- per-slot ONE-CYCLE write strobe — exactly the pulse the IP's mux_cdc_er
      -- MCP CDC needs to capture the multi-bit pointer coherently (a held level
      -- would defeat the toggle synchronizer). Defaults to 0 / no-pulse until
      -- the PS starts advancing it → legacy fill-once.
      csr_rd_ptr_addr_i => regs_out_local(C_INDEX_RING_READ_PTR / 4).data,
      csr_rd_ptr_load_i => regs_out_local(C_INDEX_RING_READ_PTR / 4).valid,
      -- DS-T2.20: DMA commit-progress taps (PS-LOOP-REQ-9).
      write_idle_o     => ddr_write_idle,
      wr_commit_addr_o => ddr_wr_commit_addr,
      -- DS-T1.13: bring the write-plane arming taps out of the IP. ddr_csr_o is
      -- registered in clk_sys (safe for the clk_sys REA + regbank); the CDC
      -- empty/full flags here are what the uploader uses to decide there is data
      -- to write. dbg_reset_status_o is a raw clk_ddr tap — synced below.
      ddr_csr_o          => ddr_csr_w,
      dbg_reset_status_o => ddr_dbg_reset_status,
      m_axi4_h2c_bus_o => axi_h2c,
      m_axi4_c2h_bus_i => axi_c2h
    );
  end generate gen_ddr_axi;

  -- PF-H0 (DS-P2.124) D3 — PolarFire SoC MSS-FIC arm. ddr_stream_fic
  -- (DDR-P2.15) presents the IDENTICAL core-side contract as ddr_stream_axi
  -- (write-plane arming, commit/wrap at 2**buffer_width_g, ddr_csr status,
  -- reset-status, the AXI4 h2c/c2h master records) because it wraps the same
  -- ddr_stream_axi core; it differs only in its PolarFire FIC defaults (64-bit
  -- native, 64-beat cap with an elaboration clamp assertion). The port/generic
  -- associations therefore mirror the "axi" arm exactly — deepskopion_pfsoc_top
  -- drives G_DDR_DATA_WIDTH=>64 / G_AXI_MAX_BURST=>64 so the clamp never fires.
  -- Same write-only shape as Zynq/DE25 (enable_read_plane_g=false); the CPU
  -- drains the ring through the MSS DDR controller.
  gen_ddr_fic : if G_DDR_BACKEND = "fic" generate
  u_ddr_burst_fic : entity rr.ddr_stream_fic
    generic map (
      axi_id_g            => 0,
      fifo_depth_g        => 9,
      ddr_data_width_g    => G_DDR_DATA_WIDTH,
      ddr_addr_width_g    => 32,
      ddr_base_addr_g     => std_logic_vector(G_DDR_BASE_ADDR),
      buffer_width_g      => G_DDR_WIN_WIDTH,
      max_burst_len_g     => G_AXI_MAX_BURST,
      enable_read_plane_g => false
    )
    port map (
      clk_sys_i        => clk_sys,
      clk_ddr_i        => clk_ddr,
      rst_sys_i        => rst_sys,
      rst_ddr_i        => rst_ddr,
      s_axis_data_i    => framer_out_data,
      s_axis_valid_i   => framer_out_valid,
      s_axis_ready_o   => ddr_s_axis_ready,
      m_axis_ready_i   => '0',
      csr_rd_ptr_addr_i => regs_out_local(C_INDEX_RING_READ_PTR / 4).data,
      csr_rd_ptr_load_i => regs_out_local(C_INDEX_RING_READ_PTR / 4).valid,
      write_idle_o     => ddr_write_idle,
      wr_commit_addr_o => ddr_wr_commit_addr,
      ddr_csr_o          => ddr_csr_w,
      dbg_reset_status_o => ddr_dbg_reset_status,
      m_axi4_h2c_bus_o => axi_h2c,
      m_axi4_c2h_bus_i => axi_c2h
    );
  end generate gen_ddr_fic;

  -- Gather back-pressure: DOUT_REQ is the gather's downstream READY.
  -- It depends on which path the current event takes:
  --   * EDGE-domain event  → must be accepted by the aggregator
  --   * non-EDGE event     → must win the 2:1 arb at the framer input
  -- Combinational mux — no loop because gather_is_edge_w depends only on
  -- gather outputs (msg_type), not on dout_req.
  dout_req <= agg_ev_ready_w  when gather_is_edge_w = '1'
               else nonedge_ready_w;

  ---------------------------------------------------------------------------
  -- AXI-Lite regbank — slices BD GP0's 32-bit address into the
  -- regbank's 11-bit window. PS firmware reads/writes at 0x4000_0000.
  ---------------------------------------------------------------------------
  u_regbank : entity tich.system_regbank_wrapper
    port map (
      CLK            => clk_sys,
      RST            => rst_sys,
      S_AXI_AWADDR   => gp0_awaddr(10 downto 0),
      S_AXI_AWPROT   => gp0_awprot,
      S_AXI_AWVALID  => gp0_awvalid,
      S_AXI_AWREADY  => gp0_awready,
      S_AXI_WDATA    => gp0_wdata,
      S_AXI_WSTRB    => gp0_wstrb,
      S_AXI_WVALID   => gp0_wvalid,
      S_AXI_WREADY   => gp0_wready,
      S_AXI_BRESP    => gp0_bresp,
      S_AXI_BVALID   => gp0_bvalid,
      S_AXI_BREADY   => gp0_bready,
      S_AXI_ARADDR   => gp0_araddr(10 downto 0),
      S_AXI_ARPROT   => gp0_arprot,
      S_AXI_ARVALID  => gp0_arvalid,
      S_AXI_ARREADY  => gp0_arready,
      S_AXI_RDATA    => gp0_rdata,
      S_AXI_RRESP    => gp0_rresp,
      S_AXI_RVALID   => gp0_rvalid,
      S_AXI_RREADY   => gp0_rready,
      DATALOGGER_CONFIG => config_word,
      TELEMETRY_IN   => telemetry_in,
      REGS_OUT       => regs_out_local  -- DS-P2.51: tap EMIO_MUX_SEL + INJ_* slots
    );

  ---------------------------------------------------------------------------
  -- DS-P2.51 Phase 2 — I2C / SPI injector responders.
  --
  -- Each injector reads its CONTROL + DATAIN slots from regs_out_local
  -- and drives its STATUS back into telemetry_in for the regbank to
  -- expose as RO. DATAIN_VLD = the regbank's per-slot one-cycle valid
  -- pulse — pushes one byte into the TX FIFO per PS write.
  ---------------------------------------------------------------------------
  i2c_inj_control_w  <= regs_out_local(C_INDEX_I2C_INJ_CONTROL / 4).data;
  i2c_inj_datain_w   <= regs_out_local(C_INDEX_I2C_INJ_DATAIN  / 4).data;
  i2c_inj_datain_vld <= regs_out_local(C_INDEX_I2C_INJ_DATAIN  / 4).valid;

  spi_inj_control_w  <= regs_out_local(C_INDEX_SPI_INJ_CONTROL / 4).data;
  spi_inj_datain_w   <= regs_out_local(C_INDEX_SPI_INJ_DATAIN  / 4).data;
  spi_inj_datain_vld <= regs_out_local(C_INDEX_SPI_INJ_DATAIN  / 4).valid;

  u_i2c_injector : entity tich.i2c_injector
    generic map (
      G_CLK_FREQ_HZ => G_SYS_CLK_HZ   -- DS-T1.10: parametrized (100M Zynq default / 50M DE25); scales the SCL-stuck-low timeout
    )
    port map (
      CLK        => clk_sys,
      RST        => rst_sys,
      TIMESTAMP  => timestamp,
      ENABLE     => '1',
      CONTROL    => i2c_inj_control_w,
      DATAIN     => i2c_inj_datain_w,
      DATAIN_VLD => i2c_inj_datain_vld,
      STATUS     => i2c_inj_status_w,
      SCL        => i2c0_bus_scl_w,
      SDA_I      => i2c0_bus_sda_w,
      SDA_O      => i2c_inj_sda_o_w,
      SDA_T      => i2c_inj_sda_t_w,
      DBG        => i2c_slave_dbg_w,    -- DS-T2.34 REA probe
      DBG_FIFO   => i2c_dbg_fifo_w,     -- DS-T2.34 FIFO internals
      DBG_SHIFT  => i2c_dbg_shift_w,    -- DS-T2.34 slave latched byte
      DBG2       => i2c_dbg2_w          -- DS-T2.34 bail-decision conds
    );

  u_spi_injector : entity tich.spi_injector
    port map (
      CLK        => clk_sys,
      RST        => rst_sys,
      ENABLE     => '1',
      CONTROL    => spi_inj_control_w,
      DATAIN     => spi_inj_datain_w,
      DATAIN_VLD => spi_inj_datain_vld,
      STATUS     => spi_inj_status_w,
      SCLK       => spi0_bus_sclk_w,
      MOSI       => spi0_bus_mosi_w,
      -- DS-T2.16: CS now from the EMIO-GPIO line (cs-gpios), NOT the dead
      -- native SPI0_SS_O (spi0_bus_ss_w never asserts at the seam).
      CS_N       => spi0_cs_gpio_w,
      MISO_O     => spi_inj_miso_o_w,
      MISO_T     => spi_inj_miso_t_w
    );

  ---------------------------------------------------------------------------
  -- DS-P2.100 / DS-D.38 option a — fabric-side stimulus generator.
  --
  -- Replaces the DS-P2.95 tied-off DE25 EMIO seam: the stim_*_o ports feed
  -- back into this entity's own emio_*_i legs at the DE25 top level (a
  -- loopback the top wires, not this file — deepskopion_core stays
  -- platform-portable per its DE-H0 header). CONTROL/DATA come straight
  -- from regbank, same idiom as the injectors above.
  ---------------------------------------------------------------------------
  stim_uart_control_w  <= regs_out_local(C_INDEX_STIM_UART_CONTROL / 4).data;
  stim_uart_baud_w     <= regs_out_local(C_INDEX_STIM_UART_BAUD_DIV / 4).data;
  stim_uart_data_w     <= regs_out_local(C_INDEX_STIM_UART_DATA / 4).data;
  stim_uart_data_vld_w <= regs_out_local(C_INDEX_STIM_UART_DATA / 4).valid;

  stim_i2c_control_w  <= regs_out_local(C_INDEX_STIM_I2C_CONTROL / 4).data;
  stim_i2c_clk_div_w  <= regs_out_local(C_INDEX_STIM_I2C_CLK_DIV / 4).data;
  stim_i2c_data_w     <= regs_out_local(C_INDEX_STIM_I2C_DATA / 4).data;
  stim_i2c_data_vld_w <= regs_out_local(C_INDEX_STIM_I2C_DATA / 4).valid;

  stim_spi_control_w  <= regs_out_local(C_INDEX_STIM_SPI_CONTROL / 4).data;
  stim_spi_clk_div_w  <= regs_out_local(C_INDEX_STIM_SPI_CLK_DIV / 4).data;
  stim_spi_data_w     <= regs_out_local(C_INDEX_STIM_SPI_DATA / 4).data;
  stim_spi_data_vld_w <= regs_out_local(C_INDEX_STIM_SPI_DATA / 4).valid;

  stim_edge_control_w <= regs_out_local(C_INDEX_STIM_EDGE_CONTROL / 4).data;

  u_stim_gen : entity tich.ds_stimulus_gen
    generic map (G_CLK_HZ => G_SYS_CLK_HZ)
    port map (
      CLK           => clk_sys,
      RST           => rst_sys,
      UART_CONTROL  => stim_uart_control_w,
      UART_BAUD_DIV => stim_uart_baud_w,
      UART_DATA     => stim_uart_data_w,
      UART_DATA_VLD => stim_uart_data_vld_w,
      UART_TX_O     => stim_uart_tx_w,
      I2C_CONTROL   => stim_i2c_control_w,
      I2C_CLK_DIV   => stim_i2c_clk_div_w,
      I2C_DATA      => stim_i2c_data_w,
      I2C_DATA_VLD  => stim_i2c_data_vld_w,
      I2C_SCL_O     => stim_i2c_scl_o_w,
      I2C_SCL_T     => stim_i2c_scl_t_w,
      I2C_SDA_O     => stim_i2c_sda_o_w,
      I2C_SDA_T     => stim_i2c_sda_t_w,
      SPI_CONTROL   => stim_spi_control_w,
      SPI_CLK_DIV   => stim_spi_clk_div_w,
      SPI_DATA      => stim_spi_data_w,
      SPI_DATA_VLD  => stim_spi_data_vld_w,
      SPI_SCLK_O    => stim_spi_sclk_w,
      SPI_MOSI_O    => stim_spi_mosi_w,
      SPI_SS_O      => stim_spi_ss_w,
      EDGE_CONTROL  => stim_edge_control_w,
      EDGE_O        => stim_edge_w
    );
  stim_uart_tx_o    <= stim_uart_tx_w;
  stim_i2c_scl_o_o  <= stim_i2c_scl_o_w;  stim_i2c_scl_t_o <= stim_i2c_scl_t_w;
  stim_i2c_sda_o_o  <= stim_i2c_sda_o_w;  stim_i2c_sda_t_o <= stim_i2c_sda_t_w;
  stim_spi_sclk_o_o <= stim_spi_sclk_w;
  stim_spi_mosi_o_o <= stim_spi_mosi_w;
  stim_spi_ss_o_o   <= stim_spi_ss_w;

  ---------------------------------------------------------------------------
  -- Telemetry accumulators (heartbeat + FIFO overflow stickies).
  ---------------------------------------------------------------------------
  p_heartbeat_cnt : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        heartbeat_cnt <= (others => '0');
      else
        heartbeat_cnt <= heartbeat_cnt + 1;
      end if;
    end if;
  end process;

  p_burst_ovf_cnt : process(clk_sys)
    variable v_prev : std_logic := '0';
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        fifo_overflow_cnt <= (others => '0');
        v_prev := '0';
      else
        if fifo_csr.fifoStatus.overflow = '1' and v_prev = '0' then
          fifo_overflow_cnt <= fifo_overflow_cnt + 1;
        end if;
        v_prev := fifo_csr.fifoStatus.overflow;
      end if;
    end if;
  end process;

  -- Ring write counter — increments on every 32-bit word the framer
  -- pushes into rr-ddr-stream. With G_PAYLOAD_WORDS=1 this is +6 per
  -- source message (5 header + 1 payload). DS-P3.27/3.28.
  p_ring_write_cnt : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        ring_write_cnt_r <= (others => '0');
      elsif framer_out_valid = '1' then
        ring_write_cnt_r <= ring_write_cnt_r + 1;
      end if;
    end if;
  end process;

  -- DS-T2.22 mechanism B: DDR backpressure-active counter. Counts clk_sys
  -- cycles where the framer has a frame to write but the DMA can't accept it
  -- (framer_out_valid && !ddr_s_axis_ready) — i.e. the ring is FULL and
  -- incoming real-time datalogger traffic is being silently lost UPSTREAM of the
  -- per-protocol DROP_CNT (which can't see it: the datalogger can't even form a
  -- frame). This is the out-of-band 'consumer fell behind, losing data NOW'
  -- signal. Stays 0 while a consumer keeps the ring draining (commit wraps).
  p_ddr_backpressure_cnt : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        ddr_backpressure_cnt_r <= (others => '0');
      elsif framer_out_valid = '1' and ddr_s_axis_ready = '0' then
        ddr_backpressure_cnt_r <= ddr_backpressure_cnt_r + 1;
      end if;
    end if;
  end process;

  -- DS-P3.91: burst arbiter grant counter. Increments once per granted message
  -- handoff out of the gather's burst arbiter (valid & ready). Drives the
  -- ARB_GRANT_CNT regbank slot (was undriven -> always 0).
  p_arb_grant_cnt : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        arb_grant_cnt_r <= (others => '0');
      elsif dout_valid = '1' and dout_req = '1' then
        arb_grant_cnt_r <= arb_grant_cnt_r + 1;
      end if;
    end if;
  end process;

  -- DS-P3.60: ring-fill watermark + hysteresis alarm (clk_sys). Drives
  -- IRQ_F2P[0] at 75% so the PS drains BEFORE overflow; the Schmitt trigger
  -- (set@75% / clear@50%) gives one interrupt episode per fill excursion, not
  -- a 75%↔drain thrash loop. alarm_cnt is the silicon-observable proof of the
  -- FSM via the RING_ALARM_CNT regbank slot, independent of the Linux IRQ path.
  -- Offsets are the low G_DDR_WIN_WIDTH bits of the absolute commit address and
  -- the SW read pointer (base is 2^28-aligned → low bits are ring-relative).
  u_ring_watermark : entity tich.ring_watermark
    generic map (
      G_ADDR_WIDTH => G_DDR_WIN_WIDTH,
      G_HIGH_NUM   => 3,   -- HIGH = 75%
      G_LOW_NUM    => 2,   -- LOW  = 50%
      G_DEN        => 4
    )
    port map (
      clk        => clk_sys,
      rst        => rst_sys,
      commit_off => ddr_commit_addr_sys(G_DDR_WIN_WIDTH - 1 downto 0),
      read_off   => regs_out_local(C_INDEX_RING_READ_PTR / 4).data(G_DDR_WIN_WIDTH - 1 downto 0),
      fill       => ring_fill_w,
      alarm      => ring_alarm_w,
      alarm_cnt  => ring_alarm_cnt_w
    );
  ring_alarm_irq(0) <= ring_alarm_w;

  -- Stream-tier overflow counter dropped post-D.4 (single-tier arbiter;
  -- the regbank slot C_INDEX_FIFO_OVERFLOW_CNT_RSVD now reads zero
  -- through the default-init telemetry_in slot until the regbank.yml
  -- catches up with the strip).

  telemetry_in(C_INDEX_HEARTBEAT_CNT            / 4).data  <= std_logic_vector(heartbeat_cnt);
  telemetry_in(C_INDEX_HEARTBEAT_CNT            / 4).valid <= '1';
  telemetry_in(C_INDEX_FIFO_OVERFLOW_CNT  / 4).data  <= std_logic_vector(fifo_overflow_cnt);
  telemetry_in(C_INDEX_FIFO_OVERFLOW_CNT  / 4).valid <= '1';

  -- DS-P3.91: arbiter grant counters (were undriven -> always 0). BURST counts
  -- gather burst-arbiter grants. STREAM is driven to 0 BY DESIGN — the stream
  -- tier was dropped post-D.4 (single-tier arbiter; no stream arbiter exists to
  -- grant), same rationale as C_INDEX_FIFO_OVERFLOW_CNT_RSVD. Driving it (not
  -- leaving it ROM-default) makes the 0 explicit + intentional, not forgotten.
  telemetry_in(C_INDEX_ARB_GRANT_CNT      / 4).data  <= std_logic_vector(arb_grant_cnt_r);
  telemetry_in(C_INDEX_ARB_GRANT_CNT      / 4).valid <= '1';
  telemetry_in(C_INDEX_ARB_GRANT_CNT_RSVD     / 4).data  <= (others => '0');
  telemetry_in(C_INDEX_ARB_GRANT_CNT_RSVD     / 4).valid <= '1';

  -- RTL<->PS-software contract ABI version (RTL-P2.654). Mirror of
  -- sw.stream.testbench.provenance.CONTRACT_ABI_VER (0x0002_0000 = v0.2.0).
  -- run_self_test reads this regbank slot and ERRORs (exit 2, harness-broken)
  -- if it disagrees with the testbench constant — so a bitstream/testbench
  -- version skew can never again be mis-read as a DUT FAIL. BUMP THIS AND
  -- provenance.CONTRACT_ABI_VER TOGETHER on any breaking change to the EMIO
  -- injector load/edge-detect contract.
  -- v0.2.0 (DS-T9, 2026-05-31): EMIO-seam REA probe widened 31->32 bits (added
  -- bit 31 = SPI SS read-in; capped at 32 by the rr_rea trigger width, RTL-P2.658).
  -- Regbank EMIO_PROBE slot is still wire-compatible (low 31 bits unchanged), but
  -- emio_seam.yml must be in lockstep with this build to decode the wider capture
  -- — hence a minor bump.
  telemetry_in(C_INDEX_CONTRACT_ABI_VER         / 4).data  <= x"00020000";
  telemetry_in(C_INDEX_CONTRACT_ABI_VER         / 4).valid <= '1';

  -- ── DS-T2.18/DS-T2.20 DDR-commit observers (RO) ──────────────────────────
  -- CLK_DDR_TICKS: clk_ddr liveness edge-count (read twice; nonzero delta =>
  -- clk_ddr is running). DDR_RESET_STATUS: bit0 = rst_ddr live (1=asserted),
  -- bit1 = rst_ddr ever-deasserted (sticky), bit2 = ddr_write_idle (DMA caught
  -- up — all enqueued frames committed). RING_COMMIT_PTR: the DMA write/commit
  -- DDR address (PS-LOOP-REQ-9 — walk the ring up to THIS, not RING_WRITE_PTR).
  telemetry_in(C_INDEX_CLK_DDR_TICKS    / 4).data  <= std_logic_vector(clk_ddr_live_cnt);
  telemetry_in(C_INDEX_CLK_DDR_TICKS    / 4).valid <= '1';
  -- DS-T1.13: bits[2:0] preserved (write_idle / ever-deassert / live rst_ddr).
  -- The reserved upper bits now carry the write-plane ARMING word so the
  -- 0.3.2-vs-0.3.1 A/B is a single devmem read of 0x50 (the register that proved
  -- the wedge). Decode: [6:3]=dbg_reset_status
  -- {downloader_cs,uploader_cs,pipeBusyDdr,pipeBusy}; [7]=chip_empty (CDC empty,
  -- uploader-sees-no-data — KEY); [8]=empty; [9]=full; [10]=write_error;
  -- [16:11]=seen {aw_valid,aw_hs,wvalid,bvalid,wr_active,wr_err}; [31:17]=0.
  telemetry_in(C_INDEX_DDR_RESET_STATUS / 4).data  <=
      (31 downto 17 => '0')
    & wp_seen_sync                        -- 16..11 wr_err,wr_active,bvalid,wvalid,aw_hs,aw_valid (MSB..LSB)
    & ddr_csr_w.fifoStatus.write_error    -- 10
    & ddr_csr_w.fifoStatus.full           --  9
    & ddr_csr_w.fifoStatus.empty          --  8
    & ddr_csr_w.fifoStatus.chip_empty     --  7  CDC empty (KEY divergence probe)
    & wp_dbg_sync                         --  6..3 dbg_reset_status (synced)
    & ddr_write_idle_sync(1)              --  2  (preserved)
    & rst_ddr_ever_deassert               --  1  (preserved)
    & rst_ddr_sync(1);                    --  0  (preserved)
  telemetry_in(C_INDEX_DDR_RESET_STATUS / 4).valid <= '1';
  telemetry_in(C_INDEX_RING_COMMIT_PTR  / 4).data  <= ddr_commit_addr_sys;
  telemetry_in(C_INDEX_RING_COMMIT_PTR  / 4).valid <= '1';
  -- DS-P3.60: RING_ALARM_CNT — # times the fill crossed the 75% HIGH watermark
  -- (one per excursion thanks to the hysteresis latch). bit31 mirrors the LIVE
  -- alarm level so devmem can watch the latch set/clear directly on silicon.
  telemetry_in(C_INDEX_RING_ALARM_CNT   / 4).data  <=
      ring_alarm_w & ring_alarm_cnt_w(30 downto 0);
  telemetry_in(C_INDEX_RING_ALARM_CNT   / 4).valid <= '1';

  -- DS-T2.26 probe slots.
  telemetry_in(C_INDEX_EMIT_VALID_CNT   / 4).data  <= std_logic_vector(emit_valid_cnt_r);
  telemetry_in(C_INDEX_EMIT_VALID_CNT   / 4).valid <= '1';
  telemetry_in(C_INDEX_EMIT_DONE_CNT    / 4).data  <= std_logic_vector(emit_done_cnt_r);
  telemetry_in(C_INDEX_EMIT_DONE_CNT    / 4).valid <= '1';

  -- ── DS-T9 / DS-T2.11: EMIO-loop REA probe word + regbank tap ─────────────
  -- 31-bit probe_word, LSB-first — MUST match debug/emio_seam.yml order.
  emio_probe_word <=
      spi0_bus_ss_w         &  -- 31  SPI SS read-in (resolved bus = SPI0_SS_I): CS reaches the injector?
      spi0_cs_gpio_r        &  -- 30  DS-T2.16/18: registered EMIO-GPIO SPI chip-select (what the datalogger sees) — asserts low during xfer
      spi_inj_status_w(0)   &  -- 29  SPI inj FIFO empty
      i2c_inj_status_w(0)   &  -- 28  I2C inj FIFO empty
      framer_out_valid      &  -- 27
      framer_in_valid       &  -- 26
      emio_mux_sel          &  -- 25..23  sel(2),sel(1),sel(0)
      src_uart_tx_w         &  -- 22
      src_spi_ss_w          &  -- 21
      src_spi_miso_w        &  -- 20
      src_spi_mosi_w        &  -- 19
      src_spi_sclk_w        &  -- 18
      src_i2c_sda_w         &  -- 17
      src_i2c_scl_w         &  -- 16
      uart0_tx_w            &  -- 15
      spi_inj_control_w(0)  &  -- 14  SPI inj enable
      spi_inj_miso_t_w      &  -- 13
      spi_inj_miso_o_w      &  -- 12
      spi0_bus_miso_w       &  -- 11  resolved MISO bus (what the PS reads)
      spi0_ss_o_w           &  -- 10
      spi0_mosi_o_w         &  -- 9
      spi0_sclk_o_w         &  -- 8
      i2c_inj_control_w(0)  &  -- 7   I2C inj enable
      i2c_inj_sda_t_w       &  -- 6
      i2c_inj_sda_o_w       &  -- 5
      i2c0_bus_scl_w        &  -- 4   resolved SCL bus
      i2c0_bus_sda_w        &  -- 3   resolved SDA bus (what the PS reads)
      i2c0_sda_t_w          &  -- 2
      i2c0_sda_o_w          &  -- 1
      i2c0_scl_o_w;            -- 0

  -- REA trigger sticky — latches that the core fired; Vivado optimizer anchor
  -- for rea_trigger_out (else the REA hierarchy is pruned).
  p_rea_sticky : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        rea_trig_sticky <= '0';
      elsif rea_trigger_out = '1' then
        rea_trig_sticky <= '1';
      end if;
    end if;
  end process;

  -- DS-P3.166 / DS-T1.21: per-bit scalar 2-FF sync of the EMIO probe word.
  p_emio_probe_sync : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      for i in emio_probe_sync_r'range loop
        emio_probe_sync_r(i) <= emio_probe_sync_r(i)(0) & emio_probe_word(i);
      end loop;
    end if;
  end process;

  g_emio_probe_sync_w : for i in emio_probe_sync_r'range generate
    emio_probe_sync_w(i) <= emio_probe_sync_r(i)(1);
  end generate;

  -- EMIO_PROBE slot: [31]=REA-triggered sticky, [30:0]=synchronized EMIO-seam snapshot.
  -- Static devmem tap takes only probe bits [30:0] (sticky&31b fits one 32-bit
  -- reg). Synchronized per bit via emio_probe_sync_r (DS-P3.166 / DS-T1.21).
  -- Probe bit 31 (SPI SS read-in) is wider than this reg allows and is
  -- observable in the REA JTAG capture only (DS-T9).
  telemetry_in(C_INDEX_EMIO_PROBE / 4).data  <= rea_trig_sticky & emio_probe_sync_w;
  telemetry_in(C_INDEX_EMIO_PROBE / 4).valid <= '1';

  -- ── DS-T2.18 clk_ddr liveness divider (clk_ddr domain, FREE-RUNNING) ──────
  -- Deliberately has NO reset: it ticks from config (GSR release) regardless of
  -- rst_ddr, so the PS can tell "clk_ddr dead" (count stuck) from "clk_ddr alive
  -- but rst_ddr stuck". Divide-by-256 keeps the CDC toggle ~244 kHz — safely
  -- oversampled by clk_sys (100 MHz).
  p_clk_ddr_div : process(clk_ddr)
  begin
    if rising_edge(clk_ddr) then
      clk_ddr_div_r <= clk_ddr_div_r + 1;
      if clk_ddr_div_r = x"00" then
        clk_ddr_toggle_r <= not clk_ddr_toggle_r;
      end if;
    end if;
  end process;

  -- clk_sys side: 2-FF synchronise the toggle, count edges = clk_ddr liveness.
  p_clk_ddr_live : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      if rst_sys = '1' then
        clk_ddr_tgl_sync   <= (others => '0');
        clk_ddr_tgl_sync_d <= '0';
        clk_ddr_live_cnt   <= (others => '0');
      else
        clk_ddr_tgl_sync   <= clk_ddr_tgl_sync(0) & clk_ddr_toggle_r;
        clk_ddr_tgl_sync_d <= clk_ddr_tgl_sync(1);
        if clk_ddr_tgl_sync(1) /= clk_ddr_tgl_sync_d then
          clk_ddr_live_cnt <= clk_ddr_live_cnt + 1;
        end if;
      end if;
    end if;
  end process;

  -- rst_ddr observed in clk_sys: 2-FF sync (live) + sticky ever-deasserted
  -- (init '0' at config, latches '1' the first time rst_ddr is seen low; never
  -- cleared — answers "did clk_ddr's reset EVER release since config?").
  p_rst_ddr_obs : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      rst_ddr_sync <= rst_ddr_sync(0) & rst_ddr;
      if rst_ddr_sync(1) = '0' then
        rst_ddr_ever_deassert <= '1';
      end if;
    end if;
  end process;

  -- DS-T1.21: commit address toggle handshake launch in clk_ddr.
  -- Holds commit_addr_held_ddr stable while a toggle handshake is in flight.
  p_commit_xfer_ddr : process(clk_ddr)
  begin
    if rising_edge(clk_ddr) then
      commit_ack_sync_ddr <= commit_ack_sync_ddr(0) & commit_ack_tgl_sys;
      if commit_req_tgl_ddr = commit_ack_sync_ddr(1) then
        if ddr_wr_commit_addr /= commit_addr_held_ddr then
          commit_addr_held_ddr <= ddr_wr_commit_addr;
          commit_req_tgl_ddr   <= not commit_req_tgl_ddr;
        end if;
      end if;
    end if;
  end process;

  -- DS-T2.20 / DS-T1.21: DMA commit observers into clk_sys for the regbank. write_idle
  -- is a clean 1-bit CDC (the robust RingOracle 'caught up' signal). The commit
  -- address is sampled via toggle handshake (commit_addr_held_ddr held stable in
  -- clk_ddr across the toggle sync window). Exposed as DDR_RESET_STATUS bit2 +
  -- RING_COMMIT_PTR.
  p_ddr_commit_obs : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      ddr_write_idle_sync <= ddr_write_idle_sync(0) & ddr_write_idle;
      commit_req_sync_sys <= commit_req_sync_sys(0) & commit_req_tgl_ddr;
      if commit_req_sync_sys(1) /= commit_ack_tgl_sys then
        ddr_commit_addr_sys <= commit_addr_held_ddr;
        commit_ack_tgl_sys  <= commit_req_sync_sys(1);
      end if;
    end if;
  end process;

  -- rst_sys synchronised into clk_ddr — benign power-on reset for the REA core
  -- (independent of the suspect rst_ddr).
  p_rst_sys_in_ddr : process(clk_ddr)
  begin
    if rising_edge(clk_ddr) then
      rst_sys_in_ddr <= rst_sys_in_ddr(0) & rst_sys;
    end if;
  end process;

  -- DS-P2.167: ddr_write_idle (clk_sys) synchronized into clk_ddr for p_wp_sticky.
  p_ddr_write_idle_sync_ddr : process(clk_ddr)
  begin
    if rising_edge(clk_ddr) then
      ddr_write_idle_ddr_sync <= ddr_write_idle_ddr_sync(0) & ddr_write_idle;
    end if;
  end process;

  -- DS-T1.13: clk_ddr sticky arming latches — set once on the first occurrence,
  -- hold until rst_ddr. These make the A/B a single register read at any time
  -- after boot: "did AW ever fire", "did a write response ever return", "did the
  -- write engine ever leave idle" — the exact questions the DS-T1.12 wedge poses.
  p_wp_sticky : process(clk_ddr)
  begin
    if rising_edge(clk_ddr) then
      if rst_ddr = '1' then
        aw_valid_seen_ddr  <= '0';
        aw_hs_seen_ddr     <= '0';
        w_valid_seen_ddr   <= '0';
        b_valid_seen_ddr   <= '0';
        wr_active_seen_ddr <= '0';
        wr_err_seen_ddr    <= '0';
      else
        if axi_h2c.AWVALID = '1' then
          aw_valid_seen_ddr <= '1';
        end if;
        if axi_h2c.AWVALID = '1' and axi_c2h.AWREADY = '1' then
          aw_hs_seen_ddr <= '1';
        end if;
        if axi_h2c.WVALID = '1' then
          w_valid_seen_ddr <= '1';
        end if;
        if axi_c2h.BVALID = '1' then
          b_valid_seen_ddr <= '1';
        end if;
        -- DS-P2.167: sample 2-FF synchronized write-idle in clk_ddr domain
        if ddr_write_idle_ddr_sync(1) = '0' then
          wr_active_seen_ddr <= '1';
        end if;
        if axi_c2h.BVALID = '1' and axi_c2h.BRESP /= "00" then
          wr_err_seen_ddr <= '1';
        end if;
      end if;
    end if;
  end process;

  -- DS-T1.13 / DS-T1.21: scalar 2-FF sync the clk_ddr sticky levels into
  -- clk_sys. Each independent wire uses its own shift register. DS-P2.167's
  -- source-aware debug synchronization remains below: pipeBusy is clk_sys
  -- native, pipeBusyDdr has one chain, and the merged CS source has one chain.
  -- DS-P3.196: register the combinational CS bit in clk_ddr so the crossing
  -- launches from a flop (no logic ahead of wp_dbg_cs_meta).
  p_wp_dbg_cs_ddr : process(clk_ddr)
  begin
    if rising_edge(clk_ddr) then
      wp_dbg_cs_ddr_r <= ddr_dbg_reset_status(1);
    end if;
  end process;

  p_wp_sync : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      aw_valid_seen_sync_r  <= aw_valid_seen_sync_r(0)  & aw_valid_seen_ddr;
      aw_hs_seen_sync_r     <= aw_hs_seen_sync_r(0)     & aw_hs_seen_ddr;
      w_valid_seen_sync_r   <= w_valid_seen_sync_r(0)   & w_valid_seen_ddr;
      b_valid_seen_sync_r   <= b_valid_seen_sync_r(0)   & b_valid_seen_ddr;
      wr_active_seen_sync_r <= wr_active_seen_sync_r(0) & wr_active_seen_ddr;
      wr_err_seen_sync_r    <= wr_err_seen_sync_r(0)    & wr_err_seen_ddr;
      wp_seen_sync          <= wr_err_seen_sync_r(1) & wr_active_seen_sync_r(1)
                             & b_valid_seen_sync_r(1) & w_valid_seen_sync_r(1)
                             & aw_hs_seen_sync_r(1) & aw_valid_seen_sync_r(1);

      -- bit 3: pipeBusy is native to clk_sys (registered directly)
      wp_dbg_pipe_busy_r    <= ddr_dbg_reset_status(3);
      -- bit 2: pipeBusyDdr is clk_ddr (dedicated 2-FF sync)
      wp_dbg_pbusy_ddr_meta <= ddr_dbg_reset_status(2);
      wp_dbg_pbusy_ddr_sync <= wp_dbg_pbusy_ddr_meta;
      -- bits 1..0: CS is clk_ddr (single 2-FF sync chain avoids launch flop fan-out),
      -- launched from the clk_ddr register wp_dbg_cs_ddr_r (DS-P3.196)
      wp_dbg_cs_meta        <= wp_dbg_cs_ddr_r;
      wp_dbg_cs_sync        <= wp_dbg_cs_meta;
    end if;
  end process;

  wp_dbg_sync <= wp_dbg_pipe_busy_r & wp_dbg_pbusy_ddr_sync & wp_dbg_cs_sync & wp_dbg_cs_sync;

  -- DS-T1.13: clk_sys write-plane observation word, LSB-first — matches
  -- debug/de25_datalogger.yml's decode note. EVERY field is clk_sys (ddr_csr_o is
  -- registered in clk_sys inside the IP; the *_seen / dbg bits are 2-FF synced
  -- above), so this is a coherent clk_sys word safe for the clk_sys REA
  -- (sample_clk => clk_sys) — unlike the legacy clk_ddr ddr_commit_probe_word,
  -- which a clk_sys REA could only sample through an unsynchronised 32-bit CDC.
  write_plane_dbg_word <=
      wp_dbg_sync                        -- 31..28 dbg_reset_status {pipeBusy,pipeBusyDdr,uploader_cs,downloader_cs}
    & ddr_csr_w.fifoStatus.write_error   -- 27     BRESP/=OKAY sticky (clk_sys)
    & ddr_csr_w.fifoStatus.full          -- 26     user FIFO full
    & ddr_csr_w.fifoStatus.empty         -- 25     pipe empty
    & ddr_csr_w.fifoStatus.chip_empty    -- 24     CDC empty (uploader sees no data — KEY)
    & wp_seen_sync                       -- 23..18 seen: wr_err,wr_active,bvalid,wvalid,aw_hs,aw_valid
    & ddr_write_idle_sync(1)             -- 17     write-idle (DMA caught up)
    & rst_ddr_ever_deassert              -- 16     rst_ddr ever deasserted
    & ddr_commit_addr_sys(15 downto 0);  -- 15..0  commit addr low (progress trend)

  -- DDR-commit REA probe word (clk_ddr domain), LSB-first — MUST match
  -- debug/ddr_commit.yml. DS-T2.20: the AW/W/B handshake (proven) PLUS the DMA
  -- COMMIT progress — write_idle + the live commit address — so a capture shows
  -- the commit pointer marching through the ring vs the enqueue rate, and when
  -- the DMA goes idle (caught up). ddr_wr_commit_addr is clk_ddr-native (clean);
  -- framer_out_valid / ddr_s_axis_ready are clk_sys (mild CDC blur, OK).
  ddr_commit_probe_word <=
      ddr_wr_commit_addr(22 downto 4) -- 31..13  DMA commit DDR address (16-byte granular)
    & ddr_write_idle                  -- 12      DMA write-idle (all enqueued committed)
    & framer_out_valid                -- 11      framer presents a frame (clk_sys)
    & ddr_s_axis_ready                -- 10      ddr_stream s_axis ready (clk_sys)
    & rst_ddr                         --  9      DDR-domain reset (live)
    & axi_c2h.BRESP(1)                --  8
    & axi_c2h.BRESP(0)                --  7      BRESP[1:0] (00=OKAY)
    & axi_h2c.BREADY                  --  6
    & axi_c2h.BVALID                  --  5      write response handshake
    & axi_h2c.WLAST                   --  4
    & axi_c2h.WREADY                  --  3
    & axi_h2c.WVALID                  --  2      write-data handshake
    & axi_c2h.AWREADY                 --  1
    & axi_h2c.AWVALID;                --  0      write-address handshake

  -- DS-T2.34 i2c REA probe word (clk_sys domain). LSB-first; matches
  -- debug/i2c_seam.yml. BAIL-DECISION probe (2026-06-09): the fault is a
  -- SEND_DATA->WAIT_START bail at a byte boundary (FIFO/cntSamples/ACK all ruled
  -- out). The only RTL paths to WAIT_START are stopCondition / sclLowTimeout /
  -- glitchError, so this probes those decision signals + their inputs
  -- (sclStableHigh, sdaRising/Falling, slaveDriving) alongside fsm_state and
  -- cnt_samples, so a full-rate capture on the bail attributes it to the exact
  -- trigger. LSB-first; mirrors the yml.
  i2c_probe_word <=
      i2c_slave_dbg_w(19 downto 16)     -- 31..28  fsm_state
    & i2c_slave_dbg_w(15 downto 12)     -- 27..24  cntSamples
    & i2c_dbg2_w(7)                     -- 23      stopCondition
    & i2c_dbg2_w(6)                     -- 22      startCondition
    & i2c_dbg2_w(5)                     -- 21      sclLowTimeout
    & i2c_dbg2_w(4)                     -- 20      glitchError
    & i2c_dbg2_w(3)                     -- 19      sclStableHigh
    & i2c_dbg2_w(2)                     -- 18      sdaRisingEdge
    & i2c_dbg2_w(1)                     -- 17      sdaFallingEdge
    & i2c_dbg2_w(0)                     -- 16      slaveDriving (from DBG2)
    & i2c_slave_dbg_w(7)                -- 15      sclS (synced SCL)
    & i2c_slave_dbg_w(6)                -- 14      sdaS (synced SDA)
    & i2c_slave_dbg_w(4)                -- 13      ack_r
    & i2c_slave_dbg_w(1)                -- 12      sdaOut_r (slave SDA level/release)
    & i2c0_bus_scl_w                    -- 11      bus_scl (raw resolved)
    & i2c0_bus_sda_w                    -- 10      bus_sda (raw resolved)
    & i2c_inj_sda_t_w                   --  9      inj_sda_t (release on bus)
    & i2c_slave_dbg_w(3)                --  8      sclRisingEdge
    & i2c_slave_dbg_w(2)                --  7      sclFallingEdge
    & i2c_slave_dbg_w(11 downto 10)     --  6..5   sclGlitchCnt
    & i2c_dbg_fifo_w(7 downto 4)        --  4..1   fifo_data_cnt (cross-check)
    & "0";                              --  0      pad

  -- DS-P3.107: the DS-T1.6 GP1/APB-hang probe word is dropped — its question
  -- (does canfd0_apb_psel assert on a hung devmem read?) was resolved. The single
  -- USER1 REA is repurposed below to the CAN frame-FSM fixed-stuff trace.

  -- Vendor JTAG primitive lives in the platform top.  The portable core
  -- exports the exact probe and consumes only abstract TAP/trigger legs.
  -- DS-T1.13: the single USER1 REA is repurposed from the CAN destuff trace to
  -- the clk_sys write-plane arming word so a live capture (if the Altera REA
  -- readback confirms — rea-issp Rung 0b) shows the AW/empty/write-idle history
  -- power-on→first-ingress. The same word is mirrored into DDR_RESET_STATUS[..]
  -- for the trustworthy devmem readback (the channel that proved DS-T1.12).
  rea_probe_o   <= write_plane_dbg_word;
  rea_trigger_out <= rea_trigger_i;
  rea_tap_tck <= rea_tap_tck_i;
  rea_tap_tms <= rea_tap_tms_i;
  rea_tap_tdi <= rea_tap_tdi_i;
  rea_tap_tdo <= rea_tap_tdo_i;

  -- Per-protocol PKT_CNT_* / ERR_CNT_* fan-in (DS-P3.26). One pair per
  -- active protocol in datalogger_config.yml; QSPI / I2S regbank slots
  -- stay zero-valued because those protocols aren't present in this
  -- bitstream (their C_PROTO_IDX constants aren't emitted).
  gen_tel_i2c : if C_I2C_DATALOGGERS > 0 generate
    telemetry_in(C_INDEX_PKT_CNT_I2C  / 4).data  <= proto_counters(C_PROTO_IDX_I2C ).pkt_cnt;
    telemetry_in(C_INDEX_ERR_CNT_I2C  / 4).data  <= proto_counters(C_PROTO_IDX_I2C ).err_cnt;
    telemetry_in(C_INDEX_DROP_CNT_I2C / 4).data  <= proto_counters(C_PROTO_IDX_I2C ).drop_cnt;
  else generate
    telemetry_in(C_INDEX_PKT_CNT_I2C  / 4).data  <= (others => '0');
    telemetry_in(C_INDEX_ERR_CNT_I2C  / 4).data  <= (others => '0');
    telemetry_in(C_INDEX_DROP_CNT_I2C / 4).data  <= (others => '0');
  end generate gen_tel_i2c;
  telemetry_in(C_INDEX_PKT_CNT_I2C  / 4).valid <= '1';
  telemetry_in(C_INDEX_ERR_CNT_I2C  / 4).valid <= '1';
  telemetry_in(C_INDEX_DROP_CNT_I2C / 4).valid <= '1';

  gen_tel_spi : if C_SPI_DATALOGGERS > 0 generate
    telemetry_in(C_INDEX_PKT_CNT_SPI  / 4).data  <= proto_counters(C_PROTO_IDX_SPI ).pkt_cnt;
    telemetry_in(C_INDEX_ERR_CNT_SPI  / 4).data  <= proto_counters(C_PROTO_IDX_SPI ).err_cnt;
    telemetry_in(C_INDEX_DROP_CNT_SPI / 4).data  <= proto_counters(C_PROTO_IDX_SPI ).drop_cnt;
  else generate
    telemetry_in(C_INDEX_PKT_CNT_SPI  / 4).data  <= (others => '0');
    telemetry_in(C_INDEX_ERR_CNT_SPI  / 4).data  <= (others => '0');
    telemetry_in(C_INDEX_DROP_CNT_SPI / 4).data  <= (others => '0');
  end generate gen_tel_spi;
  telemetry_in(C_INDEX_PKT_CNT_SPI  / 4).valid <= '1';
  telemetry_in(C_INDEX_ERR_CNT_SPI  / 4).valid <= '1';
  telemetry_in(C_INDEX_DROP_CNT_SPI / 4).valid <= '1';

  gen_tel_uart : if C_UART_DATALOGGERS > 0 generate
    telemetry_in(C_INDEX_PKT_CNT_UART / 4).data  <= proto_counters(C_PROTO_IDX_UART).pkt_cnt;
    telemetry_in(C_INDEX_ERR_CNT_UART / 4).data  <= proto_counters(C_PROTO_IDX_UART).err_cnt;
    telemetry_in(C_INDEX_DROP_CNT_UART / 4).data  <= proto_counters(C_PROTO_IDX_UART).drop_cnt;
  else generate
    telemetry_in(C_INDEX_PKT_CNT_UART / 4).data  <= (others => '0');
    telemetry_in(C_INDEX_ERR_CNT_UART / 4).data  <= (others => '0');
    telemetry_in(C_INDEX_DROP_CNT_UART / 4).data  <= (others => '0');
  end generate gen_tel_uart;
  telemetry_in(C_INDEX_PKT_CNT_UART / 4).valid <= '1';
  telemetry_in(C_INDEX_ERR_CNT_UART / 4).valid <= '1';
  telemetry_in(C_INDEX_DROP_CNT_UART / 4).valid <= '1';

  gen_tel_edge : if C_EDGE_DATALOGGERS > 0 generate
    telemetry_in(C_INDEX_PKT_CNT_EDGE / 4).data  <= proto_counters(C_PROTO_IDX_EDGE).pkt_cnt;
    telemetry_in(C_INDEX_ERR_CNT_EDGE / 4).data  <= proto_counters(C_PROTO_IDX_EDGE).err_cnt;
  else generate
    telemetry_in(C_INDEX_PKT_CNT_EDGE / 4).data  <= (others => '0');
    telemetry_in(C_INDEX_ERR_CNT_EDGE / 4).data  <= (others => '0');
  end generate gen_tel_edge;
  telemetry_in(C_INDEX_PKT_CNT_EDGE / 4).valid <= '1';
  telemetry_in(C_INDEX_ERR_CNT_EDGE / 4).valid <= '1';

  -- DS-T2.56: CAN (DS-P3.100, DS-P2.121: tied off when CAN is not in the active mix) + JTAG (DS-P3.56) PKT/ERR counters.
  telemetry_in(C_INDEX_PKT_CNT_CAN  / 4).data  <= (others => '0');
  telemetry_in(C_INDEX_PKT_CNT_CAN  / 4).valid <= '1';
  telemetry_in(C_INDEX_ERR_CNT_CAN  / 4).data  <= (others => '0');
  telemetry_in(C_INDEX_ERR_CNT_CAN  / 4).valid <= '1';

  gen_tel_jtag : if C_JTAG_DATALOGGERS > 0 generate
    telemetry_in(C_INDEX_PKT_CNT_JTAG / 4).data  <= proto_counters(C_PROTO_IDX_JTAG).pkt_cnt;
    telemetry_in(C_INDEX_ERR_CNT_JTAG / 4).data  <= proto_counters(C_PROTO_IDX_JTAG).err_cnt;
  else generate
    telemetry_in(C_INDEX_PKT_CNT_JTAG / 4).data  <= (others => '0');
    telemetry_in(C_INDEX_ERR_CNT_JTAG / 4).data  <= (others => '0');
  end generate gen_tel_jtag;
  telemetry_in(C_INDEX_PKT_CNT_JTAG / 4).valid <= '1';
  telemetry_in(C_INDEX_ERR_CNT_JTAG / 4).valid <= '1';

  -- DS-T2.22 mechanism B: DDR ring-full backpressure-active cycle count — the
  -- only counter that sees the upstream real-time loss when the consumer falls
  -- behind (the per-protocol DROP_CNT can't: no frame even forms).
  telemetry_in(C_INDEX_DDR_BACKPRESSURE_CNT / 4).data  <= std_logic_vector(ddr_backpressure_cnt_r);
  telemetry_in(C_INDEX_DDR_BACKPRESSURE_CNT / 4).valid <= '1';

  -- DS-P2.51 Phase 2: injector STATUS slots (RO).
  telemetry_in(C_INDEX_I2C_INJ_STATUS / 4).data  <= i2c_inj_status_w;
  telemetry_in(C_INDEX_I2C_INJ_STATUS / 4).valid <= '1';
  telemetry_in(C_INDEX_SPI_INJ_STATUS / 4).data  <= spi_inj_status_w;
  telemetry_in(C_INDEX_SPI_INJ_STATUS / 4).valid <= '1';

  -- DDR ring locator + write pointer (DS-P3.27 Phase B). RING_READ_PTR is RW:
  -- SW writes it, and the PL now CONSUMES it as the ring drain pointer (read
  -- via regs_out_local → u_ddr_burst.csr_rd_ptr_addr_i, DS-P2.59) — so it is
  -- not injected on the telemetry_in (RO read-back) path.
  telemetry_in(C_INDEX_RING_WRITE_PTR / 4).data  <= std_logic_vector(ring_write_cnt_r);
  telemetry_in(C_INDEX_RING_WRITE_PTR / 4).valid <= '1';
  telemetry_in(C_INDEX_RING_BASE_LOW  / 4).data  <= C_RING_BASE_LOW;
  telemetry_in(C_INDEX_RING_BASE_LOW  / 4).valid <= '1';
  telemetry_in(C_INDEX_RING_BASE_HIGH / 4).data  <= C_RING_BASE_HIGH;
  telemetry_in(C_INDEX_RING_BASE_HIGH / 4).valid <= '1';
  telemetry_in(C_INDEX_RING_BYTES     / 4).data  <= C_RING_BYTES;
  telemetry_in(C_INDEX_RING_BYTES     / 4).valid <= '1';

  -- DS-T5: identity / build-metadata slots. These are static constants
  -- from rom_info_pkg.vhd (regenerated by gen_build_metadata.py at synth
  -- time). The inner regbank's reset-time C_INIT_REGBANK only seeds the
  -- underlying RAM at reset, but the read path goes through telemetry_in
  -- → regs_in → reg_bank. Without these drivers the slots read zero,
  -- which is what PS-LOOP-REQ-1 + REQ-2 (run_self_test) caught.
  telemetry_in(C_INDEX_GIT_HASH          / 4).data  <= C_GIT_HASH;
  telemetry_in(C_INDEX_GIT_HASH          / 4).valid <= '1';
  telemetry_in(C_INDEX_SYSTEM_VERSION    / 4).data  <= C_SYSTEM_VERSION;
  telemetry_in(C_INDEX_SYSTEM_VERSION    / 4).valid <= '1';
  telemetry_in(C_INDEX_BUILD_DATE        / 4).data  <= C_BUILD_DATE;
  telemetry_in(C_INDEX_BUILD_DATE        / 4).valid <= '1';
  telemetry_in(C_INDEX_REGBANK_VERSION   / 4).data  <= C_REGBANK_VERSION;
  telemetry_in(C_INDEX_REGBANK_VERSION   / 4).valid <= '1';
  -- REGBANK_NUM_WORDS / CONFIG_NUM_WORDS are driven inside
  -- system_regbank_wrapper from the generated rom_info_pkg word counts of
  -- the hex files it loads (DS-P1.82, DS-P2.249).

  ---------------------------------------------------------------------------
  -- LEDs: heartbeat + activity mirrors.
  ---------------------------------------------------------------------------
  p_heartbeat : process(clk_sys)
  begin
    if rising_edge(clk_sys) then
      hb_div <= hb_div + 1;
    end if;
  end process;

  led(0) <= hb_div(hb_div'high);                -- ~1 Hz blink (~0.75 Hz at 100 MHz)
  led(1) <= fifo_csr.fifoStatus.full;     -- burst FIFO full warning
  led(2) <= fifo_csr.fifoStatus.overflow; -- burst overflow sticky
  -- DDR-P3.10: LED reflects input-side back-pressure (skid full →
  -- pipe is consuming as fast as producer; ready=0 means stalling).
  ddr_busy <= not ddr_s_axis_ready;
  led(3)   <= ddr_busy;                         -- DDR DMA back-pressured

end architecture rtl;
