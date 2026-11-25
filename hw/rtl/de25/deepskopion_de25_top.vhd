-- rr unknown-llm (AI-generated)
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-- DE-H0 Agilex 5 / DE25 platform shell. Platform Designer generation is
-- owned by rr-native ip.yml declaration (DS-P1.52/DS-P1.53) -> `rr synth
-- run`'s pre-synth HPS generator (sdk.engine.hps_gen / hps_preset), NOT a
-- hand-authored qsys-script.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library rr;
use rr.ddr_stream_pkg.all;
library tich;
library rr_rea;

entity deepskopion_de25_top is
  port (
    -- Board oscillator (CLOCK2_50, LVCMOS12, pin BM71) — the proven
    -- de25_hps_smoke / de25_led_smoke fabric clock. Drives every fabric-side
    -- bridge clock sink (fabric_clk / f2sdram_adapter_clk / lwhps2fpga_clk).
    CLOCK2_50 : in  std_logic;

    -- DE-B0: a second, unused board 50 MHz oscillator. boards/de25-standard's
    -- master.sdc unconditionally declares `create_clock` on all three board
    -- oscillators (board-authoritative, one file shared by every target)
    -- even though this design clocks itself from CLOCK2_50 alone. A
    -- `create_clock -name X [get_ports {X}]` against a name absent from the
    -- top netlist has no port to bind — the Assembler reports it as a
    -- Critical Warning (25207-class) instead of silently matching nothing.
    -- Declaring it as a real, unused input port gives the SDC a target and
    -- lets the board pinout subscription (targets/de25_datalogger.yml) bind it
    -- a real location/IO standard, same as any other unused input pin.
    --
    -- CLOCK1_50 is deliberately NOT declared here. DS-P3.143 ran the DE-P0
    -- §5.2 schematic cross-check this exclusion was waiting on:
    -- D8/1.8-V is CONFIRMED, not a vendor-seed guess — it matches the
    -- DE25-Standard revD User Manual's Table 3-9 pin-assignment table
    -- ("CLOCK1_50, PIN_D8, 1.8V") verbatim. That does not change the
    -- outcome: D8 is a fixed PCB trace (no alternate schematic-sourced
    -- location exists to "re-derive" to), and it is genuinely,
    -- reproducibly Fitter-illegal once this design's actual HPS DDR4 EMIF
    -- is in the fit — "Error (175020): cannot place logic pin in region
    -- (108, 53) ... no valid locations ... that supports the 1.8-V
    -- standard", reproduced BYTE-FOR-BYTE by two independent queued builds
    -- (job 3d510c77d9bd, and job a68149b2dab5 re-run after this ticket's
    -- own DE-F0 fitter cure landed — same error, same region, same pin, so
    -- that fitter cure did not touch this conflict). This is a real,
    -- permanent I/O-bank resource conflict on this SKU, confirmed by
    -- schematic, not a data-entry bug or an unverified guess; there is no
    -- pin to declare here as long as the HPS DDR4 EMIF occupies this
    -- design's fit. Left undeclared, its `create_clock` target is empty
    -- and harmless — confirmed by a passing build (job 143337099b45:
    -- Assembler 0 errors/0 warnings, timing met) with CLOCK0_50 bound and
    -- CLOCK1_50 absent.
    CLOCK0_50 : in  std_logic;

    -- ── User LEDs (autonomous verdict, §6) ────────────────────────────────
    -- FABRIC-driveable user LEDs (LEDR bank, the de25_hps_smoke / de25_led_smoke
    -- canary pins — NOT HPS_LED Y127, which is in the HPS-dedicated I/O bank and
    -- cannot be driven by fabric logic). Pins bound from boards/de25-standard
    -- by the board pinout subscription (no hand-written QSF).
    -- LD0 running · LD1 done · LD2 pass (latched) · LD3 fail (latched).
    LEDR : out std_logic_vector(3 downto 0);

    -- ── HPS DDR4 (io96b EMIF) physical pins (RTL-P3.722) ──────────────────
    -- TRIVIAL CONDUIT PASS-THROUGH: the EMIF (inside hps_subsys) owns all DDR4
    -- logic; the top merely exposes its mem/ck/reset/oct/refclk conduits as
    -- board pins and passes them straight through. Widths transcribed from the
    -- regenerated hps_subsys wrapper (EMIF_TOPOLOGY=1x32 → 32-bit bus). Bound to
    -- the DE25 board DDR4_* / DDR4_REFCLK / DDR4_RZQ pins by the board pinout
    -- subscription (no hand-written QSF). The HPS cannot boot without these:
    -- they ARE the DRAM bus the SPL/Linux runs from.
    DDR4_CKE     : out   std_logic;
    DDR4_ODT     : out   std_logic;
    DDR4_CS_n    : out   std_logic;
    DDR4_A       : out   std_logic_vector(16 downto 0);
    DDR4_BA      : out   std_logic_vector(1 downto 0);
    DDR4_BG      : out   std_logic_vector(0 downto 0);
    DDR4_ACT_n   : out   std_logic;
    DDR4_PAR     : out   std_logic;
    DDR4_DQ      : inout std_logic_vector(31 downto 0);
    DDR4_DQS     : inout std_logic_vector(3 downto 0);   -- mem_dqs_t
    DDR4_DQS_n   : inout std_logic_vector(3 downto 0);   -- mem_dqs_c
    -- Read-DBI data-bus-inversion strobes (RTL-P3.733). The harvested EMIF .ip
    -- has CTRL_RD_DBI_EN=true (x9 byte lane), so the regenerated wrapper exposes
    -- mem_dbi_n[3:0]; bound to the board DDR4_DBI_n[3:0] pins (pinout already
    -- lists them). One per byte lane (4 lanes for the 1x32 bus).
    DDR4_DBI_n   : inout std_logic_vector(3 downto 0);   -- mem_dbi_n (read-DBI)
    DDR4_ALERT_n  : in    std_logic;
    DDR4_CK       : out   std_logic;                       -- mem_ck_t (board scalar pin)
    DDR4_CK_n     : out   std_logic;                       -- mem_ck_c (board scalar pin)
    DDR4_RESET_n  : out   std_logic;
    DDR4_RZQ      : in    std_logic;                       -- oct_rzqin
    DDR4_REFCLK_p : in    std_logic;                       -- ref_clk (150 MHz, board pin name)

    -- ── HPS dedicated I/O (frozen HPS IO48 pin-mux, RTL-P3.722 Option A) ────
    -- The 48 HPS-dedicated board pins the frozen agilex_hps.ip routes via its
    -- baked-in pin-mux (board names match boards/de25-standard + golden_top.qsf).
    -- HPS_UART_TX (M124) is the SPL/Linux console (ttyS1) — the silent-boot fix.
    -- HPS_SD_* is the SD boot device; HPS_ENET_* the EMAC0 dev loop. Bound to the
    -- HPS-bank pins by the board pinout subscription (no hand-written QSF).
    HPS_CLK_25       : in    std_logic;                    -- hps_io_hps_osc_clk
    HPS_SD_CLK       : out   std_logic;                    -- hps_io_sdmmc_cclk
    HPS_SD_CMD       : inout std_logic;                    -- hps_io_sdmmc_cmd
    HPS_SD_DATA      : inout std_logic_vector(3 downto 0); -- hps_io_sdmmc_data0..3
    HPS_UART_TX      : out   std_logic;                    -- hps_io_uart1_tx (console)
    HPS_UART_RX      : in    std_logic;                    -- hps_io_uart1_rx
    HPS_ENET_MDC     : out   std_logic;                    -- hps_io_mdio0_mdc
    HPS_ENET_MDIO    : inout std_logic;                    -- hps_io_mdio0_mdio
    HPS_ENET_RX_CLK  : in    std_logic;                    -- hps_io_emac0_rx_clk
    HPS_ENET_RX_CTL  : in    std_logic;                    -- hps_io_emac0_rx_ctl
    HPS_ENET_RX_DATA : in    std_logic_vector(3 downto 0); -- hps_io_emac0_rxd0..3
    HPS_ENET_TX_CLK  : out   std_logic;                    -- hps_io_emac0_tx_clk
    HPS_ENET_TX_CTL  : out   std_logic;                    -- hps_io_emac0_tx_ctl
    HPS_ENET_TX_DATA : out   std_logic_vector(3 downto 0); -- hps_io_emac0_txd0..3
    HPS_USB_CLK      : in    std_logic;                    -- hps_io_usb0_clk
    HPS_USB_STP      : out   std_logic;                    -- hps_io_usb0_stp
    HPS_USB_DIR      : in    std_logic;                    -- hps_io_usb0_dir
    HPS_USB_NXT      : in    std_logic;                    -- hps_io_usb0_nxt
    HPS_USB_DATA     : inout std_logic_vector(7 downto 0); -- hps_io_usb0_data0..7
    HPS_I2C_SCL      : inout std_logic;                    -- hps_io_i2c1_scl
    HPS_I2C_SDA      : inout std_logic;                    -- hps_io_i2c1_sda
    HPS_LCM_SPIM_CLK : out   std_logic;                    -- hps_io_spim0_clk
    HPS_LCM_SPIM_MOSI : out  std_logic;                    -- hps_io_spim0_mosi
    HPS_LCM_SPIM_SS  : out   std_logic;                    -- hps_io_spim0_ss0_n
    -- The 8 HPS GPIO-mapped pins (LCM control, sensor, key, LED, …) the frozen
    -- pin-mux routes as GPIO1 — passed straight to their board pins.
    HPS_LCM_D_C      : inout std_logic;                    -- hps_io_gpio35
    HPS_GPIO         : inout std_logic_vector(1 downto 0); -- hps_io_gpio32/33
    HPS_LCM_RST_n    : inout std_logic;                    -- hps_io_gpio34
    HPS_LCM_BK       : inout std_logic;                    -- hps_io_gpio42
    HPS_KEY          : inout std_logic;                    -- hps_io_gpio40
    HPS_LED          : inout std_logic;                    -- hps_io_gpio41
    HPS_GSENSOR_INT  : inout std_logic;                    -- hps_io_gpio28
    GPIO_D           : in std_logic_vector(15 downto 0)
  );
