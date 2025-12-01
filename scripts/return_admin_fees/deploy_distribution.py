#!/usr/bin/env python3

import boa
import csv
import json
import os
from eth_account import account
from boa.explorer import Etherscan
from networks import NETWORK
from networks import ETHERSCAN_API_KEY
from getpass import getpass
from time import sleep
from boa.verifiers import verify


FORK = True
TOKEN = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"  # WBTC
# Total to send: 6.02448982 == 602448982 WBTC for 2187 users
# TOKEN = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"  # cbBTC
# Total to send: 20.24372557 == 2024372557 cbBTC for 343 users
# TOKEN = "0x18084fbA666a33d37592fA2633fD49a74DD93a88"  # tBTC
# Total to send: 18.53837754089672 == 18538377540896722592 tBTC for 593 users
DEPLOYER = "0xa41074e0472E4e014c655dD143E9f5b87784a9DF"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
BATCH_SIZE = 100

return_files = {
    "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": "overcharge-return-WBTC.csv",
    "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf": "overcharge-return-cbBTC.csv",
    "0x18084fbA666a33d37592fA2633fD49a74DD93a88": "overcharge-return-tBTC.csv"
}


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
        admin = account_load('???')
        boa.env.add_account(admin)

    deployed_multisend_filename = os.path.dirname(__file__) + f"/multisend-{TOKEN}.json"
    multisend_deployer = boa.load_partial('contracts/dao/Multisend.vy')

    erc20 = boa.load_abi(os.path.dirname(__file__) + '/erc20.abi.json')
    token = erc20.at(TOKEN)

    if os.path.exists(deployed_multisend_filename) and not FORK:
        with open(deployed_multisend_filename, 'r') as f:
            data = json.load(f)
            multisend = multisend_deployer.at(data['multisend'])
            token_amount = int(data['amount'])

    else:
        multisend = multisend_deployer.deploy(TOKEN)
        token.approve(multisend.address, 2**256-1)
        if FORK:
            with boa.env.prank(DAO):
                token.transfer(admin, token.balanceOf(DAO))
        else:
            sleep(30)
            verify(multisend, etherscan, wait=True)
        with open(deployed_multisend_filename, 'w') as f:
            token_amount = token.balanceOf(admin)
            json.dump({'multisend': multisend.address, 'amount': token_amount}, f)

    users = {}
    with open(os.path.dirname(__file__) + '/' + return_files[TOKEN], 'r') as f:
        reader = csv.reader(f)
        assert next(reader)[0] == "Address"
        for address, amount in reader:
            users[address] = float(amount)

    sum_amount = sum(users.values())
    users = {u: int(v * 10**token.decimals()) for u, v in users.items()}
    users = {u: v for u, v in users.items() if v > 0}
    print(f"Total in file: {sum_amount} {token.symbol()}")
    print(f"Total to send: {sum(users.values())/10**token.decimals()} == {sum(users.values())} {token.symbol()} for {len(users)} users")

    items = list(users.items())

    print("Balance before:", token.balanceOf(admin))

    while len(items) > 0:
        print(f"Items left: {len(items)}")
        batch = items[:BATCH_SIZE]
        items = items[len(batch):]
        users, amounts = list(zip(*batch))
        number_received = sum(multisend.already_sent(u) for u in users)
        if number_received == len(batch):
            print("Skip")
        else:
            before = token.balanceOf(admin)
            multisend.send(users, amounts)
            after = token.balanceOf(admin)
            print(f"Sent: {before - after}, Expected: {sum(amounts)}")
            if not FORK:
                sleep(60)

    print("Balance after:", token.balanceOf(admin))
