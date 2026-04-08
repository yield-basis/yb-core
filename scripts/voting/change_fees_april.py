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
from time import sleep, time

from boa.explorer import Etherscan
from boa.verifiers import verify as boa_verify


MARKETS = [3, 4, 5]

FEE = int(0.0091 * 10**18)   # 0.91%

FORK = True
EXTRA_TIMEOUT = 10

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"

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
    factory_owner = boa.load_partial('contracts/HybridFactoryOwner.vy').at(factory.admin())

    # Gather current market state
    market_data = []
    for market_id in MARKETS:
        market = factory.markets(market_id)
        lt = boa.load_partial('contracts/LT.vy').at(market.lt)
        amm = boa.load_partial('contracts/AMM.vy').at(market.amm)
        current_fee = amm.fee()

        market_data.append({
            'market_id': market_id,
            'lt': lt,
            'amm': amm,
            'current_fee': current_fee,
        })
        print(f"Market {market_id}: current fee={current_fee / 1e18:.4%}")

    # --- Vote: Set fee to 0.91% unconditionally ---
    actions = []
    for m in market_data:
        actions.append(Action(to=factory_owner.address, value=0,
                              data=factory_owner.lt_set_amm_fee.prepare_calldata(m['lt'].address, FEE)))

    title = 'Set AMM fee to 0.91% for markets 3, 4, 5'
    summary = 'Set AMM fee to 0.91% for markets 3, 4, 5 unconditionally.'

    if not FORK:
        proposal_id = voting.createProposal(*Proposal(
            metadata=pin_to_ipfs({
                'title': title,
                'summary': summary,
                'resources': []}).encode(),
            actions=actions,
            allowFailureMap=0,
            startDate=0,
            endDate=0,
            voteOption=0,
            tryEarlyExecution=True
        ))
        print(f"Proposal ID: {proposal_id}")

    if FORK:
        print(f"\n=== Simulating: {title} ===")
        with boa.env.prank(DAO):
            for action in actions:
                boa.env.raw_call(to_address=action.to, data=action.data)
        for m in market_data:
            print(f"  Market {m['market_id']} fee: {m['amm'].fee() / 1e18:.4%}")
