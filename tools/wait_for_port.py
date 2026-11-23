#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Wait until a TCP port accepts a connection, for at most --timeout seconds.

Exit 0 once a connection is accepted; exit 1 with a message naming the address and
port when the time runs out; exit 2 for an address that is not an IP literal or a
timeout that is not a finite, non-negative number. Used by the README's first-boot
steps to wait for the board's stream daemon before RouteWave attaches.
"""
from __future__ import annotations

import argparse
import ipaddress
import math
import socket
import sys
import time


def wait_for_port(host: str, port: int, timeout: float, interval: float = 2.0,
                  connect=None, clock=time.monotonic, sleep=time.sleep) -> bool:
    """True once a connection is accepted; False when *timeout* seconds have
    passed. No connection attempt or sleep runs past the deadline. *host* is an
    IP literal, so no name lookup runs outside the deadline."""
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError(f"timeout must be a finite, non-negative number: {timeout!r}")
    ipaddress.ip_address(host)
    connect = connect or socket.create_connection
    deadline = clock() + timeout
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        try:
            connect((host, port), timeout=min(interval, remaining)).close()
            return True
        except OSError:
            pass
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(interval, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", help="the board's IPv4 or IPv6 address")
    parser.add_argument("port", type=int)
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds (default 180)")
    args = parser.parse_args(argv)
    try:
        ok = wait_for_port(args.host, args.port, args.timeout)
    except ValueError as exc:
        print(f"wait_for_port: {exc}", file=sys.stderr)
        return 2
    if ok:
        print(f"{args.host}:{args.port} accepts connections")
        return 0
    print(f"{args.host}:{args.port} did not accept a connection within {args.timeout:g} s; "
          "check the board's serial console", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
