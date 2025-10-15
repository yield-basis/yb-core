#!/usr/bin/env python3

import boa
import json
import os
from eth_account import account
from boa.explorer import Etherscan
from networks import NETWORK
from networks import ETHERSCAN_API_KEY
from getpass import getpass
from time import sleep
from time import time
from boa.verifiers import verify


FORK = True
YB = "0x01791F726B4103694969820be083196cC7c045fF"
DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # YB Deployer
TEST_YB_HOLDER = "0xdD6969f143D919C72052111c6679b21c71268b7a"
VESTING_SHIFT = 5 * 60  # s
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
FUND_AMOUNT = 5_625_000 * 10**18


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


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
        admin = account_load('distribution-voter')
        boa.env.add_account(admin)

    t0 = int(time()) + VESTING_SHIFT
    t1 = t0 + 365 * 86400

    vesting = boa.load('contracts/dao/VestingEscrow.vy', YB, t0, t1, True, ZERO_ADDRESS)
    if not FORK:
        sleep(30)
        verify(vesting, etherscan, wait=True)

    yb_interface = boa.load_partial('contracts/dao/YB.vy')
    yb = yb_interface.at(YB)
    yb.approve(vesting.address, 2**256-1)
    if not FORK:
        sleep(30)

    if FORK:
        with boa.env.prank(TEST_YB_HOLDER):
            yb.transfer(admin, FUND_AMOUNT)

    with open(os.path.dirname(__file__) + "/split-vested.json", "r") as f:
        users = json.load(f)

    amount = sum(users.values())
    print(f"Total to vest: {amount/1e18} for {len(users)} users")

    vesting.add_tokens(amount)
    if not FORK:
        sleep(30)

    recipients, amounts = list(zip(*users.items()))
    vesting.fund(recipients, amounts, 0)

    print(vesting.unallocated_supply())
