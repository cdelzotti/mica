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

"""Craft and send MICA-protocol packets with scapy.

This is a small testing tool to sanity-check a running netbench_server: it
builds requests using MICA's own binary wire format (see ../src/proto.h and
../src/table.h), sends them as raw Ethernet/IPv4/UDP frames, and decodes
whatever the server sends back. It is meant to answer "is MICA working?",
not to measure performance -- it sends one request at a time and prints what
happened. For real load generation, write (or point MICA at) a dedicated
benchmarking client instead.

Requires scapy and, like the rest of MICA, raw-socket privileges (run as
root, or grant the interpreter CAP_NET_RAW/CAP_NET_ADMIN).

Examples:

    # set a key, then read it back, and check the value round-trips
    sudo ./gen_packets.py --iface eth0 --dst-mac aa:bb:cc:dd:ee:ff selftest

    # send one raw request and print the reply
    sudo ./gen_packets.py --iface eth0 --dst-mac aa:bb:cc:dd:ee:ff \\
        send --op set --key hello --value world
    sudo ./gen_packets.py --iface eth0 --dst-mac aa:bb:cc:dd:ee:ff \\
        send --op get --key hello
"""

import argparse
import random
import struct
import sys

from scapy.all import Ether, IP, UDP, Raw, get_if_hwaddr, srp1


# ---------------------------------------------------------------------------
# CityHash64 -- a pure-Python port of MICA's src/city.c CityHash64().
#
# Every request packet carries its own key_hash. MICA's table code (see
# mehcached_set()/mehcached_get() in src/table.c) trusts this value as-is and
# never recomputes it from the key bytes, so strictly speaking any hash
# function would do *if* every key we ever SET is later GET with that same
# hash. But matching MICA's real hash lets this tool also read back data the
# server prepopulated itself (see the "Prepopulation" section of the
# top-level README), which is hashed with the server's own CityHash64. This
# port has been checked byte-for-byte against src/city.c across every length
# branch (0, 1-8, 9-16, 17-32, 33-64, and >64 with multiple 64-byte chunks)
# up to MEHCACHED_MAX_KEY_LENGTH (255) -- it is not a guess.
# ---------------------------------------------------------------------------

_MASK64 = (1 << 64) - 1
_K0 = 0xC3A5C85C97CB3127
_K1 = 0xB492B66FBE98F273
_K2 = 0x9AE16A3B2F90404F
_K3 = 0xC949D7C7509E6557


def _fetch64(s, off):
    return int.from_bytes(s[off:off + 8], "little")


def _fetch32(s, off):
    return int.from_bytes(s[off:off + 4], "little")


def _rotate(val, shift):
    if shift == 0:
        return val
    return ((val >> shift) | (val << (64 - shift))) & _MASK64


def _shift_mix(val):
    return (val ^ (val >> 47)) & _MASK64


def _hash128to64(lo, hi):
    kmul = 0x9DDFEA08EB382D69
    a = ((lo ^ hi) * kmul) & _MASK64
    a ^= (a >> 47)
    b = ((hi ^ a) * kmul) & _MASK64
    b ^= (b >> 47)
    b = (b * kmul) & _MASK64
    return b


def _hash_len16(u, v):
    return _hash128to64(u, v)


def _hash_len0to16(s):
    length = len(s)
    if length > 8:
        a = _fetch64(s, 0)
        b = _fetch64(s, length - 8)
        return (_hash_len16(a, _rotate((b + length) & _MASK64, length)) ^ b) & _MASK64
    if length >= 4:
        a = _fetch32(s, 0)
        return _hash_len16((length + (a << 3)) & _MASK64, _fetch32(s, length - 4))
    if length > 0:
        a = s[0]
        b = s[length >> 1]
        c = s[length - 1]
        y = (a + (b << 8)) & 0xFFFFFFFF
        z = (length + (c << 2)) & 0xFFFFFFFF
        return (_shift_mix((((y * _K2) & _MASK64) ^ ((z * _K3) & _MASK64))) * _K2) & _MASK64
    return _K2


