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

"""Generate a .pcap trace of MICA-protocol SET/GET requests, for replay with
a real load-generation tool (e.g. tcpreplay) against a running
netbench_server instead of using gen_packets.py's own one-request-at-a-time
sender.

Each packet carries exactly one request (SET or GET), built with the same
binary wire format as gen_packets.py (see ../src/proto.h, ../src/table.h).
The requested --percentage-set/--percentage-get split is respected exactly
(rounded to whole packets) and the SET/GET order is shuffled. Keys are drawn
from a fixed-size pool (--nb-keys); GETs only ever reference a key that an
earlier SET in the trace already wrote (the first packet is forced to be a
SET whenever --percentage-set > 0, to guarantee this from packet 1 onward),
so replaying the trace from the start produces real hits, not a stream of
NOT_FOUNDs. The one unavoidable exception is --percentage-set 0: with no
SETs at all, every GET necessarily misses.

Packet timestamps are spaced 1/--rate seconds apart, so a replay tool that
honors capture timing (e.g. `tcpreplay --pcap-timestamps-adaptive` or plain
`-t` for as-fast-as-possible) reproduces the requested rate.

Unlike gen_packets.py/client.py, this script only *builds* packets -- it
never sends anything, so it needs neither an interface nor raw-socket
privileges. --dst-mac still has to be the real MAC of the NIC
netbench_server owns (printed at server startup), because that's what
determines the flow rule's queue routing when the trace is actually replayed.

Requires scapy.

Example:

    ./gen_trace.py --nb-packets 1000000 --percentage-set 5 --percentage-get 95 \\
        --rate 100000 --dst-mac aa:bb:cc:dd:ee:ff --output workload.pcap

    # then, on the machine connected to the server:
    sudo tcpreplay --intf1=eth0 --pps=100000 workload.pcap
"""

import argparse
import random
import sys
import time

from scapy.utils import PcapWriter
from scapy.all import Ether, IP, UDP, Raw

from gen_packets import MEHCACHED_GET, MEHCACHED_SET, build_batch_packet


def build_ops(nb_packets, percentage_set):
    """Return a shuffled list of MEHCACHED_SET/MEHCACHED_GET, with counts
    that respect the requested SET percentage exactly (rounded to whole
    packets; the GET count is whatever makes the total equal nb_packets, so
    the two counts always add up even if the rounded SET count drifts by
    one packet from its exact percentage). percentage-get is only used by
    main() to validate that percentage-set + percentage-get == 100."""
    num_set = round(nb_packets * percentage_set / 100)
    num_set = max(0, min(nb_packets, num_set))
    num_get = nb_packets - num_set
    ops = [MEHCACHED_SET] * num_set + [MEHCACHED_GET] * num_get
    random.shuffle(ops)
    if num_set > 0 and ops[0] != MEHCACHED_SET:
        # guarantee at least one populated key before the first GET is ever
        # assigned one (see generate_trace()'s read-after-write key
        # selection) -- otherwise the shuffle could put a GET first and it
        # would have nothing to reference yet
        first_set = ops.index(MEHCACHED_SET)
        ops[0], ops[first_set] = ops[first_set], ops[0]
    return ops, num_set, num_get


def build_packet(args, operation, key, value, sport):
    if operation == MEHCACHED_SET:
        requests = [(MEHCACHED_SET, key, value, 0)]
    else:
        requests = [(MEHCACHED_GET, key, b"", 0)]
    payload = build_batch_packet(requests)
    return (
        Ether(src=args.src_mac, dst=args.dst_mac)
        / IP(src=args.src_ip, dst=args.dst_ip)
        / UDP(sport=sport, dport=args.dport)
        / Raw(load=payload)
    )


