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


def test_deposit_withdraw(cryptopool, yb_lt, collateral_token, yb_allocated, seed_cryptopool):
    pass
