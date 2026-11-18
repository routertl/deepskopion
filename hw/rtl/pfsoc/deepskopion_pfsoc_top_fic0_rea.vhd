-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-- DS-P2.326: bench top for the PolarFire FIC0 read-path capture. Not a product top;
-- built only by bench/targets/pfsoc_fic0_rea.yml. It wraps the product fabric
-- (deepskopion_pfsoc_fabric, unchanged) with the five dedicated JTAG TAP ports and an
-- on-chip analyzer (routertl/rea via rr_rea_ujtag_local) on the fabric's FIC0 read
-- channel. The analyzer is read over UJTAG, not over FIC0, so a stopped FIC0 path
-- can still be captured. fic0_stall_trigger fires it once a read has been stalled
-- for G_STALL_CYCLES clocks; the core stores only samples with event = 1.
-- The routertl/rea package SDC (rr_rea_scoped_microchip.sdc: the UJTAG UDRCK clock and the
-- JTAG CDC bounds) currently fails Libero's synthesis check (RTL-P2.2439); the bench target
-- does NOT exclude it, so this image is not built until RTL-P2.2439 lands (MC ruling).
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library rr;
use rr.ddr_stream_pkg.all;
library tich;

entity deepskopion_pfsoc_top_fic0_rea is
  generic (
    -- Clocks a read may stay stalled before the analyzer fires (frozen in the PREREG).
    G_STALL_CYCLES : positive := 1_000_000
  );
  port (
    -- On-board 50 MHz fabric oscillator (ref_clk_50mhz, pin R18, HSIO Bank 0 —
    -- PF-P0 §3.9). Feeds the MSS/CCC; the CCC-generated 100 MHz clk_sys is owned
    -- by the SmartDesign, exported back on pfsoc_mss.clk_fabric_o.
    ref_clk_50mhz : in std_logic;

    -- FABRIC user LEDs (led[3:0] = T18/V17/U20/U21, HSIO Bank 0) are NOT ports
    -- of this top until PF-B0: the polarfire-soc-discovery registry emits
    -- LVCMOS33 for pins on the 1.8 V rail (PDCPF-13 abort once the io PDC is
    -- associated with PLACEROUTE), and no per-target io_std override exists in
    -- rr schema — the registry fix is PF-B0's (docs/design/pfsoc_port_plan.md
    -- §1.1 "registry is wrong"). Until then the core's led output is left open
    -- here; a led port with no hardware.pins subscription would be the DE-B0
    -- unplaced-pin class test_every_top_port_is_subscribed exists to refuse.
    -- PF-B0 restores the port AND its subscription together
    -- (LD0 running · LD1 done · LD2 pass · LD3 fail).

    -- 14 capture-source inputs on the 40-pin Raspberry Pi header GPIO (GPIO
    -- Bank 1, LVCMOS33 — PF-P0 §1.1/§3.4.1). datalogger_config.yml slot indices
    -- are unchanged (logical); only the physical subscription moves vs. Zynq PMOD
    -- ja/jb. The MSS-dedicated pins (DDR4, SD, MAC-SGMII, USB, MSS UARTs) are
    -- located by the MSS configuration, but Libero still surfaces them as
    -- auto-promoted pad PORTS that must ride through an HDL root — see the
    -- passthrough block below (DS-P2.152; revises the PF-P0 §3.1 assumption).
    rpi_gpio4  : in std_logic;
    rpi_gpio17 : in std_logic;
    rpi_gpio18 : in std_logic;
    rpi_gpio27 : in std_logic;
    rpi_gpio22 : in std_logic;
    rpi_gpio23 : in std_logic;
    rpi_gpio24 : in std_logic;
    rpi_gpio25 : in std_logic;
    rpi_gpio5  : in std_logic;
    rpi_gpio6  : in std_logic;
    rpi_gpio12 : in std_logic;
    rpi_gpio13 : in std_logic;
    rpi_gpio19 : in std_logic;
    rpi_gpio16 : in std_logic;

    -- DS-P2.157: the VSC8221 SGMII PHY's fabric-side pins (registry names,
    -- reference MPFS_DISCOVERY_MAC.pdc). MAC_0 management is FABRIC in the .cxz,
    -- so MDC/MDIO reach the PHY only through here; the two PHY resets are held
    -- deasserted, as the reference ties VSC_RESETN / VSC_TXDIS_SRESETN to VCC.
    -- Undriven, the PHY never answered MDIO (macb "Could not attach PHY (-19)").
    mdc               : out   std_logic;
    mdio              : inout std_logic;
    phy_rst_n         : out   std_logic;
    phy_txdis_sresetn : out   std_logic;
    -- rpi_gpio26 / rpi_gpio20 (core jb(6)/jb(7)) are NOT ports until PF-B0:
    -- unused top inputs Synplify prunes from the netlist, so a -fixed set_io
    -- on them is a hard PDCPF-01 abort. jb(6)/jb(7) are tied low below; PF-B0
    -- restores the ports AND their subscription together.

    -- ── MSS dedicated-pad passthrough (DS-P2.152) ─────────────────────────────
    -- Libero auto-promotes every MSS PAD pin to a port of the generated
    -- pfsoc_mss SmartDesign (dedicated package balls: DDR4, SD/eMMC, MMUART0,
    -- CAN, I2C, USB, QSPI, SGMII + REFCLK). With an HDL root those ports must
    -- ride through this entity verbatim (the reference design's SmartDesign
    -- root gets the same promotion implicitly, MPFS_DISCOVERY_KIT.tcl
    -- auto_promote_pad_pins). They are dedicated balls — located by the MSS
    -- configuration, never placed from hardware.pins. Names/dirs/types are the
    -- generated component's, verbatim (build .../work/pfsoc_mss/pfsoc_mss.vhd).
    REFCLK                      : in    std_logic;
    REFCLK_N                    : in    std_logic;
    MMUART_4_RXD                : in    std_logic;
    SD_CD                       : in    std_logic;
    SPI_1_DI                    : in    std_logic;
    MMUART_4_TXD                : out   std_logic;
    SD_CLK                      : out   std_logic;
    SD_VOLT_CMD_DIR             : out   std_logic;
    SD_VOLT_DIR_0               : out   std_logic;
    SD_VOLT_DIR_1_3             : out   std_logic;
    SD_VOLT_EN                  : out   std_logic;
    SD_VOLT_SEL                 : out   std_logic;
    SPI_1_DO                    : out   std_logic;
    SD_CMD                      : inout std_logic;
    SD_DATA0                    : inout std_logic;
    SD_DATA1                    : inout std_logic;
    SD_DATA2                    : inout std_logic;
    SD_DATA3                    : inout std_logic;
    SPI_1_CLK                   : inout std_logic;
    SPI_1_SS0                   : inout std_logic;
    SGMII_RX0_N                 : in    std_logic;
    SGMII_RX0_P                 : in    std_logic;
    A                           : out   std_logic_vector(13 downto 0);
    ACT_N                       : out   std_logic;
    BA                          : out   std_logic_vector(1 downto 0);
    BG0                         : out   std_logic;
    CAS_N                       : out   std_logic;
    CK0                         : out   std_logic;
    CK0_N                       : out   std_logic;
    CKE0                        : out   std_logic;
    CS0_N                       : out   std_logic;
    DM                          : out   std_logic_vector(1 downto 0);
    ODT0                        : out   std_logic;
    RAS_N                       : out   std_logic;
    RESET_N                     : out   std_logic;
    SGMII_TX0_N                 : out   std_logic;
    SGMII_TX0_P                 : out   std_logic;
    WE_N                        : out   std_logic;
    DQ                          : inout std_logic_vector(15 downto 0);
    DQS                         : inout std_logic_vector(1 downto 0);
    DQS_N                       : inout std_logic_vector(1 downto 0);
    -- Dedicated JTAG TAP pins for the UJTAG analyzer (rr_rea_ujtag_local): real
    -- top-level ports on the hardwired TAP pads, no user package pins (CMPPF_073).
    jtag_tck                    : in    std_logic;
    jtag_tdi                    : in    std_logic;
    jtag_tms                    : in    std_logic;
    jtag_trstb                  : in    std_logic;
    jtag_tdo                    : out   std_logic
  );
