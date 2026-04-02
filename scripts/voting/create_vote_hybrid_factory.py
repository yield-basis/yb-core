#!/usr/bin/env python3
"""
Deploy HybridFactoryOwner, HybridVaultFactory, and new LTMigrator,
then create a single DAO vote to:
  1. Transfer Factory from old MigrationFactoryOwner back to DAO
  2. Pass Factory to new HybridFactoryOwner
  3. Set LTMigrator as limit setter
  4. Set HybridVaultFactory as limit setter
  5. Disable old LTs (markets 0-2)
"""

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


FORK = True
EXTRA_TIMEOUT = 10

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"

OLD_MARKET_IDX = [0, 1, 2]
POOL_IDS = [6]
POOL_LIMITS = [40_000_000 * 10**18]

USER = "0xeAfD26ffA47a9e387FB7409A456c4f7c4EF31ad8"

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
        USER = account_load('yb-deployer-2')
        boa.env.add_account(USER)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    lt_interface = boa.load_partial('contracts/LT.vy')
    old_lts = [lt_interface.at(factory.markets(i).lt) for i in OLD_MARKET_IDX]

    # Current factory owner (MigrationFactoryOwner)
    old_factory_owner = boa.load_partial('contracts/MigrationFactoryOwner.vy').at(factory.admin())

    # Deploy new contracts
    factory_owner = boa.load('contracts/HybridFactoryOwner.vy', DAO, FACTORY)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(factory_owner, etherscan, wait=True)

    migrator = boa.load('contracts/LTMigrator.vy', factory.STABLECOIN(), factory_owner.address)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(migrator, etherscan, wait=True)

    hybrid_vault_factory = boa.load('contracts/HybridVaultFactory.vy', FACTORY, POOL_IDS, POOL_LIMITS)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(hybrid_vault_factory, etherscan, wait=True)

    print(f"HybridFactoryOwner: {factory_owner.address}")
    print(f"LTMigrator: {migrator.address}")
    print(f"HybridVaultFactory: {hybrid_vault_factory.address}")

    # Build vote actions
    actions = [
        # 1. Transfer factory from old MigrationFactoryOwner back to DAO
        Action(
            to=old_factory_owner.address, value=0,
            data=old_factory_owner.transfer_ownership_back.prepare_calldata()
        ),
        # 2. Pass factory to new HybridFactoryOwner
        Action(
            to=factory.address, value=0,
            data=factory.set_admin.prepare_calldata(factory_owner.address, factory.emergency_admin())
        ),
        # 3. Set LTMigrator as limit setter
        Action(
            to=factory_owner.address, value=0,
            data=factory_owner.set_limit_setter.prepare_calldata(migrator.address, True)
        ),
        # 4. Set HybridVaultFactory as limit setter
        Action(
            to=factory_owner.address, value=0,
            data=factory_owner.set_limit_setter.prepare_calldata(hybrid_vault_factory.address, True)
        ),
    ]

    # 5. Disable old LTs
    for lt in old_lts:
        actions.append(
            Action(
                to=factory_owner.address, value=0,
                data=factory_owner.lt_allocate_stablecoins.prepare_calldata(lt.address, 0)
            )
        )

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs({
            'title': 'Deploy HybridVault infrastructure and migrate Factory ownership',
            'summary': (
                'Transfer Factory from MigrationFactoryOwner to new HybridFactoryOwner. '
                'Set LTMigrator and HybridVaultFactory as limit setters. '
                'Disable old markets (0-2) in the new owner.'
            ),
            'resources': []}).encode(),
        actions=actions,
        allowFailureMap=0,
        startDate=0,
        endDate=0,
        voteOption=0,
        tryEarlyExecution=True
    ))
    print(f"Proposal ID: {proposal_id}")
