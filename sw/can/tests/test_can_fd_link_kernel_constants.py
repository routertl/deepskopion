# rr unknown-llm (AI-generated)
"""DS-T2.91: can_fd_link's kernel constants and struct layouts, checked against the real
uapi headers by a C compiler, not against a hand transcription.

DS-P2.226 shipped IFLA_CAN_DATA_BITTIMING = 10. In <linux/can/netlink.h> 10 is
IFLA_CAN_DATA_BITTIMING_CONST; the data bit timing is 9. Its policy length is
sizeof(struct can_bittiming_const), so the 32-byte attribute was shorter than
the policy and lib/nlattr.c rejected the whole RTM_NEWLINK with -ERANGE. On
the DE25 (kernel 6.18.2) `can_fd_link setup can0` failed with "[Errno 34]
rtnetlink seq 2" and both links stayed down (DS-P3.211 board session,
2026-11-06). The byte test written by the same hand carried the same 10.

This test compiles a probe against the installed uapi headers and compares
every constant and size the module relies on. It skips, with the reason,
only when no C compiler or no kernel uapi headers are available.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from sw.can import can_fd_link as L

CONSTANTS = [
    "NLMSG_ERROR", "RTM_NEWLINK", "NLM_F_REQUEST", "NLM_F_ACK", "IFF_UP",
    "IFLA_LINKINFO", "IFLA_INFO_KIND", "IFLA_INFO_DATA",
    "IFLA_CAN_BITTIMING", "IFLA_CAN_CTRLMODE", "IFLA_CAN_DATA_BITTIMING",
    "CAN_CTRLMODE_FD", "CAN_EFF_FLAG", "CAN_SFF_MASK", "CAN_EFF_MASK",
    "CANFD_BRS", "CANFD_ESI", "CANFD_FDF", "CANFD_MTU", "CAN_MTU",
]
# (C expression, value the module's encoders assume)
LAYOUT = [
    ("sizeof(struct can_bittiming)", 32),
    ("sizeof(struct can_ctrlmode)", 8),
    ("sizeof(struct ifinfomsg)", 16),
    ("sizeof(struct nlmsghdr)", 16),
    ("sizeof(struct nlattr)", 4),
    ("sizeof(struct canfd_frame)", 72),
    ("offsetof(struct canfd_frame, data)", 8),
    ("sizeof(struct can_frame)", 16),
]
HEADERS = ["stddef.h", "stdio.h", "linux/netlink.h", "linux/rtnetlink.h", "linux/if_link.h",
           "linux/can.h", "linux/can/raw.h", "linux/can/netlink.h", "net/if.h"]


@pytest.fixture(scope="module")
def kernel(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        pytest.skip("no C compiler: kernel uapi constants not checked")
    d = tmp_path_factory.mktemp("uapi")
    lines = [f"#include <{h}>" for h in HEADERS]
    lines.append("int main(void) {")
    for name in CONSTANTS:
        lines.append(f'  printf("{name}=%lld\\n", (long long)({name}));')
    for i, (expr, _) in enumerate(LAYOUT):
        lines.append(f'  printf("L{i}=%lld\\n", (long long)({expr}));')
    lines.append("  return 0; }")
    (d / "probe.c").write_text("\n".join(lines) + "\n")
    comp = subprocess.run([cc, "-o", str(d / "probe"), str(d / "probe.c")],
                          capture_output=True, text=True, errors="replace")
    if comp.returncode != 0:
        if "No such file or directory" in comp.stderr and ".h" in comp.stderr:
            pytest.skip("kernel uapi headers not installed: " + comp.stderr.strip()[:200])
        pytest.fail("uapi probe failed to compile:\n" + comp.stderr[-2000:])
    out = subprocess.run([str(d / "probe")], capture_output=True, text=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines())


@pytest.mark.parametrize("name", CONSTANTS)
def test_constant_matches_the_kernel_header(kernel, name):
    assert int(getattr(L, name)) == int(kernel[name]), (
        f"{name}: module {getattr(L, name)}, <linux/...> {kernel[name]}")


@pytest.mark.parametrize("idx", range(len(LAYOUT)), ids=[e for e, _ in LAYOUT])
def test_struct_layout_matches_the_kernel_header(kernel, idx):
    expr, assumed = LAYOUT[idx]
    assert int(kernel[f"L{idx}"]) == assumed, f"{expr}: kernel {kernel[f'L{idx}']}, assumed {assumed}"


def test_data_bittiming_is_not_the_const_attribute():
    """The specific confusion that reached silicon: 9 is the timing, 10 is _CONST."""
    assert L.IFLA_CAN_DATA_BITTIMING == 9
