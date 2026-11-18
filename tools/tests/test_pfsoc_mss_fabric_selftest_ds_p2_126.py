"""DS-P2.126 — PolarFire MSS fabric peripherals are a selectable logger source.

The frozen MSS already routes MMUART_1 / I2C_0 / SPI_0 / CAN_0 to the fabric.
This guard fails when those pins are tied off again, when EMIO_MUX_SEL
bits [11:8] no longer steer them (deleted assignment, or ``when sel = '0'``
polarity), when a pfsoc arm selects a logger it does not drive (CAN bit 11,
or I2C/SPI MSS bits with no device node), when the UART payload never leaves
``/dev/ttyS1``, or when the stimulus payloads for the protocols that stay on
``ds_stimulus_gen`` are deleted.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "hw/rtl/deepskopion_core.vhd"
FABRIC = ROOT / "hw/rtl/pfsoc/deepskopion_pfsoc_fabric.vhd"
TCL = ROOT / "hw/libero/pfsoc/pfsoc_mss_subsys.tcl"
README = ROOT / "README.md"
SELF_TEST_SVG = ROOT / "docs" / "images" / "self_test.svg"
CONTRACT = ROOT / "sw/stream/stimulus_contract.py"  # moved out of testbench, DS-P2.136
TARGET = ROOT / "targets/pfsoc_datalogger.yml"
INIT = ROOT / "sw/linux/rootfs_overlay/etc/init.d/S99deepskopion-stream"

_MISS = "MSS fabric self-test drives no logger traffic"

# Full concurrent assignments. A port name such as mss_i2c_scl_oe_i survives
# on the entity after the steer is deleted, and ``when mss_uart_sel`` survives
# a polarity flip to ``= '0'``. Only the assignment the simulator executes
# catches both mutants.
_STEERS = (
    "mss_i2c_sel  <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(8);",
    "mss_uart_sel <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(9);",
    "mss_spi_sel  <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(10);",
    "mss_can_sel  <= regs_out_local(C_INDEX_EMIO_MUX_SEL/4).data(11);",
    "emio_uart_tx_w    <= stim_uart_tx_w   when stim_uart_on = '1' else emio_uart_tx_i;",
    "emio_i2c_scl_t_w  <= stim_i2c_scl_t_w when stim_i2c_on = '1' else emio_i2c_scl_t_i;",
    "emio_i2c_sda_t_w  <= stim_i2c_sda_t_w when stim_i2c_on = '1' else emio_i2c_sda_t_i;",
    "emio_spi_sclk_o_w <= stim_spi_sclk_w  when stim_spi_on = '1' else emio_spi_sclk_o_i;",
    "emio_spi_mosi_o_w <= stim_spi_mosi_w  when stim_spi_on = '1' else emio_spi_mosi_o_i;",
    "emio_spi_ss_o_w   <= stim_spi_ss_w    when stim_spi_on = '1' else emio_spi_ss_o_i;",
    "emio_gpio_o_w     <= stim_spi_ss_w    when stim_spi_on = '1' else emio_gpio_o_i;",
    "uart0_tx_w <= mss_uart_tx_i when mss_uart_sel = '1' else emio_uart_tx_w;",
    "i2c0_scl_t_w <= (not mss_i2c_scl_oe_i) when mss_i2c_sel = '1' else emio_i2c_scl_t_w;",
    "i2c0_sda_t_w <= (not mss_i2c_sda_oe_i) when mss_i2c_sel = '1' else emio_i2c_sda_t_w;",
    "spi0_sclk_o_w <= mss_spi_clk_w when mss_spi_sel = '1' else emio_spi_sclk_o_w;",
    "spi0_mosi_o_w <= mss_spi_do_w when mss_spi_sel = '1' else emio_spi_mosi_o_w;",
    "spi0_ss_o_w <= mss_spi_ss_w when mss_spi_sel = '1' else emio_spi_ss_o_w;",
    "gpio_o_w(0) <= mss_spi_ss_w when mss_spi_sel = '1' else emio_gpio_o_w;",
    "can0_phy_tx_w <= mss_can_tx_i when mss_can_sel = '1' else emio_can0_tx_i;",
)


class _Rec:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []
        self.value = 0

    def read_u32(self, _offset: int) -> int:
        return self.value

    def write_u32(self, offset: int, value: int) -> None:
        self.writes.append((offset, value))
        self.value = value


def _muxes(writes: list[tuple[int, int]]) -> list[int]:
    from sw.stream.testbench.de_c0_stimulus_contract import EMIO_MUX_SEL
    return [v for o, v in writes if o == EMIO_MUX_SEL]


def test_mss_fabric_source_select_reaches_the_datalogger_inputs(monkeypatch) -> None:
    """MSS fabric source-select is not decoded onto the datalogger inputs."""
    core = CORE.read_text(encoding="utf-8")
    for steer in _STEERS:
        assert steer in core, f"{_MISS} ({steer})"

    top = FABRIC.read_text(encoding="utf-8")
    assert "mmuart1_txd_o   => mss_uart_tx" in top
    assert "mss_uart_tx_i => mss_uart_tx" in top
    assert "i2c0_scl_oe_o   => mss_i2c_scl_oe" in top
    assert "spi0_di_i       => mss_spi_di" in top
    assert "can0_tx_o       => mss_can_tx" in top

    tcl = TCL.read_text(encoding="utf-8")
    for row in (
        "pf_wire mmuart1_txd_o OUT 1 MMUART_1_TXD_M2F",
        "pf_wire mmuart1_rxd_i IN  1 MMUART_1_RXD_F2M",
        "pf_wire i2c0_scl_oe_o OUT 1 I2C_0_SCL_OE_M2F",
        "pf_wire i2c0_sda_oe_o OUT 1 I2C_0_SDA_OE_M2F",
        "pf_wire spi0_clk_o    OUT 1 SPI_0_CLK_M2F",
        "pf_wire spi0_do_o     OUT 1 SPI_0_DO_M2F",
        "pf_wire spi0_ss_o     OUT 1 SPI_0_SS1_M2F",
        "pf_wire spi0_di_i     IN  1 SPI_0_DI_F2M",
        "pf_wire can0_tx_o     OUT 1 CAN_0_TXBUS_M2F",
        "pf_wire can0_rx_i     IN  1 CAN_0_RXBUS_F2M",
    ):
        assert row in tcl, (
            "MSS fabric source-select is not decoded onto the datalogger inputs"
            f" (builder missing {row})"
        )
    tied = "\n".join(
        ln for ln in tcl.splitlines() if "sd_connect_pins_to_constant" in ln or "u_mss:" in ln
    )
    for pin in (
        "MMUART_1_RXD_F2M",
        "I2C_0_SCL_F2M",
        "I2C_0_SDA_F2M",
        "SPI_0_DI_F2M",
        "CAN_0_RXBUS_F2M",
    ):
        assert pin not in tied, (
            "MSS fabric source-select is not decoded onto the datalogger inputs"
            f" ({pin} is still tied to a constant)"
        )

    readme = README.read_text(encoding="utf-8")
    # DS-P1.68: the self-test diagram is an SVG; the PolarFire panel lives there.
    assert "Microchip PolarFire SoC Discovery Kit" in SELF_TEST_SVG.read_text(encoding="utf-8")
    assert "self_test.svg" in readme
    assert "EMIO_MUX_SEL" in readme and "bits [11:8]" in readme
    assert "ds_stimulus_gen.vhd" in readme

    contract = CONTRACT.read_text(encoding="utf-8")
    assert re.search(
        r"EMIO_MUX_MSS_I2C, EMIO_MUX_MSS_UART, EMIO_MUX_MSS_SPI, EMIO_MUX_MSS_CAN = \(\s*"
        r"1 << 8, 1 << 9, 1 << 10, 1 << 11,",
        contract,
    ), "daemon STIM/mux contract is missing the MSS source-select bits"

    # Callers a witness actually runs. Selecting an MSS bit without a matching
    # transmit leaves the logger on a pin that never toggles. I2C/SPI stay on
    # STIM_* because this image has no device node for them. CAN bit 11 stays
    # clear so the recessive tie is what the CAN logger samples.
    import pytest

    from sw.stream.arm_datalogger_defaults import arm as boot_arm
    from sw.stream.testbench import de_c0_production_armer as armer
    # DS-P2.136: the contract moved to production; transmit_mmuart1 resolves
    # _open_mmuart there, so that is the module to patch.
    from sw.stream import stimulus_contract as contract_mod
    from sw.stream.testbench.de_c0_stimulus_contract import (
        EMIO_MUX_EDGE, EMIO_MUX_I2C, EMIO_MUX_SPI, EMIO_MUX_UART,
        EMIO_MUX_MSS_CAN, EMIO_MUX_MSS_UART, I2C_REGISTER_SEQUENCE,
        SPI_PAYLOAD, STIM_I2C_DATA, STIM_SPI_DATA, STIM_UART_DATA,
        UART_PAYLOAD,
    )
    mmuart_dev = getattr(contract_mod, "MMUART1_DEV", "/dev/ttyS1")
    from sw.stream.testbench.emio_inject import RegbankExt

    tty_writes: list[tuple[str, bytes]] = []

    class _Tty:
        def __init__(self) -> None:
            self.buf = bytearray()

        def write(self, data: bytes) -> int:
            self.buf += data
            return len(data)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

        def fileno(self) -> int:
            raise OSError("not a tty")

    def _open_tty(path: str, mode: str = "wb"):
        held = _Tty()
        real_write = held.write

        def _write(data: bytes) -> int:
            tty_writes.append((path, bytes(data)))
            return real_write(data)

        held.write = _write  # type: ignore[method-assign]
        return held

    monkeypatch.setattr(contract_mod, "_open_mmuart", _open_tty, raising=False)
    monkeypatch.setenv("DS_MSS_FABRIC", "1")

    uart = _Rec()
    armer.arm(uart, "uart")
    assert _muxes(uart.writes) == [EMIO_MUX_UART | EMIO_MUX_MSS_UART], (
        f"{_MISS} (armer uart wrote {_muxes(uart.writes)!r})"
    )
    assert [v for o, v in uart.writes if o == STIM_UART_DATA] == [], (
        f"{_MISS} (MSS UART arm still writes STIM_UART_DATA; the logger is on MMUART1)"
    )
    assert tty_writes == [(mmuart_dev, bytes(UART_PAYLOAD))], (
        f"{_MISS} (MMUART1 never transmits, saw {tty_writes!r})"
    )
    tty_writes.clear()

    i2c = _Rec()
    armer.arm(i2c, "i2c")
    assert _muxes(i2c.writes) == [EMIO_MUX_I2C], (
        f"{_MISS} (armer i2c wrote {_muxes(i2c.writes)!r}; MSS I2C is not drivable)"
    )
    assert (EMIO_MUX_MSS_CAN & _muxes(i2c.writes)[0]) == 0
    i2c_tail = i2c.writes[-len(I2C_REGISTER_SEQUENCE):]
    assert i2c_tail == list(I2C_REGISTER_SEQUENCE), (
        f"{_MISS} (STIM I2C payload writes deleted: {i2c_tail!r})"
    )
    assert any(o == STIM_I2C_DATA for o, _v in i2c.writes), (
        f"{_MISS} (STIM_I2C_DATA payload write deleted)"
    )

    spi = _Rec()
    armer.arm(spi, "spi")
    assert _muxes(spi.writes) == [EMIO_MUX_SPI], (
        f"{_MISS} (armer spi wrote {_muxes(spi.writes)!r}; MSS SPI is not drivable)"
    )
    assert [v for o, v in spi.writes if o == STIM_SPI_DATA] == SPI_PAYLOAD, (
        f"{_MISS} (STIM_SPI_DATA payload writes deleted)"
    )

    with pytest.raises(SystemExit):
        armer.arm(_Rec(), "can")

    edge = _Rec()
    armer.arm(edge, "edge")
    assert _muxes(edge.writes) == [EMIO_MUX_EDGE], (
        f"{_MISS} (edge has no MSS source, wrote {_muxes(edge.writes)!r})"
    )

    boot = _Rec()
    boot_arm(boot, sleep=lambda _s: None,
             config_hex=ROOT / 'hw/rtl/regbank/config_schema.hex')
    want_boot = EMIO_MUX_UART | EMIO_MUX_MSS_UART
    assert _muxes(boot.writes) == [want_boot], (
        f"{_MISS} (boot arm wrote {_muxes(boot.writes)!r}; "
        "CAN bit 11 and undriven I2C/SPI MSS bits must stay clear)"
    )
    assert (want_boot & EMIO_MUX_MSS_CAN) == 0
    assert tty_writes == [(mmuart_dev, bytes(UART_PAYLOAD))], (
        f"{_MISS} (boot arm: MMUART1 never transmits, saw {tty_writes!r})"
    )
    tty_writes.clear()

    injected = _Rec()
    RegbankExt(injected).set_emio_mux(uart=True, i2c=True, spi=True)
    want_inject = EMIO_MUX_I2C | EMIO_MUX_UART | EMIO_MUX_SPI | EMIO_MUX_MSS_UART
    assert _muxes(injected.writes) == [want_inject], (
        f"{_MISS} (set_emio_mux wrote {_muxes(injected.writes)!r}; "
        "bit 11 must stay clear)"
    )
    assert (_muxes(injected.writes)[0] & EMIO_MUX_MSS_CAN) == 0, (
        f"{_MISS} (set_emio_mux ORed CAN bit 11)"
    )
    assert tty_writes == [(mmuart_dev, bytes(UART_PAYLOAD))], (
        f"{_MISS} (set_emio_mux: MMUART1 never transmits, saw {tty_writes!r})"
    )

    monkeypatch.setenv("DS_MSS_FABRIC", "0")
    tty_writes.clear()
    legacy = _Rec()
    armer.arm(legacy, "uart")
    assert _muxes(legacy.writes) == [EMIO_MUX_UART], (
        f"{_MISS} (Zybo/DE25 armer must keep the legacy mux, "
        f"wrote {_muxes(legacy.writes)!r})"
    )
    assert [v for o, v in legacy.writes if o == STIM_UART_DATA] == UART_PAYLOAD, (
        f"{_MISS} (STIM_UART_DATA payload writes deleted)"
    )
    assert tty_writes == [], (
        f"{_MISS} (legacy arm opened MMUART1: {tty_writes!r})"
    )

    yml = TARGET.read_text(encoding="utf-8")
    assert "ds_mss_fabric=1" in yml, f"{_MISS} (pfsoc bootargs omit ds_mss_fabric=1)"
    assert 'serial1: "/soc/serial@20100000"' in yml, (
        f"{_MISS} (fabric MMUART has no serial1 alias)"
    )
    # The node is enabled as ttyS1. serial0 stays on the FlashPro5 UART so
    # ttyS0 remains the console. serial4 must not point at either node.
    assert 'serial4: "/soc/serial@20100000"' not in yml, (
        f"{_MISS} (serial4 still targets the fabric MMUART)"
    )
    assert re.search(
        r"path: /soc/serial@20100000\n    properties:\n(?:.*\n)*?      status: \"okay\"",
        yml,
    ), f"{_MISS} (/soc/serial@20100000 is not status okay)"
    assert "is `disabled` so neither steals line 0" not in yml, (
        f"{_MISS} (bootargs comment still says MMUART1 is disabled)"
    )
    assert "ttyS0 stays the console" in yml, (
        f"{_MISS} (target never states how ttyS0 keeps the console)"
    )

    init = INIT.read_text(encoding="utf-8")
    assert "DS_MSS_FABRIC" in init and "ds_mss_fabric" in init, (
        f"{_MISS} (boot init never exports DS_MSS_FABRIC)"
    )
    arm_at = init.index("sw.stream.arm_datalogger_defaults")
    export_at = init.index("DS_MSS_FABRIC")
    assert export_at < arm_at, (
        f"{_MISS} (DS_MSS_FABRIC is exported after the boot STIM arm)"
    )
