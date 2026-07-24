#!/usr/bin/python

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

        f = open('conf_prepopulation_%s' % dataset, 'w')
        p = PrePopulationConf('server')
        p.set(num_items, key_length, value_length)
        p.write(f)


if __name__ == '__main__':
    main()
