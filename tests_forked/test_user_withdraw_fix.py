import boa
import pytest
from tests_forked.networks import NETWORK

# Reproduces a live mainnet revert: user 0x4F8d...197E calling
# HybridVault.withdraw(8, 10**14, 0) reverts because the deployed
# HybridFactoryOwner.lt_allocate_stablecoins() forwards the requested limit
# verbatim to LT.allocate_stablecoins(), which reverts when the limit drops
# below what is currently borrowed. The fixed HybridFactoryOwner clamps the
# limit up to >=95% of the current collateral value.
#
# All addresses below were discovered on-chain (mainnet) before writing this
# test, so no discovery happens at test time:
#   - Factory.admin()                  -> OLD_OWNER (current HybridFactoryOwner)
#   - OLD_OWNER.ADMIN()                -> DAO
#   - Factory.emergency_admin()        -> EMERGENCY_ADMIN
#   - vault.VAULT_FACTORY()            -> HybridVaultFactory (0xBdC3...)
#   - SetLimitSetter events on OLD_OWNER (current enabled set, verified at head)

FORK_BLOCK = 25232722

USER = "0x4F8dB1e75Bf70c2B3b078811c2b1c2219238197E"
VAULT = "0x7cef005Ba1F7cF0D8e4db1Bf1DA6be40Af6C23f0"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
OLD_OWNER = "0x0f4e1a1BcDe549653E9302Ba1cAaB403373f1048"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
EMERGENCY_ADMIN = "0x467947EE34aF926cF1DCac093870f613C96B1E0c"

# Currently-enabled limit_setters on the live HybridFactoryOwner (the disabled
# ones, 0x2cdb... and 0xDfD6..., are intentionally omitted).
LIMIT_SETTERS = [
    "0xBdC32268851C324c6185809271dfe6d8dab8dC5b",  # HybridVaultFactory
    "0xE707c7a9dD58fb7eEa17acFF875CEF8d10eD1a9F",
]

POOL_ID = 8
SHARES = 10**14
MIN_ASSETS = 0

# Old markets to deallocate to 0 as part of the migration cleanup.
OLD_MARKETS = [3, 4, 5, 6]

# A different HybridVault with a position in market 6 (WETH). Its withdraw also
# reverts today, but with "Not disabled": the market-6 LT is stuck with
# stablecoin_allocation == 1 wei while stablecoin_allocated is frozen at ~41M,
# so withdraw ends up calling lt_allocate_stablecoins(lt6, 0) and the owner's
# deallocate branch reverts because the LT is not marked disabled. Disabling
# market 6 in step 4 clears that path.
MARKET6_POOL_ID = 6
MARKET6_VAULT = "0x46dC80Aad1E2F89615801563E535982615829D7b"
MARKET6_OWNER = "0x1aE8703497900263ECa1A01aEFcd2016EC85A6c4"


# Override the conftest autouse fork (which pins a different block) so this
# module forks at the block where the user's tx is failing.
@pytest.fixture(scope="module", autouse=True)
def forked_env():
    with boa.fork(NETWORK, block_identifier=FORK_BLOCK):
        yield


def test_user_withdraw_after_owner_fix(forked_env):
    factory = boa.load_partial("contracts/Factory.vy").at(FACTORY)
    vault = boa.load_partial("contracts/HybridVault.vy").at(VAULT)
    old_owner = boa.load_partial("contracts/HybridFactoryOwner.vy").at(OLD_OWNER)
    market6_vault = boa.load_partial("contracts/HybridVault.vy").at(MARKET6_VAULT)

    # 1. Confirm the live (buggy) owner still makes the user's tx revert, and
    #    that a market-6 vault is likewise stuck (reverts "Not disabled")
    with boa.env.prank(USER):
        with boa.reverts():
            vault.withdraw(POOL_ID, SHARES, MIN_ASSETS)
    with boa.env.prank(MARKET6_OWNER):
        with boa.reverts("Not disabled"):
            market6_vault.withdraw(MARKET6_POOL_ID, SHARES, MIN_ASSETS)

    # 2. Deploy the fixed HybridFactoryOwner and hand it Factory ownership:
    #    old_owner -> DAO (transfer_ownership_back) -> new_owner (set_admin)
    new_owner = boa.load("contracts/HybridFactoryOwner.vy", DAO, FACTORY)
    with boa.env.prank(DAO):
        old_owner.transfer_ownership_back()
        factory.set_admin(new_owner.address, EMERGENCY_ADMIN)
        # 3. Replicate the live limit_setters config on the new owner
        for setter in LIMIT_SETTERS:
            new_owner.set_limit_setter(setter, True)

    assert factory.admin() == new_owner.address
    for setter in LIMIT_SETTERS:
        assert new_owner.limit_setters(setter)

    # 4. Migration cleanup: zero out the old markets (3,4,5,6). All as the DAO:
    #    disable each LT via the new owner (lt_allocate_stablecoins(lt, 0)),
    old_lts = [factory.markets(mid).lt for mid in OLD_MARKETS]
    with boa.env.prank(DAO):
        for lt_addr in old_lts:
            new_owner.lt_allocate_stablecoins(lt_addr, 0)

    # 5. The previously-failing transaction now succeeds
    with boa.env.prank(USER):
        assets = vault.withdraw(POOL_ID, SHARES, MIN_ASSETS)

    assert assets > 0
    print(assets)

    # 6. The market-6 vault can now withdraw too: market 6 is disabled (step 4),
    #    so its lt_allocate_stablecoins(lt6, 0) becomes a no-op instead of
    #    reverting "Not disabled".
    with boa.env.prank(MARKET6_OWNER):
        market6_assets = market6_vault.withdraw(MARKET6_POOL_ID, SHARES, MIN_ASSETS)

    assert market6_assets > 0
    print(market6_assets)
