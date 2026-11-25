# rr unknown-llm (AI-generated)
"""Regression guard for DS-P2.154: the UART datalogger's clock-derived math must
follow the instance's real system clock, not uart_pkg's hard-coded 100 MHz.

The bug: uart_pkg hard-coded C_SYSTEM_CLK_FREQ = 100_000_000 and derived
C_TICK_PRESCALER = clk/(25 * C_UART_MAX_BAUDRATE) = 4 from it, and
uart_datalogger defaulted G_TICK_PRESCALER to that package constant while
datalogger_gather fed its G_SYSTEM_CLK_FREQ a bare 100000000 literal. DS-T1.10
parametrised the gather/glitch-filter path (G_SYS_CLK_HZ) but missed this
package. On the 50 MHz DE25 the sampler therefore ran prescaler 4 ->
12.5 Mticks/s -> 12.5 ticks/bit at 1 Mbaud — exactly the
calibration-never-locked regime uart_pkg's own prescaler rationale documents —
so the DE25 datalogger ran at HALF its validated resolution and its 1 Mbaud
ceiling was not credible.

The semantic tests below elaborate the REAL uart_datalogger entity under GHDL
(the open-logic FIFO internals are stubbed — only elaboration-time constants
matter) and read its GEN_DEBUG_TRACES reports: at 50 MHz the derived prescaler
must be 2, keeping the validated 25 ticks/bit at 1 Mbaud; at the 100 MHz
default nothing may move (prescaler 4, 25 ticks/bit, C_DIST_WIDTH 16 — the
silicon-validated widths). The source-contract tests pin the plumbing the same
way test_core_sys_clk_generic_guard.py does for DS-T1.10.
"""

from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
RTL = ROOT / "hw" / "rtl"
UART_DATALOGGER = RTL / "datalogger" / "uart" / "uart_datalogger.vhd"
UART_PKG = RTL / "pkg" / "uart_pkg.vhd"
GATHER_COPIES = (
    RTL / "datalogger" / "infra" / "datalogger_gather.vhd",
    RTL / "datalogger" / "infra" / "datalogger_gather" / "datalogger_gather.vhd",
)

GHDL = shutil.which("ghdl")

# Analysis order for the tich library (elaboration closure of uart_datalogger).
TICH_SOURCES = (
    "hw/rtl/pkg/datalogger_config_pkg.vhd",
    "hw/rtl/pkg/datalogger_pkg.vhd",
    "hw/rtl/pkg/uart_pkg.vhd",
    "libs/routertl/edge_detector/edge_detector.vhd",
    "libs/routertl/ram_dual_port/ram_dual_port.vhd",
    "hw/rtl/fifo_sync.vhd",
    "hw/rtl/datalogger/infra/config_control/config_control.vhd",
    "hw/rtl/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
    "hw/rtl/datalogger/uart/uart_playback.vhd",
    "hw/rtl/datalogger/uart/uart_baudrate_gen.vhd",
    "hw/rtl/datalogger/uart/uart_stop_finder.vhd",
    "hw/rtl/datalogger/uart/uart_frame_gen.vhd",
    "hw/rtl/datalogger/uart/uart_datalogger.vhd",
)

# Interface-only stand-in for the registry-pinned open-logic FIFO, which the
# package resolver does not install into a bare clone. uart_datalogger's
# clock-derived constants are elaboration-time values; FIFO behaviour is
# irrelevant to them.
OLO_FIFO_STUB = """
library ieee;
use ieee.std_logic_1164.all;
entity olo_base_fifo_sync is
  generic (
    Width_g         : positive;
    Depth_g         : positive;
    AlmFullOn_g     : boolean   := false;
    AlmFullLevel_g  : natural   := 0;
    AlmEmptyOn_g    : boolean   := false;
    AlmEmptyLevel_g : natural   := 0;
    RamStyle_g      : string    := "auto";
    RamBehavior_g   : string    := "RBW";
    ReadyRstState_g : std_logic := '1'
  );
  port (
    Clk       : in  std_logic;
    Rst       : in  std_logic;
    In_Data   : in  std_logic_vector(Width_g-1 downto 0);
    In_Valid  : in  std_logic := '1';
    In_Ready  : out std_logic;
    In_Level  : out std_logic_vector;
    Out_Data  : out std_logic_vector(Width_g-1 downto 0);
    Out_Valid : out std_logic;
    Out_Ready : in  std_logic := '1';
    Out_Level : out std_logic_vector;
    Full      : out std_logic;
    AlmFull   : out std_logic;
    Empty     : out std_logic;
    AlmEmpty  : out std_logic
  );
end entity;
architecture stub of olo_base_fifo_sync is
begin
end architecture;
"""


