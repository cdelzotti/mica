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

#include "net_common.h"
#include "util.h"
#include "stopwatch.h"

#include <stdio.h>
#include <string.h>
#include <assert.h>

#include <rte_eal.h>
#include <rte_lcore.h>
#include <rte_byteorder.h>
#include <rte_ethdev.h>
#include <rte_flow.h>
#include <rte_log.h>
#include <rte_debug.h>

// data room size of each mbuf's packet buffer (excludes struct rte_mbuf itself and its private area,
// which rte_pktmbuf_pool_create() now accounts for on its own -- see mehcached_init_network())
#define MEHCACHED_MBUF_DATA_ROOM_SIZE (2048 + RTE_PKTMBUF_HEADROOM)
#define MEHCACHED_MBUF_SIZE (MEHCACHED_MAX_PORTS * MEHCACHED_MAX_QUEUES * 4096)     // TODO: need to divide by numa node count

#define MEHCACHED_MAX_PKT_BURST (32)

#define MEHCACHED_RX_PTHRESH (8)
#define MEHCACHED_RX_HTHRESH (8)
#define MEHCACHED_RX_WTHRESH (4)

#define MEHCACHED_TX_PTHRESH (36)
#define MEHCACHED_TX_HTHRESH (0)
#define MEHCACHED_TX_WTHRESH (0)

#define RTE_TEST_RX_DESC_DEFAULT (128)
#define RTE_TEST_TX_DESC_DEFAULT (512)
static uint16_t mehcached_num_rx_desc = RTE_TEST_RX_DESC_DEFAULT;
static uint16_t mehcached_num_tx_desc = RTE_TEST_TX_DESC_DEFAULT;

//#define MEHCACHED_USE_QUICK_SLEEP
//#define MEHCACHED_USE_DEEP_SLEEP

// NOTE: struct rte_eth_conf lost most of its .rxmode bitfields (header_split, hw_ip_checksum,
// hw_vlan_filter, jumbo_frame, hw_strip_crc, ...) somewhere around the DPDK 17-18 offload API
// rework. They are now opt-in flags in .rxmode.offloads (RTE_ETH_RX_OFFLOAD_*) / .txmode.offloads
// (RTE_ETH_TX_OFFLOAD_*), validated against what the device advertises in struct rte_eth_dev_info.
// Since this table only ever disabled everything, leaving .offloads at 0 (its zero-init default)
// reproduces the old behavior exactly, so there was nothing left to port for rxmode/txmode here.
//
// .fdir_conf, however, is gone completely: legacy Flow Director (rte_eth_dev_fdir_*, rte_fdir_filter,
// rte_fdir_masks) was replaced project-wide by the generic rte_flow API. There is no struct field to
// configure up front any more -- seem mehcached_set_dst_port_mask()/mehcached_set_dst_port_mapping()
// below, which is where the real complexity of this migration lives.
static const struct rte_eth_conf mehcached_port_conf = {
	.rxmode = {
		.mq_mode = RTE_ETH_MQ_RX_NONE,
	},
	.txmode = {
		.mq_mode = RTE_ETH_MQ_TX_NONE,
	},
};

static const struct rte_eth_rxconf mehcached_rx_conf = {
	.rx_thresh = {
		.pthresh = MEHCACHED_RX_PTHRESH,
		.hthresh = MEHCACHED_RX_HTHRESH,
		.wthresh = MEHCACHED_RX_WTHRESH,
	},
	.rx_free_thresh = 32,
	.rx_drop_en = 0,		// (does not seem to be used)
};

// NOTE: .txq_flags (ETH_TXQ_FLAGS_NOMULTSEGS | ETH_TXQ_FLAGS_NOREFCOUNT | ...) was removed from
// struct rte_eth_txconf along with the rest of the old offload bitfields; the closest surviving
// equivalent is the RTE_ETH_TX_OFFLOAD_MBUF_FAST_FREE offload (roughly: "no refcount, no multi-seg,
// safe to free mbufs the fast way"), and it must be checked against the port's dev_info.tx_offload_capa
// before being requested. Because of that per-port capability check, mehcached_tx_conf can no longer
// be a single file-scope constant: it is now built per port inside mehcached_init_network(), seeded
// from the port's own dev_info.default_txconf (itself a new concept -- previously the whole txconf
// was supplied by the application) with only the thresholds and offloads overridden.

