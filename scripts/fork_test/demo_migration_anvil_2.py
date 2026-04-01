#!/usr/bin/env python3
import boa
from networks import NETWORK

import subprocess
from boa.network import ExternalAccount
from time import sleep


DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
TEST_USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # <- YB deployer

STAKED_WHALES = [
    "0xe496298e4ab3d59c4a6ef0e22d983cddcc52f2cf",
    "0xc534bb38da4f1498cf367ab19e3200a5b324503b",
    "0x46633b491c0dd7b245f47da22855f33fa20a4e06"
]


ANVIL_COMMAND = ["anvil", "--fork-url", NETWORK, "--chain-id", "1", "--silent"]


# Steps:
# 1. Transfer factory from old MigrationFactoryOwner back to DAO
# 2. Deploy HybridFactoryOwner and pass factory to it
# 3. Set migrator as limit setter
# 4. Test migration from markets [0,1,2] to [3,4,5]
# 5. Transfer ownership back


if __name__ == '__main__':
    anvil = subprocess.Popen(ANVIL_COMMAND)
    sleep(1)

    boa.set_network_env("http://localhost:8545")
    boa.env._fork_try_prefetch_state = False

    # Prepare to impersonate all relevant accounts in Anvil
    boa.env._rpc.fetch("anvil_impersonateAccount", [DAO])
    boa.env.add_account(ExternalAccount(_rpc=boa.env._rpc, address=DAO))
    boa.env._rpc.fetch("anvil_setBalance", [DAO, "0x1000000000000000000"])
    boa.env._rpc.fetch("anvil_impersonateAccount", [TEST_USER])
    boa.env.add_account(ExternalAccount(_rpc=boa.env._rpc, address=TEST_USER))
    for whale in STAKED_WHALES:
        boa.env._rpc.fetch("anvil_impersonateAccount", [whale])
        boa.env.add_account(ExternalAccount(_rpc=boa.env._rpc, address=whale))

    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)

    # Transfer factory ownership back from the existing on-chain MigrationFactoryOwner
    old_factory_owner = boa.load_partial('contracts/MigrationFactoryOwner.vy').at(factory.admin())
    with boa.env.prank(DAO):
        old_factory_owner.transfer_ownership_back()

    factory_owner = boa.load('contracts/HybridFactoryOwner.vy', DAO, FACTORY)
    migrator = boa.load('contracts/LTMigrator.vy', factory.STABLECOIN(), factory_owner.address)

    lt_interface = boa.load_partial('contracts/LT.vy')
    gauge_interface = boa.load_partial('contracts/dao/LiquidityGauge.vy')

    # Old markets [0,1,2] and new markets [3,4,5]
    lts = [lt_interface.at(factory.markets(i).lt) for i in range(3)]
    gauges = [gauge_interface.at(factory.markets(i).staker) for i in range(3)]
    new_lts = [lt_interface.at(factory.markets(i).lt) for i in range(3, 6)]
    new_gauges = [gauge_interface.at(factory.markets(i).staker) for i in range(3, 6)]

    print(f"Before: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")

    # Pass factory to HybridFactoryOwner
    with boa.env.prank(DAO):
        factory.set_admin(factory_owner.address, factory.emergency_admin())
        factory_owner.set_limit_setter(migrator.address, True)
        for lt in lts:
            factory_owner.lt_allocate_stablecoins(lt.address, 0)

    print(f"During migration: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")

    # Deployer as a test user
    with boa.env.prank(TEST_USER):
        for old_lt, new_lt in zip(lts, new_lts):
            migration_amount = old_lt.balanceOf(TEST_USER)
            if migration_amount > 0:
                print("Migrating", old_lt.symbol())
                old_lt.approve(migrator.address, 2**256 - 1)
                amount_to = migrator.preview_migrate_plain(old_lt.address, new_lt.address, migration_amount)
                print("  calculated amount:", amount_to / 1e18)
                migrator.migrate_plain(old_lt.address, new_lt.address, migration_amount, int(amount_to * 0.998))
                print("  actual amount:", new_lt.balanceOf(TEST_USER) / 1e18)

    # Whale migration test: staked migration
    for user, old_lt, old_g, new_lt, new_g in zip(STAKED_WHALES, lts, gauges, new_lts, new_gauges):
        with boa.env.prank(user):
            staked_balance = old_g.balanceOf(user)
            if staked_balance > 0:
                print(f"Migrating staked {old_g.symbol()} for {user}")
                balance_before = new_g.balanceOf(user)
                old_g.approve(migrator.address, 2**256 - 1)
                amount_to = migrator.preview_migrate_staked(old_lt.address, new_lt.address, staked_balance)
                print("  calculated amount:", amount_to / 1e18)
                migrator.migrate_staked(old_lt.address, new_lt.address, staked_balance, int(amount_to * 0.998))
                print("  actual amount:", (new_g.balanceOf(user) - balance_before) / 1e18)

    # Pass ownership back
    with boa.env.prank(DAO):
        factory_owner.transfer_ownership_back()

    print(f"After migration: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")

    anvil.wait()
