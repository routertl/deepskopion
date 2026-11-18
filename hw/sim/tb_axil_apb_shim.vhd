library ieee;
use ieee.std_logic_1164.all;
use std.env.all;

entity tb_axil_apb_shim is end entity;
architecture test of tb_axil_apb_shim is
  signal clk, rst : std_logic := '0';
  signal awvalid, awready, wvalid, wready, bvalid, bready : std_logic := '0';
  signal arvalid, arready, rvalid, rready : std_logic := '0';
  signal psel, penable, pwrite, pready, pslverr : std_logic := '0';
  signal awaddr, araddr, wdata, rdata, paddr, pwdata, prdata : std_logic_vector(31 downto 0) := (others => '0');
  signal wstrb : std_logic_vector(3 downto 0) := (others => '0');
  signal bresp, rresp : std_logic_vector(1 downto 0);
begin
  clk <= not clk after 5 ns;
  dut: entity work.axil_apb_shim port map (
    clk, rst, awaddr, awvalid, awready, wdata, wstrb, wvalid, wready,
    bresp, bvalid, bready, araddr, arvalid, arready, rdata, rresp,
    rvalid, rready, paddr, psel, penable, pwrite, pwdata, prdata,
    pready, pslverr);

  stimulus: process
  begin
    rst <= '1'; wait until rising_edge(clk); wait until rising_edge(clk); rst <= '0';
    awaddr <= x"00010024"; awvalid <= '1'; wait until rising_edge(clk); awvalid <= '0';
    wait until rising_edge(clk); assert psel = '0' report "APB launched before W" severity failure;
    wdata <= x"CAFE1234"; wstrb <= "1111"; wvalid <= '1'; wait until rising_edge(clk); wvalid <= '0';
    wait until rising_edge(clk); wait for 1 ns; assert psel = '1' and penable = '0' report "missing APB setup" severity failure;
    wait until rising_edge(clk); wait for 1 ns; assert psel = '1' and penable = '1' and pwrite = '1' report "missing APB access" severity failure;
    assert paddr = x"00010024" and pwdata = x"CAFE1234" report "APB payload changed" severity failure;
    wait until rising_edge(clk); wait for 1 ns; assert psel = '1' and penable = '1' report "stalled APB access not held" severity failure;
    pready <= '1'; wait until rising_edge(clk); pready <= '0'; bready <= '1';
    wait for 1 ns; assert bvalid = '1' and bresp = "00" report "successful write response missing" severity failure;
    wait until rising_edge(clk); bready <= '0'; wait until rising_edge(clk);
    awaddr <= x"00010028"; wstrb <= "0011"; awvalid <= '1'; wvalid <= '1';
    wait until rising_edge(clk); awvalid <= '0'; wvalid <= '0';
    wait until rising_edge(clk); wait until rising_edge(clk); wait for 1 ns;
    assert psel = '0' and bvalid = '1' and bresp = "10" report "partial write was not refused" severity failure;
    report "DE25_APB_SHIM_PASS" severity note;
    stop;
  end process;
end architecture;
