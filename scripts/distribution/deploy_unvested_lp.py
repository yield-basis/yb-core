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
# from boa.verifiers import verify


FORK = False
YB = "0x01791F726B4103694969820be083196cC7c045fF"
DEPLOYER = "0xa41074e0472E4e014c655dD143E9f5b87784a9DF"
BATCH_SIZE = 100


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
        admin = account_load('ADMIN')
        boa.env.add_account(admin)

    # multisend = boa.load('contracts/dao/Multisend.vy', YB)
    multisend = boa.load_partial('contracts/dao/Multisend.vy').at("0x0926a73Bb2D169a86c2Da9c7E03aC63A5cf42883")
    # if not FORK:
    #     sleep(30)
    #     verify(multisend, etherscan, wait=True)

    yb_interface = boa.load_partial('contracts/dao/YB.vy')
    yb = yb_interface.at(YB)
    # yb.approve(multisend.address, 2**256-1)
    # if not FORK:
    #     sleep(30)

    with open(os.path.dirname(__file__) + "/split-nonvested.json", "r") as f:
        users = json.load(f)

    amount = sum(users.values())
    print(f"Total to send: {amount/1e18} for {len(users)} users")

    items = list(users.items())

    print("Balance before:", yb.balanceOf(admin))
    # assert yb.balanceOf(admin) >= amount

    while len(items) > 0:
        print(f"Items left: {len(items)}")
        batch = items[:BATCH_SIZE]
        items = items[len(batch):]
        users, amounts = list(zip(*batch))
        number_received = sum(multisend.already_sent(u) for u in users)
        if number_received == len(batch):
            print("Skip")
        else:
            before = yb.balanceOf(admin)
            multisend.send(users, amounts)
            after = yb.balanceOf(admin)
            print("Sent:", before - after, ", Expected:", sum(amounts))
            if not FORK:
                sleep(60)

    print("Balance after:", yb.balanceOf(admin))
