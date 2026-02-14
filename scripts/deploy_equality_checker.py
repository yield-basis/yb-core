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


FORK = True
EXTRA_TIMEOUT = 10
DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # YB Deployer
YB = "0x01791F726B4103694969820be083196cC7c045fF"


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

    checker = boa.load('contracts/dao/EqualityChecker.vy')
    if not FORK:
        verify(checker, etherscan, wait=True)

    print(checker.address)

    if FORK:
        yb = boa.load_partial('contracts/dao/YB.vy').at(YB)
        total_supply = yb.totalSupply()
        print(f"YB totalSupply: {total_supply}")

        selector = boa.eval('method_id("totalSupply()")')
        # Should pass - totalSupply equals itself
        checker.check_equal(YB, selector, total_supply)
        print("check_equal passed")

        # Should pass - totalSupply is not 0
        checker.check_nonequal(YB, selector, 0)
        print("check_nonequal passed")

        # Should revert - totalSupply != 0
        try:
            checker.check_equal(YB, selector, 0)
            raise Exception("check_equal should have reverted")
        except boa.BoaError:
            print("check_equal correctly reverted for wrong value")

        # Should revert - totalSupply == total_supply
        try:
            checker.check_nonequal(YB, selector, total_supply)
            raise Exception("check_nonequal should have reverted")
        except boa.BoaError:
            print("check_nonequal correctly reverted for equal value")
