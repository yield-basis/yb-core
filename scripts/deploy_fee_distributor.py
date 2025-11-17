#!/usr/bin/env python3

import boa
import os
import json
from time import sleep
from eth_account import account
from getpass import getpass
from boa.explorer import Etherscan
from boa.verifiers import verify as boa_verify

from networks import NETWORK
from networks import ETHERSCAN_API_KEY


FORK = False
EXTRA_TIMEOUT = 10
DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # YB Deployer
INITIAL_TOKEN_SET = ["0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
                     "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
                     "0x18084fba666a33d37592fa2633fd49a74dd93a88"]
VE = "0x8235c179E9e84688FBd8B12295EfC26834dAC211"
ADMIN = DEPLOYER


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


def verify(*args, **kw):
    while True:
        try:
            sleep(EXTRA_TIMEOUT)
            boa_verify(*args, **kw)
            break
        except ValueError as e:
            print(e)
            if "Already Verified" in str(e):
                return


if __name__ == '__main__':
    if FORK:
        boa.fork(NETWORK)
    else:
        boa.set_network_env(NETWORK)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)

    if FORK:
        admin = DEPLOYER
        boa.env.eoa = admin
    else:
        admin = account_load('yb-deployer')
        boa.env.add_account(admin)

    fee_distributor = boa.load('contracts/dao/FeeDistributor.vy', INITIAL_TOKEN_SET, VE, ADMIN)
    if not FORK:
        verify(fee_distributor, etherscan, wait=True)

    print(fee_distributor.address)
