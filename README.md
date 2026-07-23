MICA
====

A fast in-memory key-value store.


Hardware Requirements
---------------------

 * Dual CPU system
 * NICs supported by DPDK, with a PMD that implements `rte_flow` ETH/IPV4/UDP matching + a QUEUE
   action (the original 2014 code assumed Intel 82599 "ixgbe" 10 GbE NICs specifically and its own
   Flow Director filters; see "A note on DPDK version" below for why that changed).
 * Note: The current codebase has several assumptions on the hardware configuration of the server and clients.
         It runs ideally on a dual octa-core server with 4 dual-port 10 GbE NICs, and clients with 2 dual-port 10 GbE NICs.
 * `netbench_server.c`/`netbench_client.c` also hardcode a couple of PCI addresses to blacklist
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

 * build/netbench_server: MICA server in cache mode (use with netbench_client)
 * build/netbench_server_store: MICA server in store mode (use with netbench_client)
 * build/netbench_server_latency: MICA server in cache mode modified for end-to-end latency measurement (use with netbench_client_latency)
 * build/netbench_server_soft_fdir: MICA server in cache mode using software-based request direction (use with netbench_client_soft_fdir)
 * build/netbench_client*: MICA clients
 * build/netbench_analysis: workload analyzer (used for generating preset configurations)
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

This is exactly what the bundled `configure_all.sh`/`configure_server.sh`/`configure_client.sh`
wrapper scripts do (they just set a couple of `cmake` cache variables first -- `NDEBUG=yes` to
disable extra runtime checks, and `NSERVER`/`NCLIENT` to skip building the server- or client-only
executables):

	$ cd mica
	$ ./configure_all.sh	# or configure_server.sh / configure_client.sh
	$ cd build
	$ make


Generating Configuration Files
------------------------------

	# conf_* files determine how MICA uses system resources. build/gen_confs.py generates a preset of configuration files for a 16-core server and 12-core clients
	# in mica
	$ ./run_analysis_for_conf.py	# this uses sudo
	$ ./gen_confs.py


Running a Server
----------------

	# in mica/build
	$ sudo ./netbench_server conf_machines_DATASET_CMODE_0.5 server 0 0 conf_prepopulation_empty
	# DATASET=0,1,2 (used to determine how much memory to allocate); CMODE=EREW,CREW,CRCWS (specifies the data access mode)


Running a Client (e.g., client0)
--------------------------------

	# in mica/build
	$ sudo ./netbench_client conf_machines_DATASET_CMODE_0.5 client0 0 0 conf_workload_DATASET_SKEW_GET_PUT_0.00_1
	# DATASET=0,1,2 (specifies the dataset to use); SKEW=uniform,skewed,single (specifies the workload skew); GET/PUT=0.00,0.50,0.95,1.00 (specifies the read/write ratio)


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

