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

    print(f"Before: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")

    # Pass ownership to the owner
    with boa.env.prank(DAO):
        factory.set_admin(factory_owner.address, factory.emergency_admin())

    print(f"During migration: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")

    # Pass ownership back
    with boa.env.prank(DAO):
        factory_owner.transfer_ownership_back()

    print(f"After migration: admin = {factory.admin()}, emergency_admin = {factory.emergency_admin()}")
