# modern MICA

A fast in-memory key-value store.

Adapted from the Initial MICA paper, this aim to modernize the 2014 codebase with modern designs and DPDK versions. Mainly :

- The hotkey logic that allowed users to manually assign heavily requested keys to separated cores has been removed.
- Each application uses one single port and contains one partition. This forces both a *single port per application* and *single partition per application* logic. In order to multiply partitions, simply run the application multiple times, one on each port.
- The client is gone, users can now use their own packet generation method to run the application.
- The benchmark is gone, users can use their own benchmarking system.
- DPDK API calls have been moved to 23.11 compatible calls.

## Building

### Prerequisites

- linux x86_64
- gcc
- cmake >= 3.6
- DPDK, exposed to `pkg-config` as `libdpdk` (verified against DPDK 23.11) -- installed as a normal
  system package, or via `meson`/`ninja`/`ninja install` from source. `pkg-config --exists libdpdk`
  must succeed; no DPDK source tree needs to sit next to `mica/`.
- Hugepages set up and a NIC bound to a DPDK-usable driver (`vfio-pci`, `uio_pci_generic`, etc. via
  `dpdk-hugepages.py`/`dpdk-devbind.py`). This is regular DPDK/machine setup, independent of MICA
  itself, and is not covered here.

### Build steps

```sh
cmake -Bbuild .
cd build
make
```

This produces the following executables in `build/`:

| Executable | Description |
|---|---|
| `netbench_server` | MICA server, cache mode |
| `netbench_server_store` | MICA server, store mode (no eviction) |
| `netbench_server_latency` | MICA server, cache mode, instrumented for end-to-end latency measurement |
| `netbench_server_soft_fdir` | MICA server, cache mode, using software-based request steering instead of `rte_flow` |
| `test` | table API correctness check |
| `load` | table fill/read-back success-rate check |


## Running

`netbench_server` (and its `_latency`/`_soft_fdir`/`_store` variants) is a standard DPDK
application: EAL options come first on the command line, followed by `--`, followed by the
application's own options, e.g.:

```sh
sudo ./netbench_server -l 0-3 -n 4 -b 0000:06:00.1 -- \
    --num-items=1000000 --alloc-size=134217728 \
    --concurrent-table-read=1 --concurrent-table-write=0 --concurrent-alloc-write=0 \
    --thread-id=0 --mth-threshold=0.5 \
    --prepopulate-nb-items=1000000 --prepopulate-key-length=8 --prepopulate-value-length=8 \
    0
```

EAL options (`-l`, `-n`, `-m`/`--socket-mem`, `-a`/`-b`, `--file-prefix`, ...) are standard DPDK and
not covered here. A single instance serves exactly one partition on one port; to serve multiple
partitions, run multiple instances (each with its own `--file-prefix` and its own port, e.g. via
`-a`/`-b` PCI allow/block-listing).

### Application parameters

| Parameter | Description |
|---|---|
| `--num-items=N` | expected number of items the partition's hash table should be sized for |
| `--alloc-size=N` | total bytes budgeted for the partition's item log/allocator |
| `--concurrent-table-read=0\|1` | allow concurrent reads from multiple threads |
| `--concurrent-table-write=0\|1` | allow concurrent writes from multiple threads |
| `--concurrent-alloc-write=0\|1` | use a single shared allocator instead of one per writing thread (only meaningful when `--concurrent-table-write=1`) |
| `--thread-id=N` | lcore id of this partition's exclusive owner thread (the thread that UDP dst port `0` routes writes to) |
| `--mth-threshold=F` | move-to-head threshold for the LRU/FIFO eviction policy, from `0.0` (full LRU) to `1.0` (full FIFO) |
| `--prepopulate-nb-items=N` | number of synthetic items to preload before the server starts accepting requests (`0` disables prepopulation) |
| `--prepopulate-key-length=N` | byte length of each synthetic prepopulated key |
| `--prepopulate-value-length=N` | byte length of each synthetic prepopulated value |
| *`CPU-MODE`* (positional) | right-shift applied to the detected lcore count to decide how many of the polled lcores actually run the server loop (`0` = all lcores; `1` = half; `2` = a quarter; ...) -- mainly useful for isolating single/few-core performance |
| *`TARGET-REQUEST-RATE`* (positional, `netbench_server_latency` only) | fixes the server's self-throttling target request rate (ops/s) instead of letting it adapt automatically |

