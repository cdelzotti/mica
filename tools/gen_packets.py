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

"""Craft and send MICA-protocol packets with scapy to verify a running
netbench_server, in three steps:

  1) the server answers at all (a NOOP_READ request gets a reply);
  2) if the server was started with --prepopulate-*, that data is really
     there (a sample of it is read back and checked byte-for-byte);
  3) the server actually stores data (a SET followed by a GET round-trips
     the value).

It builds requests using MICA's own binary wire format (see ../src/proto.h
and ../src/table.h) and sends them as raw Ethernet/IPv4/UDP frames. It is
meant to answer "is MICA working?", not to measure performance -- it sends
one request at a time and prints what happened. For real load generation,
write (or point MICA at) a dedicated benchmarking client instead.

Requires scapy and, like the rest of MICA, raw-socket privileges (run as
root, or grant the interpreter CAP_NET_RAW/CAP_NET_ADMIN).

Examples:

    # just steps 1 and 3 (no --prepopulate-* given -> step 2 is skipped,
    # exactly like the server itself skips prepopulation when those flags
    # are omitted)
    sudo ./gen_packets.py --iface eth0 --dst-mac aa:bb:cc:dd:ee:ff

    # all three steps -- pass the *same* --prepopulate-* values you started
    # the server with, so step 2 knows what to expect
    sudo ./gen_packets.py --iface eth0 --dst-mac aa:bb:cc:dd:ee:ff \\
        --prepopulate-nb-items 1000000 --prepopulate-key-length 8 \\
        --prepopulate-value-length 8
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


def build_request(operation, key, value=b"", expire_time=0, key_hash=None):
    """Return (request_header_bytes, trailing_data_bytes) for one request.

    key_hash defaults to CityHash64(key), which is correct for anything this
    tool itself SETs. Pass it explicitly to override -- needed to read back
    the server's --prepopulate-* dataset, whose entries are stored under a
    key_hash computed from something other than the stored key bytes (see
    prepopulate_key_hash() below)."""
    key = bytes(key)
    value = bytes(value)
    if key_hash is None:
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
    """requests: list of (operation, key, value, expire_time) or
    (operation, key, value, expire_time, key_hash). Returns the bytes that
    belong after the Ethernet/IPv4/UDP headers -- i.e. struct
    mehcached_batch_packet from 'num_requests' onward. The headers themselves
    are built by scapy (Ether/IP/UDP), not here."""
    if not (1 <= len(requests) <= MEHCACHED_MAX_BATCH_SIZE):
        raise ValueError(f"a batch must have between 1 and {MEHCACHED_MAX_BATCH_SIZE} requests")
    headers = b""
    data = b""
    for request in requests:
        operation, key, value, expire_time = request[:4]
        key_hash = request[4] if len(request) > 4 else None
        h, d = build_request(operation, key, value, expire_time, key_hash)
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
# Prepopulation dataset -- a pure-Python port of
# mehcached_benchmark_prepopulate_proc() in ../src/netbench_server.c.
#
# netbench_server's --prepopulate-nb-items/--prepopulate-key-length/
# --prepopulate-value-length flags make it fill its table with a synthetic
# dataset at startup, before any client ever sends a request. To read that
# data back we have to reproduce, byte for byte, three things the server
# derives from each item's integer index:
#
#   1. the key bytes it stores (a variable-length, nibble-per-slot encoding
#      of index+1 -- NOT an ASCII hex string);
#   2. the key_hash it stores the item under. Surprisingly this is NOT
#      CityHash64 of those key bytes: mehcached_hash_key() in
#      netbench_server.c hashes the raw 8-byte little-endian index itself
#      (see mehcached_hash_key()/mehcached_benchmark_prepopulate_proc() in
#      ../src/netbench_server.c). mehcached_get() matches on key_hash and key
#      bytes exactly as given (src/table.c), so a GET with the "wrong" hash
#      -- e.g. CityHash64 of the key bytes, as this tool's own SET/GET
#      traffic uses -- looks up the wrong bucket and comes back NOT_FOUND
#      even though the data is really there.
#   3. the value bytes it stores: (index & 0xffffffff) | ((~index &
#      0xffffffff) << 32), little-endian, truncated/zero-padded to
#      value_length.
# ---------------------------------------------------------------------------

def prepopulate_key_position_step(key_length, num_items):
    log16_num_items = 0
    while (1 << (log16_num_items * 4)) < (num_items + 1):
        log16_num_items += 1
    step = key_length // log16_num_items
    return step if step != 0 else 1


def prepopulate_key_bytes(key_index, key_position_step):
    """The key bytes stored for this index: index+1 written in base 16, one
    nibble (as a raw byte 0-15, not an ASCII digit) per key_position_step-byte
    slot, most-significant nibble first."""
    n = key_index + 1
    key_length = 0
    while n > 0:
        n >>= 4
        key_length += key_position_step
    buf = bytearray(key_length)
    n = key_index + 1
    char_index = key_length
    while n > 0:
        char_index -= key_position_step
        buf[char_index] = n & 15
        n >>= 4
    return bytes(buf)


def prepopulate_key_hash(key_index):
    return city_hash64(struct.pack("<Q", key_index & _MASK64))


def prepopulate_value_bytes(key_index, value_length):
    value = (key_index & 0xFFFFFFFF) | ((~key_index & 0xFFFFFFFF) << 32)
    full = struct.pack("<Q", value & _MASK64)
    if value_length <= 8:
        return full[:value_length]
    return full + b"\0" * (value_length - 8)


# ---------------------------------------------------------------------------
# packet I/O
# ---------------------------------------------------------------------------

def send_batch(args, requests, wait_reply=True):
    """Send one batch (list of (operation, key, value, expire_time[, key_hash]))
    to the server and, unless wait_reply is False, return the decoded reply
    (a list of request dicts, see parse_batch_packet) or None on timeout."""
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


# ---------------------------------------------------------------------------
# verification steps
# ---------------------------------------------------------------------------

def step_ping(args):
    """Step 1: the server answers at all. A NOOP_READ touches no data (see
    the MEHCACHED_NOOP_READ/MEHCACHED_NOOP_WRITE case in src/table.c, which
    always just sets result = MEHCACHED_OK) -- it only proves the packet
    format, MAC/routing, and the server's request loop are all working."""
    print(f"[1/3] checking that the server answers ({args.dst_mac} via {args.iface}, dport {args.dport})")
    reply = send_batch(args, [(MEHCACHED_NOOP_READ, b"", b"", 0)])
    if reply is None:
        print("  no reply received (timeout)")
        return False
    result = reply[0]["result"]
    if result != 0:
        print(f"  unexpected result: {RESULT_NAMES.get(result, result)}")
        return False
    print("  server replied: OK")
    return True


