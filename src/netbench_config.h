// Copyright 2014 Carnegie Mellon University
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#pragma once

#include "common.h"
#include "net_common.h"

//#define MEHCACHED_MAX_PORTS (8)

// server
struct mehcached_server_partition_conf
{
	uint64_t num_items;
	uint64_t alloc_size;
	uint8_t concurrent_table_read;
	uint8_t concurrent_table_write;
	uint8_t concurrent_alloc_write;
	uint8_t thread_id;
	double mth_threshold;
};

struct mehcached_server_conf
{
	uint8_t num_ports;	// not read from config at all any more -- set from however many ports
				// DPDK actually reports (see mehcached_init_network()/netbench_server.c)
	// a single DPDK application instance now serves exactly one partition -- no partition array,
	// no in-process partition routing, and no config file either: the partition fields are taken
	// directly as CLI arguments now (see main() in netbench_server.c). To serve multiple
	// partitions, run one instance per partition (each on its own port/--file-prefix).
	struct mehcached_server_partition_conf partition;
};

#define MEHCACHED_CONCURRENT_TABLE_READ(server_conf) ((server_conf)->partition.concurrent_table_read)
#define MEHCACHED_CONCURRENT_TABLE_WRITE(server_conf) ((server_conf)->partition.concurrent_table_write)
#define MEHCACHED_CONCURRENT_ALLOC_WRITE(server_conf) ((server_conf)->partition.concurrent_alloc_write)


// prepopulation
// populated directly from CLI arguments now (--prepopulate-nb-items, --prepopulate-key-length,
// --prepopulate-value-length; see main() in netbench_server.c) -- no config file involved.
struct mehcached_prepopulation_conf
{
	uint64_t num_items;
	size_t key_length;
	size_t value_length;
};