struct mehcached_queue_state {
	struct rte_mbuf *rx_mbufs[MEHCACHED_MAX_PKT_BURST];
	uint16_t rx_length;
	uint16_t rx_next_to_use;

#ifdef MEHCACHED_USE_QUICK_SLEEP
	uint16_t rx_quick_sleep;
	uint16_t rx_full_quick_sleep_count;
#endif
#ifdef MEHCACHED_USE_DEEP_SLEEP
	uint64_t rx_last_seen;
	uint64_t rx_deep_sleep_until;
	uint64_t rx_inter_batch_time;
#endif

	struct rte_mbuf *tx_mbufs[MEHCACHED_MAX_PKT_BURST];
	uint16_t tx_length;

	uint64_t num_rx_burst;
	uint64_t num_rx_received;

	uint64_t num_tx_burst;
	uint64_t num_tx_sent;
	uint64_t num_tx_dropped;
} __rte_cache_aligned;

static struct rte_mempool *mehcached_pktmbuf_pool[MEHCACHED_MAX_NUMA_NODES];

//static uint16_t mehcached_lcore_to_queue[MEHCACHED_MAX_LCORES];
//static struct ether_addr mehcached_eth_addr[MEHCACHED_MAX_PORTS];

static struct mehcached_queue_state *mehcached_queue_states[MEHCACHED_MAX_QUEUES * MEHCACHED_MAX_PORTS];

// exact-match dst-port mask recorded by mehcached_set_dst_port_mask() and consumed by every
// subsequent mehcached_set_dst_port_mapping() call -- see the comment above mehcached_set_dst_port_mask().
static uint16_t mehcached_dst_port_mask = 0;

struct rte_mbuf *
mehcached_packet_alloc()
{
	return rte_pktmbuf_alloc(mehcached_pktmbuf_pool[rte_socket_id()]);
}

void
mehcached_packet_free(struct rte_mbuf *mbuf)
{
	rte_pktmbuf_free(mbuf);
}