def step_check_prepopulate(args):
    """Step 2: if the server was started with --prepopulate-*, verify a
    sample of that dataset is really there. Skipped (like the server itself
    skips prepopulation) when --prepopulate-nb-items is 0."""
    num_items = args.prepopulate_nb_items
    key_length = args.prepopulate_key_length
    value_length = args.prepopulate_value_length

    print("[2/3] checking prepopulated data")
    if num_items == 0:
        print("  --prepopulate-nb-items=0: nothing to check, skipping")
        return None

    key_position_step = prepopulate_key_position_step(key_length, num_items)
    sample_count = max(1, min(args.prepopulate_sample_count, num_items))
    if sample_count == 1:
        indices = [0]
    else:
        indices = sorted({round(i * (num_items - 1) / (sample_count - 1)) for i in range(sample_count)})

    print(
        f"  sampling {len(indices)} of {num_items} item(s) "
        f"(key_length={key_length}, value_length={value_length}, key_position_step={key_position_step})"
    )

    batch_size = max(1, min(args.prepopulate_batch_size, MEHCACHED_MAX_BATCH_SIZE))
    failures = 0
    checked = 0
    for start in range(0, len(indices), batch_size):
        chunk = indices[start:start + batch_size]
        requests = [
            (MEHCACHED_GET, prepopulate_key_bytes(key_index, key_position_step), b"", 0, prepopulate_key_hash(key_index))
            for key_index in chunk
        ]

        reply = send_batch(args, requests)
        if reply is None:
            print(f"  key_index {chunk[0]}..{chunk[-1]}: no reply (timeout)")
            failures += len(chunk)
            checked += len(chunk)
            continue

        for key_index, req in zip(chunk, reply):
            checked += 1
            expected_value = prepopulate_value_bytes(key_index, value_length)
            result = req["result"]
            if result != 0:
                print(f"  [{key_index}] key={req['key']!r}: FAILED ({RESULT_NAMES.get(result, result)})")
                failures += 1
            elif req["value"] != expected_value:
                print(
                    f"  [{key_index}] key={req['key']!r}: value mismatch, "
                    f"expected {expected_value!r}, got {req['value']!r}"
                )
                failures += 1

    print(f"  {checked - failures}/{checked} prepopulated item(s) verified")
    return failures == 0