def _hash_len17to32(s):
    length = len(s)
    a = (_fetch64(s, 0) * _K1) & _MASK64
    b = _fetch64(s, 8)
    c = (_fetch64(s, length - 8) * _K2) & _MASK64
    d = (_fetch64(s, length - 16) * _K0) & _MASK64
    return _hash_len16(
        (_rotate((a - b) & _MASK64, 43) + _rotate(c, 30) + d) & _MASK64,
        (a + _rotate(b ^ _K3, 20) - c + length) & _MASK64,
    )


def _weak_hash_len32_with_seeds6(w, x, y, z, a, b):
    a = (a + w) & _MASK64
    b = _rotate((b + a + z) & _MASK64, 21)
    c = a
    a = (a + x) & _MASK64
    a = (a + y) & _MASK64
    b = (b + _rotate(a, 44)) & _MASK64
    return (a + z) & _MASK64, (b + c) & _MASK64


def _weak_hash_len32_with_seeds(s, off, a, b):
    return _weak_hash_len32_with_seeds6(
        _fetch64(s, off), _fetch64(s, off + 8), _fetch64(s, off + 16), _fetch64(s, off + 24), a, b
    )


def _hash_len33to64(s):
    length = len(s)
    z = _fetch64(s, 24)
    a = (_fetch64(s, 0) + (((length + _fetch64(s, length - 16)) & _MASK64) * _K0) & _MASK64) & _MASK64
    b = _rotate((a + z) & _MASK64, 52)
    c = _rotate(a, 37)
    a = (a + _fetch64(s, 8)) & _MASK64
    c = (c + _rotate(a, 7)) & _MASK64
    a = (a + _fetch64(s, 16)) & _MASK64
    vf = (a + z) & _MASK64
    vs = (b + _rotate(a, 31) + c) & _MASK64
    a = (_fetch64(s, 16) + _fetch64(s, length - 32)) & _MASK64
    z = _fetch64(s, length - 8)
    b = _rotate((a + z) & _MASK64, 52)
    c = _rotate(a, 37)
    a = (a + _fetch64(s, length - 24)) & _MASK64
    c = (c + _rotate(a, 7)) & _MASK64
    a = (a + _fetch64(s, length - 16)) & _MASK64
    wf = (a + z) & _MASK64
    ws = (b + _rotate(a, 31) + c) & _MASK64
    r = _shift_mix((((vf + ws) & _MASK64) * _K2 + ((wf + vs) & _MASK64) * _K0) & _MASK64)
    return (_shift_mix((r * _K0 + vs) & _MASK64) * _K2) & _MASK64


def city_hash64(data):
    s = bytes(data)
    length = len(s)

    if length <= 32:
        if length <= 16:
            return _hash_len0to16(s)
        return _hash_len17to32(s)
    if length <= 64:
        return _hash_len33to64(s)

    x = _fetch64(s, length - 40)
    y = (_fetch64(s, length - 16) + _fetch64(s, length - 56)) & _MASK64
    z = _hash_len16((_fetch64(s, length - 48) + length) & _MASK64, _fetch64(s, length - 24))
    v = _weak_hash_len32_with_seeds(s, length - 64, length, z)
    w = _weak_hash_len32_with_seeds(s, length - 32, (y + _K1) & _MASK64, x)
    x = (x * _K1 + _fetch64(s, 0)) & _MASK64

    remaining = (length - 1) & (~63 & _MASK64)
    pos = 0
    while remaining != 0:
        x = _rotate((x + y + v[0] + _fetch64(s, pos + 8)) & _MASK64, 37)
        x = (x * _K1) & _MASK64
        y = _rotate((y + v[1] + _fetch64(s, pos + 48)) & _MASK64, 42)
        y = (y * _K1) & _MASK64
        x ^= w[1]
        y = (y + v[0] + _fetch64(s, pos + 40)) & _MASK64
        z = _rotate((z + w[0]) & _MASK64, 33)
        z = (z * _K1) & _MASK64
        v = _weak_hash_len32_with_seeds(s, pos, (v[1] * _K1) & _MASK64, (x + w[0]) & _MASK64)
        w = _weak_hash_len32_with_seeds(s, pos + 32, (z + w[1]) & _MASK64, (y + _fetch64(s, pos + 16)) & _MASK64)
        z, x = x, z
        pos += 64
        remaining -= 64

    return _hash_len16(
        (_hash_len16(v[0], w[0]) + ((_shift_mix(y) * _K1) & _MASK64) + z) & _MASK64,
        (_hash_len16(v[1], w[1]) + x) & _MASK64,
    )


