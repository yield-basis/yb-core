import csv
import os

from collections import defaultdict


VEST_TYPES = {
        0: 'inflation itself',
        1: '2 year vest with 6 months cliff',
        2: 'no vest and no cliff',
        3: 'inflation-like vest'
}

vests = defaultdict(list)


def read_data():
    name = os.path.dirname(__file__) + '/token_distribution_example.csv'
    with open(name, 'r') as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) > 0:
                if row[0] in ['0', '1', '2', '3']:
                    yield [int(row[0]), row[1], float(row[2]), ','.join(row[3:])]


if __name__ == '__main__':
    for vest_type, user, amount, comment in read_data():
        vests[vest_type].append([user, amount, comment])