def step_round_trip(args):
    """Step 3: the server actually stores data -- SET then GET a handful of
    fresh keys and check the values round-trip correctly."""
    print(f"[3/3] SET/GET round-trip check ({args.round_trip_count} key(s))")
    failures = 0
    for i in range(args.round_trip_count):
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

    total = args.round_trip_count
    print(f"  {total - failures}/{total} round-trip(s) passed")
    return failures == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Verify a running netbench_server in 3 steps: it answers, its prepopulated "
        "data (if any) is there, and it actually stores SET data.",
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

    # step 2: same --prepopulate-* names/semantics as netbench_server itself (see src/netbench_server.c
    # main()), defaulting to 0 just like the server's own zero-initialized config -- i.e. step 2 is
    # skipped unless you pass the same values you started the server with.
    parser.add_argument(
        "--prepopulate-nb-items", type=int, default=0,
        help="must match the server's --prepopulate-nb-items=N (default: 0, i.e. skip step 2)",
    )
    parser.add_argument(
        "--prepopulate-key-length", type=int, default=0,
        help="must match the server's --prepopulate-key-length=N",
    )
    parser.add_argument(
        "--prepopulate-value-length", type=int, default=0,
        help="must match the server's --prepopulate-value-length=N",
    )
    parser.add_argument(
        "--prepopulate-sample-count", type=int, default=16,
        help="number of prepopulated items to sample and verify in step 2, evenly spaced across "
        "the full [0, nb-items) range (default: 16)",
    )
    parser.add_argument(
        "--prepopulate-batch-size", type=int, default=MEHCACHED_MAX_BATCH_SIZE,
        help=f"GET requests per batch packet in step 2, 1-{MEHCACHED_MAX_BATCH_SIZE} "
        f"(default: {MEHCACHED_MAX_BATCH_SIZE})",
    )

    # step 3
    parser.add_argument(
        "--round-trip-count", type=int, default=4,
        help="number of distinct keys to SET then GET in step 3 (default: 4)",
    )
    parser.add_argument("--key-prefix", default="gen_packets-verify-key", help="prefix for step 3's generated test keys")
    parser.add_argument("--value-prefix", default="gen_packets-verify-value", help="prefix for step 3's generated test values")

    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    ok1 = step_ping(args)

    ok2 = None
    ok3 = None
    if ok1:
        print()
        ok2 = step_check_prepopulate(args)
        print()
        ok3 = step_round_trip(args)
    else:
        print("\nserver did not answer step 1 -- skipping steps 2 and 3")

    def status(ok):
        if ok is None:
            return "SKIPPED"
        return "OK" if ok else "FAILED"

    print("\n=== summary ===")
    print(f"1) server answers:       {status(ok1)}")
    print(f"2) prepopulated data:    {status(ok2)}")
    print(f"3) SET/GET round-trip:   {status(ok3)}")

    passed = ok1 and ok2 is not False and ok3 is not False
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
