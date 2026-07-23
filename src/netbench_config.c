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

#include "netbench_config.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

struct mehcached_server_conf *
mehcached_get_server_conf(const char *filename, const char *server_name)
{
	FILE *fp = fopen(filename, "r");
	if (!fp)
	{
		fprintf(stderr, "cannot open %s\n", filename);
		return NULL;
	}

	struct mehcached_server_conf *conf = malloc(sizeof(struct mehcached_server_conf));
	memset(conf, 0, sizeof(struct mehcached_server_conf));

	// how many server_thread lines have been parsed into conf->threads[] so far -- purely a parse-time
	// array index. The actual number of active threads is never read from this file: it's however many
	// cores DPDK was launched with (rte_lcore_count(), 1 thread per core -- see netbench_server.c).
	size_t num_threads = 0;

	while (true)
	{
		char buf[4096];
		int ret = fscanf(fp, "server,%[^,\n]\n", buf);
		if (ret == EOF)
			break;
		if (strcmp(buf, server_name) != 0)
		{
			// skip
			while (true)
			{
				if (fgets(buf, sizeof(buf), fp) == NULL)
					break;
				if (buf[0] == '\n')
					break;
			}
			continue;
		}

		while (true)
		{
			if (fgets(buf, sizeof(buf), fp) == NULL)
				break;

			{
				uint8_t port_id;
				ret = sscanf(buf, "server_thread,%hhu\n", &port_id);
				if (ret == 1)
				{
					conf->threads[num_threads].port_id = port_id;
					num_threads++;
					assert(num_threads <= MEHCACHED_MAX_THREADS);
					continue;
				}
				else if (ret != 0)
				{
					fprintf(stderr, "parse error: %s (in %s)\n", buf, filename);
					continue;
				}
			}
			{
				uint64_t num_items;
				uint64_t alloc_size;
				uint8_t concurrent_table_read;
				uint8_t concurrent_table_write;
				uint8_t concurrent_alloc_write;
				uint8_t thread_id;
				double mth_threshold;
				ret = sscanf(buf, "server_partition,%lu,%lu,%hhu,%hhu,%hhu,%hhu,%lf\n", &num_items, &alloc_size, &concurrent_table_read, &concurrent_table_write, &concurrent_alloc_write, &thread_id, &mth_threshold);
				if (ret == 7)
				{
					conf->partitions[conf->num_partitions].num_items = num_items;
					conf->partitions[conf->num_partitions].alloc_size = alloc_size;
					conf->partitions[conf->num_partitions].concurrent_table_read = concurrent_table_read;
					conf->partitions[conf->num_partitions].concurrent_table_write = concurrent_table_write;
					conf->partitions[conf->num_partitions].concurrent_alloc_write = concurrent_alloc_write;
					conf->partitions[conf->num_partitions].thread_id = thread_id;
					conf->partitions[conf->num_partitions].mth_threshold = mth_threshold;
					conf->num_partitions++;
					assert(conf->num_partitions <= MEHCACHED_MAX_PARTITIONS);
					continue;
				}
				else if (ret != 0)
				{
					fprintf(stderr, "parse error: %s (in %s)\n", buf, filename);
					continue;
				}
			}
			{
				uint64_t key_hash;
				uint8_t thread_id;
				ret = sscanf(buf, "server_hot_item,%lx,%hhu\n", &key_hash, &thread_id);
				if (ret == 2)
				{
					conf->hot_items[conf->num_hot_items].key_hash = key_hash;
					conf->hot_items[conf->num_hot_items].thread_id = thread_id;
					conf->num_hot_items++;
					assert(conf->num_hot_items <= MEHCACHED_MAX_HOT_ITEMS);
					continue;
				}
				else if (ret != 0)
				{
					fprintf(stderr, "parse error: %s (in %s)\n", buf, filename);
					continue;
				}
			}
			if (buf[0] == '\n')
				break;
			fprintf(stderr, "parse error: %s (in %s)\n", buf, filename);
		}
	}

	fclose(fp);
	return conf;
}

struct mehcached_prepopulation_conf *
mehcached_get_prepopulation_conf(const char *filename, const char *server_name)
{
	FILE *fp = fopen(filename, "r");
	if (!fp)
	{
		fprintf(stderr, "cannot open %s\n", filename);
		return NULL;
	}

	struct mehcached_prepopulation_conf *conf = malloc(sizeof(struct mehcached_prepopulation_conf));
	memset(conf, 0, sizeof(struct mehcached_prepopulation_conf));

	while (true)
	{
		char buf[4096];
		int ret = fscanf(fp, "prepopulation,%[^,\n]\n", buf);
		if (ret == EOF)
			break;
		if (strcmp(buf, server_name) != 0)
		{
			// skip
			while (true)
			{
				if (fgets(buf, sizeof(buf), fp) == NULL)
					break;
				if (buf[0] == '\n')
					break;
			}
			continue;
		}

		while (true)
		{
			if (fgets(buf, sizeof(buf), fp) == NULL)
				break;

			{
				uint64_t num_items;
				size_t key_length;
				size_t value_length;
				int ret = sscanf(buf, "dataset,%lu,%zu,%zu\n", &num_items, &key_length, &value_length);
				if (ret == 3)
				{
					conf->num_items = num_items;
					conf->key_length = key_length;
					conf->value_length = value_length;
					continue;
				}
				else if (ret != 0)
				{
					fprintf(stderr, "parse error: %s (in %s)\n", buf, filename);
					continue;
				}
			}

			if (buf[0] == '\n')
				break;
			fprintf(stderr, "parse error: %s (in %s)\n", buf, filename);
		}
	}

	fclose(fp);
	return conf;
}
