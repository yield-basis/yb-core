#!/usr/bin/env python3

# This is to test Aragon deployments mostly

import boa
import json
import os
import csv
from time import time
from boa.explorer import Etherscan
from boa.verifiers import verify
from eth_account import account
from getpass import getpass
from collections import defaultdict

from networks import ARBITRUM as NETWORK
from networks import ARBISCAN_API_KEY as ETHERSCAN_API_KEY


FORK = True
ETHERSCAN_URL = "https://api.arbiscan.io/api"

RATE = 1 / (4 * 365 * 86400)


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
                    yield [int(row[0]), row[1].strip(), float(row[2]), ','.join(row[3:])]


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


if __name__ == '__main__':
    for data_type, addr, amount, comment in read_data():
        vests[data_type].append((addr, amount, comment))

    if FORK:
        boa.fork(NETWORK)
    else:
        boa.set_network_env(NETWORK)
        etherscan = Etherscan(ETHERSCAN_URL, ETHERSCAN_API_KEY)

    if FORK:
        admin = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"
        boa.env.eoa = admin
    else:
        admin = account_load('yb-deployer')
        boa.env.add_account(admin)

    yb = boa.load('contracts/dao/YB.vy', int(vests[0][0][1] * 10**18), int(RATE * vests[0][0][1] * 10**18))
    if not FORK:
        verify(yb, etherscan, wait=False)
    ve_yb = boa.load('contracts/dao/VotingEscrow.vy', yb.address, 'Yield Basis', 'YB', '')
    if not FORK:
        verify(ve_yb, etherscan, wait=False)
    vpc = boa.load('contracts/dao/VotingPowerCondition.vy', ve_yb.address, 2_500 * 10**18)
    if not FORK:
        verify(vpc, etherscan, wait=False)
    gc = boa.load('contracts/dao/GaugeController.vy', yb.address, ve_yb.address)
    if not FORK:
        verify(gc, etherscan, wait=False)

    # Vests with cliff (1)

    cliff_impl = boa.load('contracts/dao/CliffEscrow.vy', yb.address, ve_yb.address, gc.address)
    if not FORK:
        verify(cliff_impl, etherscan, wait=False)
    t0 = int(time()) + 7 * 86400
    t1 = t0 + 2 * 365 * 86400 + 7 * 86400
    vesting = boa.load('contracts/dao/VestingEscrow.vy', yb.address, t0, t1, True, cliff_impl.address)
    if not FORK:
        verify(vesting, etherscan, wait=False)

    recipients = [row[0] for row in vests[1]]
    amounts = [int(row[1] * 10**18) for row in vests[1]]
    total = sum(amounts)

    yb.approve(vesting.address, 2**256 - 1)
    yb.mint(admin, total)
    vesting.add_tokens(total)
    vesting.fund(recipients, amounts, t0 + 365 * 86400 // 2)

    # No vesting no cliff allocations (2)

    for address, amount, comment in vests[2]:
        yb.mint(address, int(amount * 10**18))

    print(f"YB:      {yb.address}")
    print(f"veYB:    {ve_yb.address}")
    print(f"GC:      {gc.address}")
    print(f"CE:      {cliff_impl.address}")

    # Inflation-like vest(s) (3)
    for address, amount, comment in vests[3]:
        ivest = boa.load('contracts/dao/InflationaryVest.vy', yb.address, address, admin)
        yb.mint(ivest.address, int(amount * 10**18))
        ivest.start()
        print(f"IVest:   {ivest.address} for {address}")
