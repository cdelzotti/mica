#!/usr/bin/env python3
# Copyright 2014 Carnegie Mellon University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Interactive MICA client: a tiny scapy-based REPL for issuing SET/GET
requests against a running netbench_server.

Usage:

    sudo ./CLIent.py --iface eth0 --dst-mac aa:bb:cc:dd:ee:ff

Then, at the prompt:

    SET <key> <value>
    GET <key>
    exit

--dst-mac is the real MAC of the NIC netbench_server owns (printed at
server startup, "port 0 MAC: ..."). It has to be the real address:
netbench_server's rte_flow rule steers each request to the queue/thread
that owns the partition based on UDP destination port alone (see
../src/net_common.c), but a broadcast or wrong-unicast destination can be
flooded to (or classified onto) the wrong queue by the NIC itself before
that rule ever applies -- SET/GET then land on a thread that doesn't own
the partition and fail, unlike this tool's earlier broadcast-discovery
draft.

Requires scapy and, like the rest of MICA, raw-socket privileges (run as
root, or grant the interpreter CAP_NET_RAW/CAP_NET_ADMIN).
"""

import argparse
import sys
from types import SimpleNamespace

from gen_packets import MEHCACHED_GET, MEHCACHED_SET, RESULT_NAMES, send_batch


def build_args(iface, dst_mac):
    """A stand-in for gen_packets.py's argparse Namespace: send_batch() only
    reads these fields."""
    return SimpleNamespace(
        iface=iface,
        dst_mac=dst_mac,
        src_mac=None,
        src_ip="192.0.2.1",
        dst_ip="192.0.2.2",
        sport=None,
        dport=0,
        timeout=2.0,
    )


def do_set(args, key, value):
    reply = send_batch(args, [(MEHCACHED_SET, key.encode(), value.encode(), 0)])
    if reply is None:
        print("no reply (timeout)")
        return
    result = reply[0]["result"]
    print("OK" if result == 0 else RESULT_NAMES.get(result, f"UNKNOWN({result})"))


def do_get(args, key):
    reply = send_batch(args, [(MEHCACHED_GET, key.encode(), b"", 0)])
    if reply is None:
        print("no reply (timeout)")
        return
    result = reply[0]["result"]
    if result != 0:
        # netbench_server.c's GET case (src/netbench_server.c) only ever
        # returns OK or a generic MEHCACHED_ERROR -- there's no distinct
        # NOT_FOUND code in the compiled server, so ERROR here means exactly
        # one thing: the key isn't there.
        print("NOT_FOUND")
        return
    print(reply[0]["value"].decode(errors="replace"))


def run_repl(args):
    print(f"sending on {args.iface} to {args.dst_mac}")
    print("commands: SET <key> <value> | GET <key> | exit")
    while True:
        try:
            line = input("mica> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not line:
            continue

        parts = line.split(None, 2)
        command = parts[0].upper()

        if command in ("EXIT", "QUIT"):
            return
        if command == "SET":
            if len(parts) != 3:
                print("usage: SET <key> <value>")
                continue
            do_set(args, parts[1], parts[2])
        elif command == "GET":
            if len(parts) != 2:
                print("usage: GET <key>")
                continue
            do_get(args, parts[1])
        else:
            print(f"unknown command {parts[0]!r} -- expected SET or GET")


def main():
    parser = argparse.ArgumentParser(description="Interactive SET/GET client for a running netbench_server.")
    parser.add_argument("--iface", required=True, help="network interface to send/receive on")
    parser.add_argument(
        "--dst-mac", required=True, help="MAC address of the NIC netbench_server is running on (printed at server startup)"
    )
    args = parser.parse_args()
    run_repl(build_args(args.iface, args.dst_mac))
    return 0


if __name__ == "__main__":
    sys.exit(main())