def _run(cmd, cwd):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"{' '.join(map(str, cmd))}\n--- stdout ---\n{proc.stdout}"
        f"\n--- stderr ---\n{proc.stderr}"
    )
    return proc


@pytest.fixture(scope="module")
def ghdl_workdir(tmp_path_factory):
    """Analyze + elaborate uart_datalogger once for all sim-generic runs."""
    if GHDL is None:
        pytest.skip("ghdl is not on PATH; the semantic elaboration guard "
                    "cannot run in this environment")
    work = tmp_path_factory.mktemp("ds_p2_154_ghdl")
    stub = work / "olo_stub.vhd"
    stub.write_text(OLO_FIFO_STUB)
    _run([GHDL, "-a", "--std=08", "--work=olo", str(stub)], cwd=work)
    sources = [str(ROOT / rel) for rel in TICH_SOURCES]
    _run([GHDL, "-a", "--std=08", "--work=tich", "-P."] + sources, cwd=work)
    _run([GHDL, "-e", "--std=08", "--work=tich", "-P.", "uart_datalogger"],
         cwd=work)
    return work


def _elaborated_reports(workdir, *generics):
    """Run the elaborated entity for one delta cycle; return its debug notes."""
    cmd = [GHDL, "-r", "--std=08", "--work=tich", "-P.", "uart_datalogger"]
    cmd += [f"-g{name}={value}" for name, value in generics]
    cmd.append("--stop-time=1ns")
    proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"ghdl -r failed:\n{out}"
    return out


def _reported(reports, name):
    """Extract 'name = <int>' from the GEN_DEBUG_TRACES notes, or None.

    Word-bounded so the pre-fix 'G_TICK_PRESCALER = 4' note cannot satisfy a
    lookup for the post-fix 'C_PRESCALER' report.
    """
    m = re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)} = (\d+)", reports)
    return int(m.group(1)) if m else None


def test_de25_50mhz_datalogger_keeps_25_ticks_per_bit(ghdl_workdir):
    """THE DS-P2.154 pin: at the DE25's 50 MHz the elaborated datalogger must
    derive prescaler 2 so the sampler keeps the validated 25 ticks/bit at
    1 Mbaud. Pre-fix it inherited uart_pkg's 100 MHz prescaler 4 ->
    12.5 ticks/bit, the documented calibration-never-locked regime."""
    reports = _elaborated_reports(
        ghdl_workdir, ("G_SYSTEM_CLK_FREQ", 50_000_000))
    prescaler = _reported(reports, "C_PRESCALER")
    if prescaler is None:
        # Pre-fix RTL has no clock-derived prescaler at all — only the
        # 100 MHz package default echoed through G_TICK_PRESCALER.
        prescaler = _reported(reports, "G_TICK_PRESCALER")
    assert prescaler is not None, (
        "no prescaler report in the elaborated debug traces:\n" + reports)
    ticks_per_bit = 50_000_000 / prescaler / 1_000_000  # at 1 Mbaud
    assert prescaler == 2 and ticks_per_bit == 25, (
        "DS-P2.154: the DE25 50 MHz datalogger must keep 25 ticks/bit at 1 Mbaud "
        f"(validated resolution); elaborated prescaler {prescaler} gives "
        f"{ticks_per_bit} ticks/bit — the calibration-never-locked regime"
    )