struct rte_mbuf *
mehcached_receive_packet(uint8_t port_id)
{
	uint32_t lcore = rte_lcore_id();
	// uint16_t queue = mehcached_lcore_to_queue[lcore];
	// assert(queue != (uint16_t)-1);
	uint16_t queue = (uint16_t)lcore;
	struct mehcached_queue_state *state = mehcached_queue_states[queue * MEHCACHED_MAX_PORTS + port_id];

	if (state->rx_next_to_use == state->rx_length)
	{
#ifdef MEHCACHED_USE_QUICK_SLEEP
		if (state->rx_quick_sleep > 0)
		{
			// struct rte_mbuf *t = mehcached_packet_alloc();
			// if (t == NULL)
			// 	printf("cannot alloc mbuf\n");
			// mehcached_packet_free(t);
			state->rx_quick_sleep--;
			return NULL;
		}
#endif

#ifdef MEHCACHED_USE_DEEP_SLEEP
		uint64_t now = mehcached_stopwatch_now();

		// too small value makes deep sleep ineffective
		// too large value may incorrectly penalize a queue with occasional underflows
		const uint64_t max_deep_sleep_time = mehcached_stopwatch_1_usec * 50;

		// still need to sleep?
		if (state->rx_deep_sleep_until - now <= max_deep_sleep_time)
		{
			// assumed invariant: rx_deep_sleep_until <= now + max_deep_sleep_time
			//   (when no overflow happens)
			// the condition in the if statement checks the sleep time correctly under this invariant
			return NULL;
		}
#endif

		state->rx_length = rte_eth_rx_burst(port_id, queue, state->rx_mbufs, MEHCACHED_MAX_PKT_BURST);
		state->num_rx_received += state->rx_length;
		state->rx_next_to_use = 0;
		state->num_rx_burst++;

#ifdef MEHCACHED_USE_QUICK_SLEEP
		// sleep if no enough RX packets were received
		// this helps reduce PCIe traffic when # of RX packets is imbalanced across queues used by the same core
		state->rx_quick_sleep = (uint16_t)(MEHCACHED_MAX_PKT_BURST - state->rx_length);
		if (state->rx_length != 0)
			state->rx_full_quick_sleep_count = 0;
		else
		{
			if (state->rx_full_quick_sleep_count < 1024)
				state->rx_full_quick_sleep_count++;
			state->rx_quick_sleep = (uint16_t)(state->rx_quick_sleep * state->rx_full_quick_sleep_count);
		}

#endif

#ifdef MEHCACHED_USE_DEEP_SLEEP
		uint64_t to_sleep;
		uint64_t inter_batch_time;

		// adjust sleep time so that the next rx_burst can get MEHCACHED_MAX_PKT_BURST packets
		// note (state->rx_length + 1): this makes inter_batch_time slightly smaller than actual expectation
		// because we do not know whether there are additional subsequent batches
		inter_batch_time = (now - state->rx_last_seen) * MEHCACHED_MAX_PKT_BURST / (state->rx_length + 1);
		if (inter_batch_time > max_deep_sleep_time)
			inter_batch_time = max_deep_sleep_time;
		state->rx_last_seen = now;

		state->rx_inter_batch_time = (state->rx_inter_batch_time * 7 + inter_batch_time * 1) / 8;

		// deep sleep to prevent excessive PCIe traffic when RX across cores is imbalanced
		state->rx_deep_sleep_until = now + state->rx_inter_batch_time;

		// for debugging batch size
		// if ((state->num_rx_burst & 0xffffUL) == 0)
		// {
		// 	printf("port = %zu, queue = %zu; average_batch size = %lf, inter batch time = %lf us\n", port, queue, (double)state->num_rx_received / (double)state->num_rx_burst, (double)state->rx_inter_batch_time / (double)mehcached_stopwatch_1_usec);
		// 	state->num_rx_received = 0;
		// 	state->num_rx_burst = 0;
		// }
#endif
	}

	if (state->rx_next_to_use < state->rx_length)
    {
#ifndef NDEBUG
        //printf("mehcached_receive_packet: lcore=%zu, port=%zu, queue=%zu\n", lcore, port, queue);
#endif
		return state->rx_mbufs[state->rx_next_to_use++];
    }
	else
		return NULL;
}

void
mehcached_receive_packets(uint8_t port_id, struct rte_mbuf **mbufs, size_t *in_out_num_mbufs)
{
	uint32_t lcore = rte_lcore_id();
	// uint16_t queue = mehcached_lcore_to_queue[lcore];
	// assert(queue != (uint16_t)-1);
	uint16_t queue = (uint16_t)lcore;
	struct mehcached_queue_state *state = mehcached_queue_states[queue * MEHCACHED_MAX_PORTS + port_id];

	*in_out_num_mbufs = (size_t)rte_eth_rx_burst(port_id, queue, mbufs, (uint16_t)*in_out_num_mbufs);
	state->num_rx_received += *in_out_num_mbufs;
	state->num_rx_burst++;
}

void
mehcached_send_packet(uint8_t port_id, struct rte_mbuf *mbuf)
{
	uint32_t lcore = rte_lcore_id();
	// uint16_t queue = mehcached_lcore_to_queue[lcore];
	// assert(queue != (uint16_t)-1);
	uint16_t queue = (uint16_t)lcore;
	struct mehcached_queue_state *state = mehcached_queue_states[queue * MEHCACHED_MAX_PORTS + port_id];

#ifndef NDEBUG
    //printf("mehcached_send_packet: lcore=%zu, port=%zu, queue=%zu\n", lcore, port, queue);
#endif

	state->tx_mbufs[state->tx_length++] = mbuf;
	if (state->tx_length == MEHCACHED_MAX_PKT_BURST)
	{
		uint16_t count = rte_eth_tx_burst(port_id, queue, state->tx_mbufs, MEHCACHED_MAX_PKT_BURST);
		state->num_tx_sent += count;
		state->num_tx_dropped += (uint64_t)(MEHCACHED_MAX_PKT_BURST - count);
		for (; count < MEHCACHED_MAX_PKT_BURST; count++)
			rte_pktmbuf_free(state->tx_mbufs[count]);
		state->tx_length = 0;
		state->num_tx_burst++;
	}
}

