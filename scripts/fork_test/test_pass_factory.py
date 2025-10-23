#!/usr/bin/env python3
import boa
from networks import NETWORK


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
TEST_USER = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"  # <- Just one guy

DUST_AMOUNT = 10**17  # LT shares
MIGRATE_AMOUNT = 10**17  # LT shares


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

    for i in range(3):
        lts[i].withdraw_admin_fees()

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
        for i in range(3):
            new_market = factory.add_market(
                lts[i].CRYPTOPOOL(),
                int(0.0092 * 1e18),
                int(0.07 * 1e18 / (86400 * 365)),
                50 * 10**6 * 10**18)
            new_lts.append(lt_interface.at(new_market.lt))
            new_gauges.append(gauge_interface.at(new_market.staker))

    # Pass ownership to the owner
    with boa.env.prank(DAO):
        factory.set_admin(factory_owner.address, factory.emergency_admin())

    print(f"During migration: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")

    # Use claimed admin fees as a test for deposits and withdrawals

    # Pass ownership back
    with boa.env.prank(DAO):
        factory_owner.transfer_ownership_back()

    print(f"After migration: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")