end entity deepskopion_de25_top;


architecture rtl of deepskopion_de25_top is
  component hps_subsys is
    port (
      -- Always-present flat exports (contract §1). DS-P1.53: this is the
      -- REAL rr-native hps_preset wrapper shape (confirmed against a queued
      -- `rr synth run`'s hps_subsys/synth/hps_subsys.v, job 963d5342d885) —
      -- a bare fabric_clk/fabric_reset conduit pair, NOT the PLL-embedded
      -- pll_refclk/pll_locked_export/fabric_clk_out/f2h_irq0 shape this
      -- component carried before. That older shape was never produced by
      -- rr's hps_preset engine (grep confirms no pll_refclk/pll_locked/
      -- f2h_irq0 string in sdk/engine/hps_preset.py, ever) — it silently
      -- black-boxed the whole subsystem at VHDL binding (Error 13870 x4)
      -- the first time this project's build actually reached elaboration
      -- with a real generated wrapper. Mirrors the PROVEN, real-hardware
      -- rr-ddr-stream de25_agilex5_ddr_validator top_de25_ddr_validator.vhd
      -- component declaration verbatim (no PLL — the fabric clock IS
      -- CLOCK2_50, driven straight in; see the port map below).
      fabric_clk_clk             : in  std_logic := 'X';  -- fabric clock-bridge input
      fabric_reset_reset        : in  std_logic := 'X';  -- fabric reset-bridge input (active-high)
      ninit_done_ninit_done     : out std_logic;          -- Reset Release IP output

      -- ── f2sdram: FPGA→HPS-DDR AXI4 SLAVE, 256-bit data / 32 addr / 5 id (§2) ──
      -- NB: the regenerated hps_subsys.v drives the f2sdram bridge clock/reset
      -- INTERNALLY (from fabric_clk + fabric_reset via mm_interconnect) — there
      -- are NO f2sdram_adapter_clk/rst top ports. The slave is the AXI4 custom
      -- f2sdram_adapter (RTL-P3.682): 8-bit AxLEN, 1-bit AxLOCK, no WID, and it
      -- DOES carry qos/region/user — the adapter ignores the fabric AxUSER and
      -- drives the HPS-side AxUSER=0xE0 itself (the ACE5-Lite attr that routes
      -- the write to DDR). Confirmed against hps_subsys/synth/hps_subsys.v.
      -- Write address channel (slave inputs).
      f2sdram_adapter_axi4_sub_awid     : in  std_logic_vector(4 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awaddr   : in  std_logic_vector(31 downto 0) := (others => 'X');
      f2sdram_adapter_axi4_sub_awlen    : in  std_logic_vector(7 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awsize   : in  std_logic_vector(2 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awburst  : in  std_logic_vector(1 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awlock   : in  std_logic := 'X';
      f2sdram_adapter_axi4_sub_awcache  : in  std_logic_vector(3 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awprot   : in  std_logic_vector(2 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awqos    : in  std_logic_vector(3 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awregion : in  std_logic_vector(3 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awuser   : in  std_logic_vector(7 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_awvalid  : in  std_logic := 'X';
      f2sdram_adapter_axi4_sub_awready  : out std_logic;
      -- Write data channel (slave inputs). AXI4: no WID; carries WUSER.
      f2sdram_adapter_axi4_sub_wdata    : in  std_logic_vector(255 downto 0) := (others => 'X');
      f2sdram_adapter_axi4_sub_wstrb    : in  std_logic_vector(31 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_wlast    : in  std_logic := 'X';
      f2sdram_adapter_axi4_sub_wuser    : in  std_logic_vector(7 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_wvalid   : in  std_logic := 'X';
      f2sdram_adapter_axi4_sub_wready   : out std_logic;
      -- Write response channel (slave outputs).
      f2sdram_adapter_axi4_sub_bid      : out std_logic_vector(4 downto 0);
      f2sdram_adapter_axi4_sub_bresp    : out std_logic_vector(1 downto 0);
      f2sdram_adapter_axi4_sub_buser    : out std_logic_vector(7 downto 0);
      f2sdram_adapter_axi4_sub_bvalid   : out std_logic;
      f2sdram_adapter_axi4_sub_bready   : in  std_logic := 'X';
      -- Read address channel (slave inputs).
      f2sdram_adapter_axi4_sub_arid     : in  std_logic_vector(4 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_araddr   : in  std_logic_vector(31 downto 0) := (others => 'X');
      f2sdram_adapter_axi4_sub_arlen    : in  std_logic_vector(7 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arsize   : in  std_logic_vector(2 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arburst  : in  std_logic_vector(1 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arlock   : in  std_logic := 'X';
      f2sdram_adapter_axi4_sub_arcache  : in  std_logic_vector(3 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arprot   : in  std_logic_vector(2 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arqos    : in  std_logic_vector(3 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arregion : in  std_logic_vector(3 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_aruser   : in  std_logic_vector(7 downto 0)  := (others => 'X');
      f2sdram_adapter_axi4_sub_arvalid  : in  std_logic := 'X';
      f2sdram_adapter_axi4_sub_arready  : out std_logic;
      -- Read data channel (slave outputs).
      f2sdram_adapter_axi4_sub_rid      : out std_logic_vector(4 downto 0);
      f2sdram_adapter_axi4_sub_rdata    : out std_logic_vector(255 downto 0);
      f2sdram_adapter_axi4_sub_rresp    : out std_logic_vector(1 downto 0);
      f2sdram_adapter_axi4_sub_rlast    : out std_logic;
      f2sdram_adapter_axi4_sub_ruser    : out std_logic_vector(7 downto 0);
      f2sdram_adapter_axi4_sub_rvalid   : out std_logic;
      f2sdram_adapter_axi4_sub_rready   : in  std_logic := 'X';

      -- ── lwh2f: HPS→fabric AXI-Lite MASTER, 32-bit data / 29 addr / 4 id (§3) ──
      -- The lwhps2fpga bridge clock/reset are ALSO driven internally (no
      -- lwhps2fpga_clk/rst top ports in the regenerated wrapper).
      -- Write address channel (master outputs).
      lwhps2fpga_awid           : out std_logic_vector(3 downto 0);
      lwhps2fpga_awaddr         : out std_logic_vector(28 downto 0);
      lwhps2fpga_awlen          : out std_logic_vector(7 downto 0);
      lwhps2fpga_awsize         : out std_logic_vector(2 downto 0);
      lwhps2fpga_awburst        : out std_logic_vector(1 downto 0);
      lwhps2fpga_awlock         : out std_logic;
      lwhps2fpga_awcache        : out std_logic_vector(3 downto 0);
      lwhps2fpga_awprot         : out std_logic_vector(2 downto 0);
      lwhps2fpga_awvalid        : out std_logic;
      lwhps2fpga_awready        : in  std_logic := 'X';
      -- Write data channel (master outputs).
      lwhps2fpga_wdata          : out std_logic_vector(31 downto 0);
      lwhps2fpga_wstrb          : out std_logic_vector(3 downto 0);
      lwhps2fpga_wlast          : out std_logic;
      lwhps2fpga_wvalid         : out std_logic;
      lwhps2fpga_wready         : in  std_logic := 'X';
      -- Write response channel (master inputs).
      lwhps2fpga_bid            : in  std_logic_vector(3 downto 0) := (others => 'X');
      lwhps2fpga_bresp          : in  std_logic_vector(1 downto 0) := (others => 'X');
      lwhps2fpga_bvalid         : in  std_logic := 'X';
      lwhps2fpga_bready         : out std_logic;
      -- Read address channel (master outputs).
      lwhps2fpga_arid           : out std_logic_vector(3 downto 0);
      lwhps2fpga_araddr         : out std_logic_vector(28 downto 0);
      lwhps2fpga_arlen          : out std_logic_vector(7 downto 0);
      lwhps2fpga_arsize         : out std_logic_vector(2 downto 0);
      lwhps2fpga_arburst        : out std_logic_vector(1 downto 0);
      lwhps2fpga_arlock         : out std_logic;
      lwhps2fpga_arcache        : out std_logic_vector(3 downto 0);
      lwhps2fpga_arprot         : out std_logic_vector(2 downto 0);
      lwhps2fpga_arvalid        : out std_logic;
      lwhps2fpga_arready        : in  std_logic := 'X';
      -- Read data channel (master inputs).
      lwhps2fpga_rid            : in  std_logic_vector(3 downto 0) := (others => 'X');
      lwhps2fpga_rdata          : in  std_logic_vector(31 downto 0) := (others => 'X');
      lwhps2fpga_rresp          : in  std_logic_vector(1 downto 0) := (others => 'X');
      lwhps2fpga_rlast          : in  std_logic := 'X';
      lwhps2fpga_rvalid         : in  std_logic := 'X';
      lwhps2fpga_rready         : out std_logic;

      -- NB: the frozen HPS's enabled-but-unused h2f + f2s coherent MPFE bridges
      -- are TERMINATED entirely INSIDE hps_subsys as a self-contained HPS→HPS
      -- coherent loop (hps2fpga → ACE5-Lite translator → fpga2hps), so there are
      -- NO h2f/f2s top ports here — the validator datapath rides f2sdram +
      -- lwhps2fpga only.

      -- ── HPS DDR4 (io96b EMIF) conduits + refclk (§5, RTL-P3.722) ─────────
      -- Names + widths transcribed from the regenerated hps_subsys wrapper
      -- (EMIF_TOPOLOGY=1x32). These reach the board DDR4_* pins straight through.
      emif_mem_0_mem_cke           : out   std_logic_vector(0 downto 0);
      emif_mem_0_mem_odt           : out   std_logic_vector(0 downto 0);
      emif_mem_0_mem_cs_n          : out   std_logic_vector(0 downto 0);
      emif_mem_0_mem_a             : out   std_logic_vector(16 downto 0);
      emif_mem_0_mem_ba            : out   std_logic_vector(1 downto 0);
      emif_mem_0_mem_bg            : out   std_logic_vector(0 downto 0);
      emif_mem_0_mem_act_n         : out   std_logic;
      emif_mem_0_mem_par           : out   std_logic;
      emif_mem_0_mem_dq            : inout std_logic_vector(31 downto 0) := (others => 'X');
      emif_mem_0_mem_dqs_t         : inout std_logic_vector(3 downto 0)  := (others => 'X');
      emif_mem_0_mem_dqs_c         : inout std_logic_vector(3 downto 0)  := (others => 'X');
      emif_mem_0_mem_alert_n       : in    std_logic := 'X';
      emif_mem_0_mem_dbi_n         : inout std_logic_vector(3 downto 0)  := (others => 'X');
      emif_mem_ck_0_mem_ck_t       : out   std_logic_vector(0 downto 0);
      emif_mem_ck_0_mem_ck_c       : out   std_logic_vector(0 downto 0);
      emif_mem_reset_n_mem_reset_n : out   std_logic;
      emif_oct_0_oct_rzqin         : in    std_logic := 'X';
      emif_ref_clk_clk             : in    std_logic := 'X';

      -- ── HPS IO48 physical-pin conduit (RTL-P3.722 Option A, frozen HPS) ──
      -- The 48 dedicated HPS I/O signals the FROZEN agilex_hps.ip routes via its
      -- baked-in pin-mux. Wired straight through to the board HPS-dedicated pins.
      -- hps_io_uart1_tx reaching pin M124 is what makes the SPL console alive.
      -- Directions transcribed from hps_subsys/synth/hps_subsys.v.
      hps_io_hps_osc_clk : in    std_logic := 'X';
      hps_io_sdmmc_data0 : inout std_logic := 'X';
      hps_io_sdmmc_data1 : inout std_logic := 'X';
      hps_io_sdmmc_cclk  : out   std_logic;
      hps_io_sdmmc_data2 : inout std_logic := 'X';
      hps_io_sdmmc_data3 : inout std_logic := 'X';
      hps_io_sdmmc_cmd   : inout std_logic := 'X';
      hps_io_usb0_clk    : in    std_logic := 'X';
      hps_io_usb0_stp    : out   std_logic;
      hps_io_usb0_dir    : in    std_logic := 'X';
      hps_io_usb0_data0  : inout std_logic := 'X';
      hps_io_usb0_data1  : inout std_logic := 'X';
      hps_io_usb0_nxt    : in    std_logic := 'X';
      hps_io_usb0_data2  : inout std_logic := 'X';
      hps_io_usb0_data3  : inout std_logic := 'X';
      hps_io_usb0_data4  : inout std_logic := 'X';
      hps_io_usb0_data5  : inout std_logic := 'X';
      hps_io_usb0_data6  : inout std_logic := 'X';
      hps_io_usb0_data7  : inout std_logic := 'X';
      hps_io_emac0_tx_clk : out   std_logic;
      hps_io_emac0_tx_ctl : out   std_logic;
      hps_io_emac0_rx_clk : in    std_logic := 'X';
      hps_io_emac0_rx_ctl : in    std_logic := 'X';
      hps_io_emac0_txd0   : out   std_logic;
      hps_io_emac0_txd1   : out   std_logic;
      hps_io_emac0_rxd0   : in    std_logic := 'X';
      hps_io_emac0_rxd1   : in    std_logic := 'X';
      hps_io_emac0_txd2   : out   std_logic;
      hps_io_emac0_txd3   : out   std_logic;
      hps_io_emac0_rxd2   : in    std_logic := 'X';
      hps_io_emac0_rxd3   : in    std_logic := 'X';
      hps_io_mdio0_mdio  : inout std_logic := 'X';
      hps_io_mdio0_mdc   : out   std_logic;
      hps_io_spim0_clk   : out   std_logic;
      hps_io_spim0_mosi  : out   std_logic;
      hps_io_spim0_ss0_n : out   std_logic;
      hps_io_uart1_tx    : out   std_logic;
      hps_io_uart1_rx    : in    std_logic := 'X';
      hps_io_i2c1_sda    : inout std_logic := 'X';
      hps_io_i2c1_scl    : inout std_logic := 'X';
      hps_io_gpio28      : inout std_logic := 'X';
      hps_io_gpio32      : inout std_logic := 'X';
      hps_io_gpio33      : inout std_logic := 'X';
      hps_io_gpio34      : inout std_logic := 'X';
      hps_io_gpio35      : inout std_logic := 'X';
      hps_io_gpio40      : inout std_logic := 'X';
      hps_io_gpio41      : inout std_logic := 'X';
      hps_io_gpio42      : inout std_logic := 'X'
    );
  end component hps_subsys;
  -- DS-P2.155: fabric IOPLL (altera_iopll) — CLOCK2_50 (50 MHz) x2 -> 100 MHz.
  -- Declared in ip.yml vendor_ip_variants (DS-P2.196); rr makes and generates
  -- the variant in build/rr_ip_regen/. Integer-N direct mode, one output
  -- (outclk_0 = 100 MHz), locked port enabled. Config lifted verbatim from the
  -- validated de25_datalogger sys_pll that fit TIMING MET (WNS +0.001 ns) at
  -- 100 MHz [clk_fpga_0] on this exact part (A5ED013BB32AE4SCS). The declarative
  -- `clocks:` block (RTL-P2.1276) is Xilinx-only today, so the PLL is a declared
  -- vendor IP variant. gui_skip_sdc_generation=false, so the IP emits
  -- its own create_generated_clock for outclk_0 (pulled in by derive_pll_clocks
  -- in de25_timing.sdc).
  component de25_fabric_pll is
    port (
      refclk   : in  std_logic := 'X';  -- CLOCK2_50 reference (50 MHz)
      rst      : in  std_logic := 'X';  -- active-high PLL reset (tied 0: lock ASAP)
      outclk_0 : out std_logic;         -- 100 MHz fabric product clock
      locked   : out std_logic          -- gates the fabric reset synchroniser
    );
  end component de25_fabric_pll;

  signal clk_sys, rst_sys, rst_ddr : std_logic;
  signal ninit_done, pll_locked : std_logic;
  -- DS-P2.100 / DS-D.38 option a: fabric-side stimulus-generator loopback.
  -- u_core's own stim_*_o legs feed straight back into its own emio_*_i
  -- legs (replacing the DS-P2.95 tied-off constants) — a within-instance
  -- net loopback, not a self-reference; see deepskopion_core.vhd header on
  -- u_stim_gen. spi_ss_w drives BOTH emio_spi_ss_o_i (the "dead witness"
  -- native SS, per ds_emio_mux.vhd) AND emio_gpio_o_i (the EMIO-GPIO leg
  -- the SPI injector's CS_N and the datalogger's SRC_SPI_SS actually watch —
  -- DS-T2.16/DS-T2.18).
  signal stim_uart_tx_w  : std_logic;
  signal stim_i2c_scl_o_w, stim_i2c_scl_t_w : std_logic;
  signal stim_i2c_sda_o_w, stim_i2c_sda_t_w : std_logic;
  signal stim_spi_sclk_w : std_logic;
  signal stim_spi_mosi_w : std_logic;
  signal stim_spi_ss_w   : std_logic;
  signal axi_h2c : rr.ddr_stream_pkg.axi4_h2c_bus_t;
  signal axi_c2h : rr.ddr_stream_pkg.axi4_c2h_bus_t;
  signal f2s_awid, f2s_arid, f2s_bid, f2s_rid : std_logic_vector(4 downto 0);
  signal f2s_awready, f2s_wready, f2s_bvalid, f2s_arready, f2s_rlast, f2s_rvalid : std_logic;
  signal f2s_bresp, f2s_rresp : std_logic_vector(1 downto 0);
  signal f2s_rdata : std_logic_vector(255 downto 0);
  signal lw_awid, lw_arid, lw_bid, lw_rid : std_logic_vector(3 downto 0);
  signal lw_awaddr, lw_araddr : std_logic_vector(28 downto 0);
  signal lw_awprot, lw_arprot : std_logic_vector(2 downto 0);
  signal lw_awvalid, lw_awready, lw_wvalid, lw_wready : std_logic;
  signal lw_bvalid, lw_bready, lw_arvalid, lw_arready, lw_rvalid, lw_rready : std_logic;
  signal lw_wdata, lw_rdata : std_logic_vector(31 downto 0);
  signal lw_wstrb : std_logic_vector(3 downto 0);
  signal lw_bresp, lw_rresp : std_logic_vector(1 downto 0);
  signal lw_rlast : std_logic;

  signal br_awaddr, br_araddr : std_logic_vector(17 downto 0);
  signal br_awprot, br_arprot : std_logic_vector(2 downto 0);
  signal br_awvalid, br_awready, br_wvalid, br_wready : std_logic;
  signal br_bvalid, br_bready, br_arvalid, br_arready, br_rvalid, br_rready : std_logic;
  signal br_wdata, br_rdata : std_logic_vector(31 downto 0);
  signal br_wstrb : std_logic_vector(3 downto 0);
  signal br_bresp, br_rresp : std_logic_vector(1 downto 0);
  signal m_awaddr, m_araddr : std_logic_vector(47 downto 0);
  signal m_awprot, m_arprot : std_logic_vector(8 downto 0);
  signal m_awvalid, m_awready, m_wvalid, m_wready : std_logic_vector(2 downto 0);
  signal m_bvalid, m_bready, m_arvalid, m_arready, m_rvalid, m_rready : std_logic_vector(2 downto 0);
  signal m_wdata, m_rdata : std_logic_vector(95 downto 0);
  signal m_wstrb : std_logic_vector(11 downto 0);
  signal m_bresp, m_rresp : std_logic_vector(5 downto 0);
  signal core_awaddr, core_araddr : std_logic_vector(31 downto 0);
  signal apb0_addr, apb1_addr, apb0_wdata, apb1_wdata : std_logic_vector(31 downto 0);
  signal apb0_rdata, apb1_rdata : std_logic_vector(31 downto 0);
  signal apb0_sel, apb0_enable, apb0_write, apb0_ready, apb0_error : std_logic;
  signal apb1_sel, apb1_enable, apb1_write, apb1_ready, apb1_error : std_logic;
  -- DS-P1.53: deepskopion_core's irq_o has no sink — the real rr-generated
  -- hps_subsys carries no f2h_irq0 port to route it to (confirmed: no
  -- pll_refclk/pll_locked/f2h_irq0 string anywhere in
  -- sdk/engine/hps_preset.py; the validated de25_agilex5_ddr_validator
  -- sibling top has no f2h_irq0 either). Not this ticket's mechanism —
  -- hps_regenerate/hps_emif/export_f2sdram/export_lwh2f render and elaborate
  -- correctly; an HPS-bound IRQ path is a separate, not-yet-landed rr
  -- feature (field-feedback candidate, not a hand-tcl workaround target).
  -- `keep` so Quartus doesn't warn/optimise away a driven-but-unrouted net.
  signal irq_core : std_logic_vector(2 downto 0);
  signal rea_probe : std_logic_vector(31 downto 0);
  signal rea_trigger : std_logic;
  attribute keep : boolean;
  attribute keep of irq_core : signal is true;

  -- RTL-P3.737: f2sdram R-channel keep-alive sink. The DE25 datapath is
  -- WRITE-ONLY toward HPS DDR (deepskopion_core elides the ddr_stream read
  -- plane, DDR-P1.1), so no fabric logic reads the f2sdram read-response
  -- outputs (RID/RDATA/RRESP/RLAST/RVALID) and RREADY is tied low. Quartus
  -- sweeps the whole dangling R path and then fails MPFE hard-macro atom
  -- legality (Error 129015 "Output port F2SDRAM_RDATA ... not legally
  -- connected", Error 24265 "interface f2sdram is not connected") — measured
  -- IDENTICALLY on the frozen v9.0.0 and the version-native v13.0.0
  -- agilex_hps.ip, which exonerates the .ip config. Same cure class as the
  -- DE-H0 F2SOC terminator loop inside hps_subsys: give every R-channel
  -- output a real, preserved consumer. Quiescent in operation (the write-only
  -- core never raises ARVALID, so the HPS never returns read data).
  signal f2s_r_sink_r : std_logic := '0';
  attribute noprune : boolean;
  attribute preserve : boolean;
  attribute noprune of f2s_r_sink_r : signal is true;
  attribute preserve of f2s_r_sink_r : signal is true;
begin

  -- ── Clock / reset derivation (DS-P2.155) ────────────────────────────────
  -- One fabric clock domain at 100 MHz, sourced from a fabric IOPLL that
  -- multiplies the board CLOCK2_50 (50 MHz) by 2. This unifies all three legs
  -- (PolarFire PF-F0, Zynq, DE25) at 100 MHz so the shared-core count
  -- arithmetic is uniform (DS-D.45 parity convergence), and clocks the UART
  -- datalogger at its validated 25 ticks/bit @1 Mbaud (prescaler 4, derived from
  -- G_SYS_CLK_HZ=100 MHz per DS-P2.154) with 10 ns edge timestamps (one clk_sys
  -- tick), half the 20 ns the 50 MHz leg gave. clk_sys drives BOTH the fabric
  -- logic AND hps_subsys's fabric_clk conduit, so the HPS fabric bridges now
  -- truly run at the 100 MHz the ip.yml fabric_clk_mhz/EXPLICIT_CLOCK_RATE
  -- already declares (RTL-P3.1755) — before this ticket that conduit was fed a
  -- bare 50 MHz CLOCK2_50, contradicting the declaration. The u_edge_agg
  -- timing path that fails at 100 MHz is pipelined in the SHARED core by
  -- DS-P2.153, so the DE25 inherits the fix.
  u_fabric_pll : component de25_fabric_pll
    port map (
      refclk   => CLOCK2_50,   -- 50 MHz board oscillator, the PLL reference
      rst      => '0',         -- never reset the PLL from fabric — lock ASAP
      outclk_0 => clk_sys,     -- 100 MHz fabric product clock
      locked   => pll_locked);

  -- DS-P1.80: ninit_done (SDM still configuring) and pll_locked (fabric IOPLL
  -- lock) are asynchronous to clk_sys and to each other. de25_reset_ctrl
  -- synchronises each into clk_sys on its own chain and combines them after the
  -- chains, so rst_sys is held until BOTH report ready. The previous
  -- ninit_done OR (not pll_locked) in front of one chain drove a synchronizer
  -- from two clock domains (Design Assistant CDC-50012).
  --
  -- clk_sys does not toggle until the PLL locks, so before lock every register
  -- in de25_reset_ctrl holds its power-up value and rst_sys stays asserted.
  u_reset : entity work.de25_reset_ctrl
    generic map (G_STAGES => 3)
    port map (
      clk_i        => clk_sys,
      ninit_done_i => ninit_done,
      pll_locked_i => pll_locked,
      rst_o        => rst_sys);

  -- The hps_subsys fabric reset bridge (fabric_reset_reset, active-high) feeds
  -- rst_controller_001, which runs on fabric_clk = clk_sys. It takes rst_sys,
  -- a clk_sys register, so its synchronizer has one source domain as well.
  rst_ddr <= rst_sys;

  u_hps : component hps_subsys
    port map (
      -- DS-P2.155: the HPS fabric bridges now run at the 100 MHz IOPLL product
      -- (clk_sys), matching ip.yml fabric_clk_mhz=100 / EXPLICIT_CLOCK_RATE —
      -- was the bare 50 MHz CLOCK2_50, which halved lwh2f/f2sdram bandwidth and
      -- contradicted the declaration (RTL-P3.1755).
      fabric_clk_clk             => clk_sys,
      fabric_reset_reset        => rst_sys,
      ninit_done_ninit_done     => ninit_done,

      -- ── f2sdram AXI4 slave ← IP m_axi4 master (record→flat shim) ────────
      -- Bridge clock/reset are internal to hps_subsys — not driven here.
      -- Write address (slave in ← master out).
      f2sdram_adapter_axi4_sub_awid     => f2s_awid,
      f2sdram_adapter_axi4_sub_awaddr   => axi_h2c.AWADDR,
      f2sdram_adapter_axi4_sub_awlen    => axi_h2c.AWLEN,
      f2sdram_adapter_axi4_sub_awsize   => axi_h2c.AWSIZE,
      f2sdram_adapter_axi4_sub_awburst  => axi_h2c.AWBURST,
      f2sdram_adapter_axi4_sub_awlock   => axi_h2c.AWLOCK(0),
      f2sdram_adapter_axi4_sub_awcache  => axi_h2c.AWCACHE,
      f2sdram_adapter_axi4_sub_awprot   => axi_h2c.AWPROT,
      f2sdram_adapter_axi4_sub_awqos    => axi_h2c.AWQOS,
      f2sdram_adapter_axi4_sub_awregion => "0000",
      f2sdram_adapter_axi4_sub_awuser   => x"00",
      f2sdram_adapter_axi4_sub_awvalid  => axi_h2c.AWVALID,
      f2sdram_adapter_axi4_sub_awready  => f2s_awready,
      -- Write data (slave in ← master out). AXI4: no WID; WUSER tied 0 — the
      -- adapter forces the HPS-side AxUSER=0xE0 itself (RTL-P3.682).
      f2sdram_adapter_axi4_sub_wdata    => axi_h2c.WDATA(255 downto 0),
      f2sdram_adapter_axi4_sub_wstrb    => axi_h2c.WSTRB(31 downto 0),
      f2sdram_adapter_axi4_sub_wlast    => axi_h2c.WLAST,
      f2sdram_adapter_axi4_sub_wuser    => x"00",
      f2sdram_adapter_axi4_sub_wvalid   => axi_h2c.WVALID,
      f2sdram_adapter_axi4_sub_wready   => f2s_wready,
      -- Write response (slave out → master in).
      f2sdram_adapter_axi4_sub_bid      => f2s_bid,
      f2sdram_adapter_axi4_sub_bresp    => f2s_bresp,
      f2sdram_adapter_axi4_sub_buser    => open,
      f2sdram_adapter_axi4_sub_bvalid   => f2s_bvalid,
      f2sdram_adapter_axi4_sub_bready   => axi_h2c.BREADY,
      -- Read address (slave in ← master out).
      f2sdram_adapter_axi4_sub_arid     => f2s_arid,
      f2sdram_adapter_axi4_sub_araddr   => axi_h2c.ARADDR,
      f2sdram_adapter_axi4_sub_arlen    => axi_h2c.ARLEN,
      f2sdram_adapter_axi4_sub_arsize   => axi_h2c.ARSIZE,
      f2sdram_adapter_axi4_sub_arburst  => axi_h2c.ARBURST,
      f2sdram_adapter_axi4_sub_arlock   => axi_h2c.ARLOCK(0),
      f2sdram_adapter_axi4_sub_arcache  => axi_h2c.ARCACHE,
      f2sdram_adapter_axi4_sub_arprot   => axi_h2c.ARPROT,
      f2sdram_adapter_axi4_sub_arqos    => axi_h2c.ARQOS,
      f2sdram_adapter_axi4_sub_arregion => "0000",
      f2sdram_adapter_axi4_sub_aruser   => x"00",
      f2sdram_adapter_axi4_sub_arvalid  => axi_h2c.ARVALID,
      f2sdram_adapter_axi4_sub_arready  => f2s_arready,
      -- Read data (slave out → master in).
      f2sdram_adapter_axi4_sub_rid      => f2s_rid,
      f2sdram_adapter_axi4_sub_rdata    => f2s_rdata,
      f2sdram_adapter_axi4_sub_rresp    => f2s_rresp,
      f2sdram_adapter_axi4_sub_rlast    => f2s_rlast,
      f2sdram_adapter_axi4_sub_ruser    => open,
      f2sdram_adapter_axi4_sub_rvalid   => f2s_rvalid,
      f2sdram_adapter_axi4_sub_rready   => axi_h2c.RREADY,

      -- ── lwh2f AXI-Lite master → CSR slave (flat wiring) ─────────────────
      -- Bridge clock/reset internal to hps_subsys — not driven here.
      lwhps2fpga_awid           => lw_awid,
      lwhps2fpga_awaddr         => lw_awaddr,
      lwhps2fpga_awlen          => open,   -- single-beat CSR — burst outputs ignored
      lwhps2fpga_awsize         => open,
      lwhps2fpga_awburst        => open,
      lwhps2fpga_awlock         => open,
      lwhps2fpga_awcache        => open,
      lwhps2fpga_awprot         => lw_awprot,
      lwhps2fpga_awvalid        => lw_awvalid,
      lwhps2fpga_awready        => lw_awready,
      lwhps2fpga_wdata          => lw_wdata,
      lwhps2fpga_wstrb          => lw_wstrb,
      lwhps2fpga_wlast          => open,   -- single-beat — WLAST ignored
      lwhps2fpga_wvalid         => lw_wvalid,
      lwhps2fpga_wready         => lw_wready,
      lwhps2fpga_bid            => lw_bid,
      lwhps2fpga_bresp          => lw_bresp,
      lwhps2fpga_bvalid         => lw_bvalid,
      lwhps2fpga_bready         => lw_bready,
      lwhps2fpga_arid           => lw_arid,
      lwhps2fpga_araddr         => lw_araddr,
      lwhps2fpga_arlen          => open,   -- single-beat CSR — burst outputs ignored
      lwhps2fpga_arsize         => open,
      lwhps2fpga_arburst        => open,
      lwhps2fpga_arlock         => open,
      lwhps2fpga_arcache        => open,
      lwhps2fpga_arprot         => lw_arprot,
      lwhps2fpga_arvalid        => lw_arvalid,
      lwhps2fpga_arready        => lw_arready,
      lwhps2fpga_rid            => lw_rid,
      lwhps2fpga_rdata          => lw_rdata,
      lwhps2fpga_rresp          => lw_rresp,
      lwhps2fpga_rlast          => lw_rlast,
      lwhps2fpga_rvalid         => lw_rvalid,
      lwhps2fpga_rready         => lw_rready,

      -- NB: h2f + f2s coherent bridges are looped internally in hps_subsys —
      -- no top ports to wire.

      -- ── HPS DDR4 (io96b EMIF) conduits → board pins (pass-through) ───────
      emif_mem_0_mem_cke(0)        => DDR4_CKE,
      emif_mem_0_mem_odt(0)        => DDR4_ODT,
      emif_mem_0_mem_cs_n(0)       => DDR4_CS_n,
      emif_mem_0_mem_a             => DDR4_A,
      emif_mem_0_mem_ba            => DDR4_BA,
      emif_mem_0_mem_bg            => DDR4_BG,
      emif_mem_0_mem_act_n         => DDR4_ACT_n,
      emif_mem_0_mem_par           => DDR4_PAR,
      emif_mem_0_mem_dq            => DDR4_DQ,
      emif_mem_0_mem_dqs_t         => DDR4_DQS,
      emif_mem_0_mem_dqs_c         => DDR4_DQS_n,
      emif_mem_0_mem_alert_n       => DDR4_ALERT_n,
      emif_mem_0_mem_dbi_n         => DDR4_DBI_n,
      emif_mem_ck_0_mem_ck_t(0)    => DDR4_CK,
      emif_mem_ck_0_mem_ck_c(0)    => DDR4_CK_n,
      emif_mem_reset_n_mem_reset_n => DDR4_RESET_n,
      emif_oct_0_oct_rzqin         => DDR4_RZQ,
      emif_ref_clk_clk             => DDR4_REFCLK_p,

      -- ── HPS IO48 conduit → HPS dedicated board pins (frozen pin-mux) ────
      -- 1:1 pass-through, board-name↔conduit-signal mapping harvested from the
      -- golden GHRD golden_top.v (the GPIO1 assignments are board-specific).
      -- hps_io_uart1_tx → HPS_UART_TX (M124) = the SPL/Linux console.
      hps_io_hps_osc_clk  => HPS_CLK_25,
      hps_io_sdmmc_cclk   => HPS_SD_CLK,
      hps_io_sdmmc_cmd    => HPS_SD_CMD,
      hps_io_sdmmc_data0  => HPS_SD_DATA(0),
      hps_io_sdmmc_data1  => HPS_SD_DATA(1),
      hps_io_sdmmc_data2  => HPS_SD_DATA(2),
      hps_io_sdmmc_data3  => HPS_SD_DATA(3),
      hps_io_uart1_tx     => HPS_UART_TX,
      hps_io_uart1_rx     => HPS_UART_RX,
      hps_io_emac0_tx_clk => HPS_ENET_TX_CLK,
      hps_io_emac0_tx_ctl => HPS_ENET_TX_CTL,
      hps_io_emac0_rx_clk => HPS_ENET_RX_CLK,
      hps_io_emac0_rx_ctl => HPS_ENET_RX_CTL,
      hps_io_emac0_txd0   => HPS_ENET_TX_DATA(0),
      hps_io_emac0_txd1   => HPS_ENET_TX_DATA(1),
      hps_io_emac0_txd2   => HPS_ENET_TX_DATA(2),
      hps_io_emac0_txd3   => HPS_ENET_TX_DATA(3),
      hps_io_emac0_rxd0   => HPS_ENET_RX_DATA(0),
      hps_io_emac0_rxd1   => HPS_ENET_RX_DATA(1),
      hps_io_emac0_rxd2   => HPS_ENET_RX_DATA(2),
      hps_io_emac0_rxd3   => HPS_ENET_RX_DATA(3),
      hps_io_mdio0_mdio   => HPS_ENET_MDIO,
      hps_io_mdio0_mdc    => HPS_ENET_MDC,
      hps_io_usb0_clk     => HPS_USB_CLK,
      hps_io_usb0_stp     => HPS_USB_STP,
      hps_io_usb0_dir     => HPS_USB_DIR,
      hps_io_usb0_nxt     => HPS_USB_NXT,
      hps_io_usb0_data0   => HPS_USB_DATA(0),
      hps_io_usb0_data1   => HPS_USB_DATA(1),
      hps_io_usb0_data2   => HPS_USB_DATA(2),
      hps_io_usb0_data3   => HPS_USB_DATA(3),
      hps_io_usb0_data4   => HPS_USB_DATA(4),
      hps_io_usb0_data5   => HPS_USB_DATA(5),
      hps_io_usb0_data6   => HPS_USB_DATA(6),
      hps_io_usb0_data7   => HPS_USB_DATA(7),
      hps_io_i2c1_sda     => HPS_I2C_SDA,
      hps_io_i2c1_scl     => HPS_I2C_SCL,
      hps_io_spim0_clk    => HPS_LCM_SPIM_CLK,
      hps_io_spim0_mosi   => HPS_LCM_SPIM_MOSI,
      hps_io_spim0_ss0_n  => HPS_LCM_SPIM_SS,
      -- GPIO1 board mapping (golden_top.v): 28→GSENSOR_INT, 32→GPIO0, 33→GPIO1,
      -- 34→LCM_RST_n, 35→LCM_D_C, 40→KEY, 41→LED, 42→LCM_BK.
      hps_io_gpio28       => HPS_GSENSOR_INT,
      hps_io_gpio32       => HPS_GPIO(0),
      hps_io_gpio33       => HPS_GPIO(1),
      hps_io_gpio34       => HPS_LCM_RST_n,
      hps_io_gpio35       => HPS_LCM_D_C,
      hps_io_gpio40       => HPS_KEY,
      hps_io_gpio41       => HPS_LED,
      hps_io_gpio42       => HPS_LCM_BK
    );

  f2s_awid <= std_logic_vector(resize(unsigned(axi_h2c.AWID), 5));
  f2s_arid <= std_logic_vector(resize(unsigned(axi_h2c.ARID), 5));
  axi_c2h.AWREADY <= f2s_awready; axi_c2h.WREADY <= f2s_wready;
  axi_c2h.BID <= f2s_bid(1 downto 0); axi_c2h.BRESP <= f2s_bresp; axi_c2h.BVALID <= f2s_bvalid;
  axi_c2h.ARREADY <= f2s_arready; axi_c2h.RID <= f2s_rid(1 downto 0);
  axi_c2h.RDATA(255 downto 0) <= f2s_rdata;
  axi_c2h.RDATA(rr.ddr_stream_pkg.C_AXI_DATA_WIDTH-1 downto 256) <= (others => '0');
  axi_c2h.RRESP <= f2s_rresp; axi_c2h.RLAST <= f2s_rlast; axi_c2h.RVALID <= f2s_rvalid;

  -- XOR-fold every f2sdram R-channel output into the preserved sink register
  -- so each MPFE read-response port keeps fanout (see declaration comment).
  p_f2s_r_sink : process (clk_sys)
    variable v : std_logic;
  begin
    if rising_edge(clk_sys) then
      v := f2s_rvalid xor f2s_rlast;
      for i in f2s_rdata'range loop
        v := v xor f2s_rdata(i);
      end loop;
      for i in f2s_rresp'range loop
        v := v xor f2s_rresp(i);
      end loop;
      for i in f2s_rid'range loop
        v := v xor f2s_rid(i);
      end loop;
      f2s_r_sink_r <= v;
    end if;
  end process;

  u_lwh2f : entity work.lwh2f_axilite_bridge
    generic map (id_width_g => 4, addr_width_g => 29, lite_addr_g => 18)
    port map (
      clk_i => clk_sys, rst_i => rst_sys,
      s_awid => lw_awid, s_awaddr => lw_awaddr, s_awprot => lw_awprot,
      s_awvalid => lw_awvalid, s_awready => lw_awready,
      s_wdata => lw_wdata, s_wstrb => lw_wstrb, s_wvalid => lw_wvalid, s_wready => lw_wready,
      s_bid => lw_bid, s_bresp => lw_bresp, s_bvalid => lw_bvalid, s_bready => lw_bready,
      s_arid => lw_arid, s_araddr => lw_araddr, s_arprot => lw_arprot,
      s_arvalid => lw_arvalid, s_arready => lw_arready,
      s_rid => lw_rid, s_rdata => lw_rdata, s_rresp => lw_rresp, s_rlast => lw_rlast,
      s_rvalid => lw_rvalid, s_rready => lw_rready,
      m_awaddr => br_awaddr, m_awprot => br_awprot, m_awvalid => br_awvalid, m_awready => br_awready,
      m_wdata => br_wdata, m_wstrb => br_wstrb, m_wvalid => br_wvalid, m_wready => br_wready,
      m_bresp => br_bresp, m_bvalid => br_bvalid, m_bready => br_bready,
      m_araddr => br_araddr, m_arprot => br_arprot, m_arvalid => br_arvalid, m_arready => br_arready,
      m_rdata => br_rdata, m_rresp => br_rresp, m_rvalid => br_rvalid, m_rready => br_rready);

  -- DS-T1.15: enable the AXI completion watchdog on the shipped DE25 SOF.
  -- DS-P2.155: re-derived for the 100 MHz clk_sys. The wedge-timeout is a
  -- WALL-CLOCK bound (~82 us — far above any legal CTU-aperture access of a
  -- handful of cycles), so it must scale with the clock: at 50 MHz that was
  -- 4096 cycles; at 100 MHz the same 81.92 us is 8192 cycles. Left at 4096 the
  -- bound would silently halve to 41 us (still functional, but no longer the
  -- validated aperture-timeout margin). It never trips in normal operation but
  -- bounds the on-silicon read-after-MODE.RST lwh2f wedge so the HPS master can
  -- never be left without a response (the ctucanfd probe then -ETIMEDOUTs to
  -- the daemon instead of hanging the CPU forever).
  u_demux : entity work.axil_demux3
    generic map (wd_cycles_g => 8192)
    port map (clk_i => clk_sys, rst_i => rst_sys,
      s_awaddr_i => br_awaddr, s_awprot_i => br_awprot, s_awvalid_i => br_awvalid, s_awready_o => br_awready,
      s_wdata_i => br_wdata, s_wstrb_i => br_wstrb, s_wvalid_i => br_wvalid, s_wready_o => br_wready,
      s_bresp_o => br_bresp, s_bvalid_o => br_bvalid, s_bready_i => br_bready,
      s_araddr_i => br_araddr, s_arprot_i => br_arprot, s_arvalid_i => br_arvalid, s_arready_o => br_arready,
      s_rdata_o => br_rdata, s_rresp_o => br_rresp, s_rvalid_o => br_rvalid, s_rready_i => br_rready,
      m_awaddr_o => m_awaddr, m_awprot_o => m_awprot, m_awvalid_o => m_awvalid, m_awready_i => m_awready,
      m_wdata_o => m_wdata, m_wstrb_o => m_wstrb, m_wvalid_o => m_wvalid, m_wready_i => m_wready,
      m_bresp_i => m_bresp, m_bvalid_i => m_bvalid, m_bready_o => m_bready,
      m_araddr_o => m_araddr, m_arprot_o => m_arprot, m_arvalid_o => m_arvalid, m_arready_i => m_arready,
      m_rdata_i => m_rdata, m_rresp_i => m_rresp, m_rvalid_i => m_rvalid, m_rready_o => m_rready);

  core_awaddr <= x"0000" & m_awaddr(15 downto 0);
  core_araddr <= x"0000" & m_araddr(15 downto 0);

  u_apb0 : entity work.axil_apb_shim port map (
    clk_i => clk_sys, rst_i => rst_sys,
    s_awaddr_i => x"0000" & m_awaddr(31 downto 16), s_awvalid_i => m_awvalid(1), s_awready_o => m_awready(1),
    s_wdata_i => m_wdata(63 downto 32), s_wstrb_i => m_wstrb(7 downto 4), s_wvalid_i => m_wvalid(1), s_wready_o => m_wready(1),
    s_bresp_o => m_bresp(3 downto 2), s_bvalid_o => m_bvalid(1), s_bready_i => m_bready(1),
    s_araddr_i => x"0000" & m_araddr(31 downto 16), s_arvalid_i => m_arvalid(1), s_arready_o => m_arready(1),
    s_rdata_o => m_rdata(63 downto 32), s_rresp_o => m_rresp(3 downto 2), s_rvalid_o => m_rvalid(1), s_rready_i => m_rready(1),
    paddr_o => apb0_addr, psel_o => apb0_sel, penable_o => apb0_enable, pwrite_o => apb0_write,
    pwdata_o => apb0_wdata, prdata_i => apb0_rdata, pready_i => apb0_ready, pslverr_i => apb0_error);
  u_apb1 : entity work.axil_apb_shim port map (
    clk_i => clk_sys, rst_i => rst_sys,
    s_awaddr_i => x"0000" & m_awaddr(47 downto 32), s_awvalid_i => m_awvalid(2), s_awready_o => m_awready(2),
    s_wdata_i => m_wdata(95 downto 64), s_wstrb_i => m_wstrb(11 downto 8), s_wvalid_i => m_wvalid(2), s_wready_o => m_wready(2),
    s_bresp_o => m_bresp(5 downto 4), s_bvalid_o => m_bvalid(2), s_bready_i => m_bready(2),
    s_araddr_i => x"0000" & m_araddr(47 downto 32), s_arvalid_i => m_arvalid(2), s_arready_o => m_arready(2),
    s_rdata_o => m_rdata(95 downto 64), s_rresp_o => m_rresp(5 downto 4), s_rvalid_o => m_rvalid(2), s_rready_i => m_rready(2),
    paddr_o => apb1_addr, psel_o => apb1_sel, penable_o => apb1_enable, pwrite_o => apb1_write,
    pwdata_o => apb1_wdata, prdata_i => apb1_rdata, pready_i => apb1_ready, pslverr_i => apb1_error);

  u_core : entity tich.deepskopion_core
    -- DS-P2.155: clk_sys is now the 100 MHz IOPLL product (was 50 MHz
    -- CLOCK2_50), so the core's clock-scaled math (datalogger baud, I2C
    -- SCL-stuck-low, UART-datalogger prescaler/edge-timestamp) is derived for
    -- 100 MHz: prescaler 4 => 25 ticks/bit @1 Mbaud, 10 ns edge resolution.
    generic map (G_DDR_BASE_ADDR => x"A0000000", G_DDR_WIN_WIDTH => 28,
                 G_AXI_MAX_BURST => 16, G_DDR_DATA_WIDTH => 256,
                 G_SYS_CLK_HZ => 100_000_000)
    port map (
      clk_sys_i => clk_sys, rst_sys_i => rst_sys, clk_ddr_i => clk_sys, rst_ddr_i => rst_ddr,
      led => LEDR, ja => GPIO_D(7 downto 0), jb => GPIO_D(15 downto 8),
      s_axil_awaddr_i => core_awaddr, s_axil_awprot_i => m_awprot(2 downto 0), s_axil_awvalid_i => m_awvalid(0), s_axil_awready_o => m_awready(0),
      s_axil_wdata_i => m_wdata(31 downto 0), s_axil_wstrb_i => m_wstrb(3 downto 0), s_axil_wvalid_i => m_wvalid(0), s_axil_wready_o => m_wready(0),
      s_axil_bresp_o => m_bresp(1 downto 0), s_axil_bvalid_o => m_bvalid(0), s_axil_bready_i => m_bready(0),
      s_axil_araddr_i => core_araddr, s_axil_arprot_i => m_arprot(2 downto 0), s_axil_arvalid_i => m_arvalid(0), s_axil_arready_o => m_arready(0),
      s_axil_rdata_o => m_rdata(31 downto 0), s_axil_rresp_o => m_rresp(1 downto 0), s_axil_rvalid_o => m_rvalid(0), s_axil_rready_i => m_rready(0),
      canfd0_apb_paddr_i => apb0_addr, canfd0_apb_penable_i => apb0_enable, canfd0_apb_psel_i => apb0_sel, canfd0_apb_pwrite_i => apb0_write,
      canfd0_apb_pwdata_i => apb0_wdata, canfd0_apb_prdata_o => apb0_rdata, canfd0_apb_pready_o => apb0_ready, canfd0_apb_pslverr_o => apb0_error,
      canfd1_apb_paddr_i => apb1_addr, canfd1_apb_penable_i => apb1_enable, canfd1_apb_psel_i => apb1_sel, canfd1_apb_pwrite_i => apb1_write,
      canfd1_apb_pwdata_i => apb1_wdata, canfd1_apb_prdata_o => apb1_rdata, canfd1_apb_pready_o => apb1_ready, canfd1_apb_pslverr_o => apb1_error,
      m_axi_h2c_o => axi_h2c, m_axi_c2h_i => axi_c2h, irq_o => irq_core,
      -- DS-P2.100: fabric-side stimulus generator loopback (was tied-off
      -- constants — DS-P2.95's "regbank-engaged but idle" trap). The Agilex
      -- 5 HPS has no fabric EMIO injection path (unlike Zynq-7 PS7), so
      -- these legs are driven by u_core's own u_stim_gen instead of a real
      -- PS master. emio_spi_miso_*_i/emio_can*_tx_i stay tied — no MISO
      -- drive needed from a master, and CAN0/CAN1 already carry the real
      -- CTU CAN-FD wired-AND self-test bus (untouched by this ticket).
      emio_uart_tx_i => stim_uart_tx_w, emio_uart_rx_o => open,
      emio_i2c_scl_o_i => stim_i2c_scl_o_w, emio_i2c_scl_t_i => stim_i2c_scl_t_w,
      emio_i2c_sda_o_i => stim_i2c_sda_o_w, emio_i2c_sda_t_i => stim_i2c_sda_t_w,
      emio_i2c_scl_i_o => open, emio_i2c_sda_i_o => open,
      emio_spi_sclk_o_i => stim_spi_sclk_w, emio_spi_sclk_t_i => '0',
      emio_spi_mosi_o_i => stim_spi_mosi_w, emio_spi_mosi_t_i => '0',
      emio_spi_miso_o_i => '0', emio_spi_miso_t_i => '1',
      emio_spi_ss_o_i => stim_spi_ss_w, emio_spi_ss_t_i => '0',
      emio_spi_sclk_i_o => open, emio_spi_mosi_i_o => open, emio_spi_miso_i_o => open, emio_spi_ss_i_o => open,
      -- DS-T2.16/DS-T2.18: the EMIO-GPIO leg is the SPI chip-select the
      -- injector and datalogger actually watch (spi0_cs_gpio_w) — drive it
      -- from the SAME stim_spi_ss_w the generator asserts on the native
      -- SS leg above, so both CS witnesses agree.
      emio_gpio_o_i => stim_spi_ss_w, emio_gpio_t_i => '0', emio_gpio_i_o => open,
      emio_can0_tx_i => '1', emio_can1_tx_i => '1', emio_can_rx_o => open,
      stim_uart_tx_o => stim_uart_tx_w,
      stim_i2c_scl_o_o => stim_i2c_scl_o_w, stim_i2c_scl_t_o => stim_i2c_scl_t_w,
      stim_i2c_sda_o_o => stim_i2c_sda_o_w, stim_i2c_sda_t_o => stim_i2c_sda_t_w,
      stim_spi_sclk_o_o => stim_spi_sclk_w, stim_spi_mosi_o_o => stim_spi_mosi_w, stim_spi_ss_o_o => stim_spi_ss_w,
      -- DS-P2.126: Agilex has no MSS fabric peripherals. Bits [11:8] stay 0.
      mss_uart_tx_i => '1', mss_uart_rx_o => open,
      mss_i2c_scl_oe_i => '0', mss_i2c_sda_oe_i => '0',
      mss_i2c_scl_o => open, mss_i2c_sda_o => open,
      mss_spi_clk_i => '0', mss_spi_clk_oe_i => '0',
      mss_spi_do_i => '0', mss_spi_do_oe_i => '0',
      mss_spi_ss_i => '1', mss_spi_ss_oe_i => '0',
      mss_spi_di_o => open,
      mss_can_tx_i => '1', mss_can_rx_o => open,
      rea_probe_o => rea_probe, rea_trigger_i => rea_trigger,
      rea_tap_tck_i => '0', rea_tap_tms_i => '1', rea_tap_tdi_i => '1', rea_tap_tdo_i => '1');

  u_rea : entity rr_rea.rr_rea_intel
    generic map (G_SAMPLE_W => 32, G_DEPTH => 4096, G_TIMESTAMP_W => 0, G_NUM_CHAN => 1, G_CTRL_CHAIN => 1)
    port map (sample_clk_i => clk_sys, sample_rst_i => rst_sys, probe_i => rea_probe, trigger_o => rea_trigger);
end architecture rtl;
