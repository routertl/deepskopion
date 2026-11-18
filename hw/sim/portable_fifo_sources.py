"""Ordered open-logic source closure for cocotb custom-library tests.

RouteRTL's project/package resolver owns normal builds. Cocotb modules that
declare a hand-scoped ``custom_libraries`` closure must also name the package
support units, in dependency order, so focused tests use the same
registry-installed implementation.
"""

from pathlib import Path


def olo_fifo_sources(project_root: Path) -> list[str]:
    base = (
        Path(project_root)
        / "libs"
        / "open-logic"
        / "olo_base_fifo_sync"
        / "src"
        / "base"
        / "vhdl"
    )
    sources = [
        str(base / "olo_base_pkg_array.vhd"),
        str(base / "olo_base_pkg_attribute.vhd"),
        str(base / "olo_base_pkg_math.vhd"),
        str(base / "olo_base_pkg_string.vhd"),
        str(base / "olo_base_ram_sdp.vhd"),
        str(base / "olo_base_fifo_sync.vhd"),
    ]
    missing = [path for path in sources if not Path(path).is_file()]
    if missing:
        raise FileNotFoundError(
            "open-logic FIFO sources are not installed; run the package "
            "resolver so libs/open-logic/olo_base_fifo_sync exists. Missing: "
            + ", ".join(missing)
        )
    return sources
