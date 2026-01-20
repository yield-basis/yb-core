#!/usr/bin/env python3
"""
Deploy HybridVaultFactory on a forked mainnet using Anvil.

This script:
1. Forks mainnet and impersonates the DAO
2. Transfers Factory ownership: MigrationFactoryOwner -> DAO -> HybridFactoryOwner
3. Deploys HybridVault implementation and HybridVaultFactory
4. Configures HybridVaultFactory as a limit_setter
5. Allows scrvUSD as a crvusd_vault

Usage:
    python scripts/demo_hybrid_vault_factory.py
"""
import subprocess
import sys
from time import sleep

sys.path.insert(0, "tests_forked")

import boa
from boa.network import ExternalAccount
from networks import NETWORK


# Contract addresses
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
SCRVUSD = "0x0655977FEb2f289A4aB78af67BAB0d17aAb84367"

# Pool configuration: pool_id -> limit (in crvUSD, 18 decimals)
POOL_IDS = [3, 6]
POOL_LIMITS = [300_000_000 * 10**18, 300_000_000 * 10**18]  # 300M each

ANVIL_COMMAND = ["anvil", "--fork-url", NETWORK, "--chain-id", "1"]


def main():
    # Start Anvil fork
    print("Starting Anvil fork...")
    anvil = subprocess.Popen(ANVIL_COMMAND)
    sleep(1)

    boa.set_network_env("http://localhost:8545")
    boa.env._fork_try_prefetch_state = False

    # Impersonate DAO
    print(f"Impersonating DAO: {DAO}")
    boa.env._rpc.fetch("anvil_impersonateAccount", [DAO])
    boa.env.add_account(ExternalAccount(_rpc=boa.env._rpc, address=DAO))
    boa.env._rpc.fetch("anvil_setBalance", [DAO, "0x1000000000000000000"])

    # Load existing contracts
    print("Loading Factory...")
    factory = boa.load_partial("contracts/Factory.vy").at(FACTORY)

    print(f"Current Factory admin: {factory.admin()}")
    print(f"Current Factory emergency_admin: {factory.emergency_admin()}")

    # Load MigrationFactoryOwner (current admin of the factory)
    migration_owner = boa.load_partial("contracts/MigrationFactoryOwner.vy").at(factory.admin())
    assert migration_owner.ADMIN() == DAO, "MigrationFactoryOwner admin is not DAO"

    # Transfer Factory ownership from MigrationFactoryOwner back to DAO
    print("Transferring Factory ownership from MigrationFactoryOwner to DAO...")
    with boa.env.prank(DAO):
        migration_owner.transfer_ownership_back()

    assert factory.admin() == DAO, "Factory admin should be DAO after transfer"
    print(f"Factory admin after transfer: {factory.admin()}")

    # Deploy HybridFactoryOwner with DAO as admin
    print("Deploying HybridFactoryOwner...")
    hybrid_factory_owner = boa.load("contracts/HybridFactoryOwner.vy", DAO, factory.address)
    print(f"HybridFactoryOwner deployed at: {hybrid_factory_owner.address}")

    # Transfer Factory ownership to HybridFactoryOwner
    print("Transferring Factory ownership to HybridFactoryOwner...")
    emergency_admin = factory.emergency_admin()
    with boa.env.prank(DAO):
        factory.set_admin(hybrid_factory_owner.address, emergency_admin)

    assert factory.admin() == hybrid_factory_owner.address, "Factory admin should be HybridFactoryOwner"
    print(f"Factory admin after HybridFactoryOwner: {factory.admin()}")

    # Deploy HybridVault implementation
    print("Deploying HybridVault implementation...")
    vault_impl = boa.load("contracts/HybridVault.vy", factory.address, CRVUSD)
    print(f"HybridVault implementation deployed at: {vault_impl.address}")

    # Deploy HybridVaultFactory
    print("Deploying HybridVaultFactory...")
    print(f"  Pool IDs: {POOL_IDS}")
    print(f"  Pool limits: {[l // 10**18 for l in POOL_LIMITS]} crvUSD")
    hybrid_vault_factory = boa.load(
        "contracts/HybridVaultFactory.vy",
        factory.address,
        vault_impl.address,
        POOL_IDS,
        POOL_LIMITS,
    )
    print(f"HybridVaultFactory deployed at: {hybrid_vault_factory.address}")

    # Add HybridVaultFactory as limit_setter in HybridFactoryOwner
    print("Setting HybridVaultFactory as limit_setter...")
    with boa.env.prank(DAO):
        hybrid_factory_owner.set_limit_setter(hybrid_vault_factory.address, True)

    assert hybrid_factory_owner.limit_setters(hybrid_vault_factory.address), "HybridVaultFactory should be limit_setter"

    # Allow scrvUSD as a crvusd_vault
    print(f"Allowing scrvUSD ({SCRVUSD}) as crvusd_vault...")
    with boa.env.prank(DAO):
        hybrid_vault_factory.set_allowed_crvusd_vault(SCRVUSD, True)

    assert hybrid_vault_factory.allowed_crvusd_vaults(SCRVUSD), "scrvUSD should be allowed"

    # Print summary
    print("\n" + "=" * 60)
    print("Deployment Summary")
    print("=" * 60)
    print(f"Factory:              {factory.address}")
    print(f"HybridFactoryOwner:   {hybrid_factory_owner.address}")
    print(f"HybridVault impl:     {vault_impl.address}")
    print(f"HybridVaultFactory:   {hybrid_vault_factory.address}")
    print(f"DAO (admin):          {DAO}")
    print(f"crvUSD:               {CRVUSD}")
    print(f"scrvUSD (allowed):    {SCRVUSD}")
    print("=" * 60)
    print("\nHybridVaultFactory is ready for HybridVault deployments!")
    print("Users can now call hybrid_vault_factory.create_vault(scrvUSD) to create vaults.")
    print("\nAnvil is still running. Press Ctrl+C to stop.")

    # Keep anvil running
    anvil.wait()


if __name__ == "__main__":
    main()
