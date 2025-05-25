#!/usr/bin/env python3

import boa
import json
import os
from getpass import getpass
from eth_account import account
from boa.explorer import Etherscan
from boa.contracts.vyper.vyper_contract import VyperBlueprint

from keys import ARBISCAN_KEY
from keys import ARBITRUM_NETWORK as NETWORK


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


ARBISCAN_URL = "https://api.arbiscan.io/api"

VyperBlueprint.ctor_calldata = b''  # Hack to make boa verify blueprints


if __name__ == '__main__':
    verifier = Etherscan(ARBISCAN_URL, ARBISCAN_KEY)
    boa.set_network_env(NETWORK)
    boa.env.add_account(account_load('yb-deployer'))
    boa.env._fork_try_prefetch_state = False

    vpool_impl = boa.load_partial('contracts/VirtualPool.vy').deploy_as_blueprint()
    boa.verify(vpool_impl, verifier)
