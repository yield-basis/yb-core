import boa
import pytest
from tests_forked.networks import NETWORK

# Companion to test_user_withdraw_fix.py, which reproduced the revert at a
# historical block (25232722) and then *simulated* deploying the fixed
# HybridFactoryOwner + migration in-fork.
#
# By now the real fix has shipped: the DAO vote executed on mainnet, so the
# Factory admin is the fixed HybridFactoryOwner (0xb8BA...) and the old markets
# have been deallocated/disabled. This test forks at the *current head* and
# confirms the user's previously-reverting withdraw now simply succeeds against
# the live deployment - no in-fork setup, no impersonating the DAO.

USER = "0x4F8dB1e75Bf70c2B3b078811c2b1c2219238197E"
VAULT = "0x7cef005Ba1F7cF0D8e4db1Bf1DA6be40Af6C23f0"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"

# The fixed HybridFactoryOwner installed by the executed DAO vote (replaces the
# buggy OLD_OWNER 0x0f4e... from the historical test).
NEW_OWNER = "0xb8BA33CD1Ccb091a8468572950bD3669723FA5C6"

# Pinned to a post-vote block so results are deterministic (the fixed owner is
# already the Factory admin here). Bump this if mainnet state drifts.
FORK_BLOCK = 25239277

POOL_ID = 8
SHARES = 10**14
MIN_ASSETS = 0

# The market-6 vault that also reverted ("Not disabled") in the historical test.
MARKET6_POOL_ID = 6
MARKET6_VAULT = "0x46dC80Aad1E2F89615801563E535982615829D7b"
MARKET6_OWNER = "0x1aE8703497900263ECa1A01aEFcd2016EC85A6c4"


@pytest.fixture(scope="module", autouse=True)
def forked_env():
    # Fork at a fixed post-vote block: the fix is live on mainnet here.
    with boa.fork(NETWORK, block_identifier=FORK_BLOCK):
        yield


def test_user_withdraw_no_longer_reverts(forked_env):
    factory = boa.load_partial("contracts/Factory.vy").at(FACTORY)
    vault = boa.load_partial("contracts/HybridVault.vy").at(VAULT)

    # Sanity: the executed vote installed the fixed owner as Factory admin.
    assert factory.admin() == NEW_OWNER

    # The transaction that used to revert (part #1 of the historical test) now
    # succeeds with no setup, because the fix is already live on-chain.
    with boa.env.prank(USER):
        assets = vault.withdraw(POOL_ID, SHARES, MIN_ASSETS)

    assert assets > 0
    print("pool 8 assets:", assets)


def test_market6_withdraw_no_longer_reverts(forked_env):
    market6_vault = boa.load_partial("contracts/HybridVault.vy").at(MARKET6_VAULT)

    # Market 6 was disabled by the executed migration, so this no longer
    # reverts "Not disabled".
    with boa.env.prank(MARKET6_OWNER):
        assets = market6_vault.withdraw(MARKET6_POOL_ID, SHARES, MIN_ASSETS)

    assert assets > 0
    print("market 6 assets:", assets)
