-- rr unknown-llm (AI-generated)
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.env.all;

entity tb_axil_demux3 is end entity;
architecture test of tb_axil_demux3 is
  signal clk, rst : std_logic := '0';
  signal awaddr, araddr : std_logic_vector(17 downto 0) := (others => '0');
  signal awprot, arprot : std_logic_vector(2 downto 0) := (others => '0');
  signal awvalid, awready, wvalid, wready, bvalid, bready : std_logic := '0';
  signal arvalid, arready, rvalid, rready : std_logic := '0';
  signal wdata, rdata : std_logic_vector(31 downto 0) := (others => '0');
  signal wstrb : std_logic_vector(3 downto 0) := "1111";
  signal bresp, rresp : std_logic_vector(1 downto 0);
  signal mawaddr, maraddr : std_logic_vector(47 downto 0);
  signal mawprot, marprot : std_logic_vector(8 downto 0);
  signal mawvalid, mawready, mwvalid, mwready, mbvalid, mbready : std_logic_vector(2 downto 0) := (others => '0');
  signal mwdata, mrdata : std_logic_vector(95 downto 0) := (others => '0');
  signal mwstrb : std_logic_vector(11 downto 0);
  signal mbresp, mrresp : std_logic_vector(5 downto 0) := (others => '0');
  signal marvalid, marready, mrvalid, mrready : std_logic_vector(2 downto 0) := (others => '0');
  -- DS-T1.7 regression guard: slave BVALID latency, exercised 0/1/5 cycles.
  constant wr_delays : integer_vector(0 to 2) := (0, 1, 5);
begin
  clk <= not clk after 5 ns;
  dut: entity work.axil_demux3 port map (
    clk, rst, awaddr, awprot, awvalid, awready, wdata, wstrb, wvalid, wready,
    bresp, bvalid, bready, araddr, arprot, arvalid, arready, rdata, rresp,
    rvalid, rready, mawaddr, mawprot, mawvalid, mawready, mwdata, mwstrb,
    mwvalid, mwready, mbresp, mbvalid, mbready, maraddr, marprot, marvalid,
    marready, mrdata, mrresp, mrvalid, mrready);
  stimulus: process
  begin
    rst <= '1'; wait until rising_edge(clk); wait until rising_edge(clk); rst <= '0';
    for n in 0 to 2 loop
      araddr <= (others => '0'); araddr(17 downto 16) <= std_logic_vector(to_unsigned(n, 2));
      arvalid <= '1'; wait until rising_edge(clk); arvalid <= '0';
      wait for 1 ns; assert marvalid = std_logic_vector(to_unsigned(2**n, 3)) report "read decode not one-hot" severity failure;
      marready(n) <= '1'; wait until rising_edge(clk); marready(n) <= '0';
      mrdata(32*n+31 downto 32*n) <= x"12345678"; mrvalid(n) <= '1'; rready <= '1';
      wait for 1 ns; assert rvalid = '1' and rdata = x"12345678" report "read response not returned" severity failure;
      wait until rising_edge(clk); mrvalid(n) <= '0'; rready <= '0'; wait until rising_edge(clk);
    end loop;
    araddr <= "11" & x"0000"; arvalid <= '1'; wait until rising_edge(clk); arvalid <= '0';
    wait for 1 ns; assert marvalid = "000" and rvalid = '1' and rresp = "11" report "reserved window did not DECERR" severity failure;
    rready <= '1'; wait until rising_edge(clk); rready <= '0'; wait until rising_edge(clk);

    -- DS-T1.7: write to every slave with BVALID delayed by 0/1/5 cycles.
    -- s_bvalid_o must never rise before the SELECTED slave asserts
    -- m_bvalid_i(wr_sel_r) (the DE25 CTU CAN write-path lockup was an
    -- early/ungated BVALID); and the write channel must remain usable
    -- afterwards, proven by a follow-up read succeeding.
    for n in 0 to 2 loop
      for di in wr_delays'range loop
        awaddr <= (others => '0'); awaddr(17 downto 16) <= std_logic_vector(to_unsigned(n, 2));
        wdata <= x"CAFEBABE"; awvalid <= '1'; wvalid <= '1';
        wait until rising_edge(clk);
        awvalid <= '0'; wvalid <= '0';
        wait until mawvalid(n) = '1' and mwvalid(n) = '1';
        mawready(n) <= '1'; mwready(n) <= '1';
        wait until rising_edge(clk);
        mawready(n) <= '0'; mwready(n) <= '0';
        bready <= '1';
        for k in 0 to wr_delays(di) loop
          wait until rising_edge(clk);
          wait for 1 ns;
          assert bvalid = '0'
            report "BVALID asserted before slave " & integer'image(n) &
                   " responded (early/ungated BVALID, delay=" &
                   integer'image(wr_delays(di)) & ")"
            severity failure;
        end loop;
        -- s_bvalid_o is combinational on m_bvalid_i: it must rise in THIS
        -- delta, the same cycle the slave asserts, not a clock late.
        mbresp(2*n+1 downto 2*n) <= "00"; mbvalid(n) <= '1';
        wait for 1 ns;
        assert bvalid = '1'
          report "write response not returned for slave " & integer'image(n)
          severity failure;
        wait until rising_edge(clk);
        mbvalid(n) <= '0'; bready <= '0';
        wait until rising_edge(clk);

        araddr <= (others => '0'); araddr(17 downto 16) <= std_logic_vector(to_unsigned(n, 2));
        arvalid <= '1'; wait until rising_edge(clk); arvalid <= '0';
        wait for 1 ns;
        assert marvalid(n) = '1'
          report "post-write read did not decode (write channel wedged), slave " &
                 integer'image(n)
          severity failure;
        marready(n) <= '1'; wait until rising_edge(clk); marready(n) <= '0';
        mrdata(32*n+31 downto 32*n) <= x"87654321"; mrvalid(n) <= '1'; rready <= '1';
        wait for 1 ns;
        assert rvalid = '1' and rdata = x"87654321"
          report "post-write read failed (write channel wedged), slave " &
                 integer'image(n)
          severity failure;
        wait until rising_edge(clk); mrvalid(n) <= '0'; rready <= '0'; wait until rising_edge(clk);
      end loop;
    end loop;

    report "DE25_AXIL_DEMUX_PASS" severity note;
    stop;
  end process;
end architecture;
