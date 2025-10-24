#!/usr/bin/env python3
import boa
from networks import NETWORK


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
TEST_USER = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"  # <- Just one guy

STAKED_WHALES = [
    "0x196a2A9A22C2fD8f5107e97Df9ad14A23e81982B",
    "0x196a2A9A22C2fD8f5107e97Df9ad14A23e81982B",
    "0xa79a356B01ef805B3089b4FE67447b96c7e6DD4C"
]

MIGRATE_AMOUNT = 10**17  # LT shares
WHALE_AMOUNT = int(2.5e18)


if __name__ == '__main__':
    boa.fork(NETWORK)
    boa.env.eoa = DAO

    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    factory_owner = boa.load('contracts/MigrationFactoryOwner.vy', DAO, FACTORY)
    migrator = boa.load('contracts/LTMigrator.vy', factory.STABLECOIN(), factory_owner.address)

    lt_interface = boa.load_partial('contracts/LT.vy')
    gauge_interface = boa.load_partial('contracts/dao/LiquidityGauge.vy')
    lts = [lt_interface.at(factory.markets(i).lt) for i in range(3)]
    gauges = [gauge_interface.at(factory.markets(i).staker) for i in range(3)]

    for i in range(3):
        pps = lts[i].pricePerShare() / 1e18
        g_rate = gauges[i].previewRedeem(10**18) / 1e18
        print("PPS before admin claim:", pps)
        print("Gauge PPS before admin claim:", pps * g_rate)
        print()

    # Test admin fees with tBTC
    with boa.env.prank(DAO):
        factory.set_fee_receiver(DAO)

    for lt in lts:
        lt.withdraw_admin_fees()
        with boa.env.prank(DAO):
            lt.transfer(TEST_USER, lt.balanceOf(DAO))

    for i in range(3):
        print("Admin fees in DAO:", lts[i].balanceOf(DAO) / 1e18)

        pps = lts[i].pricePerShare() / 1e18
        g_rate = gauges[i].previewRedeem(10**18) / 1e18
        print("PPS before admin claim:", pps)
        print("Gauge PPS before admin claim:", pps * g_rate)
        print("Admin fees left:", lts[i].liquidity().admin / 1e18)
        print()

    print(f"Before: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")

    # Set new LT implementation
    lt_blueprint = lt_interface.deploy_as_blueprint()
    with boa.env.prank(DAO):
        factory.set_implementations(ZERO_ADDRESS, lt_blueprint.address, ZERO_ADDRESS, ZERO_ADDRESS, ZERO_ADDRESS)

    # Create new markets
    new_lts = []
    new_gauges = []
    with boa.env.prank(DAO):
        for lt in lts:
            new_market = factory.add_market(
                lt.CRYPTOPOOL(),
                int(0.0092 * 1e18),
                int(0.07 * 1e18 / (86400 * 365)),
                50 * 10**6 * 10**18)
            new_lts.append(lt_interface.at(new_market.lt))
            new_gauges.append(gauge_interface.at(new_market.staker))
            factory_owner.lt_allocate_stablecoins(lt.address, 0)

    # Pass ownership to the owner
    with boa.env.prank(DAO):
        factory.set_admin(factory_owner.address, factory.emergency_admin())

    print(f"During migration: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")

    if WHALE_AMOUNT > 0:
        # Free up some space just in case
        for user, lt, gauge in zip(STAKED_WHALES, lts, gauges):
            with boa.env.prank(user):
                gauge.redeem(WHALE_AMOUNT, user, user)
                lt.withdraw(lt.balanceOf(user), 0)

    # Withdraw admin fees minus test amounts
    with boa.env.prank(TEST_USER):
        for lt in lts:
            lt.withdraw(lt.balanceOf(TEST_USER) - 2 * MIGRATE_AMOUNT, 0)

    # Use claimed admin fees as a test for deposits and withdrawals
    with boa.env.prank(TEST_USER):
        for lt, gauge in zip(lts, gauges):
            lt.approve(gauge.address, 2**256 - 1)
            gauge.deposit(MIGRATE_AMOUNT, TEST_USER)

        for old_lt, new_lt in zip(lts, new_lts):
            print("Migrating", old_lt.symbol())
            old_lt.approve(migrator.address, 2**256 - 1)
            migrator.migrate_plain(old_lt.address, new_lt.address, MIGRATE_AMOUNT, 0)

    # Pass ownership back
    with boa.env.prank(DAO):
        factory_owner.transfer_ownership_back()

    print(f"After migration: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")