void
mehcached_send_packet_flush(uint8_t port_id)
{
	uint32_t lcore = rte_lcore_id();
	// uint16_t queue = mehcached_lcore_to_queue[lcore];
	// assert(queue != (uint16_t)-1);
	uint16_t queue = (uint16_t)lcore;
	struct mehcached_queue_state *state = mehcached_queue_states[queue * MEHCACHED_MAX_PORTS + port_id];

	if (state->tx_length > 0)
	{
		uint16_t count = rte_eth_tx_burst(port_id, queue, state->tx_mbufs, state->tx_length);
		state->num_tx_sent += count;
		state->num_tx_dropped += (uint64_t)(state->tx_length - count);
		for (; count < state->tx_length; count++)
			rte_pktmbuf_free(state->tx_mbufs[count]);
		state->tx_length = 0;
		state->num_tx_burst++;
	}
}

void
mehcached_get_stats(uint8_t port_id, uint64_t *out_num_rx_burst, uint64_t *out_num_rx_received, uint64_t *out_num_tx_burst, uint64_t *out_num_tx_sent, uint64_t *out_num_tx_dropped)
{
	mehcached_get_stats_lcore(port_id, rte_lcore_id(), out_num_rx_burst, out_num_rx_received, out_num_tx_burst, out_num_tx_sent, out_num_tx_dropped);
}

void
mehcached_get_stats_lcore(uint8_t port_id, uint32_t lcore, uint64_t *out_num_rx_burst, uint64_t *out_num_rx_received, uint64_t *out_num_tx_burst, uint64_t *out_num_tx_sent, uint64_t *out_num_tx_dropped)
{
	// uint16_t queue = mehcached_lcore_to_queue[lcore];
	// assert(queue != (uint16_t)-1);
	uint16_t queue = (uint16_t)lcore;
	struct mehcached_queue_state *state = mehcached_queue_states[queue * MEHCACHED_MAX_PORTS + port_id];

	if (out_num_rx_burst)
		*out_num_rx_burst = state->num_rx_burst;
	if (out_num_rx_received)
		*out_num_rx_received = state->num_rx_received;
	if (out_num_tx_burst)
		*out_num_tx_burst = state->num_tx_burst;
	if (out_num_tx_sent)
		*out_num_tx_sent = state->num_tx_sent;
	if (out_num_tx_dropped)
		*out_num_tx_dropped = state->num_tx_dropped;

    //struct rte_eth_stats stats;
    //rte_eth_stats_get(port, &stats);
    //printf("port %zu i %lu o %lu ie %lu oe %lu\n", port, stats.ipackets, stats.opackets, stats.ierrors, stats.oerrors);
}

struct rte_mbuf *
mehcached_clone_packet(struct rte_mbuf *mbuf_src)
{
	return rte_pktmbuf_clone(mbuf_src, mehcached_pktmbuf_pool[rte_socket_id()]);
}

