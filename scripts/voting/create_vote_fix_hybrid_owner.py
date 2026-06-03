#!/usr/bin/env python3
"""
Deploy a fixed HybridFactoryOwner and create a single DAO vote to:
  1. Transfer Factory from the current HybridFactoryOwner back to the DAO
  2. Pass Factory to the new (fixed) HybridFactoryOwner
  3. Re-set the current limit setters on the new owner
  4. Disable the old markets (3, 4, 5, 6) on the new owner (0 limit)

The fixed HybridFactoryOwner clamps lt_allocate_stablecoins() up to >=95% of the
current collateral value, and disabling the old markets makes their zero-limit
deallocate path a no-op instead of reverting. Together this unblocks withdrawals
that currently revert (e.g. the pool-8 user, and any vault with a market-6
position which reverts "Not disabled").

If FORK = True the script simulates the vote execution and asserts that:
  - all parameters were set correctly (admin, limit setters, disabled markets)
  - the previously-stuck users can withdraw again

Run from the repo root:  python scripts/voting/create_vote_fix_hybrid_owner.py
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

# Old markets to disable (0 limit) on the new owner.
OLD_MARKETS = [3, 4, 5, 6]

# Limit setters currently enabled on the live HybridFactoryOwner (verified at
# head; the disabled 0x2cdb.../0xDfD6... entries are intentionally omitted):
#   0xBdC3... is the HybridVaultFactory, 0xE707... is the keeper.
LIMIT_SETTERS = [
    "0xBdC32268851C324c6185809271dfe6d8dab8dC5b",
    "0xE707c7a9dD58fb7eEa17acFF875CEF8d10eD1a9F",
]

# (vault, owner, pool_id) cases checked under FORK: each currently reverts and
# must succeed after the vote executes.
SHARES = 10**14
WITHDRAW_CASES = [
    ("0x7cef005Ba1F7cF0D8e4db1Bf1DA6be40Af6C23f0", "0x4F8dB1e75Bf70c2B3b078811c2b1c2219238197E", 8),
    ("0x46dC80Aad1E2F89615801563E535982615829D7b", "0x1aE8703497900263ECa1A01aEFcd2016EC85A6c4", 6),
]

# EOA used as the caller under FORK (only needs to exist on chain).
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
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = USER
    else:
        boa.set_network_env(NETWORK)
        USER = account_load('yb-deployer')
        boa.env.add_account(USER)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    lt_interface = boa.load_partial('contracts/LT.vy')
    old_lts = [lt_interface.at(factory.markets(i).lt) for i in OLD_MARKETS]

    # Current factory owner (the live HybridFactoryOwner being replaced)
    old_factory_owner = boa.load_partial('contracts/HybridFactoryOwner.vy').at(factory.admin())

    # Deploy the new (fixed) HybridFactoryOwner
    factory_owner = boa.load('contracts/HybridFactoryOwner.vy', DAO, FACTORY)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(factory_owner, etherscan, wait=True)

    print(f"New HybridFactoryOwner: {factory_owner.address}")

    # Build vote actions
    actions = [
        # 1. Transfer factory from the old HybridFactoryOwner back to the DAO
        Action(
            to=old_factory_owner.address, value=0,
            data=old_factory_owner.transfer_ownership_back.prepare_calldata()
        ),
        # 2. Pass factory to the new HybridFactoryOwner
        Action(
            to=factory.address, value=0,
            data=factory.set_admin.prepare_calldata(factory_owner.address, factory.emergency_admin())
        ),
    ]
    # 3. Re-set the current limit setters on the new owner
    for setter in LIMIT_SETTERS:
        actions.append(
            Action(
                to=factory_owner.address, value=0,
                data=factory_owner.set_limit_setter.prepare_calldata(setter, True)
            )
        )
    # 4. Disable the old markets (0 limit) on the new owner
    for lt in old_lts:
        actions.append(
            Action(
                to=factory_owner.address, value=0,
                data=factory_owner.lt_allocate_stablecoins.prepare_calldata(lt.address, 0)
            )
        )

    if not FORK:
        proposal_id = voting.createProposal(*Proposal(
            metadata=pin_to_ipfs({
                'title': 'Replace HybridFactoryOwner to unblock HybridVault withdrawals',
                'summary': (
                    'Deploy a fixed HybridFactoryOwner that clamps stablecoin allocation to '
                    '>=95% of collateral value. Transfer Factory ownership from the current '
                    'HybridFactoryOwner to the new one, re-set the current limit setters '
                    '(HybridVaultFactory and keeper), and disable the old markets (3,4,5,6). '
                    'This unblocks some withdrawals that currently revert.'
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

    else:
        # === Confirm the withdrawals are stuck before the vote ===
        print("\n=== Before vote: withdrawals revert ===")
        for vault_addr, owner, pool_id in WITHDRAW_CASES:
            vault = boa.load_partial('contracts/HybridVault.vy').at(vault_addr)
            with boa.env.prank(owner):
                try:
                    vault.withdraw(pool_id, SHARES, 0)
                    raise AssertionError(f"withdraw from {vault_addr} did NOT revert pre-vote")
                except Exception as e:
                    if isinstance(e, AssertionError):
                        raise
                    print(f"  {vault_addr} pool {pool_id}: reverts (expected)")

        # === Simulate vote execution ===
        print("\n=== Simulating vote execution ===")
        with boa.env.prank(DAO):
            for action in actions:
                boa.env.raw_call(to_address=action.to, data=action.data)

        # === Check all params were set by the vote ===
        print("\n=== Checking parameters ===")
        assert factory.admin() == factory_owner.address, "Factory admin not updated"
        print(f"  Factory.admin() == new owner: {factory_owner.address}")
        for setter in LIMIT_SETTERS:
            assert factory_owner.limit_setters(setter), f"limit setter {setter} not set"
            print(f"  limit_setters[{setter}] == True")
        for lt in old_lts:
            assert factory_owner.disabled_lts(lt.address), f"market LT {lt.address} not disabled"
            print(f"  disabled_lts[{lt.address}] == True")

        # === Check users can withdraw after the vote ===
        print("\n=== After vote: withdrawals succeed ===")
        for vault_addr, owner, pool_id in WITHDRAW_CASES:
            vault = boa.load_partial('contracts/HybridVault.vy').at(vault_addr)
            with boa.env.prank(owner):
                assets = vault.withdraw(pool_id, SHARES, 0)
            assert assets > 0, f"withdraw from {vault_addr} returned 0"
            print(f"  {vault_addr} pool {pool_id}: withdrew {assets / 1e18:.6f} assets")

        print("\nAll fork checks passed!")
