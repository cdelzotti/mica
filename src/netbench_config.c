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