bool
mehcached_init_network(uint64_t cpu_mask, uint64_t port_mask, uint8_t *out_num_ports)
{
	int ret;
	size_t i;

	size_t num_numa_nodes = 0;
	uint16_t num_queues = 0;

	assert(rte_lcore_count() <= MEHCACHED_MAX_LCORES);

	// count required queues
	for (i = 0; i < rte_lcore_count(); i++)
	{
		if ((cpu_mask & ((uint64_t)1 << i)) != 0)
			num_queues++;
	}
	assert(num_numa_nodes <= MEHCACHED_MAX_QUEUES);

	// count numa nodes
	for (i = 0; i < rte_lcore_count(); i++)
	{
		uint32_t socket_id = (uint32_t)rte_lcore_to_socket_id((unsigned int)i);
		if (num_numa_nodes <= socket_id)
			num_numa_nodes = socket_id + 1;
	}
	assert(num_numa_nodes <= MEHCACHED_MAX_NUMA_NODES);

	// initialize pktmbuf
	for (i = 0; i < num_numa_nodes; i++)
	{
		printf("allocating pktmbuf on node %zu... \n", i);
		char pool_name[64];
		snprintf(pool_name, sizeof(pool_name), "pktmbuf_pool%zu", i);
		// if this is not big enough, RX/TX performance may not be consistent, e.g., between CREW and CRCW experiments
		// the mempool's per-core cache size ceiling used to be a hand-patched DPDK ./config knob
		// (CONFIG_RTE_MEMPOOL_CACHE_MAX_SIZE, see scripts/setup_dkdp_env.sh); on a meson-built DPDK
		// it is instead a build option (-Dmax_mempool_cache_size) baked into whatever package/tree
		// your system's libdpdk.pc points at, so that is what to check first if this call starts failing.
		const unsigned int cache_size = MEHCACHED_MAX_PORTS * 1024;
		// rte_pktmbuf_pool_create() replaces the old rte_mempool_create() + rte_pktmbuf_pool_init()/
		// rte_pktmbuf_init() callback trio: it derives the real mempool element size itself
		// (sizeof(struct rte_mbuf) + priv_size + data_room_size), so we now only pass the packet
		// data room size instead of the old hand-computed MEHCACHED_MBUF_ENTRY_SIZE.
		mehcached_pktmbuf_pool[i] = rte_pktmbuf_pool_create(pool_name, MEHCACHED_MBUF_SIZE, cache_size, 0, MEHCACHED_MBUF_DATA_ROOM_SIZE, (int)i);
		if (mehcached_pktmbuf_pool[i] == NULL)
		{
			fprintf(stderr, "failed to allocate mbuf for numa node %zu\n", i);
			return false;
		}
	}

	// NOTE: the old "initialize driver" step -- #ifdef RTE_LIBRTE_IXGBE_PMD rte_ixgbe_pmd_init() --
	// and the rte_eal_pci_probe() call that followed it are both gone. Modern DPDK PMDs self-register
	// via driver constructors, and rte_eal_init() itself scans and probes every bus (PCI included),
	// so there is nothing left for the application to trigger here.

	// check port and queue limits
	// rte_eth_dev_count() was removed; rte_eth_dev_count_avail() (or iterating with RTE_ETH_FOREACH_DEV)
	// is the replacement. It returns uint16_t now, but MEHCACHED_MAX_PORTS is fixed at 8, so keeping
	// mehcached_init_network()'s public uint8_t port_id/out_num_ports (net_common.h, unchanged) is safe.
	uint16_t num_ports_avail = rte_eth_dev_count_avail();
	assert(num_ports_avail <= MEHCACHED_MAX_PORTS);
	uint8_t num_ports = (uint8_t)num_ports_avail;
	*out_num_ports = num_ports;

	printf("checking queue limits\n");
	uint8_t port_id;
	for (port_id = 0; port_id < num_ports; port_id++)
	{
		if ((port_mask & ((uint64_t)1 << port_id)) == 0)
			continue;

		struct rte_eth_dev_info dev_info;
		// rte_eth_dev_info_get() used to return void; it now returns an int status that must be checked.
		ret = rte_eth_dev_info_get(port_id, &dev_info);
		if (ret != 0)
		{
			fprintf(stderr, "failed to get device info for port %hhu (err=%d)\n", port_id, ret);
			return false;
		}

		if (num_queues > dev_info.max_tx_queues || num_queues > dev_info.max_rx_queues)
		{
			fprintf(stderr, "device supports too few queues\n");
			return false;
		}
	}

	// map queues to lcores
	uint32_t lcore = 0;
	// uint16_t queue = 0;
// 	for (lcore = 0; lcore < rte_lcore_count(); lcore++)
// 	{
// 		if ((cpu_mask & ((uint64_t)1 << i)) == 0)
// 		{
// 			mehcached_lcore_to_queue[lcore] = (uint16_t)-1;
// 			continue;
// 		}

// 		mehcached_lcore_to_queue[lcore] = queue;
// #ifndef NDEBUG
// 		printf("queue %hhu mapped to lcore %hu\n", queue, lcore);
// #endif
// 		queue++;
// 	}

	// initialize ports
	for (port_id = 0; port_id < num_ports; port_id++)
	{
		if ((port_mask & ((uint64_t)1 << port_id)) == 0)
			continue;

		printf("initializing port %hhu...\n", port_id);

		// get mac address
		//rte_eth_macaddr_get((uint8_t)port, &mehcached_eth_addr[port]);

		struct rte_eth_dev_info dev_info;
		ret = rte_eth_dev_info_get(port_id, &dev_info);
		if (ret != 0)
		{
			fprintf(stderr, "failed to get device info for port %hhu (err=%d)\n", port_id, ret);
			return false;
		}

		// per-port copy: whether RTE_ETH_TX_OFFLOAD_MBUF_FAST_FREE can be requested depends on
		// dev_info, which is itself per port, so mehcached_port_conf can no longer be passed as-is.
		struct rte_eth_conf port_conf = mehcached_port_conf;
		if ((dev_info.tx_offload_capa & RTE_ETH_TX_OFFLOAD_MBUF_FAST_FREE) != 0)
			port_conf.txmode.offloads |= RTE_ETH_TX_OFFLOAD_MBUF_FAST_FREE;	// closest modern analogue of the old ETH_TXQ_FLAGS_NOREFCOUNT hint

		ret = rte_eth_dev_configure(port_id, num_queues, num_queues, &port_conf);
		if (ret < 0)
		{
			fprintf(stderr, "failed to configure port %hhu (err=%d)\n", port_id, ret);
			return false;
		}

		// new mandatory step: PMDs may require descriptor counts to be rounded up/down or clamped
		// to device-specific limits; rte_eth_rx/tx_queue_setup() no longer do this silently.
		uint16_t num_rx_desc = mehcached_num_rx_desc;
		uint16_t num_tx_desc = mehcached_num_tx_desc;
		ret = rte_eth_dev_adjust_nb_rx_tx_desc(port_id, &num_rx_desc, &num_tx_desc);
		if (ret < 0)
		{
			fprintf(stderr, "failed to adjust descriptor counts for port %hhu (err=%d)\n", port_id, ret);
			return false;
		}

		// see the comment above mehcached_rx_conf/mehcached_tx_conf's old declaration: the tx conf
		// is now seeded from the device's own default and only the thresholds/offloads we care
		// about are overridden, instead of being a single hardcoded file-scope constant.
		struct rte_eth_txconf tx_conf = dev_info.default_txconf;
		tx_conf.tx_thresh.pthresh = MEHCACHED_TX_PTHRESH;
		tx_conf.tx_thresh.hthresh = MEHCACHED_TX_HTHRESH;
		tx_conf.tx_thresh.wthresh = MEHCACHED_TX_WTHRESH;
		tx_conf.offloads = port_conf.txmode.offloads;

		uint32_t lcore;
		for (lcore = 0; lcore < rte_lcore_count(); lcore++)
		{
			// uint16_t queue = mehcached_lcore_to_queue[lcore];
			// if (queue == (uint16_t)-1)
			// 	continue;
			uint16_t queue = (uint16_t)lcore;

			size_t numa_node = rte_lcore_to_socket_id((unsigned int)lcore);

			ret = rte_eth_rx_queue_setup(port_id, queue, num_rx_desc, (unsigned int)numa_node, &mehcached_rx_conf, mehcached_pktmbuf_pool[numa_node]);
			if (ret < 0)
			{
				fprintf(stderr, "failed to configure port %hhu rx_queue %hu (err=%d)\n", port_id, queue, ret);
				return false;
			}

			ret = rte_eth_tx_queue_setup(port_id, queue, num_tx_desc, (unsigned int)numa_node, &tx_conf);
			if (ret < 0)
			{
				fprintf(stderr, "failed to configure port %hhu tx_queue %hu (err=%d)\n", port_id, queue, ret);
				return false;
			}
		}

		// start device
		ret = rte_eth_dev_start(port_id);
		if (ret < 0)
		{
			fprintf(stderr, "failed to start port %hhu (err=%d)\n", port_id, ret);
			return false;
		}

// 		// turn on promiscuous mode
// #ifndef NDEBUG
// 		printf("setting promiscuous mode on port %hhu...\n", port_id);
// #endif
// 		rte_eth_promiscuous_enable(port_id);
	}

	// the following takes some time, but this ensures the device ready for full speed RX/TX when the initialization is done
	// without this, the initial packet transmission may be blocked
	for (port_id = 0; port_id < num_ports; port_id++)
	{
		if ((port_mask & ((uint64_t)1 << port_id)) == 0)
			continue;

		printf("querying port %hhu... ", port_id);
		fflush(stdout);

		struct rte_eth_link link;
		// rte_eth_link_get() used to return void; it now returns an int status that must be checked.
		ret = rte_eth_link_get(port_id, &link);
		if (ret < 0)
		{
			fprintf(stderr, "failed to get link status for port %hhu (err=%d)\n", port_id, ret);
			return false;
		}
		if (!link.link_status)
		{
			printf("link down\n");
			return false;
		}

		// ETH_LINK_FULL_DUPLEX was renamed RTE_ETH_LINK_FULL_DUPLEX
		printf("%hu Gbps (%s)\n", link.link_speed / 1000, (link.link_duplex == RTE_ETH_LINK_FULL_DUPLEX) ? ("full-duplex") : ("half-duplex"));
	}

	memset(mehcached_queue_states, 0, sizeof(mehcached_queue_states));
	for (port_id = 0; port_id < num_ports; port_id++)
		for (lcore = 0; lcore < rte_lcore_count(); lcore++)
		{
			uint16_t queue = (uint16_t)lcore;
			mehcached_queue_states[queue * MEHCACHED_MAX_PORTS + port_id] = mehcached_eal_malloc_lcore(sizeof(struct mehcached_queue_state), lcore);
			memset(mehcached_queue_states[queue * MEHCACHED_MAX_PORTS + port_id], 0, sizeof(struct mehcached_queue_state));
		}

	return true;
}