def test_100mhz_default_is_unchanged(ghdl_workdir):
    """Back-compat pin: the validated 100 MHz build must not move — prescaler
    4, 25 ticks/bit, and the silicon-proven C_DIST_WIDTH of 16."""
    reports = _elaborated_reports(ghdl_workdir)
    prescaler = _reported(reports, "C_PRESCALER") or _reported(
        reports, "G_TICK_PRESCALER")
    assert prescaler == 4, (
        f"100 MHz default prescaler moved: {prescaler} != 4\n{reports}")
    assert _reported(reports, "C_DIST_WIDTH") == 16, (
        "100 MHz C_DIST_WIDTH moved from the validated 16:\n" + reports)


def test_explicit_prescaler_override_still_wins(ghdl_workdir):
    """The prescaler-sweep tests drive G_TICK_PRESCALER explicitly; an
    explicit value (> 0) must override the clock derivation."""
    reports = _elaborated_reports(ghdl_workdir, ("G_TICK_PRESCALER", 16))
    prescaler = _reported(reports, "C_PRESCALER") or _reported(
        reports, "G_TICK_PRESCALER")
    assert prescaler == 16, (
        f"explicit G_TICK_PRESCALER=16 not honoured: got {prescaler}\n{reports}")


def test_gather_threads_system_clk_to_uart_datalogger():
    """DS-T1.10-style plumbing pin: both datalogger_gather copies must feed
    the uart_datalogger's G_SYSTEM_CLK_FREQ from G_SYS_CLK, never a literal."""
    for path in GATHER_COPIES:
        source = path.read_text()
        m = re.search(
            r"i_uart_datalogger\s*:\s*entity\b.*?generic map\s*\((.*?)\)\s*port map",
            source, re.DOTALL | re.IGNORECASE)
        assert m, f"i_uart_datalogger instance not found in {path}"
        seam = m.group(1)
        assert re.search(r"G_SYSTEM_CLK_FREQ\s*=>\s*G_SYS_CLK", seam), (
            f"DS-P2.154: {path.name} must thread G_SYSTEM_CLK_FREQ => G_SYS_CLK"
        )
        assert not re.search(r"G_SYSTEM_CLK_FREQ\s*=>\s*\d", seam), (
            f"DS-P2.154 regression: {path} feeds uart_datalogger's "
            "G_SYSTEM_CLK_FREQ a bare literal"
        )


def test_uart_datalogger_derives_prescaler_from_its_clock_generic():
    """The datalogger must resolve its prescaler from G_SYSTEM_CLK_FREQ (with
    0 = derive), not default it to uart_pkg's 100 MHz C_TICK_PRESCALER."""
    source = UART_DATALOGGER.read_text()
    assert re.search(
        r"f_resolve_prescaler\s*\(\s*G_TICK_PRESCALER\s*,\s*G_SYSTEM_CLK_FREQ\s*\)",
        source), (
        "DS-P2.154: uart_datalogger must derive its prescaler via "
        "f_resolve_prescaler(G_TICK_PRESCALER, G_SYSTEM_CLK_FREQ)"
    )
    assert not re.search(
        r"G_TICK_PRESCALER\s*:\s*integer\s*:=\s*C_TICK_PRESCALER", source), (
        "DS-P2.154 regression: G_TICK_PRESCALER defaults to the 100 MHz "
        "package constant again"
    )


def test_uart_pkg_exposes_clock_parametrised_derivations():
    """uart_pkg must offer the clk-parametrised functions; the legacy 100 MHz
    constants may remain only as reference defaults."""
    source = UART_PKG.read_text()
    for fn in ("f_tick_prescaler", "f_resolve_prescaler",
               "f_max_bitwidth_uart_cycles"):
        assert re.search(rf"function\s+{fn}\s*\(", source), (
            f"DS-P2.154: uart_pkg must declare {fn}(clk_hz ...)"
        )
