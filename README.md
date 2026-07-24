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
  variable-length hexadecimal string padded to `--prepopulate-key-length` bytes, and hashed with
  the exact same scheme (`mehcached_hash_key()`) real requests are hashed with.
- Values are a fixed, self-verifying 8-byte bit pattern derived from the key's index (the low and
  high 32 bits are bitwise complements of each other), zero-padded to `--prepopulate-value-length`
  bytes. They are placeholder data for exercising the store, not meaningful content.

Because key generation is deterministic, an external client that wants to read back prepopulated
keys (e.g. to issue `GET`s against a warmed-up table instead of only `SET`s) must generate keys
the same way: index `i` maps to the same hexadecimal encoding and the same `mehcached_hash_key()`
hash used above. Setting `--prepopulate-nb-items=0` (the default) skips prepopulation entirely and
the server starts with an empty table.

## Runtime considerations

MICA has no client of its own -- whatever sends it requests must speak its wire format directly.
`tools/gen_packets.py` is a small scapy-based script that does exactly that: it builds
protocol-correct requests, sends them, and decodes the reply, either as a one-off (`send`) or as a
SET/GET round-trip check (`selftest`). It is meant to answer "is MICA working?", not to generate
load -- see the script's own `--help` for usage.

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
any deterministic hash function and still work correctly. To also read back the data
`--prepopulate-*` loaded (see above), a client's hash must match the server's own
`mehcached_hash_key()` (`CityHash64`, from `src/city.c`) exactly -- which is what `gen_packets.py`
does, having been checked byte-for-byte against the real C implementation.

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