The concurrency flags above select MICA's classic access modes:

| Mode | read | write | alloc-write |
|---|---|---|---|
| EREW (exclusive read/write) | 0 | 0 | 0 |
| CREW (concurrent read, exclusive write) | 1 | 0 | 0 |
| CRCW (concurrent read/write, per-thread allocators) | 1 | 1 | 0 |
| CRCWS (concurrent read/write, single shared allocator) | 1 | 1 | 1 |

### Prepopulation

If `--prepopulate-nb-items` is greater than `0`, the server fills its table with that many
synthetic key-value pairs before it starts accepting any real requests. **This data is not
random -- it is fully deterministic and reproducible**, generated the same way on every run:

- Keys are simply the integers `0` to `--prepopulate-nb-items - 1`, each encoded as a
  variable-length string of nibbles (index `i + 1` in base 16) sized up to
  `--prepopulate-key-length` bytes -- **not** an ASCII hex string.
- The `key_hash` each item is stored under is *not* `CityHash64` of that encoded key. It's
  `mehcached_hash_key()`'s hash of the raw 8-byte index itself, computed before the index is ever
  turned into the stored key bytes above.
- Values are a fixed, self-verifying 8-byte bit pattern derived from the key's index (the low and
  high 32 bits are bitwise complements of each other), zero-padded to `--prepopulate-value-length`
  bytes. They are placeholder data for exercising the store, not meaningful content.

Because generation is deterministic, an external client that wants to read back prepopulated keys
(e.g. to issue `GET`s against a warmed-up table instead of only `SET`s) must reproduce all three of
the above exactly for a given index -- key bytes *and* key_hash *and* expected value -- or a lookup
with the "obvious" hash (`CityHash64` of the key bytes) will land on the wrong bucket and come back
`NOT_FOUND` even though the data is there. Setting `--prepopulate-nb-items=0` (the default) skips
prepopulation entirely and the server starts with an empty table. `tools/gen_packets.py` (see
"Runtime considerations" below) automates this reproduction and verifies it.

## Runtime considerations

MICA has no client of its own -- whatever sends it requests must speak its wire format directly.
`tools/gen_packets.py` is a small scapy-based script that does exactly that: it builds
protocol-correct requests, sends them, and decodes the reply, to verify a running server in three
steps:

1. **it answers at all** -- a `NOOP_READ` gets a reply;
2. **its prepopulated data (if any) is there** -- a sample of the `--prepopulate-*` dataset (see
   above) is read back and checked byte-for-byte, using the same key/hash/value derivation as the
   server;
3. **it actually stores data** -- a `SET` followed by a `GET` round-trips the value.

It is meant to answer "is MICA working?", not to generate load -- see the script's own `--help`
for the full list of flags.

```sh
# --dst-mac is the NIC MAC netbench_server printed at startup ("port 0 MAC: ...")

# steps 1 and 3 only -- no --prepopulate-* given, so step 2 is skipped just like the server itself
# skips prepopulation when those flags are omitted
sudo ./tools/gen_packets.py --iface eth0 --dst-mac b8:3f:d2:37:42:4e

# all three steps -- pass the *same* --prepopulate-* values you started the server with
sudo ./tools/gen_packets.py --iface eth0 --dst-mac b8:3f:d2:37:42:4e \
    --prepopulate-nb-items 1000000 --prepopulate-key-length 8 --prepopulate-value-length 8
```

`tools/CLIent.py` is an interactive REPL built on the same wire-format code (it imports directly
from `gen_packets.py`), for poking at a server by hand instead of running a fixed check:

```sh
sudo ./tools/CLIent.py --iface eth0 --dst-mac b8:3f:d2:37:42:4e
```

```
mica> SET hello world
OK
mica> GET hello
world
mica> GET nosuchkey
NOT_FOUND
mica> exit
```

`--dst-mac` has to be the server's real MAC here, not a placeholder -- `netbench_server`'s
`rte_flow` rule steers each request to the partition-owning queue/thread by UDP destination port
alone, but a broadcast or wrong-unicast destination can be flooded to (or classified onto) the
wrong queue by the NIC itself before that rule ever applies, making every `SET`/`GET` fail.

`tools/gen_trace.py` generates a `.pcap` trace of `SET`/`GET` requests for replay with a real
load-generation tool (e.g. `tcpreplay`), rather than sending one request at a time like the two
tools above:

