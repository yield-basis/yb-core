#!/usr/bin/env python3

import boa
import json
import os
import sys
import subprocess
from time import sleep
from getpass import getpass
from eth_account import account
from collections import namedtuple
from boa.explorer import Etherscan

from keys import ARBISCAN_KEY


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


Market = namedtuple('Market', ['asset', 'cryptopool', 'amm', 'lt', 'price_oracle', 'virtual_pool', 'staker'])


NETWORK = "https://arbitrum.drpc.org"
ARBISCAN_URL = "https://api.arbiscan.io/api"
HARDHAT_COMMAND = ["npx", "hardhat", "node", "--fork", "https://arbitrum.drpc.org", "--port", "8545"]

YB_MULTISIG = "0xd396db54cAB0eCB51d43e82f71adc0B70a077aAF"
USD_TOKEN = "0x498Bf2B1e120FeD3ad3D42EA2165E9b73f99C1e5"  # crvUSD on arbutrum


if __name__ == '__main__':
    if '--hardhat' in sys.argv[1:]:
        hardhat = subprocess.Popen(HARDHAT_COMMAND)
        sleep(10)

    verifier = Etherscan(ARBISCAN_URL, ARBISCAN_KEY)
    boa.set_network_env(NETWORK)
    boa.env.add_account(account_load('yb-deployer'))
    boa.env._fork_try_prefetch_state = False

    flash = boa.load("contracts/testing/FlashLender.vy", USD_TOKEN, YB_MULTISIG)
    boa.verify(flash, verifier)

    if '--hardhat' in sys.argv[1:]:
        hardhat.wait()