# ---------------------------------------------------------------------------
# MICA wire protocol (see src/proto.h, src/table.h)
# ---------------------------------------------------------------------------

MEHCACHED_NOOP_READ = 0
MEHCACHED_NOOP_WRITE = 1
MEHCACHED_ADD = 2
MEHCACHED_SET = 3
MEHCACHED_GET = 4
MEHCACHED_TEST = 5
MEHCACHED_DELETE = 6
MEHCACHED_INCREMENT = 7

# netbench_server.c's request-processing switch only implements these -- a
# DELETE or TEST request falls into its "default" case, which just logs
# "invalid operation" and leaves the request untouched (so it would look
# like a false success if we let you send one). Only expose what the server
# actually does something with.
OPERATIONS = {
    "noop-read": MEHCACHED_NOOP_READ,
    "noop-write": MEHCACHED_NOOP_WRITE,
    "add": MEHCACHED_ADD,
    "set": MEHCACHED_SET,
    "get": MEHCACHED_GET,
    "increment": MEHCACHED_INCREMENT,
}

RESULT_NAMES = {
    0: "OK",
    1: "ERROR",
    2: "FULL",
    3: "EXIST",
    4: "NOT_FOUND",
    5: "PARTIAL_VALUE",
    6: "NOT_PROCESSED",
}

# operation(u8), result(u8), reserved0(u16), kv_length_vec(u32), key_hash(u64),
# expire_time(u32), reserved1(u32) -- struct mehcached_request in src/table.h
_REQUEST_FORMAT = "<BBHIQII"
_REQUEST_SIZE = struct.calcsize(_REQUEST_FORMAT)
assert _REQUEST_SIZE == 24

MEHCACHED_MAX_BATCH_SIZE = 36


def roundup8(n):
    return (n + 7) & ~7


def kv_length_vec(key_length, value_length):
    return ((key_length & 0xFF) << 24) | (value_length & 0xFFFFFF)


def build_request(operation, key, value=b"", expire_time=0):
    """Return (request_header_bytes, trailing_data_bytes) for one request."""
    key = bytes(key)
    value = bytes(value)
    key_hash = city_hash64(key)
    # a GET carries no value payload at all -- the server only ever reads
    # 'value_length' bytes after the key, and writes its own result there
    value_length = 0 if operation == MEHCACHED_GET else len(value)
    header = struct.pack(
        _REQUEST_FORMAT, operation, 0, 0, kv_length_vec(len(key), value_length), key_hash, expire_time, 0
    )
    data = key.ljust(roundup8(len(key)), b"\0")
    if operation != MEHCACHED_GET:
        data += value.ljust(roundup8(len(value)), b"\0")
    return header, data


def build_batch_packet(requests):
    """requests: list of (operation, key, value, expire_time). Returns the
    bytes that belong after the Ethernet/IPv4/UDP headers -- i.e. struct
    mehcached_batch_packet from 'num_requests' onward. The headers themselves
    are built by scapy (Ether/IP/UDP), not here."""
    if not (1 <= len(requests) <= MEHCACHED_MAX_BATCH_SIZE):
        raise ValueError(f"a batch must have between 1 and {MEHCACHED_MAX_BATCH_SIZE} requests")
    headers = b""
    data = b""
    for operation, key, value, expire_time in requests:
        h, d = build_request(operation, key, value, expire_time)
        headers += h
        data += d
    return struct.pack("<BBI", len(requests), 0, 0) + headers + data


