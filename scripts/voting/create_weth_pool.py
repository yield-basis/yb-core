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


CURVE_POOL = "0x6e5492F8ea2370844EE098A56DD88e1717e4A9C2"
AMM_FEE = int(1.4 * 10**16)  # 1.4%
RATE = int(2 * 0.005 * 1e18 / (365 * 86400))


FORK = True
EXTRA_TIMEOUT = 10
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
GAUGE_CONTROLLER = "0x1Be14811A3a06F6aF4fA64310a636e1Df04c1c21"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CURVE_FACTORY = "0x98EE851a00abeE0d95D08cF4CA2BdCE32aeaAF7F"

USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"

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
        USER = account_load('yb-deployer')
        boa.env.add_account(USER)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    gauge_controller = boa.load_partial('contracts/dao/GaugeController.vy').at(GAUGE_CONTROLLER)
    factory_owner = boa.load_partial('contracts/MigrationFactoryOwner.vy').at(factory.admin())

    actions = [
        Action(to=factory_owner.address, value=0,
               data=factory_owner.add_market.prepare_calldata(
                   CURVE_POOL,
                   AMM_FEE,
                   RATE,
                   2 * 25_000_000 * 10**18
                   )
               )
    ]

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs({
            'title': 'Create a pool with WETH',
            'summary': 'Create yb-WETH pool with $25M cap',
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
