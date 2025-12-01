#!/usr/bin/env python3

import boa
import os
import json
import requests

from eth_account import account
from collections import namedtuple
from getpass import getpass
from networks import NETWORK
from networks import PINATA_TOKEN


FORK = False
EXTRA_TIMEOUT = 10
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"

USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"

Proposal = namedtuple("Proposal", ["metadata", "actions", "allowFailureMap", "startDate", "endDate", "voteOption",
                                   "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])

AMOUNTS = {"0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": 602448982,
           "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf": 2024372557,
           "0x18084fbA666a33d37592fA2633fD49a74DD93a88": 18538377540896722592}


def pin_to_ipfs(content: dict):
    url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
    headers = {
        "Authorization": f"Bearer {PINATA_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "pinataContent": content,
        "pinataMetadata": {"name": "pinnie.json"},
        "pinataOptions": {"cidVersion": 1},
    }

    response = requests.request("POST", url, json=payload, headers=headers)
    assert 200 <= response.status_code < 400

    return 'ipfs://' + response.json()["IpfsHash"]


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


if __name__ == '__main__':
    if FORK:
        boa.fork(NETWORK)
        boa.env.eoa = USER
    else:
        boa.set_network_env(NETWORK)
        USER = account_load('yb-deployer')
        boa.env.add_account(USER)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    lt_interface = boa.load_partial('contracts/LT.vy')
    lts = [lt_interface.at(factory.markets(i).lt) for i in range(3)]

    actions = []

    for lt in lts:
        amount = lt.balanceOf(DAO)
        min_amount = 0
        if amount > 0:
            min_amount = int(0.97 * lt.preview_withdraw(amount))
            actions.append(
                Action(
                    to=lt.address, value=0,
                    data=lt.withdraw.prepare_calldata(amount, min_amount)
                ))
        erc20 = boa.load_abi(os.path.dirname(__file__) + '/erc20.abi.json')
        TOKEN = lt.ASSET_TOKEN()
        token = erc20.at(TOKEN)
        actions.append(
            Action(
                to=TOKEN, value=0,
                data=token.transfer.prepare_calldata("0xa41074e0472E4e014c655dD143E9f5b87784a9DF", AMOUNTS[TOKEN])
            ))

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs({
            'totle': 'Prepare overcharged WBTC, cbBTC, tBTC for distribution to affected users',
            'summary': 'Convert fees from old pools to WBTC, cbBTC and tBTC. Send 6.02448982 WBTC, 20.24372557 cbBTC and 18.53837754089672 tBTC to 0xa41074e0472E4e014c655dD143E9f5b87784a9DF: they will get a distribution executed from there.',  # noqa
            'resources': []}).encode(),
        actions=actions,
        allowFailureMap=0,
        startDate=0,
        endDate=0,
        voteOption=0,
        tryEarlyExecution=True
    ))
    print(proposal_id)
