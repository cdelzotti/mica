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

#include <stdint.h>
#include <unistd.h>

// This used to be a hand-rolled `typedef int bool;` (2014-era, predating widespread <stdbool.h>
// use). Modern DPDK's own public headers (e.g. rte_stdatomic.h) now pull in <stdbool.h> transitively,
// which -- once the codebase links against a modern DPDK -- redefines the `bool` token as the builtin
// _Bool partway through translation units that include a DPDK header after this one. Because the old
// typedef and <stdbool.h>'s macro don't agree (int vs _Bool), any function whose prototype was parsed
// before that point and definition after (or vice versa) ends up with "conflicting types" errors.
// Using <stdbool.h> directly instead makes MICA's own `bool` and DPDK's `bool` the exact same
// definition everywhere, which removes the conflict. This is safe: no on-wire/packed struct in this
// codebase uses `bool` as a field type (they all use explicit-width types like uint8_t), so nothing
// depends on the old sizeof(bool) == sizeof(int).
#ifndef __cplusplus
#include <stdbool.h>
#endif

