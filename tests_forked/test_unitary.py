import boa
from tests_forked.conftest import WBTC, SCRVUSD, CRVUSD


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
    debt = usd_value

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
        assert abs(crvusd_returned - crvusd_needed) <= 10, f"Should return ~{crvusd_needed}, but returned {crvusd_returned}"

        # Verify no crvUSD is required after full withdrawal
        assert vault.required_crvusd() == 0, "Vault should have no crvUSD requirement after withdrawal"

        # Verify HybridVault has 0 scrvUSD left
        scrvusd = erc20.at(SCRVUSD)
        assert scrvusd.balanceOf(vault.address) == 0, "HybridVault should have 0 scrvUSD after full withdrawal"


def test_uninitialized_impl_matches_empty_vault(
    hybrid_vault_factory, hybrid_vault_deployer, factory, twocrypto, erc20
):
    """
    Test that uninitialized implementation contract gives the same crvusd_for_deposit
    result as a freshly-initialized empty HybridVault.
    """

    # Get the uninitialized implementation contract
    impl_addr = hybrid_vault_factory.vault_impl()
    impl = hybrid_vault_deployer.at(impl_addr)

    # Create a fresh account and vault for comparison
    account = boa.env.generate_address()
    boa.deal(erc20.at(CRVUSD), account, 1_000_000 * 10**18)

    with boa.env.prank(account):
        vault_addr = hybrid_vault_factory.create_vault(SCRVUSD)
    empty_vault = hybrid_vault_deployer.at(vault_addr)

    # Verify the implementation is uninitialized (owner is 0x01)
    assert impl.owner() == "0x0000000000000000000000000000000000000001", "Impl should have owner 0x01"

    # Verify the empty vault is initialized but empty
    assert empty_vault.owner() == account, "Empty vault should have owner set"
    assert empty_vault.required_crvusd() == 0, "Empty vault should require no crvUSD"

    pool_id = 3
    assets = 1 * 10**8  # 1 WBTC (8 decimals)

    # Calculate debt as half the USD value of assets
    market = factory.markets(pool_id)
    cryptopool = twocrypto.at(market.cryptopool)
    price = cryptopool.price_scale()
    usd_value = assets * price // 10**8
    debt = usd_value

    # Call crvusd_for_deposit on both contracts
    impl_crvusd_result = impl.crvusd_for_deposit(pool_id, assets, debt)
    vault_crvusd_result = empty_vault.crvusd_for_deposit(pool_id, assets, debt)

    # Both should return the same result
    assert impl_crvusd_result == vault_crvusd_result, f"Impl returned {impl_crvusd_result}, empty vault returned {vault_crvusd_result}"

    # Call assets_for_crvusd on both contracts
    crvusd_amount = 10_000 * 10**18  # 10k crvUSD
    impl_assets_result = impl.assets_for_crvusd(pool_id, crvusd_amount)
    vault_assets_result = empty_vault.assets_for_crvusd(pool_id, crvusd_amount)

    # Both should return the same result
    assert impl_assets_result == vault_assets_result, f"assets_for_crvusd: Impl returned {impl_assets_result}, empty vault returned {vault_assets_result}"

    # Verify ratio consistency: crvusd_for_deposit / assets ≈ crvusd_amount / assets_for_crvusd
    # Cross-multiply: crvusd_for_deposit_result * assets_for_crvusd_result ≈ crvusd_amount * assets
    crvusd_for_deposit_result = impl_crvusd_result
    assets_for_crvusd_result = impl_assets_result
    product_from_crvusd_for_deposit = crvusd_for_deposit_result * assets_for_crvusd_result
    product_from_assets_for_crvusd = crvusd_amount * assets
    tolerance = max(product_from_crvusd_for_deposit, product_from_assets_for_crvusd) // 1000  # 0.1% tolerance
    assert abs(product_from_crvusd_for_deposit - product_from_assets_for_crvusd) <= tolerance, \
        f"Ratio mismatch: crvusd_for_deposit gives {product_from_crvusd_for_deposit}, assets_for_crvusd gives {product_from_assets_for_crvusd}"


def test_recover_tokens(
    hybrid_vault_factory, hybrid_vault_deployer, factory, twocrypto, erc20
):
    """
    Test that recover_tokens prevents recovering protected tokens but allows
    recovering accidentally sent tokens.
    """
    pool_id = 3
    assets = 1 * 10**8  # 1 WBTC (8 decimals)

    # Create a fresh account and vault
    account = boa.env.generate_address()
    wbtc_token = erc20.at(WBTC)
    crvusd_token = erc20.at(CRVUSD)
    scrvusd_token = erc20.at(SCRVUSD)
    boa.deal(wbtc_token, account, 10 * 10**8)
    boa.deal(crvusd_token, account, 1_000_000 * 10**18)

    with boa.env.prank(account):
        vault_addr = hybrid_vault_factory.create_vault(SCRVUSD)
    vault = hybrid_vault_deployer.at(vault_addr)

    # Get market info
    market = factory.markets(pool_id)
    lt_address = market.lt
    staker_address = market.staker

    # Calculate debt and make a deposit to initialize the pool (marks LT and staker as in_use)
    cryptopool = twocrypto.at(market.cryptopool)
    price = cryptopool.price_scale()
    usd_value = assets * price // 10**8
    debt = usd_value

    with boa.env.prank(account):
        wbtc_token.approve(vault.address, 2**256 - 1)
        crvusd_token.approve(vault.address, 2**256 - 1)
        vault.deposit(pool_id, assets, debt, 0, False, True)

    # Accidentally send some WBTC and crvUSD to the vault
    accidental_wbtc = 10**7  # 0.1 WBTC
    accidental_crvusd = 100 * 10**18  # 100 crvUSD
    with boa.env.prank(account):
        wbtc_token.transfer(vault.address, accidental_wbtc)
        crvusd_token.transfer(vault.address, accidental_crvusd)

    # --- Test that protected tokens cannot be recovered ---

    # LT token cannot be recovered
    with boa.env.prank(account):
        with boa.reverts("Token not allowed"):
            vault.recover_tokens(lt_address)

    # Staker token cannot be recovered
    with boa.env.prank(account):
        with boa.reverts("Token not allowed"):
            vault.recover_tokens(staker_address)

    # scrvUSD (crvusd_vault) cannot be recovered
    with boa.env.prank(account):
        with boa.reverts("Token not allowed"):
            vault.recover_tokens(SCRVUSD)

    # --- Test that accidentally sent tokens can be recovered ---

    # WBTC can be recovered
    wbtc_balance_before = wbtc_token.balanceOf(account)
    with boa.env.prank(account):
        vault.recover_tokens(WBTC)
    wbtc_balance_after = wbtc_token.balanceOf(account)
    assert wbtc_balance_after - wbtc_balance_before == accidental_wbtc, "Should recover accidental WBTC"

    # crvUSD can be recovered
    crvusd_balance_before = crvusd_token.balanceOf(account)
    with boa.env.prank(account):
        vault.recover_tokens(CRVUSD)
    crvusd_balance_after = crvusd_token.balanceOf(account)
    assert crvusd_balance_after - crvusd_balance_before == accidental_crvusd, "Should recover accidental crvUSD"