```sh
./tools/gen_trace.py --nb-packets 1000000 --percentage-set 5 --percentage-get 95 \
    --rate 100000 --dst-mac b8:3f:d2:37:42:4e --output workload.pcap

# then, on the machine wired to the server:
sudo tcpreplay --intf1=eth0 --pps=100000 workload.pcap
```

It only builds and serializes packets, so unlike the other two tools it needs neither an interface
nor raw-socket privileges to run -- `--dst-mac` is still required, though, since it's baked into the
generated packets and (for the reason above) determines real queue routing once the trace is
replayed. The requested `--percentage-set`/`--percentage-get` split is respected exactly (they must
add up to 100), and `GET`s only ever reference a key an earlier `SET` in the trace already wrote, so
replaying it produces real hits instead of a stream of `NOT_FOUND`s (the one unavoidable exception:
`--percentage-set 0` has no prior `SET`s to hit, so every `GET` misses by construction).

A request packet looks like this, from Ethernet up to MICA's own application layer:

- **Ethernet (14 bytes):** dst MAC must be the real MAC of the NIC `netbench_server` owns (printed
  at server startup) so the frame actually reaches the right port; src MAC is never inspected;
  EtherType `0x0800` (IPv4).
- **IPv4 (20 bytes, no options):** protocol `17` (UDP); src/dst addresses are arbitrary and never
  matched or validated -- MICA's `rte_flow` rule wildcards the whole IPv4 layer. Note the server's
  reply doesn't recompute the checksum after mutating the length field, so don't trust it either.
- **UDP (8 bytes):** src port is arbitrary (mirrored back on the reply so it finds its way to you);
  dst port is MICA's routing "mapping_id" -- the one field actually matched by `rte_flow` to steer
  the packet to a queue/lcore. `0` routes to the partition's exclusive owner thread (`--thread-id`);
  `1024+N` targets thread `N` directly (spread routing, only meaningful with concurrent reads/writes
  enabled).
- **MICA batch header (6 bytes, `struct mehcached_batch_packet` in `src/proto.h`):** `num_requests`
  (1 byte, max 36), `reserved0` (1 byte), `opaque` (4 bytes, echoed back, used for load feedback).
- **Per request (24 bytes each, `struct mehcached_request` in `src/table.h`), `num_requests` of
  them back to back:** `operation` (1 byte -- GET/SET/ADD/INCREMENT/NOOP_*; DELETE and TEST exist in
  the enum but aren't implemented server-side), `result` (1 byte, sent as 0, overwritten by the
  server), `reserved0` (2 bytes), `kv_length_vec` (4 bytes: top 8 bits key length, low 24 bits value
  length), `key_hash` (8 bytes, **client-supplied and trusted as-is** -- see below), `expire_time`
  (4 bytes), `reserved1` (4 bytes).
- **Trailing key/value data**, concatenated per request in the same order, each field individually
  padded up to the next 8-byte boundary: key bytes, then value bytes (value omitted entirely for
  GET, whose `kv_length_vec` value-length is 0).

On that `key_hash` field: MICA's table code (`src/table.c`) never re-derives it from the key bytes,
it just trusts whatever the request carries and uses it directly for bucket addressing and the
stored-item match check -- hashing is pushed onto whatever generates the request instead of being
redone by the server every time. A client that only ever reads back keys it wrote itself could use
any deterministic hash function and still work correctly (this is what `gen_packets.py`'s own
`SET`/`GET` round-trip, step 3 above, does: it hashes with `CityHash64` of the key bytes).

To also read back the data `--prepopulate-*` loaded (see above), a client needs more than a
matching hash *function* -- the prepopulated `key_hash` isn't `CityHash64` of the stored key bytes
at all. `mehcached_hash_key()` in `src/netbench_server.c` hashes the raw 8-byte index itself, before
it's ever turned into the hexadecimal key that's actually stored; a `GET` using
`CityHash64(key_bytes)`, as anything hashing its own keys would, looks up the wrong bucket and comes
back `NOT_FOUND` even though the data is really there. `gen_packets.py`'s step 2 reproduces both the
key encoding and this index-based hash exactly (`prepopulate_key_bytes()`/`prepopulate_key_hash()`
in the script), having been checked byte-for-byte against the real C implementation.

## License

    Copyright 2014 Carnegie Mellon University

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
