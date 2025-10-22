#!/usr/bin/env python3
import boa
from networks import NETWORK


DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"


if __name__ == '__main__':
    boa.fork(NETWORK)
    boa.env.eoa = DAO

    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    factory_owner = boa.load('contracts/MigrationFactoryOwner.vy', DAO, FACTORY)

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

    # Pass ownership to the owner
    with boa.env.prank(DAO):
        factory.set_admin(factory_owner.address, factory.emergency_admin())

    print(f"During migration: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")

    # Pass ownership back
    with boa.env.prank(DAO):
        factory_owner.transfer_ownership_back()

    print(f"After migration: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")