def parse_batch_packet(payload):
    """Decode a mehcached_batch_packet payload (everything after the
    Ethernet/IPv4/UDP headers) coming back from the server."""
    num_requests, _reserved0, _opaque = struct.unpack_from("<BBI", payload, 0)
    offset = struct.calcsize("<BBI")  # 6: num_requests(1) + reserved0(1) + opaque(4), no padding
    reqs = []
    for _ in range(num_requests):
        operation, result, _reserved0, kvlv, key_hash, expire_time, _reserved1 = struct.unpack_from(
            _REQUEST_FORMAT, payload, offset
        )
        reqs.append(
            {
                "operation": operation,
                "result": result,
                "kv_length_vec": kvlv,
                "key_hash": key_hash,
                "expire_time": expire_time,
            }
        )
        offset += _REQUEST_SIZE
    for req in reqs:
        key_length = req["kv_length_vec"] >> 24
        value_length = req["kv_length_vec"] & 0xFFFFFF
        req["key"] = payload[offset:offset + key_length]
        offset += roundup8(key_length)
        req["value"] = payload[offset:offset + value_length]
        offset += roundup8(value_length)
    return reqs


# ---------------------------------------------------------------------------
# packet I/O
# ---------------------------------------------------------------------------

def send_batch(args, requests, wait_reply=True):
    """Send one batch (list of (operation, key, value, expire_time)) to the
    server and, unless wait_reply is False, return the decoded reply (a list
    of request dicts, see parse_batch_packet) or None on timeout."""
    payload = build_batch_packet(requests)
    src_mac = args.src_mac or get_if_hwaddr(args.iface)
    sport = args.sport or random.randint(1024, 65535)

    pkt = (
        Ether(src=src_mac, dst=args.dst_mac)
        / IP(src=args.src_ip, dst=args.dst_ip)
        / UDP(sport=sport, dport=args.dport)
        / Raw(load=payload)
    )

    if not wait_reply:
        from scapy.all import sendp

        sendp(pkt, iface=args.iface, verbose=False)
        return None

    reply = srp1(pkt, iface=args.iface, timeout=args.timeout, verbose=False)
    if reply is None:
        return None
    if not reply.haslayer(UDP) or not reply.haslayer(Raw):
        print("warning: reply did not look like a MICA response, ignoring", file=sys.stderr)
        return None
    return parse_batch_packet(bytes(reply[UDP].payload))


def describe_request(index, operation_name, result, key, value):
    result_name = RESULT_NAMES.get(result, f"UNKNOWN({result})")
    if operation_name == "get":
        shown_value = value if value is not None else b""
        print(f"  [{index}] {operation_name} {key!r} -> {result_name} value={shown_value!r}")
    elif operation_name == "increment":
        shown = struct.unpack("<Q", value)[0] if value and len(value) == 8 else value
        print(f"  [{index}] {operation_name} {key!r} -> {result_name} new_value={shown}")
    else:
        print(f"  [{index}] {operation_name} {key!r} -> {result_name}")


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def cmd_send(args):
    operation = OPERATIONS[args.op]
    key = args.key.encode()
    if args.op == "increment":
        value = struct.pack("<Q", int(args.value))
    elif args.op in ("set", "add"):
        value = args.value.encode() if args.value is not None else b""
    else:
        value = b""

    if args.no_wait:
        send_batch(args, [(operation, key, value, args.expire_time)], wait_reply=False)
        print("sent (not waiting for a reply)")
        return 0

    reply = send_batch(args, [(operation, key, value, args.expire_time)])
    if reply is None:
        print("no reply received (timeout)", file=sys.stderr)
        return 1

    req = reply[0]
    describe_request(0, args.op, req["result"], key, req["value"])
    return 0 if req["result"] == 0 else 1


