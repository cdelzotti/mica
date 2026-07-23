#!/usr/bin/python

class ServerConf:
    def __init__(self, server_name):
        self.server_name = server_name
        self.threads = []
        self.partitions = []
        self.hot_items = []

    def add_thread(self, port_id):
        self.threads.append(port_id)

    def add_partition(self, num_items, alloc_size, concurrent_table_read, concurrent_table_write, concurrent_alloc_write, thread_id, mth_threshold):
        self.partitions.append((num_items, alloc_size, concurrent_table_read, concurrent_table_write, concurrent_alloc_write, thread_id, mth_threshold))

    def add_hot_item(self, key_hash, thread_id):
        self.hot_items.append((key_hash, thread_id))

    def write(self, f):
        f.write('server,%s\n' % self.server_name)
        # port count is no longer part of the config at all: the server queries however many
        # ports DPDK actually reports at startup (see net_common.c/netbench_server.c).
        for port_id in self.threads:
            f.write('server_thread,%s\n' % port_id)
        for partition in self.partitions:
            f.write('server_partition,%s,%s,%s,%s,%s,%s,%s\n' % partition)
        for hot_item in self.hot_items:
            f.write('server_hot_item,%016x,%s\n' % hot_item)
        f.write('\n')

class PrePopulationConf:
    def __init__(self, server_name):
        self.server_name = server_name
        self.dataset = None

    def set(self, num_items, key_length, value_length):
        self.dataset = (num_items, key_length, value_length)

    def write(self, f):
        f.write('prepopulation,%s\n' % self.server_name)
        f.write('dataset,%s,%s,%s\n' % self.dataset)
        f.write('\n')

class ConcurrencyModel:
    def concurrent_table_read(self, partition_id): pass
    def concurrent_table_write(self, partition_id): pass
    def concurrent_alloc_write(self, partition_id): pass
    def thread_id(self, partition_id): pass
    def hot_items(self): pass

class EREW(ConcurrencyModel):
    name = 'EREW'
    def concurrent_table_read(self, partition_id): return 0
    def concurrent_table_write(self, partition_id): return 0
    def concurrent_alloc_write(self, partition_id): return 0
    def thread_id(self, partition_id): return partition_id % 16
    def hot_items(self): return []

class CREW(EREW):
    name = 'CREW'
    def concurrent_table_read(self, partition_id): return 1

class CRCW(EREW):
    name = 'CRCW'
    def concurrent_table_read(self, partition_id): return 1
    def concurrent_table_write(self, partition_id): return 1

class CRCWS(EREW):
    name = 'CRCWS'
    def concurrent_table_read(self, partition_id): return 1
    def concurrent_table_write(self, partition_id): return 1
    def concurrent_alloc_write(self, partition_id): return 1

class CREW0(CREW):
    name = 'CREW0'
    def thread_id(self, partition_id): return 0     # all writes go to core 0

# use this for EREW partitions, CREW hot items
#class LB(EREW):
# use this for CREW partitions and hot items (uncomment MEHCACHED_LOAD_BALANCE_USE_CREW_PARTITION in netbench_analysis.c)
class LB(CREW):
    def __init__(self, num_hot_items, zipf, get_ratio):
        self.name = 'LB-%d-%s-%.2f' % (num_hot_items, zipf[0], get_ratio)
        self.thread_id_list = None
        self.hot_item_list = None

        f = open('analysis_%d_%s_%.2f' % (num_hot_items, zipf[0], get_ratio))
        lines = list(f.readlines())
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.strip() == 'partition_to_thread:':
                self.thread_id_list = eval('[' + lines[i + 1].strip() + ']')
            elif line.strip() == 'hot_item_to_thread:':
                self.hot_item_list = eval('[' + lines[i + 1].strip() + ']')
            i += 1
        assert self.thread_id_list != None
        assert self.hot_item_list != None

    def thread_id(self, partition_id): return self.thread_id_list[partition_id]
    def hot_items(self): return self.hot_item_list


def main():
    datasets = [
            (8, 8, 192 * 1048576),
            (16, 64, 128 * 1048576),
            (128, 1024, 8 * 1048576),
        ]

    f = open('conf_prepopulation_empty', 'w')
    p = PrePopulationConf('server')
    p.set(0, 8, 8)
    p.write(f)

    for dataset, (key_length, value_length, num_items) in enumerate(datasets):
        assert key_length >= len('%x' % (num_items - 1))    # for hexadecimal key
        #num_partitions = 64
        num_partitions = 16

        concurrency_list = [EREW(), CREW(), CRCW(), CRCWS(), CREW0()]
        for num_hot_items in (0, 32):
            for zipf in (('uniform', 0.), ('skewed', 0.99), ('single', 99.)):
                for get_ratio in (0., 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 1.):
                    concurrency_list.append(LB(num_hot_items, zipf, get_ratio))

        mth_threshold_list = (1.0, 0.5, 0.0)

        for concurrency in concurrency_list:
            for mth_threshold in mth_threshold_list:
                f = open('conf_machines_%s_%s_%s' % (dataset, concurrency.name, mth_threshold), 'w')

                s = ServerConf('server')
                for thread_id in range(0, 16, 2):
                    s.add_thread(0)
                    s.add_thread(4)
                for partition_id in range(num_partitions):
                    num_items_per_partition = num_items / num_partitions
                    alloc_size_per_partition = num_items * (key_length + value_length) / num_partitions

                    concurrent_table_read = concurrency.concurrent_table_read(partition_id)
                    concurrent_table_write = concurrency.concurrent_table_write(partition_id)
                    concurrent_alloc_write = concurrency.concurrent_alloc_write(partition_id)
                    thread_id = concurrency.thread_id(partition_id)
                    s.add_partition(num_items_per_partition, alloc_size_per_partition, concurrent_table_read, concurrent_table_write, concurrent_alloc_write, thread_id, mth_threshold)
                for hot_item in concurrency.hot_items():
                    s.add_hot_item(*hot_item)
                s.write(f)

        f = open('conf_prepopulation_%s' % dataset, 'w')
        p = PrePopulationConf('server')
        p.set(num_items, key_length, value_length)
        p.write(f)


if __name__ == '__main__':
    main()
