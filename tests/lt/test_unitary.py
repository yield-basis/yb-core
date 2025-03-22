import boa


def test_informational(yb_lt):
    assert yb_lt.symbol() == 'yb-xxxBTC'
    assert yb_lt.name() == 'Yield Basis liquidity for xxxBTC'
    assert yb_lt.decimals() == 18


def test_allocate_stablecoins(factory, yb_lt, yb_amm, stablecoin, admin):
    with boa.env.prank(admin):
        assert stablecoin.balanceOf(yb_amm.address) == 0

        yb_lt.allocate_stablecoins(10**25)
        assert stablecoin.balanceOf(yb_amm.address) == 10**25

        yb_lt.allocate_stablecoins(10**26)
        assert stablecoin.balanceOf(yb_amm.address) == 10**26

        yb_lt.allocate_stablecoins(10**24)
        assert stablecoin.balanceOf(yb_amm.address) == 10**24


def test_deposit_withdraw(cryptopool, yb_lt, yb_amm, collateral_token, yb_allocated, seed_cryptopool, accounts):
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

        values_0 = yb_lt.internal._calculate_values(100_000 * 10**18)
        assert (values_0[1] - amount) / amount < 1e-5

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

        values_1 = yb_lt.internal._calculate_values(100_000 * 10**18)
        assert (values_1[1] - 1.5 * amount) / (1.5 * amount) < 1e-5
        assert values_1[0] == values_0[0]  # admin fees are not earned because there were no swaps - test later

        # Test withdrawal of the amount equal to assets deposited for this amount of shares
        preview_assets = yb_lt.preview_withdraw(shares // 100)
        assert abs(preview_assets - 10**16) / 10**16 < 1e-4

        preview_assets = yb_lt.preview_withdraw(shares // 10**8)
        assert abs(preview_assets - 10**10) / 10**10 < 1e-4

        preview_assets = yb_lt.preview_withdraw(shares)
        assert abs(preview_assets - 10**18) / 10**18 < 1e-4

        # Actually withdraw
        with boa.reverts():
            yb_lt.withdraw(shares, int(1.001e18))
        yb_lt.withdraw(shares, int(0.9999e18))
        assert abs(collateral_token.balanceOf(user) - preview_assets) < 5

        values_2 = yb_lt.internal._calculate_values(100_000 * 10**18)
        assert (values_2[1] - 0.5 * amount) / (0.5 * amount) < 1e-5
        assert values_2[0] == values_1[0]  # admin fees not earned yet

        # Check pricePerShare
        assert yb_lt.pricePerShare() > 1e18
        assert yb_lt.pricePerShare() < 1.01e18

        # And the last bits
        yb_lt.withdraw(new_shares, 0)
        assert abs(collateral_token.balanceOf(user) - 1.5e18) / 1.5e18 < 1e-4


def test_stake(cryptopool, yb_lt, collateral_token, yb_allocated, seed_cryptopool, accounts, admin):
    user = accounts[0]
    staker = accounts[1]

    p = 100_000
    amount = 10**18
    collateral_token._mint_for_testing(user, amount)

    with boa.env.prank(admin):
        yb_lt.set_staker(staker)

    with boa.env.prank(user):
        # Deposit
        shares = yb_lt.deposit(amount, p * amount, int(amount * 0.9999))

        # Stake 25%
        yb_lt.transfer(staker, shares // 4)


def test_collect_fees(cryptopool, yb_lt, collateral_token, stablecoin, yb_allocated, seed_cryptopool, admin):
    with boa.env.prank(admin):
        yb_lt.set_rate(10**18 // 365 // 86400 // 2)

        collateral_token._mint_for_testing(admin, 5 * 10**17)
        yb_lt.deposit(5 * 10**17, 5 * 10**17 * 100_000, 0)

        stables_before = cryptopool.balances(0)
        assert stables_before == stablecoin.balanceOf(cryptopool.address)
        assert cryptopool.balances(1) == collateral_token.balanceOf(cryptopool.address)

        boa.env.time_travel(7 * 86400)

        yb_lt.distrubute_borrower_fees()

        assert cryptopool.balances(0) == stables_before
        assert stablecoin.balanceOf(cryptopool.address) > stables_before
