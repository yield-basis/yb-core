import boa
from tests_forked.conftest import WBTC, SCRVUSD


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


def test_deposit_withdraw_crvusd_from_wallet(
    hybrid_vault_factory, hybrid_vault_deployer, wbtc, crvusd, factory, twocrypto, erc20
):
    """
    Test depositing 1 WBTC where crvUSD is pulled from user's wallet during deposit,
    and returned to user's wallet during withdraw.
    Uses a fresh vault with no pre-deposited crvUSD.
    """
    # Create a fresh account for this test (separate from funded_account which already has a vault)
    from tests_forked.conftest import WBTC, CRVUSD
    account = boa.env.generate_address()
    boa.deal(erc20.at(WBTC), account, 10 * 10**8)
    boa.deal(erc20.at(CRVUSD), account, 1_000_000 * 10**18)

    # Create a fresh vault for this test
    with boa.env.prank(account):
        vault_addr = hybrid_vault_factory.create_vault(SCRVUSD)
    vault = hybrid_vault_deployer.at(vault_addr)

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

    with boa.env.prank(account):
        # Approve vault to spend user's tokens
        wbtc.approve(vault.address, 2**256 - 1)
        crvusd.approve(vault.address, 2**256 - 1)

        # Verify vault has no crvUSD deposited initially
        assert vault.required_crvusd() == 0, "Vault should start with no crvUSD requirement"

        # Record initial crvUSD balance
        initial_crvusd_balance = crvusd.balanceOf(account)

        # Calculate how much crvUSD will be needed
        crvusd_needed = vault.crvusd_for_deposit(pool_id, assets, debt)
        assert crvusd_needed > 0, "Should need some crvUSD for deposit"
        assert initial_crvusd_balance >= crvusd_needed, "User should have enough crvUSD"

        # Deposit 1 WBTC with deposit_stablecoins=True (pulls crvUSD from wallet)
        lt_shares = vault.deposit(pool_id, assets, debt, 0, False, True)
        assert lt_shares > 0, "Should receive LT shares"

        # Verify exactly crvusd_needed was pulled from user's wallet
        balance_after_deposit = crvusd.balanceOf(account)
        crvusd_used = initial_crvusd_balance - balance_after_deposit
        assert crvusd_used == crvusd_needed, f"Should use exactly {crvusd_needed}, but used {crvusd_used}"

        # Verify vault now has crvUSD requirement
        assert vault.required_crvusd() > 0, "Vault should now require crvUSD"

        # Withdraw all shares with withdraw_stablecoins=True (returns crvUSD to wallet)
        vault.withdraw(pool_id, lt_shares, 0, False, account, True)

        # Verify crvUSD was returned to user's wallet (approximately the same amount)
        final_crvusd_balance = crvusd.balanceOf(account)
        crvusd_returned = final_crvusd_balance - balance_after_deposit
        # Allow small difference due to vault mechanics and rounding
        assert crvusd_returned >= crvusd_needed * 999 // 1000, f"Should return ~{crvusd_needed}, but returned {crvusd_returned}"

        # Verify no crvUSD is required after full withdrawal
        assert vault.required_crvusd() == 0, "Vault should have no crvUSD requirement after withdrawal"
