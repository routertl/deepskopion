# rr unknown-llm (AI-generated)
"""DS-P2.216: the Zybo-Z7-20 image must carry the ds_ip its deployment declares.

Measured 2026-11-05 (DS-P2.211): a current Zybo rootfs booted with the
canfd_demo slot's system.dtb printed "network: no DHCP lease". The DTB carried
``bench_ip=``, S40network reads ``ds_ip=``, and the bench link has no DHCP.
``tools/check_bench_ip.py --target zybo`` refuses such an image, ``--print-bootargs``
gives the bootargs for ``rr linux dtb --bootargs``, and
``tools/zybo_linux_image.py compose`` writes them into an existing DTB.

Assertions are on the CLI exit code and output. Fixture addresses are TEST-NET
(RFC 5737), so this module carries no bench identity (DS-C1).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "check_bench_ip.py"
COMPOSE = ROOT / "tools" / "zybo_linux_image.py"

DECLARED_ADDR = "198.51" + ".100.50"
OTHER_ADDR = "198.51" + ".100.51"
# targets/zybo_datalogger.yml linux.bootargs, which names no address (DS-D.39; DS-P1.103: the Zybo product).
BASE_BOOTARGS = ("earlycon=cdns,mmio32,0xe0001000 console=ttyPS1,115200 "
                 "uio_pdrv_genirq.of_id=generic-uio ds_source=ddr")


def _pfsoc_test_module():
    spec = importlib.util.spec_from_file_location(
        "pfsoc_tests", ROOT / "tools" / "tests" / "test_check_bench_ip_pfsoc_p2_125.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_fdt = _pfsoc_test_module()._fdt


def _root(tmp_path: Path, bootargs: str = BASE_BOOTARGS) -> Path:
    root = tmp_path / "root"
    (root / "targets").mkdir(parents=True)
    (root / "targets/zybo_datalogger.yml").write_text(
        yaml.safe_dump({"linux": {"bootargs": bootargs}}), encoding="utf-8")
    return root


def _hw(tmp_path: Path, addr: str, key: str | None = "ds_ip") -> Path:
    target = {"board": "zybo-z7-20", "bench_ip": "zybo_address"}
    if key is not None:
        target["cmdline_ip_key"] = key
    doc = {"bench": {"resources": {"zybo_address": {"kind": "bench_ip", "addr": addr,
                                                    "prefix": 24}},
                     "targets": {"z7_20_primary": target}}}
    path = tmp_path / "hw.yml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _run(tool: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["python3", str(tool), *args], cwd=ROOT, capture_output=True,
                          text=True, check=False,
                          env={**os.environ, "RR_BENCH_CONFIG": "/nonexistent/hw.yml"})
    return proc.returncode, proc.stdout + proc.stderr


def _check(root: Path, hw: Path, *args: str) -> tuple[int, str]:
    return _run(TOOL, "--target", "zybo", "--root", str(root), "--hw", str(hw), *args)


def test_the_dtb_that_failed_on_2026_11_05_is_refused(tmp_path):
    """The slot DTB's shape: bench_ip= and no ds_ip=."""
    root, hw = _root(tmp_path), _hw(tmp_path, DECLARED_ADDR)
    dtb = tmp_path / "slot.dtb"
    dtb.write_bytes(_fdt(f"{BASE_BOOTARGS} bench_ip={DECLARED_ADDR}"))
    rc, out = _check(root, hw, "--dtb", str(dtb))
    assert rc == 1, out
    assert "no ds_ip= token" in out and "Zybo-Z7-20" in out and "DS-P2.216" in out
    assert "DE25" not in out and "PolarFire" not in out, out


def test_a_dtb_with_the_declared_token_is_accepted(tmp_path):
    root, hw = _root(tmp_path), _hw(tmp_path, DECLARED_ADDR)
    dtb = tmp_path / "image.dtb"
    dtb.write_bytes(_fdt(f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24"))
    rc, out = _check(root, hw, "--dtb", str(dtb))
    assert rc == 0 and f"ds_ip={DECLARED_ADDR}/24" in out, out


def test_a_live_cmdline_with_another_address_is_refused(tmp_path):
    root, hw = _root(tmp_path), _hw(tmp_path, OTHER_ADDR)
    live = tmp_path / "cmdline"
    live.write_text(f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24\n", encoding="utf-8")
    rc, out = _check(root, hw, "--cmdline-file", str(live))
    assert rc == 1 and f"ds_ip={OTHER_ADDR}/24" in out, out


def test_the_default_dtb_is_rr_linux_dtb_output(tmp_path):
    rc, out = _check(_root(tmp_path), _hw(tmp_path, DECLARED_ADDR))
    assert rc == 1 and "out/system.dtb" in out, out
    assert "de25" not in out and "pfsoc" not in out, out


def test_print_bootargs_appends_the_declared_token(tmp_path):
    rc, out = _check(_root(tmp_path), _hw(tmp_path, DECLARED_ADDR), "--print-bootargs")
    assert rc == 0, out
    assert out.strip() == f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24"


def test_print_bootargs_refuses_a_bench_target_without_the_key(tmp_path):
    """Today's bench descriptor: z7_20_primary names its bench_ip, no cmdline_ip_key."""
    rc, out = _check(_root(tmp_path), _hw(tmp_path, DECLARED_ADDR, key=None),
                     "--print-bootargs")
    assert rc == 1 and "cmdline_ip_key must be 'ds_ip'" in out, out


def test_a_public_target_carrying_an_address_is_refused(tmp_path):
    root = _root(tmp_path, f"{BASE_BOOTARGS} bench_ip={DECLARED_ADDR}")
    rc, out = _check(root, _hw(tmp_path, DECLARED_ADDR), "--print-bootargs")
    assert rc == 1 and "DS-D.39" in out and "zybo_datalogger.yml" in out, out


def test_compose_writes_a_dtb_the_checker_accepts(tmp_path):
    root, hw = _root(tmp_path), _hw(tmp_path, DECLARED_ADDR)
    base = tmp_path / "slot.dtb"
    base.write_bytes(_fdt(f"{BASE_BOOTARGS} bench_ip={DECLARED_ADDR}"))
    out_dtb = tmp_path / "new" / "system.dtb"
    rc, out = _run(COMPOSE, "compose", "--root", str(root), "--base-dtb", str(base),
                   "--output", str(out_dtb))
    assert rc == 1 and "z7_20_primary" in out, out    # RR_BENCH_CONFIG is unset in _run
    proc = subprocess.run(["python3", str(COMPOSE), "compose", "--root", str(root),
                           "--base-dtb", str(base), "--output", str(out_dtb)],
                          cwd=ROOT, capture_output=True, text=True, check=False,
                          env={**os.environ, "RR_BENCH_CONFIG": str(hw)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rc, out = _check(root, hw, "--dtb", str(out_dtb))
    assert rc == 0, out
    spec = importlib.util.spec_from_file_location("cbi", TOOL)
    cbi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cbi)
    assert cbi.dtb_bootargs(out_dtb) == f"{BASE_BOOTARGS} ds_ip={DECLARED_ADDR}/24"


def test_compose_refuses_a_missing_base(tmp_path):
    rc, out = _run(COMPOSE, "compose", "--root", str(_root(tmp_path)),
                   "--base-dtb", str(tmp_path / "absent.dtb"))
    assert rc == 1 and "base Zybo DTB missing" in out, out