void
mehcached_free_network(uint64_t port_mask)
{
	uint8_t port_id;
	uint8_t num_ports = (uint8_t)rte_eth_dev_count_avail();	// rte_eth_dev_count() was removed; see mehcached_init_network()

	for (port_id = 0; port_id < num_ports; port_id++)
	{
		if ((port_mask & ((uint64_t)1 << port_id)) == 0)
			continue;

		// modern apps are expected to tear down their own rte_flow rules explicitly (see the
		// mehcached_set_dst_port_mapping() comment) rather than relying on rte_eth_dev_stop()/
		// _close() to implicitly discard them the way legacy FDIR filters were.
		struct rte_flow_error flow_error;
		memset(&flow_error, 0, sizeof(flow_error));
		if (rte_flow_flush(port_id, &flow_error) != 0)
			fprintf(stderr, "warning: failed to flush flow rules on port %hhu (%s)\n", port_id, flow_error.message ? flow_error.message : "unknown error");

		printf("stopping port %hhu...\n", port_id);
		// rte_eth_dev_stop() used to return void; it now returns an int status. Teardown proceeds
		// best-effort regardless, so this is logged rather than treated as fatal.
		int ret = rte_eth_dev_stop(port_id);
		if (ret != 0)
			fprintf(stderr, "warning: failed to stop port %hhu (err=%d)\n", port_id, ret);
	}

	for (port_id = 0; port_id < num_ports; port_id++)
	{
		if ((port_mask & ((uint64_t)1 << port_id)) == 0)
			continue;

		printf("closing port %hhu...\n", port_id);
		// rte_eth_dev_close() used to return void; same best-effort logging as rte_eth_dev_stop() above.
		int ret = rte_eth_dev_close(port_id);
		if (ret != 0)
			fprintf(stderr, "warning: failed to close port %hhu (err=%d)\n", port_id, ret);
	}
}

