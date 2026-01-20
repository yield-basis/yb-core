import boa
from tests_forked.conftest import WBTC


def test_stake_unstake_wbtc(
    vault, funded_account, wbtc, crvusd, setup_approvals, factory, twocrypto
):
    """Test depositing 1 WBTC, staking, and unstaking."""
    pool_id = 3
    assets = 1 * 10**8  # 1 WBTC (8 decimals)

    # Get market info
    market = factory.markets(pool_id)
    assert market.asset_token == WBTC, "Pool 3 should use WBTC"

    # Calculate debt as half the USD value of assets
    cryptopool = twocrypto.at(market.cryptopool)
    price = cryptopool.price_scale()  # price of WBTC in crvUSD (18 decimals)
    usd_value = assets * price // 10**8  # adjust for WBTC decimals
    debt = usd_value // 2

    with boa.env.prank(funded_account):
        # Deposit 1 WBTC without staking
        lt_shares = vault.deposit(pool_id, assets, debt, 0, False, True)
        assert lt_shares > 0, "Should receive LT shares"

        # Stake all LT shares
        staked_shares = vault.stake(pool_id, lt_shares)
        assert staked_shares > 0, "Should receive staked shares"

        # Unstake all shares
        unstaked_lt_shares = vault.unstake(pool_id, staked_shares)
        assert unstaked_lt_shares > 0, "Should receive LT shares back"

        # LT shares should be approximately the same (may differ slightly due to gauge mechanics)
        assert unstaked_lt_shares >= lt_shares * 99 // 100, "Should get back ~same LT shares"

        # Withdraw all LT shares
        vault.withdraw(pool_id, unstaked_lt_shares, 0, False, funded_account, False)

        # Verify no crvUSD is required after full withdrawal
        assert vault.required_crvusd() == 0
