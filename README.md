MICA
====

A fast in-memory key-value store.


A note on the client
---------------------

MICA no longer ships its own load-generating client (the old `netbench_client*` executables and
the `mehcached_workload_conf`/`mehcached_get_workload_conf` config format that drove them are gone).
Generating and sending requests into `netbench_server` is the caller's responsibility -- use
whatever external packet generator or test harness you like. Whatever you use needs to speak
MICA's wire format (see `src/proto.h` for the packet layout and `mehcached_hash_key()` in
`src/netbench_server.c` for the key-hashing scheme that determines partition ownership/routing).


Hardware Requirements
---------------------

 * NICs supported by DPDK, with a PMD that implements `rte_flow` ETH/IPV4/UDP matching + a QUEUE
   action (the original 2014 code assumed Intel 82599 "ixgbe" 10 GbE NICs specifically and its own
   Flow Director filters; see "A note on DPDK version" below for why that changed).
 * Note: The current codebase has several assumptions on the hardware configuration of the server.
         It runs ideally on a dual octa-core server with 4 dual-port 10 GbE NICs.
 * `netbench_server.c` also hardcodes a couple of PCI addresses to blacklist
   (`"-b", "0000:06:00.0"`, `"-b", "0000:06:00.1"` in `mehcached_benchmark_server()`), left over from
   the original 2014 test machine. These almost certainly don't match your hardware's PCI addresses,
   so update or remove those `-b` arguments for your machine before running.


Software Requirements
---------------------

 * linux x86_64 >= 3.2.0
 * gcc >= 4.6.0
 * Python >= 2.7.0
 * DPDK, exposed to `pkg-config` as `libdpdk` (see "A note on DPDK version" below)
 * bash >= 4.0.0
 * cmake >= 3.6
 * Hugepages and a NIC bound to a DPDK-usable driver, set up ahead of time (see "A note on DPDK
   version" below) -- this is regular machine/DPDK setup, independent of MICA itself.


A note on DPDK version
-----------------------

This codebase originally targeted Intel DPDK 1.5 (2014) and expected a hand-built copy of it living
in a sibling `DPDK/` directory next to `mica/`, configured via `RTE_SDK`/`RTE_TARGET` and linked
against a fixed list of `.a` files. It has since been updated to build against a **modern DPDK
(verified with 23.11)** installed as a normal system package (or any DPDK built with `meson`/`ninja`
and `ninja install`), discovered the standard modern way via `pkg-config libdpdk`. Concretely:

 * You no longer need to unpack a DPDK source tree next to `mica/`, and `scripts/setup_dkdp_env.sh`
   (which built old DPDK from source and loaded the `igb_uio` kernel module) is obsolete -- it is
   kept only for reference to the pre-migration process. Install DPDK on your system such that
   `pkg-config --exists libdpdk` succeeds; that's the only thing MICA's own build depends on.
 * The legacy Flow Director-based request steering (`rte_eth_dev_fdir_*`) was ported to the generic
   `rte_flow` API, since Flow Director itself was removed from modern DPDK. See the comments around
   `mehcached_set_dst_port_mapping()` in `src/net_common.c` for the details of that migration.
 * Hugepage setup and binding a NIC to a userspace driver (`vfio-pci`, `uio_pci_generic`, etc. via
   DPDK's own `dpdk-devbind.py`/`dpdk-hugepages.py`) are ordinary DPDK prerequisites, not something
   MICA's build or these instructions cover -- set those up per your own DPDK install/distro's
   documentation before running any of the executables below. `scripts/unbind.sh`, which used the
   old `DPDK/tools/pci_unbind.py`, is obsolete for the same reason.


Executables
-----------

 * build/netbench_server: MICA server in cache mode
 * build/netbench_server_store: MICA server in store mode
 * build/netbench_server_latency: MICA server in cache mode modified for end-to-end latency measurement
 * build/netbench_server_soft_fdir: MICA server in cache mode using software-based request direction
 * build/netbench_analysis: workload analyzer (used for generating preset server configurations)
 * build/microbench: a local microbenchmark for MICA in cache mode
 * build/microbench_store: a local microbenchmark for MICA in store mode
 * build/test: a simple feature test program
 * build/load: a load factor experiment


Compiling Executables
---------------------

MICA is built with CMake; the only DPDK-specific prerequisite is that `pkg-config --exists libdpdk`
succeeds (see "A note on DPDK version" above) -- no DPDK source tree needs to sit next to `mica/`
any more.

	$ cd mica
	$ mkdir build && cd build
	$ cmake ..
	$ make

This is exactly what the bundled `configure_all.sh`/`configure_server.sh` wrapper scripts do (they
just set a `cmake` cache variable first -- `NDEBUG=yes` to disable extra runtime checks; `configure_server.sh`
is currently equivalent to `configure_all.sh` since there are no client-only executables left to
distinguish it from):

	$ cd mica
	$ ./configure_all.sh	# or configure_server.sh
	$ cd build
	$ make


Generating Configuration Files
------------------------------

	# conf_* files determine how MICA uses system resources. build/gen_confs.py generates a preset of server configuration files for a 16-core server
	# in mica
	$ ./run_analysis_for_conf.py	# this uses sudo
	$ ./gen_confs.py


Running a Server
----------------

	# in mica/build
	$ sudo ./netbench_server conf_machines_DATASET_CMODE_0.5 server 0 conf_prepopulation_empty
	# DATASET=0,1,2 (used to determine how much memory to allocate); CMODE=EREW,CREW,CRCWS (specifies the data access mode)

There is no bundled client any more -- see "A note on the client" above for how to send requests
into a running server.


Running a Local Microbenchmark
------------------------------

	# in mica/build
	$ sudo ./microbench CMODE SKEWNESS 0.5
	# CMODE=EREW,CREW,CRCWS (specifies the data acces mode); SKEWNESS=0(uniform),0.99(skewed),99(single) (specifies the workload skew)


License
-------

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