def generate_trace(args):
    if args.seed is not None:
        random.seed(args.seed)

    ops, num_set, num_get = build_ops(args.nb_packets, args.percentage_set)
    sport = args.sport if args.sport is not None else random.randint(1024, 65535)

    print(
        f"generating {args.nb_packets} packet(s): {num_set} SET, {num_get} GET "
        f"({args.nb_keys} key(s), value length {args.value_length}) at {args.rate} pkt/s"
    )

    writer = PcapWriter(args.output, append=False, sync=True)
    try:
        populated = []
        populated_set = set()
        next_key_to_populate = 0
        base_time = time.time()
        inter_packet_gap = 1.0 / args.rate

        for i, operation in enumerate(ops):
            if operation == MEHCACHED_SET:
                key_index = next_key_to_populate % args.nb_keys
                next_key_to_populate += 1
                if key_index not in populated_set:
                    populated_set.add(key_index)
                    populated.append(key_index)
                value = random.randbytes(args.value_length)
            else:
                key_index = random.choice(populated) if populated else 0
                value = b""

            key = f"{args.key_prefix}-{key_index}".encode()
            pkt = build_packet(args, operation, key, value, sport)
            pkt.time = base_time + i * inter_packet_gap
            writer.write(pkt)

            if args.progress and (i + 1) % args.progress == 0:
                print(f"  {i + 1}/{args.nb_packets} packet(s) written", file=sys.stderr)
    finally:
        writer.close()

    duration = args.nb_packets / args.rate
    print(f"wrote {args.output} ({args.nb_packets} packet(s), ~{duration:.2f}s at {args.rate} pkt/s)")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Generate a .pcap trace of MICA SET/GET requests for replay with a real load-generation tool.",
    )
    parser.add_argument("--nb-packets", type=int, required=True, help="total number of packets (requests) to generate")
    parser.add_argument(
        "--percentage-set", type=float, required=True, help="percentage of packets that are SET requests"
    )
    parser.add_argument(
        "--percentage-get", type=float, required=True, help="percentage of packets that are GET requests"
    )
    parser.add_argument("--rate", type=float, required=True, help="packets per second to space the trace at")
    parser.add_argument("--output", "-o", default="trace.pcap", help="output .pcap path (default: trace.pcap)")

    parser.add_argument(
        "--dst-mac", required=True, help="MAC address of the NIC netbench_server is running on (printed at server startup)"
    )
    parser.add_argument("--src-mac", default="02:00:00:00:00:01", help="source MAC to embed (arbitrary; never checked)")
    parser.add_argument("--src-ip", default="192.0.2.1", help="source IPv4 address (arbitrary; MICA does not validate it)")
    parser.add_argument("--dst-ip", default="192.0.2.2", help="destination IPv4 address (arbitrary; MICA does not validate it)")
    parser.add_argument("--sport", type=int, default=None, help="UDP source port for every packet (default: random, fixed for the whole trace)")
    parser.add_argument(
        "--dport", type=int, default=0,
        help="UDP destination port, i.e. MICA's routing 'mapping_id' (0 = partition owner thread). Default: 0.",
    )

    parser.add_argument(
        "--nb-keys", type=int, default=None,
        help="size of the key pool SETs cycle through and GETs are drawn from (default: --nb-packets, i.e. keys are "
        "unique unless you shrink this to create read/write overlap)",
    )
    parser.add_argument("--key-prefix", default="gen_trace-key", help="prefix for generated keys (default: gen_trace-key)")
    parser.add_argument("--value-length", type=int, default=8, help="byte length of generated SET values (default: 8)")
    parser.add_argument("--seed", type=int, default=None, help="random seed, for a reproducible trace (default: unseeded)")
    parser.add_argument(
        "--progress", type=int, default=0, help="print progress every N packets to stderr (default: 0, i.e. off)"
    )

    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.nb_packets < 1:
        parser.error("--nb-packets must be at least 1")
    if abs(args.percentage_set + args.percentage_get - 100) > 1e-9:
        parser.error(
            f"--percentage-set ({args.percentage_set}) and --percentage-get ({args.percentage_get}) "
            "must add up to 100 -- SET and GET are the only two request types this script generates"
        )
    if args.rate <= 0:
        parser.error("--rate must be positive")
    if args.nb_keys is None:
        args.nb_keys = args.nb_packets
    if args.nb_keys < 1:
        parser.error("--nb-keys must be at least 1")

    generate_trace(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