// --- Flow Director -> rte_flow migration -------------------------------------------------------
//
// This pair of functions is the hardest part of this file to port, because legacy FDIR and rte_flow
// don't just rename a few symbols -- they model hardware packet steering differently:
//
//  * FDIR was configured in two separate steps against implicit, port-wide state: set a single mask
//    once (rte_eth_dev_fdir_set_masks()), then add any number of filters that were implicitly
//    matched against that shared mask (rte_eth_dev_fdir_add_perfect_filter()). rte_flow has no
//    port-wide state at all: every rule is a self-contained (pattern, mask, actions) tuple passed to
//    a single rte_flow_create() call. There is no "set the mask" primitive to call into any more --
//    mehcached_set_dst_port_mask() below just remembers the value for the next
//    mehcached_set_dst_port_mapping() call to embed into its own rule.
//
//  * FDIR's struct rte_fdir_filter was flat: one struct carried iptype + l4type + the UDP dst port to
//    match. rte_flow instead requires an explicit, layered *pattern* from L2 up (ETH, then IPV4, then
//    UDP, then an END marker) -- there is no "just match UDP dst port, whatever the packet is wrapped
//    in" shortcut. The ETH/IPV4 items below are left wildcarded (NULL spec/mask) purely to select
//    "IPv4-over-Ethernet", mirroring FDIR's old iptype = RTE_FDIR_IPTYPE_IPV4.
//
//  * FDIR identified a filter by an application-chosen "soft_id" you could use to look it up or
//    delete it later. rte_flow instead returns an opaque struct rte_flow* handle from
//    rte_flow_create() that IS the rule's identity; there is no soft_id/lookup-by-value concept.
//    MICA never reused soft_id for anything beyond bookkeeping, so this is a non-issue functionally,
//    but it does mean there is nowhere to stash per-rule handles if this code ever needs to remove
//    individual rules later (mehcached_free_network() above only supports flushing everything on a
//    port at once, via rte_flow_flush()).
//
//  * Not every PMD/NIC implements rte_flow UDP-dst-port -> queue steering, so, unlike the old direct
//    ioctl-style FDIR calls, it's worth validating the rule with rte_flow_validate() before actually
//    creating it, to get a clearer error message when the hardware can't do this.

