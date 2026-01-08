#!/usr/bin/env python3

import boa
import os
import json
import requests

from eth_account import account
from collections import namedtuple
from getpass import getpass
from networks import NETWORK
from networks import ETHERSCAN_API_KEY
from networks import PINATA_TOKEN
from time import sleep

from boa.explorer import Etherscan
from boa.verifiers import verify as boa_verify


FORK = False
VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
FEE_DISTRIBUTOR = "0xD11b416573EbC59b6B2387DA0D2c0D1b3b1F7A90"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
USER = "0x8F7D0C877c99eD71ce68d41e741B6b4C959853D5"

EXTRA_TIMEOUT = 10


Proposal = namedtuple("Proposal", ["metadata", "actions", "allowFailureMap", "startDate", "endDate", "voteOption",
                                   "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])


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
        USER = account_load('yb-deployer-a')
        boa.env.add_account(USER)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_abi(os.path.dirname(__file__) + '/Factory.abi.json', name="Factory").at("0x370a449FeBb9411c95bf897021377fe0B7D100c0")
    fee_distributor = boa.load_partial('contracts/dao/FeeDistributor.vy').at(FEE_DISTRIBUTOR)
    lts = [factory.markets(i).lt for i in [3, 4, 5, 6]]
    print(lts)

    actions = [Action(to=fee_distributor.address, value=0, data=fee_distributor.add_token_set.prepare_calldata(lts))]

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs({
            'title': 'Add yb-WETH to FeeDistributor',
            'summary': 'Allow FeeDistributor to use and distribute fees made by yb-WETH pool',
            'resources': []}).encode(),
        actions=actions,
        allowFailureMap=0,
        startDate=0,
        endDate=0,
        voteOption=0,
        tryEarlyExecution=True
    ))
    print(proposal_id)

    if FORK:
        print("Simulating execution")
        with boa.env.prank(DAO):
            for i, action in enumerate(actions):
                print(i + 1, 'out of', len(actions))
                boa.env.raw_call(to_address=action.to, data=action.data)
