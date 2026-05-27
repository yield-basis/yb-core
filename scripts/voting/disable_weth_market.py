#!/usr/bin/env python3
"""
Disable old WETH market (id=6) by zeroing out its stablecoin allocation.

HybridFactoryOwner.lt_allocate_stablecoins(lt, 0) only marks `disabled_lts[lt] = True`
when called by ADMIN; the actual deallocation path enforces a safe-reserves check
that duplicates limits already enforced inside LT.vy. To bypass that redundant
gate without redeploying HybridFactoryOwner, the vote:

  1. HybridFactoryOwner.transfer_ownership_back() -- factory admin -> DAO
  2. HybridFactoryOwner.lt_allocate_stablecoins(weth_lt, 0) -- sets disabled_lts[weth_lt]=True
  3. weth_lt.allocate_stablecoins(0) -- DAO calls LT directly to zero stablecoin_allocation
  4. Factory.set_admin(HybridFactoryOwner, emergency_admin) -- restore factory admin

Result: disabled_lts(weth_lt) == True and weth_lt.stablecoin_allocation() == 0.
"""

import boa
import os
import json
import requests

from eth_account import account
from collections import namedtuple
from getpass import getpass
from networks import NETWORK
from networks import PINATA_TOKEN


MARKET_ID = 6  # Old WETH market

FORK = True

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
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = USER
    else:
        boa.set_network_env(NETWORK)
        USER = account_load('yb-deployer')
        boa.env.add_account(USER)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    factory_owner = boa.load_partial('contracts/HybridFactoryOwner.vy').at(factory.admin())
    emergency_admin = factory.emergency_admin()

    weth_lt = boa.load_partial('contracts/LT.vy').at(factory.markets(MARKET_ID).lt)

    factory_admin_before = factory.admin()
    print(f"Factory admin (HybridFactoryOwner): {factory_owner.address}")
    print(f"Factory emergency admin:            {emergency_admin}")
    print(f"WETH LT (market {MARKET_ID}):              {weth_lt.address}")
    print(f"  stablecoin_allocation before:     {weth_lt.stablecoin_allocation()}")
    print(f"  stablecoin_allocated before:      {weth_lt.stablecoin_allocated()}")
    print(f"  disabled_lts before:              {factory_owner.disabled_lts(weth_lt.address)}")

    actions = [
        # 1. HybridFactoryOwner -> DAO admin of Factory
        Action(
            to=factory_owner.address, value=0,
            data=factory_owner.transfer_ownership_back.prepare_calldata()
        ),
        # 2. Mark LT as disabled in HybridFactoryOwner (DAO is ADMIN; no factory call needed)
        Action(
            to=factory_owner.address, value=0,
            data=factory_owner.lt_allocate_stablecoins.prepare_calldata(weth_lt.address, 0)
        ),
        # 3. DAO calls LT directly to zero the allocation (bypasses redundant HybridFactoryOwner check)
        Action(
            to=weth_lt.address, value=0,
            data=weth_lt.allocate_stablecoins.prepare_calldata(0)
        ),
        # 4. Restore HybridFactoryOwner as factory admin
        Action(
            to=factory.address, value=0,
            data=factory.set_admin.prepare_calldata(factory_owner.address, emergency_admin)
        ),
    ]

    title = 'Disable old WETH market (id=6) by zeroing stablecoin allocation'
    summary = (
        'Transfer Factory admin from HybridFactoryOwner back to DAO; mark old WETH LT (market 6) '
        'as disabled in HybridFactoryOwner; DAO directly calls LT.allocate_stablecoins(0) to set '
        'stablecoin_allocation to 0 (LT.vy already enforces safe deallocation limits internally); '
        'restore HybridFactoryOwner as Factory admin so existing limits remain in place.'
    )

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

    else:
        print(f"\n=== Simulating: {title} ===")
        with boa.env.prank(DAO):
            for i, action in enumerate(actions):
                print(f"  action {i + 1}/{len(actions)} -> {action.to}")
                boa.env.raw_call(to_address=action.to, data=action.data)

        print()
        print(f"Factory admin after:                {factory.admin()}")
        print(f"  stablecoin_allocation after:      {weth_lt.stablecoin_allocation()}")
        print(f"  stablecoin_allocated after:       {weth_lt.stablecoin_allocated()}")
        print(f"  disabled_lts after:               {factory_owner.disabled_lts(weth_lt.address)}")

        assert factory.admin() == factory_admin_before, "Factory admin changed"
        assert factory.admin() == factory_owner.address, "Factory admin not restored"
        assert weth_lt.stablecoin_allocation() == 0, "stablecoin_allocation not zeroed"
        assert factory_owner.disabled_lts(weth_lt.address), "LT not marked disabled"
        print("\nAll assertions passed.")