bool
mehcached_set_dst_port_mask(uint8_t port_id, uint16_t l4_dst_port_mask)
{
	(void)port_id;	// no per-port DPDK call any more -- see the migration note above
	mehcached_dst_port_mask = l4_dst_port_mask;
	return true;
}

bool
mehcached_set_dst_port_mapping(uint8_t port_id, uint16_t l4_dst_port, uint32_t lcore)
{
	// uint16_t queue = mehcached_lcore_to_queue[lcore];
	// if (queue == (uint16_t)-1)
	// {
	// 	fprintf(stderr, "no queue on port %hhu exists for lcore %u\n", port_id, lcore);
	// 	return false;
	// }
	uint16_t queue = (uint16_t)lcore;

	struct rte_flow_item_udp udp_spec;
	memset(&udp_spec, 0, sizeof(udp_spec));
	udp_spec.hdr.dst_port = rte_cpu_to_be_16(l4_dst_port);	// this must be big-endian, same as the old filter.port_dst

	struct rte_flow_item_udp udp_mask;
	memset(&udp_mask, 0, sizeof(udp_mask));
	udp_mask.hdr.dst_port = rte_cpu_to_be_16(mehcached_dst_port_mask);

	struct rte_flow_item pattern[] = {
		{ .type = RTE_FLOW_ITEM_TYPE_ETH },	// wildcard: just says "there is an Ethernet header here"
		{ .type = RTE_FLOW_ITEM_TYPE_IPV4 },	// wildcard: "...carrying IPv4" (replaces filter.iptype)
		{ .type = RTE_FLOW_ITEM_TYPE_UDP, .spec = &udp_spec, .mask = &udp_mask },	// replaces filter.l4type + filter.port_dst
		{ .type = RTE_FLOW_ITEM_TYPE_END },
	};

	struct rte_flow_action_queue queue_action = { .index = queue };	// replaces the (uint8_t)queue argument to rte_eth_dev_fdir_add_perfect_filter()
	struct rte_flow_action actions[] = {
		{ .type = RTE_FLOW_ACTION_TYPE_QUEUE, .conf = &queue_action },
		{ .type = RTE_FLOW_ACTION_TYPE_END },
	};

	struct rte_flow_attr attr;
	memset(&attr, 0, sizeof(attr));
	attr.ingress = 1;

	struct rte_flow_error error;
	memset(&error, 0, sizeof(error));

	if (rte_flow_validate(port_id, &attr, pattern, actions, &error) != 0)
	{
		fprintf(stderr, "failed to add perfect filter entry on port %hhu (rule not supported: %s)\n", port_id, error.message ? error.message : "unknown error");
		return false;
	}

	struct rte_flow *flow = rte_flow_create(port_id, &attr, pattern, actions, &error);
	if (flow == NULL)
	{
		fprintf(stderr, "failed to add perfect filter entry on port %hhu (%s)\n", port_id, error.message ? error.message : "unknown error");
		return false;
	}

	return true;
}
