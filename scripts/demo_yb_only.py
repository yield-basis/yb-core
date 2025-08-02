#!/usr/bin/env python3

# This is to test Aragon deployments mostly

import boa
import json
import os
from boa.explorer import Etherscan
from boa.verifiers import verify
from eth_account import account
from getpass import getpass

from networks import ARBITRUM
from networks import ARBISCAN_API_KEY


NETWORK = "http://localhost:8545"

RESERVE = 10**9 * 10**18
RATE = 10**9 * 10**18 // (4 * 365 * 86400)


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


if __name__ == '__main__':
    boa.set_network_env(ARBITRUM)
    arbiscan = Etherscan("https://api.arbiscan.io/api", ARBISCAN_API_KEY)

    admin = account_load('yb-deployer')
    boa.env.add_account(admin)

    yb = boa.load('contracts/dao/YB.vy', RESERVE, RATE)
    verify(yb, arbiscan, wait=False)
    ve_yb = boa.load('contracts/dao/VotingEscrow.vy', yb.address, 'Yield Basis', 'YB', '')
    verify(ve_yb, arbiscan, wait=False)

    print(f"YB:      {yb.address}")
    print(f"veYB:    {ve_yb.address}")
