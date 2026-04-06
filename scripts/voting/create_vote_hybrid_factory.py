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
SCRVUSD = "0x0655977FEb2f289A4aB78af67BAB0d17aAb84367"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"

OLD_MARKET_IDX = [0, 1, 2]
WETH_MARKET_ID = 6
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
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
        USER = account_load('yb-deployer')
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

    vault_impl = boa.load('contracts/HybridVault.vy', FACTORY, CRVUSD, hybrid_vault_factory.address)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(vault_impl, etherscan, wait=True)

    print(f"HybridFactoryOwner: {factory_owner.address}")
    print(f"LTMigrator: {migrator.address}")
    print(f"HybridVaultFactory: {hybrid_vault_factory.address}")
    print(f"HybridVault impl: {vault_impl.address}")

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
        # 5. Set HybridVault implementation
        Action(
            to=hybrid_vault_factory.address, value=0,
            data=hybrid_vault_factory.set_vault_impl.prepare_calldata(vault_impl.address)
        ),
        # 6. Allow scrvUSD as crvusd vault with 100M limit
        Action(
            to=hybrid_vault_factory.address, value=0,
            data=hybrid_vault_factory.set_allowed_crvusd_vault.prepare_calldata(SCRVUSD, True, 100_000_000 * 10**18)
        ),
    ]

    # 7. Disable old LTs
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
                'title': 'Deploy HybridVault infrastructure and migrate Factory ownership',
                'summary': (
                    'Transfer Factory from MigrationFactoryOwner to new HybridFactoryOwner. '
                    'Set WETH market to be $20M larger if going via HybridVault. '
                    'Set LTMigrator and HybridVaultFactory as limit setters. '
                    'Set HybridVault implementation. Allow scrvUSD as crvUSD vault. '
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

    else:
        # === Simulate vote execution ===
        print("\n=== Simulating vote execution ===")
        with boa.env.prank(DAO):
            for action in actions:
                boa.env.raw_call(to_address=action.to, data=action.data)
        print(f"Vote executed. Factory admin = {factory.admin()}")

        # === a) Test migration via LTMigrator ===
        print("\n=== Testing LTMigrator ===")
        new_lts = [lt_interface.at(factory.markets(i).lt) for i in range(3, 6)]
        gauge_interface = boa.load_partial('contracts/dao/LiquidityGauge.vy')
        gauges = [gauge_interface.at(factory.markets(i).staker) for i in range(3)]
        new_gauges = [gauge_interface.at(factory.markets(i).staker) for i in range(3, 6)]

        TEST_USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"
        with boa.env.prank(TEST_USER):
            for old_lt, new_lt in zip(old_lts, new_lts):
                amount = old_lt.balanceOf(TEST_USER)
                if amount > 0:
                    print(f"Migrating {old_lt.symbol()}")
                    old_lt.approve(migrator.address, 2**256 - 1)
                    preview = migrator.preview_migrate_plain(old_lt.address, new_lt.address, amount)
                    print(f"  preview: {preview / 1e18}")
                    migrator.migrate_plain(old_lt.address, new_lt.address, amount, int(preview * 0.998))
                    print(f"  received: {new_lt.balanceOf(TEST_USER) / 1e18}")

        STAKED_WHALES = [
            "0xe496298e4ab3d59c4a6ef0e22d983cddcc52f2cf",
            "0xc534bb38da4f1498cf367ab19e3200a5b324503b",
            "0x46633b491c0dd7b245f47da22855f33fa20a4e06"
        ]
        for user, old_lt, old_g, new_lt, new_g in zip(STAKED_WHALES, old_lts, gauges, new_lts, new_gauges):
            with boa.env.prank(user):
                staked = old_g.balanceOf(user)
                if staked > 0:
                    print(f"Migrating staked {old_g.symbol()} for {user}")
                    balance_before = new_g.balanceOf(user)
                    old_g.approve(migrator.address, 2**256 - 1)
                    preview = migrator.preview_migrate_staked(old_lt.address, new_lt.address, staked)
                    print(f"  preview: {preview / 1e18}")
                    migrator.migrate_staked(old_lt.address, new_lt.address, staked, int(preview * 0.998))
                    print(f"  received: {(new_g.balanceOf(user) - balance_before) / 1e18}")

        # === b) Test deposit into WETH market via HybridVault ===
        print("\n=== Testing WETH market deposit via HybridVault ===")
        erc20 = boa.load_partial('contracts/testing/ERC20Mock.vy')
        hybrid_vault_deployer = boa.load_partial('contracts/HybridVault.vy')
        weth = boa.load_partial('contracts/testing/WETH.vy').at(WETH)

        # Create funded test account
        depositor = boa.env.generate_address()
        boa.env.set_balance(depositor, 10 * 10**18)
        with boa.env.prank(depositor):
            weth.deposit(value=10 * 10**18)
        boa.deal(erc20.at(CRVUSD), depositor, 1_000_000 * 10**18)

        # Create HybridVault for depositor
        with boa.env.prank(depositor):
            vault_addr = hybrid_vault_factory.create_vault(SCRVUSD)
        vault = hybrid_vault_deployer.at(vault_addr)

        # Calculate debt: assets * price / 2
        twocrypto = boa.load_partial('contracts/testing/twocrypto/Twocrypto.vy')
        weth_lt = lt_interface.at(factory.markets(WETH_MARKET_ID).lt)
        cryptopool = twocrypto.at(weth_lt.CRYPTOPOOL())
        price = cryptopool.price_scale()
        deposit_amount = 1 * 10**18  # 1 WETH
        debt = deposit_amount * price // 10**18 // 2

        with boa.env.prank(depositor):
            weth.approve(vault.address, 2**256 - 1)
            erc20.at(CRVUSD).approve(vault.address, 2**256 - 1)
            shares = vault.deposit(WETH_MARKET_ID, deposit_amount, debt, 0, False, True)
            print(f"Deposited 1 WETH (debt={debt / 1e18:.2f} crvUSD), got {shares / 1e18} shares")
            assert shares > 0

        print("\nAll fork tests passed!")
