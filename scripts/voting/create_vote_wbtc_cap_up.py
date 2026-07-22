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


# WBTC market. Cap ($) = stablecoin_allocation / 2 (2x leverage), so bumping the
# cap by CAP_INCREASE_USD requires allocating +2 * CAP_INCREASE_USD crvUSD.
MARKET_ID = 7
CAP_INCREASE_USD = 10 * 10**6                    # +$10M cap
CRVUSD_INCREASE = 2 * CAP_INCREASE_USD * 10**18  # +$20M crvUSD allocation

FORK = True
EXTRA_TIMEOUT = 10
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"

# Whale with voting power, used only to simulate createProposal in fork mode.
# In production the proposal is created by the yb-deployer account.
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
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = USER
    else:
        boa.set_network_env(NETWORK)
        USER = account_load('yb-deployer')
        boa.env.add_account(USER)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    factory_owner = boa.load_partial('contracts/MigrationFactoryOwner.vy').at(factory.admin())

    market = factory.markets(MARKET_ID)
    lt = boa.load_partial('contracts/LT.vy').at(market.lt)

    symbol = lt.symbol()
    assert symbol == 'yb-WBTC', f"Market {MARKET_ID} is {symbol}, expected yb-WBTC"

    current_allocation = lt.stablecoin_allocation()
    new_allocation = current_allocation + CRVUSD_INCREASE

    print(f"Market {MARKET_ID} ({symbol}), LT {lt.address}")
    print(f"  crvUSD allocation: {current_allocation / 1e18:,.0f} -> {new_allocation / 1e18:,.0f}")
    print(f"  cap ($):           {current_allocation / 2 / 1e18:,.0f} -> {new_allocation / 2 / 1e18:,.0f}")

    actions = [
        Action(to=factory_owner.address, value=0,
               data=factory_owner.lt_allocate_stablecoins.prepare_calldata(lt.address, new_allocation))
    ]

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs({
            'title': 'Increase WBTC market cap by $10M',
            'summary': 'Raise the WBTC (yb-WBTC) market cap by $10M, from ~$22M to ~$32M. This requires allocating '
                       '+$20M crvUSD to it (cap = crvUSD allocation / 2 under 2x leverage), bringing its total crvUSD '
                       'allocation to ~$64M.',
            'resources': []}).encode(),
        actions=actions,
        allowFailureMap=0,
        startDate=0,
        endDate=0,
        voteOption=0,
        tryEarlyExecution=True
    ))
    print("Proposal ID:", proposal_id)

    if FORK:
        print("\nSimulating execution")
        print(f"  old allocation: {lt.stablecoin_allocation() / 1e18:,.0f} crvUSD  (cap ${lt.stablecoin_allocation() / 2 / 1e18:,.0f})")

        with boa.env.prank(DAO):
            for i, action in enumerate(actions):
                print(f"  action {i + 1} out of {len(actions)}")
                boa.env.raw_call(to_address=action.to, data=action.data)

        final_allocation = lt.stablecoin_allocation()
        print(f"  new allocation: {final_allocation / 1e18:,.0f} crvUSD  (cap ${final_allocation / 2 / 1e18:,.0f})")
        assert final_allocation == new_allocation, "Allocation not updated as expected"
        print("OK")