end entity deepskopion_pfsoc_top_fic0_rea;

architecture rtl of deepskopion_pfsoc_top_fic0_rea is

  -- Sample word: see fic0_rea_sampler.vhd (37 bits, bit 36 = event, the storage qualifier).
  constant C_SAMPLE_W : positive := 37;

  signal probe         : std_logic_vector(31 downto 0);
  signal stall         : std_logic;
  signal outstanding   : std_logic;
  signal trig          : std_logic;
  signal heartbeat     : std_logic;
  signal sample_r      : std_logic_vector(C_SAMPLE_W - 1 downto 0);
  signal clk_sys       : std_logic;
  signal rst_sys       : std_logic;

  -- Bound by default binding, with no library clause for the REA library here (as pf_known_answer_top_rea):
  -- a file under hw/rtl that names that library would break every target whose compile set lacks it
  -- (test_pfsoc_rea_single_source_ds_p1_73). The bench target places the wrapper in that library.
  component rr_rea_ujtag_local is
    generic (
      G_SAMPLE_W    : positive := 12;
      G_DEPTH       : positive := 4096;
      G_TIMESTAMP_W : natural  := 32;
      G_NUM_CHAN    : positive := 1;
      G_CTRL_CHAIN  : integer  := 1;
      G_QUAL_CONDS  : natural  := 0
      );
    port (
      sample_clk  : in  std_logic;
      sample_rst  : in  std_logic;
      probe_in    : in  std_logic_vector(G_SAMPLE_W - 1 downto 0);
      trigger_out : out std_logic;
      ext_trigger : in  std_logic := '0';
      jtag_tck    : in  std_logic;
      jtag_tdi    : in  std_logic;
      jtag_tms    : in  std_logic;
      jtag_trstb  : in  std_logic;
      jtag_tdo    : out std_logic
      );
  end component rr_rea_ujtag_local;