def cmd_selftest(args):
    print(f"running {args.count} SET/GET round-trip check(s) against {args.dst_mac} via {args.iface}")
    failures = 0
    for i in range(args.count):
        key = f"{args.key_prefix}-{i}".encode()
        value = f"{args.value_prefix}-{i}".encode()

        set_reply = send_batch(args, [(MEHCACHED_SET, key, value, 0)])
        if set_reply is None:
            print(f"  [{i}] SET {key!r}: no reply (timeout)")
            failures += 1
            continue
        set_result = set_reply[0]["result"]
        if set_result != 0:
            print(f"  [{i}] SET {key!r}: FAILED ({RESULT_NAMES.get(set_result, set_result)})")
            failures += 1
            continue

        get_reply = send_batch(args, [(MEHCACHED_GET, key, b"", 0)])
        if get_reply is None:
            print(f"  [{i}] GET {key!r}: no reply (timeout)")
            failures += 1
            continue
        get_result = get_reply[0]["result"]
        got_value = get_reply[0]["value"]
        if get_result != 0:
            print(f"  [{i}] GET {key!r}: FAILED ({RESULT_NAMES.get(get_result, get_result)})")
            failures += 1
        elif got_value != value:
            print(f"  [{i}] GET {key!r}: value mismatch, sent {value!r}, got {got_value!r}")
            failures += 1
        else:
            print(f"  [{i}] {key!r} -> {value!r}: OK")

    total = args.count
    print(f"\n{total - failures}/{total} round-trip(s) passed")
    return 0 if failures == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Craft and send MICA-protocol packets with scapy, to sanity-check a running netbench_server.",
    )
    parser.add_argument("--iface", required=True, help="network interface to send/receive on")
    parser.add_argument(
        "--dst-mac", required=True, help="MAC address of the NIC netbench_server is running on (printed at server startup)"
    )
    parser.add_argument("--src-mac", default=None, help="source MAC to use (default: --iface's own address)")
    parser.add_argument("--src-ip", default="192.0.2.1", help="source IPv4 address (arbitrary; MICA does not validate it)")
    parser.add_argument("--dst-ip", default="192.0.2.2", help="destination IPv4 address (arbitrary; MICA does not validate it)")
    parser.add_argument("--sport", type=int, default=None, help="UDP source port (default: random)")
    parser.add_argument(
        "--dport",
        type=int,
        default=0,
        help="UDP destination port, i.e. MICA's routing 'mapping_id': 0 always routes to the "
        "partition's exclusive owner thread (--thread-id); 1024+N targets thread N directly "
        "(only meaningful with concurrent reads/writes enabled). Default: 0.",
    )
    parser.add_argument("--timeout", type=float, default=2.0, help="seconds to wait for a reply (default: 2.0)")

    sub = parser.add_subparsers(dest="command", required=True)

    p_send = sub.add_parser("send", help="send a single request and print the reply")
    p_send.add_argument("--op", choices=sorted(OPERATIONS), required=True, help="operation to send")
    p_send.add_argument("--key", required=True, help="key, as a UTF-8 string")
    p_send.add_argument("--value", default=None, help="value: a UTF-8 string for set/add, an integer for increment")
    p_send.add_argument("--expire-time", type=int, default=0, help="expire_time field to send (default: 0)")
    p_send.add_argument("--no-wait", action="store_true", help="fire and forget, don't wait for a reply")
    p_send.set_defaults(func=cmd_send)

    p_selftest = sub.add_parser(
        "selftest", help="SET then GET a handful of keys and check the values round-trip correctly"
    )
    p_selftest.add_argument("--count", type=int, default=4, help="number of distinct keys to round-trip (default: 4)")
    p_selftest.add_argument("--key-prefix", default="gen_packets-selftest-key", help="prefix for generated test keys")
    p_selftest.add_argument("--value-prefix", default="gen_packets-selftest-value", help="prefix for generated test values")
    p_selftest.set_defaults(func=cmd_selftest)

    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
