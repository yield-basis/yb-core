import boa


def test_allocate_stablecoins(cryptopool, cryptopool_oracle, yb_lt, yb_amm, stablecoin, collateral_token, admin):
    stablecoin._mint_for_testing(admin, 10**30)

    assert stablecoin.balanceOf(yb_amm.address) == 0
    with boa.env.prank(admin):
        yb_lt.allocate_stablecoins(admin, 10**25)
        assert stablecoin.balanceOf(yb_amm.address) == 10**25

        yb_lt.allocate_stablecoins(admin, 10**26)
        assert stablecoin.balanceOf(yb_amm.address) == 10**26

        yb_lt.allocate_stablecoins(admin, 10**24)
        assert stablecoin.balanceOf(yb_amm.address) == 10**24


def test_deposit_withdraw(cryptopool, yb_lt, collateral_token, yb_allocated, seed_cryptopool, accounts):
    user = accounts[0]
    p = 100_000
    amount = 10**18
    collateral_token._mint_for_testing(user, amount)

    with boa.env.prank(user):
        # Test first deposit
        preview_shares = yb_lt.preview_deposit(amount, p * amount)
        shares = yb_lt.deposit(amount, p * amount, int(amount * 0.9999))
        assert shares == yb_lt.balanceOf(user)
        assert (shares - preview_shares) / shares < 1e-4  # Not exact equality because calc_token_amount is not exact
        assert abs(shares - amount) / amount < 1e-4

        # Test second deposit
        new_amount = amount // 2
        collateral_token._mint_for_testing(user, new_amount)
        with boa.reverts():
            yb_lt.deposit(new_amount, p * new_amount, int(new_amount * 1.0001))
        preview_shares = yb_lt.preview_deposit(new_amount, p * new_amount)
        new_shares = yb_lt.deposit(new_amount, p * new_amount, int(new_amount * 0.9999))
        assert new_shares + shares == yb_lt.balanceOf(user)
        assert (new_shares - preview_shares) / new_shares < 1e-4  # Not exact equality because calc_token_amount is not exact
        assert abs(new_shares - new_amount) / new_amount < 1e-4

        # Test withdrawal of the amount equal to assets deposited for this amount of shares
        preview_assets = yb_lt.preview_withdraw(shares // 100)
        assert abs(preview_assets - 10**16) / 10**16 < 1e-5

        preview_assets = yb_lt.preview_withdraw(shares // 10**8)
        assert abs(preview_assets - 10**10) / 10**10 < 1e-5

        preview_assets = yb_lt.preview_withdraw(shares)
        assert abs(preview_assets - 10**18) / 10**18 < 1e-5

        # Actually withdraw
        with boa.reverts():
            yb_lt.withdraw(shares, int(1.001e18))
        yb_lt.withdraw(shares, int(0.9999e18))
        assert collateral_token.balanceOf(user) == preview_assets