begin

  -- The product fabric, unchanged; the probe, its clock and its reset come out.
  u_fabric : entity work.deepskopion_pfsoc_fabric
    port map (
      ref_clk_50mhz     => ref_clk_50mhz,
      rpi_gpio4         => rpi_gpio4,
      rpi_gpio17        => rpi_gpio17,
      rpi_gpio18        => rpi_gpio18,
      rpi_gpio27        => rpi_gpio27,
      rpi_gpio22        => rpi_gpio22,
      rpi_gpio23        => rpi_gpio23,
      rpi_gpio24        => rpi_gpio24,
      rpi_gpio25        => rpi_gpio25,
      rpi_gpio5         => rpi_gpio5,
      rpi_gpio6         => rpi_gpio6,
      rpi_gpio12        => rpi_gpio12,
      rpi_gpio13        => rpi_gpio13,
      rpi_gpio19        => rpi_gpio19,
      rpi_gpio16        => rpi_gpio16,
      mdc               => mdc,
      mdio              => mdio,
      phy_rst_n         => phy_rst_n,
      phy_txdis_sresetn => phy_txdis_sresetn,
      REFCLK            => REFCLK,
      REFCLK_N          => REFCLK_N,
      MMUART_4_RXD      => MMUART_4_RXD,
      SD_CD             => SD_CD,
      SPI_1_DI          => SPI_1_DI,
      MMUART_4_TXD      => MMUART_4_TXD,
      SD_CLK            => SD_CLK,
      SD_VOLT_CMD_DIR   => SD_VOLT_CMD_DIR,
      SD_VOLT_DIR_0     => SD_VOLT_DIR_0,
      SD_VOLT_DIR_1_3   => SD_VOLT_DIR_1_3,
      SD_VOLT_EN        => SD_VOLT_EN,
      SD_VOLT_SEL       => SD_VOLT_SEL,
      SPI_1_DO          => SPI_1_DO,
      SD_CMD            => SD_CMD,
      SD_DATA0          => SD_DATA0,
      SD_DATA1          => SD_DATA1,
      SD_DATA2          => SD_DATA2,
      SD_DATA3          => SD_DATA3,
      SPI_1_CLK         => SPI_1_CLK,
      SPI_1_SS0         => SPI_1_SS0,
      SGMII_RX0_N       => SGMII_RX0_N,
      SGMII_RX0_P       => SGMII_RX0_P,
      A                 => A,
      ACT_N             => ACT_N,
      BA                => BA,
      BG0               => BG0,
      CAS_N             => CAS_N,
      CK0               => CK0,
      CK0_N             => CK0_N,
      CKE0              => CKE0,
      CS0_N             => CS0_N,
      DM                => DM,
      ODT0              => ODT0,
      RAS_N             => RAS_N,
      RESET_N           => RESET_N,
      SGMII_TX0_N       => SGMII_TX0_N,
      SGMII_TX0_P       => SGMII_TX0_P,
      WE_N              => WE_N,
      DQ                => DQ,
      DQS               => DQS,
      DQS_N             => DQS_N,
      fic0_rd_probe_o   => probe,
      clk_sys_o         => clk_sys,
      rst_sys_o         => rst_sys);

  u_trig : entity work.fic0_stall_trigger
    generic map (G_STALL_CYCLES => G_STALL_CYCLES, G_HEARTBEAT_BIT => 9)
    port map (
      clk_i => clk_sys, rst_i => rst_sys,
      arvalid_i => probe(0), arready_i => probe(1), rvalid_i => probe(2), rready_i => probe(3),
      stall_o => stall, outstanding_o => outstanding, trig_o => trig, heartbeat_o => heartbeat);

  u_sampler : entity work.fic0_rea_sampler
    port map (
      clk_i => clk_sys, probe_i => probe, stall_i => stall, outstanding_i => outstanding,
      trig_i => trig, heartbeat_i => heartbeat, sample_o => sample_r);

  u_rea : rr_rea_ujtag_local
    generic map (
      G_SAMPLE_W    => C_SAMPLE_W,
      G_DEPTH       => 4096,
      G_TIMESTAMP_W => 32,
      G_NUM_CHAN    => 1,
      G_CTRL_CHAIN  => 1,
      G_QUAL_CONDS  => 1)
    port map (
      sample_clk  => clk_sys,
      sample_rst  => rst_sys,
      probe_in    => sample_r,
      trigger_out => open,
      ext_trigger => trig,
      jtag_tck    => jtag_tck,
      jtag_tdi    => jtag_tdi,
      jtag_tms    => jtag_tms,
      jtag_trstb  => jtag_trstb,
      jtag_tdo    => jtag_tdo);

end architecture rtl;
